"""Regression tests for hippo_brain.server — pin real behavior, not coverage.

Each test here pins a behavior that would silently break the product if regressed.
Complements tests/test_server.py by covering validation paths, semantic→lexical
fallback, and the full enrichment pipeline for shell/browser/workflow sources.
"""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from starlette.applications import Starlette
from starlette.testclient import TestClient

from hippo_brain.server import BrainServer
from hippo_brain.watchdog import PreflightDecision


def _make_server(db_path: str, **kwargs) -> BrainServer:
    defaults = dict(
        db_path=db_path,
        lmstudio_base_url="http://localhost:1234/v1",
        enrichment_model="test-model",
        poll_interval_secs=60,
        enrichment_batch_size=5,
    )
    defaults.update(kwargs)
    return BrainServer(**defaults)


def _make_app(db_path: str, **kwargs) -> Starlette:
    server = _make_server(db_path, **kwargs)
    return Starlette(routes=server.get_routes())


# ---- /health embed_model_drift ----


def test_health_reports_degraded_on_embed_model_drift(tmp_db):
    """When the stored embedding model differs from the running one, /health
    reports status=degraded and includes drift info.

    Regression target: silent model drift in the vector store — if someone
    swaps the embedding model without rebuilding the index, we must surface it.
    """
    _, db_path = tmp_db
    server = _make_server(str(db_path), embedding_model="live-model")
    # Simulate a live vector DB handle without actually needing lancedb.
    server._vector_db = object()  # sentinel — health only checks `is not None`

    with patch("hippo_brain.server.get_stored_embed_model", return_value="stored-model"):
        app = Starlette(routes=server.get_routes())
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["embed_model_drift"] is not None
        assert "stored-model" in data["embed_model_drift"]
        assert "live-model" in data["embed_model_drift"]


def test_health_ok_when_embed_models_match(tmp_db):
    """When stored and live embedding models match, no drift is reported."""
    _, db_path = tmp_db
    server = _make_server(str(db_path), embedding_model="same-model")
    server._vector_db = object()

    with patch("hippo_brain.server.get_stored_embed_model", return_value="same-model"):
        app = Starlette(routes=server.get_routes())
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["embed_model_drift"] is None


# ---- /query limit validation ----


def test_query_rejects_zero_limit(tmp_db):
    """POST /query with limit=0 returns 400 — otherwise SQL LIMIT 0 returns
    nothing silently and callers can't tell why."""
    _, db_path = tmp_db
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.post("/query", json={"text": "hello", "limit": 0})
    assert resp.status_code == 400
    assert "greater than 0" in resp.json()["error"]


def test_query_rejects_negative_limit(tmp_db):
    """POST /query with negative limit returns 400."""
    _, db_path = tmp_db
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.post("/query", json={"text": "hello", "limit": -1})
    assert resp.status_code == 400
    assert "greater than 0" in resp.json()["error"]


# ---- /query semantic → lexical fallback ----


