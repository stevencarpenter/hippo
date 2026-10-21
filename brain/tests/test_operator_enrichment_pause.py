"""The operator enrichment pause (enrichment-state.json) gates only the enrichment loop."""

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from hippo_brain.server import BrainServer
from hippo_brain.watchdog import PreflightDecision

CHAT_REPLY = (
    '{"summary": "ran tests", "intent": "testing", "outcome": "success", '
    '"entities": {"projects": [], "tools": [], "files": [], "services": [], "errors": []}, '
    '"tags": ["t"], "embed_text": "ran tests"}'
)
OK = PreflightDecision(proceed=True, reason="ok", loaded_models=["test-model"])
FAIL = PreflightDecision(proceed=False, reason="unreachable", error="down", loaded_models=[])


def _write_state(data_dir: Path, paused: bool, changed_at_ms: int = 1792500000000) -> None:
    (data_dir / "enrichment-state.json").write_text(
        json.dumps({"paused": paused, "changed_at_ms": changed_at_ms})
    )


def _seed_shell_event(conn) -> None:
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


def _make_server(db_path, data_dir: Path) -> BrainServer:
    server = BrainServer(
        db_path=str(db_path),
        data_dir=str(data_dir),
        inference_base_url="http://localhost:1234/v1",
        enrichment_model="test-model",
        embedding_model="fake-embed",
        poll_interval_secs=0,
        enrichment_batch_size=5,
    )
    server.client.chat = AsyncMock(return_value=CHAT_REPLY)

    async def _no_embed(node_id, node_dict, source_label):
        pass

    server._embed_node = _no_embed  # type: ignore[method-assign]
    return server


async def _hang(*args, **kwargs):
    await asyncio.Event().wait()


async def _run_loop(server: BrainServer, decision=OK, secs: float = 0.2) -> None:
    """Run the loop briefly. decision=None makes preflight hang, so it never touches source_health."""
    mock = AsyncMock(side_effect=_hang) if decision is None else AsyncMock(return_value=decision)
    with patch("hippo_brain.server.preflight_inference", mock):
        task = asyncio.create_task(server._enrichment_loop())
        await asyncio.sleep(secs)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _queue_status(conn) -> str:
    return conn.execute("SELECT status FROM enrichment_queue WHERE event_id = 1").fetchone()[0]


def _preflight_failures(conn) -> int:
    return conn.execute(
        "SELECT consecutive_failures FROM source_health WHERE source = 'brain-preflight'"
    ).fetchone()[0]


@pytest.fixture
def env(tmp_db, tmp_path):
    conn, db_path = tmp_db
    _seed_shell_event(conn)
    return conn, db_path, tmp_path


@pytest.mark.asyncio
async def test_operator_pause_stops_enrichment_claims(env):
    conn, db_path, data_dir = env
    server = _make_server(db_path, data_dir)
    try:
        _write_state(data_dir, paused=True)
        await _run_loop(server)
        assert _queue_status(conn) == "pending"
        server.client.chat.assert_not_awaited()
    finally:
        server.close()


@pytest.mark.asyncio
async def test_brain_starting_paused_stays_paused(env):
    conn, db_path, data_dir = env
    _write_state(data_dir, paused=True)
    server = _make_server(db_path, data_dir)
    try:
        await _run_loop(server)
        assert _queue_status(conn) == "pending"
        server.client.chat.assert_not_awaited()
    finally:
        server.close()


@pytest.mark.asyncio
async def test_resume_on_disk_restarts_enrichment_without_restart(env):
    conn, db_path, data_dir = env
    _write_state(data_dir, paused=True)
    server = _make_server(db_path, data_dir)
    try:
        await _run_loop(server)
        assert _queue_status(conn) == "pending"
        _write_state(data_dir, paused=False)
        await _run_loop(server)
        assert _queue_status(conn) != "pending"
        server.client.chat.assert_awaited()
    finally:
        server.close()


@pytest.mark.asyncio
async def test_bench_resume_does_not_lift_operator_pause(env):
    conn, db_path, data_dir = env
    _write_state(data_dir, paused=True)
    server = _make_server(db_path, data_dir)
    try:
        await server.control_pause(None)
        await server.control_resume(None)
        await _run_loop(server)
        assert _queue_status(conn) == "pending"
        server.client.chat.assert_not_awaited()
    finally:
        server.close()


