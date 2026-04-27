"""Tests for hippo_brain.server — BrainServer endpoints and create_app."""

import asyncio
import os
import time
from unittest.mock import AsyncMock, patch

import pytest

from starlette.testclient import TestClient

from hippo_brain.server import BrainServer, create_app
from hippo_brain.version import get_version
from hippo_brain.watchdog import PreflightDecision


def _make_server(db_path: str) -> BrainServer:
    return BrainServer(
        db_path=db_path,
        lmstudio_base_url="http://localhost:1234/v1",
        enrichment_model="test-model",
        poll_interval_secs=60,
        enrichment_batch_size=5,
    )


def _make_app(db_path: str):
    """Create a Starlette app from BrainServer routes WITHOUT the startup enrichment loop."""
    from starlette.applications import Starlette

    server = _make_server(db_path)
    return Starlette(routes=server.get_routes())


def _seed_events(conn):
    """Insert a session and sample events for query testing."""
    now_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (now_ms,),
    )
    conn.execute(
        "INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms, "
        "cwd, hostname, shell) VALUES (1, 1, ?, 'cargo test -p hippo-core', 0, 3000, "
        "'/projects/hippo', 'laptop', 'zsh')",
        (now_ms,),
    )
    conn.execute(
        "INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms, "
        "cwd, hostname, shell) VALUES (2, 1, ?, 'npm run build', 0, 5000, "
        "'/projects/webapp', 'laptop', 'zsh')",
        (now_ms + 1,),
    )
    conn.commit()


def _seed_knowledge_nodes(conn):
    """Insert a knowledge node for query testing."""
    import json

    now_ms = int(time.time() * 1000)
    content = json.dumps({"summary": "Ran cargo test", "intent": "testing"})
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, outcome, tags, "
        "enrichment_model, created_at, updated_at) "
        "VALUES (1, 'uuid-1', ?, 'cargo test hippo-core all passed', 'success', "
        "'[\"rust\"]', 'model', ?, ?)",
        (content, now_ms, now_ms),
    )
    conn.commit()


# ---- /health ----


def test_health_endpoint(tmp_db):
    conn, db_path = tmp_db
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "version" in data
    assert data["version"] == get_version()
    assert "lmstudio_reachable" in data
    assert "enrichment_running" in data
    assert "db_reachable" in data
    assert "queue_depth" in data
    assert "queue_failed" in data
    assert "last_success_at_ms" in data
    assert "last_error" in data
    assert "last_error_at_ms" in data
    # Telemetry status is consumed by `hippo doctor` to detect the
    # configured-on-but-dead silent-degrade mode. Locks in the contract so
    # the doctor check can't go blind to a future /health refactor.
    assert "telemetry_enabled" in data
    assert "telemetry_active" in data
    assert isinstance(data["telemetry_enabled"], bool)
    assert isinstance(data["telemetry_active"], bool)
    assert data["enrichment_running"] is False
    assert data["db_reachable"] is True
    assert data["queue_depth"] == 0
    assert data["queue_failed"] == 0
    assert data["last_success_at_ms"] is None
    assert data["last_error"] is None
    assert data["last_error_at_ms"] is None


def test_health_telemetry_fields_reflect_runtime_state(tmp_db):
    """When telemetry is gated off, /health must report enabled=False AND
    active=False — the only state combination that's safe in CI without an
    OTel collector. Then with the gate on and providers initialized, both
    must flip True. Catches regressions that decouple the fields from the
    real module-level state.
    """
    # Reach into the exact module that server.py's bound function reads from.
    # `import hippo_brain.telemetry` would resolve via sys.modules, which
    # other tests can replace via del+reimport — leaving us setting state on
    # a module that server.py no longer references. The function's
    # `__globals__` is the unambiguous source of truth.
    from hippo_brain.server import is_telemetry_active as server_is_active

    telemetry_globals = server_is_active.__globals__

    _, db_path = tmp_db
    app = _make_app(str(db_path))
    client = TestClient(app)

    env_off = {k: v for k, v in os.environ.items() if k != "HIPPO_OTEL_ENABLED"}
    original_active = telemetry_globals.get("_telemetry_active", False)
    try:
        with patch.dict(os.environ, env_off, clear=True):
            telemetry_globals["_telemetry_active"] = False
            data = client.get("/health").json()
            assert data["telemetry_enabled"] is False
            assert data["telemetry_active"] is False

        # Simulate "providers wired up" without actually running
        # init_telemetry, which would attach a global OTel exporter that
        # pollutes other tests.
        with patch.dict(os.environ, {"HIPPO_OTEL_ENABLED": "1"}):
            telemetry_globals["_telemetry_active"] = True
            data = client.get("/health").json()
            assert data["telemetry_enabled"] is True
            assert data["telemetry_active"] is True
    finally:
        telemetry_globals["_telemetry_active"] = original_active