def test_query_semantic_falls_back_to_lexical_on_embed_failure(tmp_db):
    """When the embedding call raises, semantic search falls back to lexical
    and still returns results with a warning flag.

    Regression target: a transient LM Studio outage must not black-hole user
    queries — we promise graceful degradation.
    """
    conn, db_path = tmp_db
    now_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (now_ms,),
    )
    conn.execute(
        "INSERT INTO events (id, session_id, timestamp, command, exit_code, "
        "duration_ms, cwd, hostname, shell) "
        "VALUES (1, 1, ?, 'cargo test foo', 0, 100, '/p', 'h', 'zsh')",
        (now_ms,),
    )
    conn.commit()

    # Force the semantic branch: embedding_model set AND vector_table stub
    server = _make_server(str(db_path), embedding_model="fake-embed")
    server._vector_table = object()  # truthy but embed will fail before use
    server.client.embed = AsyncMock(side_effect=RuntimeError("embed offline"))

    app = Starlette(routes=server.get_routes())
    client = TestClient(app)
    resp = client.post("/query", json={"text": "cargo", "mode": "semantic"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "lexical"
    assert "warning" in data
    assert "embed offline" in data["warning"]
    assert len(data["events"]) == 1


# ---- /knowledge since_ms validation and filtering ----


def test_knowledge_rejects_non_integer_since_ms(tmp_db):
    """GET /knowledge?since_ms=abc returns 400 — prevents silent empty results
    from garbled timestamps."""
    _, db_path = tmp_db
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.get("/knowledge?since_ms=abc")
    assert resp.status_code == 400
    assert "since_ms" in resp.json()["error"]


def test_knowledge_filter_by_since_ms_cuts_off_old(tmp_db):
    """since_ms filter excludes nodes with created_at <= threshold."""
    import json as _json

    conn, db_path = tmp_db
    old_ms = int(time.time() * 1000) - 60_000
    new_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, "
        "outcome, tags, enrichment_model, created_at, updated_at) "
        "VALUES (10, 'uuid-old', ?, 'old', 'observation', 'success', ?, 'm', ?, ?)",
        (_json.dumps({"summary": "old"}), _json.dumps([]), old_ms, old_ms),
    )
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, "
        "outcome, tags, enrichment_model, created_at, updated_at) "
        "VALUES (11, 'uuid-new', ?, 'new', 'observation', 'success', ?, 'm', ?, ?)",
        (_json.dumps({"summary": "new"}), _json.dumps([]), new_ms, new_ms),
    )
    conn.commit()

    app = _make_app(str(db_path))
    client = TestClient(app)

    cutoff = old_ms + 1000  # between the two
    resp = client.get(f"/knowledge?since_ms={cutoff}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    uuids = [n["uuid"] for n in data["nodes"]]
    assert uuids == ["uuid-new"]


# ---- Shell enrichment pipeline: embedding task fires ----


@pytest.mark.asyncio
async def test_shell_enrichment_schedules_embedding(tmp_db):
    """When embedding_model is configured, the shell enrichment pipeline
    schedules _embed_node so nodes land in the vector store.

    Regression target: knowledge nodes written to SQLite but never embedded
    would silently fall out of semantic search.
    """
    conn, db_path = tmp_db
    old_ms = int(time.time() * 1000) - 300_000
    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (old_ms,),
    )
    conn.execute(
        "INSERT INTO events (id, session_id, timestamp, command, exit_code, "
        "duration_ms, cwd, hostname, shell) "
        "VALUES (1, 1, ?, 'cargo test', 0, 1000, '/p', 'l', 'zsh')",
        (old_ms,),
    )
    conn.execute("INSERT INTO enrichment_queue (event_id) VALUES (1)")
    conn.commit()

    server = _make_server(str(db_path), embedding_model="fake-embed")
    server.poll_interval_secs = 0
    server.session_stale_secs = 120
    server.client.chat = AsyncMock(
        return_value=(
            '{"summary": "ran tests", "intent": "testing", "outcome": "success", '
            '"entities": {"projects": [], "tools": [], "files": [], "services": [], "errors": []}, '
            '"tags": ["t"], "embed_text": "ran tests"}'
        )
    )

    # Replace _embed_node with a recording stub so we don't need a real vector store
    embed_calls: list[tuple[int, str]] = []

    async def _rec(node_id, node_dict, source_label):
        embed_calls.append((node_id, source_label))

    server._embed_node = _rec  # type: ignore[method-assign]

    ok = PreflightDecision(proceed=True, reason="ok", loaded_models=["test-model"])
    with patch("hippo_brain.server.preflight_lm_studio", new_callable=AsyncMock, return_value=ok):
        task = asyncio.create_task(server._enrichment_loop())
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(embed_calls) >= 1
    assert embed_calls[0][1] == "shell"


# ---- Browser enrichment pipeline ----


