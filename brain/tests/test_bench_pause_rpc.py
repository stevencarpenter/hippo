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
    assert data["in_flight_finished"] is True


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
