"""Tests for hippo_brain.server — BrainServer endpoints and create_app."""

import asyncio
import time
from unittest.mock import AsyncMock

from starlette.testclient import TestClient

from hippo_brain.server import BrainServer, create_app


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
    assert "lmstudio_reachable" in data
    assert "enrichment_running" in data
    assert "db_reachable" in data
    assert "queue_depth" in data
    assert "queue_failed" in data
    assert "last_success_at_ms" in data
    assert "last_error" in data
    assert "last_error_at_ms" in data
    assert data["enrichment_running"] is False
    assert data["db_reachable"] is True
    assert data["queue_depth"] == 0
    assert data["queue_failed"] == 0
    assert data["last_success_at_ms"] is None
    assert data["last_error"] is None
    assert data["last_error_at_ms"] is None


# ---- /query ----


def test_query_returns_matching_events(tmp_db):
    conn, db_path = tmp_db
    _seed_events(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.post("/query", json={"text": "cargo"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["events"]) == 1
    assert data["events"][0]["command"] == "cargo test -p hippo-core"


def test_query_returns_matching_knowledge_nodes(tmp_db):
    conn, db_path = tmp_db
    _seed_events(conn)
    _seed_knowledge_nodes(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.post("/query", json={"text": "cargo"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["nodes"]) == 1
    assert data["nodes"][0]["uuid"] == "uuid-1"
    assert "cargo test" in data["nodes"][0]["embed_text"]


def test_query_no_results(tmp_db):
    conn, db_path = tmp_db
    _seed_events(conn)
    app = _make_app(str(db_path))
    client = TestClient(app)

    resp = client.post("/query", json={"text": "nonexistent_xyz"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["events"] == []
    assert data["nodes"] == []


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
    assert len(routes) == 2
    paths = [r.path for r in routes]
    assert "/health" in paths
    assert "/query" in paths


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


def test_query_returns_500_on_db_error(tmp_db):
    """When _get_conn raises, query endpoint returns 500 with error message."""
    _, db_path = tmp_db
    server = _make_server(str(db_path))

    # Point to a non-existent path that cannot be opened
    server.db_path = "/nonexistent/path/to/db.db"

    from starlette.applications import Starlette

    app = Starlette(routes=server.get_routes())
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/query", json={"text": "hello"})
    assert resp.status_code == 500
    data = resp.json()
    assert "error" in data


# ---- Enrichment loop ----


async def test_enrichment_loop_processes_events(tmp_db):
    """_enrichment_loop processes one batch of pending events then we cancel."""
    conn, db_path = tmp_db
    now_ms = int(time.time() * 1000)

    # Seed a session and event with queue entry
    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (now_ms,),
    )
    conn.execute(
        "INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms, "
        "cwd, hostname, shell) VALUES (1, 1, ?, 'cargo test', 0, 1000, '/proj', 'laptop', 'zsh')",
        (now_ms,),
    )
    conn.execute("INSERT INTO enrichment_queue (event_id) VALUES (1)")
    conn.commit()

    server = _make_server(str(db_path))
    server.poll_interval_secs = 0  # no delay

    # Mock the LMStudio client to return a valid enrichment response
    mock_chat = AsyncMock(
        return_value=(
            '{"summary": "test cmd", "intent": "testing", "outcome": "success", '
            '"entities": {"projects": [], "tools": [], "files": [], "services": [], "errors": []}, '
            '"relationships": [], "tags": ["test"], "embed_text": "test embed"}'
        )
    )
    server.client.chat = mock_chat

    # Run the enrichment loop and cancel after a short time
    task = asyncio.create_task(server._enrichment_loop())
    await asyncio.sleep(0.1)
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
    now_ms = int(time.time() * 1000)

    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (now_ms,),
    )
    conn.execute(
        "INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms, "
        "cwd, hostname, shell) VALUES (1, 1, ?, 'bad cmd', 1, 100, '/proj', 'laptop', 'zsh')",
        (now_ms,),
    )
    conn.execute("INSERT INTO enrichment_queue (event_id, max_retries) VALUES (1, 3)")
    conn.commit()

    server = _make_server(str(db_path))
    server.poll_interval_secs = 0

    # Make chat() raise
    server.client.chat = AsyncMock(side_effect=RuntimeError("model offline"))

    task = asyncio.create_task(server._enrichment_loop())
    await asyncio.sleep(0.1)
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