@pytest.mark.asyncio
async def test_operator_resume_does_not_lift_bench_pause(env):
    conn, db_path, data_dir = env
    _write_state(data_dir, paused=False)
    server = _make_server(db_path, data_dir)
    try:
        await server.control_pause(None)
        await _run_loop(server)
        assert _queue_status(conn) == "pending"
        server.client.chat.assert_not_awaited()
    finally:
        server.close()


@pytest.mark.asyncio
async def test_operator_pause_keeps_queries_and_reaper(env):
    conn, db_path, data_dir = env
    old = int(time.time() * 1000) - 3_600_000
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS knowledge_vectors_rowids "
        "(rowid INTEGER PRIMARY KEY, id, chunk_id, chunk_offset);"
    )
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, "
        "created_at, updated_at) VALUES (1, 'u1', 'c', 'et', 'observation', ?, ?)",
        (old, old),
    )
    conn.commit()
    _write_state(data_dir, paused=True)
    server = _make_server(db_path, data_dir)
    try:
        await _run_loop(server)
        embedded: list[int] = []

        async def _rec(node_id, node_dict, source_label):
            embedded.append(node_id)

        server._embed_node = _rec  # type: ignore[method-assign]
        await server._embed_reaper_tick()
        assert embedded == [1]

        with TestClient(Starlette(routes=server.get_routes())) as client:
            resp = client.post("/query", json={"text": "tests", "mode": "lexical"})
            assert resp.status_code == 200
    finally:
        server.close()


@pytest.mark.asyncio
async def test_pause_transition_resets_preflight_failures(env):
    conn, db_path, data_dir = env
    server = _make_server(db_path, data_dir)
    try:
        # Running with failing preflight: the counter climbs.
        await _run_loop(server, decision=FAIL, secs=0.05)
        assert _preflight_failures(conn) >= 1
        conn.execute(
            "UPDATE source_health SET consecutive_failures = 40 WHERE source = 'brain-preflight'"
        )
        conn.commit()
        _write_state(data_dir, paused=True)
        await _run_loop(server, decision=FAIL)
        assert _preflight_failures(conn) == 0
    finally:
        server.close()


@pytest.mark.asyncio
async def test_pause_to_running_transition_resets_preflight_failures(env):
    conn, db_path, data_dir = env
    _write_state(data_dir, paused=True)
    server = _make_server(db_path, data_dir)
    try:
        await _run_loop(server, decision=None)
        conn.execute(
            "UPDATE source_health SET consecutive_failures = 40 WHERE source = 'brain-preflight'"
        )
        conn.commit()
        _write_state(data_dir, paused=False)
        await _run_loop(server, decision=None)
        assert _preflight_failures(conn) == 0
    finally:
        server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("paused", "expected"), [(True, 0), (False, 40)])
async def test_first_observation_resets_preflight_failures_only_when_paused(env, paused, expected):
    conn, db_path, data_dir = env
    conn.execute(
        "UPDATE source_health SET consecutive_failures = 40 WHERE source = 'brain-preflight'"
    )
    conn.commit()
    _write_state(data_dir, paused=paused)
    server = _make_server(db_path, data_dir)
    try:
        await _run_loop(server, decision=None)
        assert _preflight_failures(conn) == expected
    finally:
        server.close()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            json.dumps({"paused": True, "changed_at_ms": 1792500000000}),
            (True, 1792500000000, True),
        ),
        (json.dumps({"paused": False, "changed_at_ms": 1792500000000}), (False, None, True)),
        ("not json {", (True, None, False)),
        (None, (False, None, True)),
    ],
)
def test_health_reports_operator_pause(tmp_db, tmp_path, content, expected):
    _, db_path = tmp_db
    if content is not None:
        (tmp_path / "enrichment-state.json").write_text(content)
    server = _make_server(db_path, tmp_path)
    server.client.is_reachable = AsyncMock(return_value=False)
    try:
        body = TestClient(Starlette(routes=server.get_routes())).get("/health").json()
        assert (
            body["enrichment_paused"],
            body["enrichment_paused_since_ms"],
            body["enrichment_state_valid"],
        ) == expected
    finally:
        server.close()
