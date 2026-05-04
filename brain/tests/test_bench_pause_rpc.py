"""Tests for pause/resume control RPC + PauseRpcClient skip flag."""

from __future__ import annotations

from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from hippo_brain.bench.pause_rpc import PauseRpcClient
from hippo_brain.server import BrainServer


def _make_app(db_path: str) -> Starlette:
    server = BrainServer(
        db_path=db_path,
        lmstudio_base_url="http://localhost:1234/v1",
        enrichment_model="test-model",
        poll_interval_secs=60,
        enrichment_batch_size=5,
    )
    return Starlette(routes=server.get_routes())


def test_pause_returns_200_with_paused_at(tmp_db):
    _, db_path = tmp_db
    client = TestClient(_make_app(str(db_path)))

    resp = client.post("/control/pause")
    assert resp.status_code == 200
    data = resp.json()
    assert "paused_at" in data
    assert isinstance(data["paused_at"], str)
    # Validate ISO-8601 (datetime.fromisoformat must accept it)
    import datetime as _dt

    parsed = _dt.datetime.fromisoformat(data["paused_at"])
    assert parsed.tzinfo is not None
    # in_flight_finished is True only when both query inflight count is 0
    # AND the enrichment loop is not mid-batch — bench needs both quiescent.
    assert data["in_flight_finished"] is True
    assert data["enrichment_active"] is False
    assert data["query_inflight"] == 0


def test_pause_in_flight_finished_false_when_enrichment_active(tmp_db):
    """in_flight_finished must reflect enrichment_active, not just queries."""
    _, db_path = tmp_db
    server = BrainServer(
        db_path=str(db_path),
        lmstudio_base_url="http://localhost:1234/v1",
        enrichment_model="test-model",
        poll_interval_secs=60,
        enrichment_batch_size=5,
    )
    server._enrichment_active = True
    client = TestClient(Starlette(routes=server.get_routes()))

    data = client.post("/control/pause").json()
    assert data["enrichment_active"] is True
    assert data["in_flight_finished"] is False


def test_resume_sets_resume_event(tmp_db):
    """control_resume must set _resume_event so a paused loop wakes immediately."""
    _, db_path = tmp_db
    server = BrainServer(
        db_path=str(db_path),
        lmstudio_base_url="http://localhost:1234/v1",
        enrichment_model="test-model",
        poll_interval_secs=60,
        enrichment_batch_size=5,
    )
    client = TestClient(Starlette(routes=server.get_routes()))

    client.post("/control/pause")
    assert not server._resume_event.is_set()
    client.post("/control/resume")
    assert server._resume_event.is_set()


def test_pause_idempotent(tmp_db):
    _, db_path = tmp_db
    client = TestClient(_make_app(str(db_path)))

    first = client.post("/control/pause").json()
    second = client.post("/control/pause").json()
    assert first["paused_at"] == second["paused_at"]


def test_resume_returns_200_with_resumed_at(tmp_db):
    _, db_path = tmp_db
    client = TestClient(_make_app(str(db_path)))

    client.post("/control/pause")
    resp = client.post("/control/resume")
    assert resp.status_code == 200
    data = resp.json()
    assert "resumed_at" in data
    assert isinstance(data["resumed_at"], str)
    import datetime as _dt

    parsed = _dt.datetime.fromisoformat(data["resumed_at"])
    assert parsed.tzinfo is not None


def test_resume_idempotent(tmp_db):
    _, db_path = tmp_db
    client = TestClient(_make_app(str(db_path)))

    # No prior pause — resume must still return 200 without crashing.
    resp = client.post("/control/resume")
    assert resp.status_code == 200
    assert "resumed_at" in resp.json()


def test_health_reflects_paused_state(tmp_db):
    _, db_path = tmp_db
    client = TestClient(_make_app(str(db_path)))

    pre = client.get("/health").json()
    assert pre["paused"] is False
    assert pre["paused_at"] is None

    client.post("/control/pause")
    post = client.get("/health").json()
    assert post["paused"] is True
    assert isinstance(post["paused_at"], str)


def test_health_reflects_resumed_state(tmp_db):
    _, db_path = tmp_db
    client = TestClient(_make_app(str(db_path)))

    client.post("/control/pause")
    client.post("/control/resume")
    data = client.get("/health").json()
    assert data["paused"] is False
    assert data["paused_at"] is None


def test_skip_flag_no_http_calls():
    """skip=True must short-circuit every method without touching httpx."""
    rpc = PauseRpcClient(base_url="http://x", skip=True)

    with (
        patch("hippo_brain.bench.pause_rpc.httpx.post") as mock_post,
        patch("hippo_brain.bench.pause_rpc.httpx.get") as mock_get,
    ):
        assert rpc.pause() is None
        assert rpc.resume() is None
        assert rpc.probe_health() is None
        mock_post.assert_not_called()
        mock_get.assert_not_called()


async def test_enrichment_active_cleared_on_cancellation(tmp_db):
    """Post-review I-2: regression test against future refactors that might add
    `return_exceptions=True` to the gather() and accidentally swallow
    asyncio.CancelledError. The try/finally around _enrichment_active must
    clear the flag on BaseException too — the bench's pause-quiescence
    contract depends on it.

    Unlike the original BT-13 test (which built a synthetic inner task whose
    own finally cleared the flag — proving Python's language semantic, not the
    application contract), this test runs the actual `_enrichment_loop` task,
    parks it at `preflight_lm_studio` where `_enrichment_active = True` is
    already set on server.py:871, then cancels and asserts the loop's own
    `finally` (server.py:949-950) cleared the flag.
    """
    import asyncio

    _, db_path = tmp_db
    server = BrainServer(
        db_path=str(db_path),
        lmstudio_base_url="http://localhost:1234/v1",
        enrichment_model="test-model",
        poll_interval_secs=0.01,
        enrichment_batch_size=5,
    )

    # Park preflight_lm_studio in a long sleep so the loop reaches the line
    # AFTER `_enrichment_active = True` and waits there until we cancel. The
    # patch target is `hippo_brain.server.preflight_lm_studio` (where the
    # symbol is bound by `from ... import ...`), not the source module.
    async def _hang(*_args, **_kwargs):
        await asyncio.sleep(60)

    with patch("hippo_brain.server.preflight_lm_studio", side_effect=_hang):
        task = asyncio.create_task(server._enrichment_loop())
        try:
            # Wait up to 1 s for the loop to enter the inner try and set
            # _enrichment_active = True. Tight poll because poll_interval_secs
            # is 0.01 and preflight is the first await after the flag is set.
            for _ in range(100):
                if server._enrichment_active:
                    break
                await asyncio.sleep(0.01)
            assert server._enrichment_active, (
                "loop never reached preflight — patch target or fixture wrong"
            )

            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass

    assert server._enrichment_active is False, (
        "Post-review I-2: _enrichment_loop's finally (server.py:949-950) must "
        "clear _enrichment_active on CancelledError — the pause-quiescence "
        "contract depends on it"
    )