@pytest.mark.asyncio
async def test_browser_enrichment_writes_node_and_schedules_embedding(tmp_db):
    """Browser enrichment claims a pending event, writes a knowledge node,
    and schedules embedding. Regression target: parity with shell — browser
    source must not silently bypass the vector store."""
    conn, db_path = tmp_db
    old_ms = int(time.time() * 1000) - 300_000
    conn.execute(
        "INSERT INTO browser_events (id, timestamp, url, title, domain, dwell_ms, "
        "scroll_depth, extracted_text, search_query) "
        "VALUES (1, ?, 'https://example.com/a', 'A', 'example.com', 60000, 0.8, "
        "'hello world content for enrichment', NULL)",
        (old_ms,),
    )
    conn.execute("INSERT INTO browser_enrichment_queue (browser_event_id) VALUES (1)")
    conn.commit()

    server = _make_server(str(db_path), embedding_model="fake-embed")
    server.poll_interval_secs = 0
    server.client.chat = AsyncMock(
        return_value=(
            '{"summary": "read article", "intent": "research", "outcome": "success", '
            '"entities": {"projects": [], "tools": [], "files": [], "services": [], "errors": []}, '
            '"tags": ["read"], "embed_text": "read article"}'
        )
    )

    embed_calls: list[tuple[int, str]] = []

    async def _rec(node_id, node_dict, source_label):
        embed_calls.append((node_id, source_label))

    server._embed_node = _rec  # type: ignore[method-assign]

    ok = PreflightDecision(proceed=True, reason="ok", loaded_models=["test-model"])
    with patch("hippo_brain.server.preflight_lm_studio", new_callable=AsyncMock, return_value=ok):
        task = asyncio.create_task(server._enrichment_loop())
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Queue entry transitioned to done
    status = conn.execute(
        "SELECT status FROM browser_enrichment_queue WHERE browser_event_id = 1"
    ).fetchone()[0]
    assert status == "done"

    # Embedding task fired for the browser source
    assert any(src == "browser" for _, src in embed_calls)


# ---- Workflow enrichment ----


@pytest.mark.asyncio
async def test_workflow_enrichment_calls_enrich_one_async(tmp_db):
    """Workflow enrichment claims a run_id and dispatches to enrich_one_async.

    Regression target: the workflow source is delegated to enrich_one_async,
    which owns its own DB writes. If the server stops calling it, CI
    retrospectives silently stop being generated.
    """
    conn, db_path = tmp_db
    now_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO workflow_runs (id, repo, head_sha, event, status, html_url, "
        "raw_json, first_seen_at, last_seen_at) "
        "VALUES (42, 'org/repo', 'abc123', 'push', 'completed', 'https://x', '{}', ?, ?)",
        (now_ms, now_ms),
    )
    conn.execute(
        "INSERT INTO workflow_enrichment_queue (run_id, enqueued_at, updated_at) VALUES (42, ?, ?)",
        (now_ms, now_ms),
    )
    conn.commit()

    server = _make_server(str(db_path))
    server.poll_interval_secs = 0

    enrich_calls: list[int] = []

    async def _fake_enrich(db_path_arg, run_id, lm, query_model):
        enrich_calls.append(run_id)

    ok = PreflightDecision(proceed=True, reason="ok", loaded_models=["test-model"])
    with (
        patch(
            "hippo_brain.server.preflight_lm_studio",
            new_callable=AsyncMock,
            return_value=ok,
        ),
        patch(
            "hippo_brain.server.enrich_one_async",
            new=_fake_enrich,
        ),
    ):
        task = asyncio.create_task(server._enrichment_loop())
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert 42 in enrich_calls


# ---- stop_enrichment with no tasks ----


@pytest.mark.asyncio
async def test_stop_enrichment_is_noop_with_no_tasks(tmp_db):
    """Calling stop_enrichment without ever starting it must not raise.

    Regression target: the Starlette lifespan shutdown calls stop_enrichment
    unconditionally — if startup was skipped or failed, stop must still be
    safe.
    """
    _, db_path = tmp_db
    server = _make_server(str(db_path))
    assert server._enrichment_task is None
    assert server._reaper_task is None

    # Should not raise
    await server.stop_enrichment()