# ---- /query ----


def test_query_returns_matching_events(tmp_db):
    conn, db_path = tmp_db
    _seed_events(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.post("/query", json={"text": "cargo", "mode": "lexical"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "lexical"
    assert len(data["events"]) == 1
    assert data["events"][0]["command"] == "cargo test -p hippo-core"


def test_query_returns_matching_knowledge_nodes(tmp_db):
    conn, db_path = tmp_db
    _seed_events(conn)
    _seed_knowledge_nodes(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.post("/query", json={"text": "cargo", "mode": "lexical"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "lexical"
    assert len(data["nodes"]) == 1
    assert data["nodes"][0]["uuid"] == "uuid-1"
    assert "cargo test" in data["nodes"][0]["embed_text"]


def test_query_no_results(tmp_db):
    conn, db_path = tmp_db
    _seed_events(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.post("/query", json={"text": "nonexistent_xyz", "mode": "lexical"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "lexical"
    assert data["events"] == []
    assert data["nodes"] == []


def test_query_limit_is_applied(tmp_db):
    conn, db_path = tmp_db
    _seed_events(conn)
    conn.execute(
        "INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms, cwd, hostname, shell) "
        "VALUES (3, 1, ?, 'cargo build', 0, 1200, '/projects/hippo', 'laptop', 'zsh')",
        (int(time.time() * 1000) + 2,),
    )
    conn.commit()

    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.post("/query", json={"text": "cargo", "mode": "lexical", "limit": 1})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["events"]) == 1


def test_query_empty_text_returns_400(tmp_db):
    conn, db_path = tmp_db
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.post("/query", json={"text": ""})
    assert resp.status_code == 400
    data = resp.json()
    assert "error" in data
    assert data["error"] == "text is required"


def test_query_missing_text_returns_400(tmp_db):
    conn, db_path = tmp_db
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.post("/query", json={})
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"] == "text is required"


def test_query_limit_above_max_returns_400(tmp_db):
    """Cap on /query.limit prevents pathological semantic-search requests
    from forcing expensive embedding lookups and oversized responses."""
    from hippo_brain.server import MAX_QUERY_LIMIT

    _, db_path = tmp_db
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.post(
        "/query", json={"text": "anything", "limit": MAX_QUERY_LIMIT + 1, "mode": "lexical"}
    )
    assert resp.status_code == 400
    assert str(MAX_QUERY_LIMIT) in resp.json()["error"]


def test_query_limit_at_max_is_accepted(tmp_db):
    """The boundary case (limit == MAX_QUERY_LIMIT) must succeed — only
    strictly greater is rejected."""
    from hippo_brain.server import MAX_QUERY_LIMIT

    _, db_path = tmp_db
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.post(
        "/query", json={"text": "anything", "limit": MAX_QUERY_LIMIT, "mode": "lexical"}
    )
    assert resp.status_code == 200, resp.text


def test_ask_limit_validates_integer_and_range(tmp_db):
    """/ask had zero limit validation prior to this change. Verify all
    three guards: non-int, <= 0, > MAX_QUERY_LIMIT."""
    from hippo_brain.server import MAX_QUERY_LIMIT

    _, db_path = tmp_db
    app = _make_app(str(db_path))
    client = TestClient(app)

    bad_inputs = [
        ({"question": "hi", "limit": "not-a-number"}, "must be an integer"),
        ({"question": "hi", "limit": 0}, "greater than 0"),
        ({"question": "hi", "limit": -5}, "greater than 0"),
        ({"question": "hi", "limit": MAX_QUERY_LIMIT + 1}, str(MAX_QUERY_LIMIT)),
    ]
    for body, expected_substring in bad_inputs:
        resp = client.post("/ask", json=body)
        assert resp.status_code == 400, f"input {body} should 400, got {resp.status_code}"
        assert expected_substring in resp.json()["error"], (
            f"input {body} error '{resp.json()['error']}' missing '{expected_substring}'"
        )


def test_list_endpoints_reject_oversized_limit(tmp_db):
    """The list endpoints (/knowledge, /events, /sessions) cap limit at
    MAX_LIST_LIMIT to bound response size."""
    from hippo_brain.server import MAX_LIST_LIMIT

    _, db_path = tmp_db
    app = _make_app(str(db_path))
    client = TestClient(app)

    too_big = MAX_LIST_LIMIT + 1
    for path in ("/knowledge", "/events", "/sessions"):
        resp = client.get(path, params={"limit": too_big})
        assert resp.status_code == 400, f"{path} should reject limit={too_big}"
        assert str(MAX_LIST_LIMIT) in resp.json()["error"]


def test_list_endpoints_reject_negative_offset(tmp_db):
    """Negative offsets would produce undefined SQLite behavior; reject explicitly."""
    _, db_path = tmp_db
    app = _make_app(str(db_path))
    client = TestClient(app)

    for path in ("/knowledge", "/events", "/sessions"):
        resp = client.get(path, params={"offset": -1})
        assert resp.status_code == 400, f"{path} should reject offset=-1"
        assert "offset" in resp.json()["error"]


# ---- BrainServer init ----


def test_brain_server_default_db_path():
    """When db_path is empty, it defaults to ~/.local/share/hippo/hippo.db."""
    from pathlib import Path

    server = BrainServer()
    expected = str(Path.home() / ".local" / "share" / "hippo" / "hippo.db")
    assert server.db_path == expected


def test_brain_server_custom_db_path(tmp_db):
    _, db_path = tmp_db
    server = BrainServer(db_path=str(db_path))
    assert server.db_path == str(db_path)


def test_brain_server_get_conn(tmp_db):
    _, db_path = tmp_db
    server = _make_server(str(db_path))
    conn = server._get_conn()
    # Should return a working connection
    cursor = conn.execute("SELECT 1")
    assert cursor.fetchone() == (1,)
    conn.close()


def test_brain_server_get_routes(tmp_db):
    _, db_path = tmp_db
    server = _make_server(str(db_path))
    routes = server.get_routes()
    assert len(routes) == 9
    paths = [r.path for r in routes]
    assert "/health" in paths
    assert "/sessions" in paths
    assert "/events" in paths
    assert "/knowledge" in paths
    assert "/query" in paths
    assert "/ask" in paths
    assert "/control/pause" in paths
    assert "/control/resume" in paths


# ---- create_app ----


def test_create_app_is_callable(tmp_db):
    """create_app is importable and callable."""
    from hippo_brain.server import create_app as create_app_fn

    assert callable(create_app_fn)


def test_create_app_routes_work(tmp_db):
    """Verify create_app produces a fully functional app."""
    _, db_path = tmp_db
    app = create_app(
        db_path=str(db_path),
        lmstudio_base_url="http://localhost:1234/v1",
        enrichment_model="test-model",
        poll_interval_secs=9999,
        enrichment_batch_size=5,
    )
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200


# ---- Query error handling ----


def test_query_semantic_fallback_to_lexical(tmp_db):
    """When no embedding model, semantic mode falls back to lexical with a warning."""
    conn, db_path = tmp_db
    _seed_events(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.post("/query", json={"text": "cargo"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "lexical"
    assert "warning" in data
    assert len(data["events"]) == 1


def test_query_returns_500_on_db_error(tmp_db):
    """When _get_conn raises, query endpoint returns 500 with error message."""
    _, db_path = tmp_db
    server = _make_server(str(db_path))

    # Point to a non-existent path that cannot be opened
    server.db_path = "/nonexistent/path/to/db.db"

    from starlette.applications import Starlette

    app = Starlette(routes=server.get_routes())
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/query", json={"text": "hello", "mode": "lexical"})
    assert resp.status_code == 500
    data = resp.json()
    assert "error" in data


# ---- Enrichment loop ----


async def test_enrichment_loop_processes_events(tmp_db):
    """_enrichment_loop processes one batch of pending events then we cancel."""
    conn, db_path = tmp_db
    # Use a timestamp old enough to be considered stale (> session_stale_secs ago)
    old_ms = int(time.time() * 1000) - 300_000

    # Seed a session and event with queue entry
    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (old_ms,),
    )
    conn.execute(
        "INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms, "
        "cwd, hostname, shell) VALUES (1, 1, ?, 'cargo test', 0, 1000, '/proj', 'laptop', 'zsh')",
        (old_ms,),
    )
    conn.execute("INSERT INTO enrichment_queue (event_id) VALUES (1)")
    conn.commit()

    server = _make_server(str(db_path))
    server.poll_interval_secs = 0  # no delay
    server.session_stale_secs = 120

    # Mock the LMStudio client to return a valid enrichment response
    mock_chat = AsyncMock(
        return_value=(
            '{"summary": "test cmd", "intent": "testing", "outcome": "success", '
            '"entities": {"projects": [], "tools": [], "files": [], "services": [], "errors": []}, '
            '"tags": ["test"], "embed_text": "test embed"}'
        )
    )
    server.client.chat = mock_chat

    # Bypass preflight — this test is about enrichment processing, not model discovery
    ok = PreflightDecision(proceed=True, reason="ok", loaded_models=["test-model"])
    with patch("hippo_brain.server.preflight_lm_studio", new_callable=AsyncMock, return_value=ok):
        task = asyncio.create_task(server._enrichment_loop())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert server.enrichment_running is False
    mock_chat.assert_called()

    # Verify enrichment happened
    status = conn.execute("SELECT status FROM enrichment_queue WHERE event_id = 1").fetchone()[0]
    assert status == "done"


async def test_enrichment_loop_handles_chat_failure(tmp_db):
    """When client.chat raises, events get marked as failed/pending."""
    conn, db_path = tmp_db
    old_ms = int(time.time() * 1000) - 300_000

    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (old_ms,),
    )
    conn.execute(
        "INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms, "
        "cwd, hostname, shell) VALUES (1, 1, ?, 'bad cmd', 1, 100, '/proj', 'laptop', 'zsh')",
        (old_ms,),
    )
    conn.execute("INSERT INTO enrichment_queue (event_id, max_retries) VALUES (1, 3)")
    conn.commit()

    server = _make_server(str(db_path))
    server.poll_interval_secs = 0
    server.session_stale_secs = 120

    # Make chat() raise
    server.client.chat = AsyncMock(side_effect=RuntimeError("model offline"))

    # Bypass preflight — this test is about error handling, not model discovery
    ok = PreflightDecision(proceed=True, reason="ok", loaded_models=["test-model"])
    with patch("hippo_brain.server.preflight_lm_studio", new_callable=AsyncMock, return_value=ok):
        task = asyncio.create_task(server._enrichment_loop())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Queue entry was retried; with poll_interval_secs=0 all 3 retries may exhaust
    row = conn.execute(
        "SELECT status, retry_count, error_message FROM enrichment_queue WHERE event_id = 1"
    ).fetchone()
    assert row[0] in ("pending", "failed")  # depends on how many retries ran
    assert row[1] >= 1
    assert "model offline" in row[2]


async def test_enrichment_loop_skips_empty_queue(tmp_db):
    """When no pending events, loop just sleeps and continues."""
    _, db_path = tmp_db
    server = _make_server(str(db_path))
    server.poll_interval_secs = 0

    task = asyncio.create_task(server._enrichment_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Should have run without errors and cleaned up after cancellation
    assert server.enrichment_running is False


async def test_start_enrichment_creates_task(tmp_db):
    """start_enrichment() stores an asyncio task."""
    _, db_path = tmp_db
    server = _make_server(str(db_path))
    server.poll_interval_secs = 9999  # don't actually poll

    server.start_enrichment()
    assert server._enrichment_task is not None

    # Clean up
    server._enrichment_task.cancel()
    try:
        await server._enrichment_task
    except asyncio.CancelledError:
        pass


async def test_enrichment_loop_yields_to_arriving_query(tmp_db):
    """If a query arrives during the poll sleep, the enrichment loop must
    wake immediately and wait for the query to drain before running
    preflight. After the query completes, the loop must proceed normally.
    Validates the asyncio.Event wake mechanism plus the drain spin."""
    _, db_path = tmp_db
    server = _make_server(str(db_path))
    # Long poll interval — only the Event wake should bring us back fast.
    server.poll_interval_secs = 30

    preflight_calls = 0

    async def fake_preflight(*args, **kwargs):
        nonlocal preflight_calls
        preflight_calls += 1
        return PreflightDecision(proceed=False, loaded_models=[])

    with patch("hippo_brain.server.preflight_lm_studio", side_effect=fake_preflight):
        task = asyncio.create_task(server._enrichment_loop())
        try:
            # Loop should be sleeping on its first wait_for.
            await asyncio.sleep(0.1)
            assert preflight_calls == 0, "loop should still be sleeping on its first iteration"

            # Simulate /ask entry: increment counter, set event. The loop
            # must wake from wait_for and enter the drain spin.
            server._query_inflight += 1
            server._query_arrived.set()

            # While inflight > 0, the drain spin keeps the loop pinned and
            # preflight cannot fire.
            await asyncio.sleep(0.3)
            assert preflight_calls == 0, "preflight must not run while a query is in flight"

            # Simulate /ask exit: decrement, clear event. The drain spin
            # exits next tick, preflight runs once, then wait_for blocks
            # for the full 30s timeout (the test never sees a 2nd call).
            server._query_inflight = max(0, server._query_inflight - 1)
            if server._query_inflight == 0:
                server._query_arrived.clear()

            await asyncio.sleep(0.3)
            assert preflight_calls == 1, (
                "loop must proceed to preflight exactly once after the query drains"
            )

            # No further preflight calls without another event/timeout.
            await asyncio.sleep(0.3)
            assert preflight_calls == 1, "no more preflight while wait_for sleeps"
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


# ---- schema version checks ----


def test_brain_server_rejects_wrong_schema_version(tmp_path):
    """_get_conn raises RuntimeError when user_version does not match."""
    import sqlite3

    db_path = tmp_path / "bad_version.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 99")
    conn.close()

    server = _make_server(str(db_path))
    try:
        server._get_conn()
        raise AssertionError("Expected RuntimeError was not raised")
    except RuntimeError as e:
        assert "schema version mismatch" in str(e).lower()


def test_brain_server_rejects_v11_db(tmp_path):
    """Regression for C-4/R2-5: brain reads `content_hash` (added in v12),
    so a v11 DB must be rejected at connect time rather than crashing later
    inside `claim_pending_claude_segments` with `no such column`.
    """
    import sqlite3

    db_path = tmp_path / "v11.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 11")
    conn.close()

    server = _make_server(str(db_path))
    try:
        server._get_conn()
        raise AssertionError("Expected RuntimeError was not raised")
    except RuntimeError as e:
        assert "schema version mismatch" in str(e).lower()


# ---- query alignment ----


@pytest.mark.xfail(reason="requires embedding model and vector data for true semantic match")
def test_query_returns_semantically_related_result(tmp_db):
    """When semantic retrieval is wired with an embedding model, querying
    'version control' should find knowledge about 'git' even if 'version
    control' never appears literally. Without an embedding model, falls back
    to lexical (which won't find it)."""
    conn, db_path = tmp_db
    now_ms = int(time.time() * 1000)

    # Seed a session and git-related events (none contain "version control")
    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (now_ms,),
    )
    git_commands = [
        (1, "git commit -m 'fix auth bug'"),
        (2, "git push origin main"),
        (3, "git log --oneline -10"),
    ]
    for eid, cmd in git_commands:
        conn.execute(
            "INSERT INTO events (id, session_id, timestamp, command, exit_code, "
            "duration_ms, cwd, hostname, shell) "
            "VALUES (?, 1, ?, ?, 0, 500, '/projects/hippo', 'laptop', 'zsh')",
            (eid, now_ms + eid, cmd),
        )
    conn.commit()

    app = _make_app(str(db_path))
    client = TestClient(app)

    # "version control" doesn't literally appear in any command, so lexical
    # LIKE search will return nothing.  Semantic search should match.
    resp = client.post("/query", json={"text": "version control"})
    assert resp.status_code == 200
    data = resp.json()
    # Without embedding model, falls back to lexical — which won't find these
    assert data["mode"] == "semantic"
    assert len(data["results"]) > 0, (
        "Expected semantic search to find git commands for 'version control'"
    )


# ---- create_app full coverage ----


def test_create_app_starts_and_stops_enrichment_task(tmp_db):
    """create_app should start the background task on startup and stop cleanly on shutdown."""
    _, db_path = tmp_db
    app = create_app(
        db_path=str(db_path),
        lmstudio_base_url="http://localhost:1234/v1",
        enrichment_model="test",
        poll_interval_secs=9999,
        enrichment_batch_size=5,
    )

    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ---- _pick_enrichment_model ----


def _server_with_preferred(tmp_db, preferred: str) -> BrainServer:
    _, db_path = tmp_db
    server = _make_server(str(db_path))
    server._preferred_model = preferred
    server.enrichment_model = preferred
    return server


def test_resolve_model_preferred_available(tmp_db):
    """When preferred model is loaded, enrichment_model stays as preferred."""
    server = _server_with_preferred(tmp_db, "qwen3.5-35b-a3b")
    result = server._pick_enrichment_model(["qwen3.5-35b-a3b", "text-embedding-nomic"])
    assert result is True
    assert server.enrichment_model == "qwen3.5-35b-a3b"


def test_resolve_model_fallback_when_preferred_missing(tmp_db):
    """When preferred model is not loaded, falls back to first available chat model."""
    server = _server_with_preferred(tmp_db, "qwen3.5-35b-a3b")
    result = server._pick_enrichment_model(["gemma-4-26b", "text-embedding-nomic"])
    assert result is True
    assert server.enrichment_model == "gemma-4-26b"


def test_resolve_model_restores_preferred(tmp_db):
    """When preferred becomes available again, switches back from fallback."""
    server = _server_with_preferred(tmp_db, "qwen3.5-35b-a3b")
    server._pick_enrichment_model(["gemma-4-26b", "text-embedding-nomic"])
    assert server.enrichment_model == "gemma-4-26b"

    result = server._pick_enrichment_model(
        ["qwen3.5-35b-a3b", "gemma-4-26b", "text-embedding-nomic"]
    )
    assert result is True
    assert server.enrichment_model == "qwen3.5-35b-a3b"


def test_resolve_model_no_chat_models(tmp_db):
    """When only embedding models are loaded, returns False."""
    server = _server_with_preferred(tmp_db, "qwen3.5-35b-a3b")
    result = server._pick_enrichment_model(
        ["text-embedding-nomic", "nomic-embed-v2", "modernbert-base"]
    )
    assert result is False


def test_resolve_model_empty_preferred_uses_first(tmp_db):
    """When no preferred model configured, uses first available chat model."""
    server = _server_with_preferred(tmp_db, "")
    result = server._pick_enrichment_model(["llama-3-8b", "text-embedding-nomic"])
    assert result is True
    assert server.enrichment_model == "llama-3-8b"


def test_resolve_model_empty_model_list(tmp_db):
    """When LM Studio returns an empty model list, returns False."""
    server = _server_with_preferred(tmp_db, "qwen3.5-35b-a3b")
    result = server._pick_enrichment_model([])
    assert result is False


def test_health_exposes_enrichment_model(tmp_db):
    """Health endpoint includes enrichment_model and enrichment_model_preferred."""
    _, db_path = tmp_db
    app = create_app(
        db_path=str(db_path),
        lmstudio_base_url="http://localhost:1234/v1",
        enrichment_model="my-model",
        poll_interval_secs=9999,
        enrichment_batch_size=5,
    )
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enrichment_model"] == "my-model"
        assert data["enrichment_model_preferred"] == "my-model"


# ---- /knowledge ----


def _seed_knowledge_nodes_for_list(conn):
    """Insert multiple knowledge nodes for list testing."""
    import json

    now_ms = int(time.time() * 1000)
    nodes = [
        {
            "id": 1,
            "uuid": "uuid-1",
            "content": json.dumps({"summary": "First node", "key": "value1"}),
            "embed_text": "first node embed text",
            "node_type": "observation",
            "outcome": "success",
            "tags": json.dumps(["rust", "testing"]),
            "created": now_ms,
        },
        {
            "id": 2,
            "uuid": "uuid-2",
            "content": json.dumps({"summary": "Second node", "key": "value2"}),
            "embed_text": "second node embed text",
            "node_type": "concept",
            "outcome": "success",
            "tags": json.dumps(["python"]),
            "created": now_ms + 1000,
        },
        {
            "id": 3,
            "uuid": "uuid-3",
            "content": json.dumps({"summary": "Third node", "key": "value3"}),
            "embed_text": "third node embed text",
            "node_type": "observation",
            "outcome": "failure",
            "tags": json.dumps(["debug"]),
            "created": now_ms + 2000,
        },
    ]
    for node in nodes:
        conn.execute(
            "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, outcome, tags, "
            "enrichment_model, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'model', ?, ?)",
            (
                node["id"],
                node["uuid"],
                node["content"],
                node["embed_text"],
                node["node_type"],
                node["outcome"],
                node["tags"],
                node["created"],
                node["created"],
            ),
        )
    conn.commit()


def test_knowledge_list_default(tmp_db):
    """GET /knowledge returns nodes with default pagination."""
    conn, db_path = tmp_db
    _seed_knowledge_nodes_for_list(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.get("/knowledge")
    assert resp.status_code == 200
    data = resp.json()
    assert "nodes" in data
    assert "total" in data
    assert data["total"] == 3
    assert len(data["nodes"]) == 3
    node = data["nodes"][0]
    assert "id" in node
    assert "uuid" in node
    assert "content" in node
    assert "node_type" in node
    assert "outcome" in node
    assert "tags" in node
    assert "created_at" in node


def test_knowledge_list_pagination(tmp_db):
    """GET /knowledge supports limit and offset params."""
    conn, db_path = tmp_db
    _seed_knowledge_nodes_for_list(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.get("/knowledge?limit=2&offset=1")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["nodes"]) == 2
    assert data["total"] == 3
    assert data["nodes"][0]["uuid"] == "uuid-2"


def test_knowledge_list_filter_by_node_type(tmp_db):
    """GET /knowledge supports node_type filter."""
    conn, db_path = tmp_db
    _seed_knowledge_nodes_for_list(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.get("/knowledge?node_type=observation")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    for node in data["nodes"]:
        assert node["node_type"] == "observation"


def test_knowledge_list_invalid_params_returns_400(tmp_db):
    """Invalid limit/offset params return 400."""
    conn, db_path = tmp_db
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.get("/knowledge?limit=abc")
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_knowledge_list_routes_included(tmp_db):
    """The /knowledge route is included in get_routes()."""
    _, db_path = tmp_db
    server = _make_server(str(db_path))
    routes = server.get_routes()
    paths = [r.path for r in routes]
    assert "/knowledge" in paths
    assert "/knowledge/{id:int}" in paths
    assert len(routes) == 9


# ---- /knowledge/{id} ----


def test_get_knowledge_returns_full_details(tmp_db):
    """GET /knowledge/{id} returns full node details including embed_text."""
    conn, db_path = tmp_db
    _seed_knowledge_nodes_for_list(conn)
    now_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO entities (id, type, name, canonical, first_seen, last_seen, created_at) "
        "VALUES (1, 'tool', 'swift', 'swift', ?, ?, ?)",
        (now_ms, now_ms, now_ms),
    )
    conn.execute("INSERT INTO knowledge_node_entities (knowledge_node_id, entity_id) VALUES (1, 1)")
    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) VALUES (99, ?, 'zsh', 'laptop', 'user')",
        (now_ms,),
    )
    conn.execute(
        "INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms, cwd, hostname, shell) "
        "VALUES (77, 99, ?, 'swift test', 0, 350, '/projects/hippo', 'laptop', 'zsh')",
        (now_ms,),
    )
    conn.execute("INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (1, 77)")
    conn.commit()
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.get("/knowledge/1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == 1
    assert data["uuid"] == "uuid-1"
    assert "First node" in data["content"]
    assert "embed_text" in data
    assert data["embed_text"] == "first node embed text"
    assert data["node_type"] == "observation"
    assert data["outcome"] == "success"
    assert data["tags"] == ["rust", "testing"]
    assert "created_at" in data
    assert data["related_entities"] == [{"id": 1, "name": "swift", "type": "tool"}]
    assert data["related_events"] == [{"id": 77, "command": "swift test"}]


def test_get_knowledge_returns_404_for_missing_node(tmp_db):
    """GET /knowledge/{id} returns 404 when node does not exist."""
    conn, db_path = tmp_db
    _seed_knowledge_nodes_for_list(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.get("/knowledge/999")
    assert resp.status_code == 404
    assert "error" in resp.json()


# ---- /events ----


def _seed_events_for_list(conn):
    """Insert multiple events for list testing."""
    now_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (now_ms,),
    )
    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (2, ?, 'zsh', 'laptop', 'user')",
        (now_ms,),
    )
    events = [
        (1, 1, now_ms, "cargo test -p hippo-core", 0, 3000, "/projects/hippo", "main"),
        (2, 1, now_ms + 1, "npm run build", 0, 5000, "/projects/webapp", "main"),
        (3, 2, now_ms + 2, "git status", 0, 100, "/projects/hippo", "feature-branch"),
        (4, 2, now_ms + 3, "make lint", 1, 2000, "/projects/hippo", "feature-branch"),
    ]
    for eid, sid, ts, cmd, exit_code, dur, cwd, branch in events:
        conn.execute(
            "INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms, "
            "cwd, hostname, shell, git_branch) VALUES (?, ?, ?, ?, ?, ?, ?, 'laptop', 'zsh', ?)",
            (eid, sid, ts, cmd, exit_code, dur, cwd, branch),
        )
    conn.commit()


def test_events_list_default(tmp_db):
    """GET /events returns events with default pagination."""
    conn, db_path = tmp_db
    _seed_events_for_list(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.get("/events")
    assert resp.status_code == 200
    data = resp.json()
    assert "events" in data
    assert "total" in data
    assert data["total"] == 4
    assert len(data["events"]) == 4
    event = data["events"][0]
    assert "id" in event
    assert "session_id" in event
    assert "timestamp" in event
    assert "command" in event
    assert "exit_code" in event
    assert "duration_ms" in event
    assert "cwd" in event
    assert "git_branch" in event


def test_events_list_pagination(tmp_db):
    """GET /events supports limit and offset params."""
    conn, db_path = tmp_db
    _seed_events_for_list(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.get("/events?limit=2&offset=1")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["events"]) == 2
    assert data["total"] == 4


def test_events_list_filter_by_session_id(tmp_db):
    """GET /events supports session_id filter."""
    conn, db_path = tmp_db
    _seed_events_for_list(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.get("/events?session_id=1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    for event in data["events"]:
        assert event["session_id"] == 1


def test_events_list_filter_by_project(tmp_db):
    """GET /events supports project filter (cwd LIKE)."""
    conn, db_path = tmp_db
    _seed_events_for_list(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.get("/events?project=webapp")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["events"][0]["command"] == "npm run build"


def test_events_list_invalid_params_returns_400(tmp_db):
    """Invalid limit/offset params return 400."""
    conn, db_path = tmp_db
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.get("/events?limit=abc")
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_events_list_invalid_session_id_returns_400(tmp_db):
    """Non-integer session_id param returns 400."""
    _, db_path = tmp_db
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.get("/events?session_id=notanint")
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_events_list_routes_included(tmp_db):
    """The /events route is included in get_routes()."""
    _, db_path = tmp_db
    server = _make_server(str(db_path))
    routes = server.get_routes()
    paths = [r.path for r in routes]
    assert "/events" in paths
    assert len(routes) == 9


# ---- /sessions ----


def _seed_sessions_for_list(conn):
    """Insert multiple sessions with events for list testing."""
    now_ms = int(time.time() * 1000)
    sessions = [
        (1, now_ms - 10000, "zsh", "laptop", "user"),
        (2, now_ms - 5000, "bash", "desktop", "user"),
        (3, now_ms, "zsh", "laptop", "user"),
    ]
    for sid, start, shell, host, user in sessions:
        conn.execute(
            "INSERT INTO sessions (id, start_time, shell, hostname, username) VALUES (?, ?, ?, ?, ?)",
            (sid, start, shell, host, user),
        )
    events = [
        (1, 1, now_ms - 10000, "cargo test", 0, 1000, "/projects/hippo"),
        (2, 1, now_ms - 9000, "cargo build", 0, 2000, "/projects/hippo"),
        (3, 2, now_ms - 5000, "make", 0, 500, "/projects/make"),
        (4, 3, now_ms, "ls", 0, 10, "/home"),
    ]
    for eid, sid, ts, cmd, exit_code, dur, cwd in events:
        conn.execute(
            "INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms, "
            "cwd, hostname, shell) VALUES (?, ?, ?, ?, ?, ?, ?, 'laptop', 'zsh')",
            (eid, sid, ts, cmd, exit_code, dur, cwd),
        )
    conn.commit()


def test_sessions_list_default(tmp_db):
    """GET /sessions returns sessions with default pagination."""
    conn, db_path = tmp_db
    _seed_sessions_for_list(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.get("/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert "sessions" in data
    assert "total" in data
    assert data["total"] == 3
    assert len(data["sessions"]) == 3
    session = data["sessions"][0]
    assert "id" in session
    assert "start_time" in session
    assert "hostname" in session
    assert "shell" in session
    assert "event_count" in session


def test_sessions_list_with_event_counts(tmp_db):
    """GET /sessions returns correct event_count for each session."""
    conn, db_path = tmp_db
    _seed_sessions_for_list(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.get("/sessions")
    assert resp.status_code == 200
    data = resp.json()
    sessions = {s["id"]: s for s in data["sessions"]}
    assert sessions[1]["event_count"] == 2
    assert sessions[2]["event_count"] == 1
    assert sessions[3]["event_count"] == 1


def test_sessions_list_pagination(tmp_db):
    """GET /sessions supports limit and offset params."""
    conn, db_path = tmp_db
    _seed_sessions_for_list(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.get("/sessions?limit=2&offset=1")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["sessions"]) == 2
    assert data["total"] == 3


def test_sessions_list_filter_by_since_ms(tmp_db):
    """GET /sessions supports since_ms filter."""
    conn, db_path = tmp_db
    _seed_sessions_for_list(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    now_ms = int(time.time() * 1000)
    resp = client.get(f"/sessions?since_ms={now_ms - 15000}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    for session in data["sessions"]:
        assert session["start_time"] > now_ms - 15000


def test_sessions_list_invalid_params_returns_400(tmp_db):
    """Invalid limit/offset params return 400."""
    conn, db_path = tmp_db
    _seed_sessions_for_list(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.get("/sessions?limit=abc")
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_sessions_list_routes_included(tmp_db):
    """The /sessions route is included in get_routes()."""
    _, db_path = tmp_db
    server = _make_server(str(db_path))
    routes = server.get_routes()
    paths = [r.path for r in routes]
    assert "/sessions" in paths
    assert len(routes) == 9
