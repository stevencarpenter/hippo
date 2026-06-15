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
from hippo_brain.server import _collect_queue_depths, _source_label_for_claude_segments
from hippo_brain.watchdog import PreflightDecision


def _make_server(db_path: str, **kwargs) -> BrainServer:
    defaults = dict(
        db_path=db_path,
        inference_base_url="http://localhost:1234/v1",
        enrichment_model="test-model",
        poll_interval_secs=60,
        enrichment_batch_size=5,
    )
    defaults.update(kwargs)
    return BrainServer(**defaults)


def _make_app(db_path: str, **kwargs) -> Starlette:
    server = _make_server(db_path, **kwargs)
    return Starlette(routes=server.get_routes())


# ---- Enrichment queue telemetry source coverage ----


def test_collect_queue_depths_splits_agentic_sources():
    """The queue-depth gauge must expose Codex and opencode as first-class
    sources, not hide them under claude or omit them entirely."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE enrichment_queue (status TEXT NOT NULL);
        CREATE TABLE browser_enrichment_queue (status TEXT NOT NULL);
        CREATE TABLE workflow_enrichment_queue (status TEXT NOT NULL);
        CREATE TABLE agentic_sessions (
            id INTEGER PRIMARY KEY,
            harness TEXT NOT NULL,
            probe_tag TEXT
        );
        CREATE TABLE agentic_enrichment_queue (
            session_id INTEGER NOT NULL,
            status TEXT NOT NULL
        );
        INSERT INTO enrichment_queue (status) VALUES ('pending');
        INSERT INTO browser_enrichment_queue (status) VALUES ('failed');
        INSERT INTO workflow_enrichment_queue (status) VALUES ('processing');
        INSERT INTO agentic_sessions (id, harness, probe_tag) VALUES
            (1, 'opencode', NULL),
            (2, 'claude-code', NULL),
            (3, 'codex', NULL),
            (4, 'codex', NULL),
            (5, 'opencode', 'probe');
        INSERT INTO agentic_enrichment_queue (session_id, status) VALUES
            (1, 'failed'), (2, 'pending'), (3, 'pending'), (4, 'pending'), (5, 'pending');
        """
    )

    rows = {(source, status): count for source, status, count in _collect_queue_depths(conn)}

    assert rows[("shell", "pending")] == 1
    assert rows[("claude", "pending")] == 1
    assert rows[("codex", "pending")] == 2
    assert rows[("opencode", "failed")] == 1
    assert rows[("workflow", "processing")] == 1
    assert rows[("browser", "failed")] == 1


def test_collect_queue_depths_tolerates_missing_table():
    """A missing table must drop only that source, not blank the whole metric.

    Regression target: on an older schema missing workflow_enrichment_queue or
    agentic_sessions/agentic_enrichment_queue, the entire gauge callback went
    blank instead of just omitting the unavailable source.
    """
    import sqlite3

    # DB with only the shell enrichment_queue table — all others absent.
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE enrichment_queue (status TEXT NOT NULL);
        INSERT INTO enrichment_queue (status) VALUES ('pending');
        INSERT INTO enrichment_queue (status) VALUES ('pending');
        """
    )

    rows = {(source, status): count for source, status, count in _collect_queue_depths(conn)}

    # Shell depths must be populated even though other tables are missing.
    assert rows[("shell", "pending")] == 2
    # Sources whose tables do not exist must be absent, not raise.
    assert ("workflow", "pending") not in rows
    assert ("opencode", "pending") not in rows


def test_source_label_for_claude_segments_splits_codex_metrics():
    assert _source_label_for_claude_segments([{"harness": "codex"}]) == "codex"
    assert (
        _source_label_for_claude_segments(
            [{"source_file": "/Users/me/.codex/sessions/rollout-abc.jsonl"}]
        )
        == "codex"
    )
    assert (
        _source_label_for_claude_segments(
            [{"source_file": "/Users/me/.claude/projects/proj/session.jsonl"}]
        )
        == "claude"
    )


def test_source_label_for_cursor_segments():
    from hippo_brain.server import _source_label_for_claude_segments

    cursor_segs = [{"source_file": "/Users/me/.cursor/projects/p/agent-transcripts/s/s.jsonl"}]
    assert _source_label_for_claude_segments(cursor_segs) == "cursor"

    mixed = [
        {"source_file": "/Users/me/.cursor/projects/p/agent-transcripts/s/s.jsonl"},
        {"source_file": "/Users/me/.claude/projects/p/s.jsonl"},
    ]
    assert _source_label_for_claude_segments(mixed) == "claude"


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
    with patch("hippo_brain.server.preflight_inference", new_callable=AsyncMock, return_value=ok):
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
    with patch("hippo_brain.server.preflight_inference", new_callable=AsyncMock, return_value=ok):
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

    async def _fake_enrich(db_path_arg, run_id, inference, query_model):
        enrich_calls.append(run_id)

    ok = PreflightDecision(proceed=True, reason="ok", loaded_models=["test-model"])
    with (
        patch(
            "hippo_brain.server.preflight_inference",
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


@pytest.mark.asyncio
async def test_workflow_enrichment_schedules_embedding(tmp_db):
    """Workflow enrichment must schedule _embed_node, like every other source.

    Regression target: workflow/CI knowledge nodes were written to SQLite but
    never embedded, silently falling out of semantic search.
    """
    conn, db_path = tmp_db
    now_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO workflow_runs (id, repo, head_sha, event, status, conclusion, "
        "html_url, raw_json, first_seen_at, last_seen_at) "
        "VALUES (42, 'org/repo', 'abc123', 'push', 'completed', 'success', "
        "'https://x', '{}', ?, ?)",
        (now_ms, now_ms),
    )
    conn.execute(
        "INSERT INTO workflow_enrichment_queue (run_id, enqueued_at, updated_at) VALUES (42, ?, ?)",
        (now_ms, now_ms),
    )
    conn.commit()

    server = _make_server(str(db_path), embedding_model="fake-embed")
    server.poll_interval_secs = 0
    server.client.chat = AsyncMock(
        return_value=(
            '{"summary": "CI run completed successfully", "intent": "ci", '
            '"outcome": "success", "entities": {"projects": [], "tools": [], '
            '"files": [], "services": [], "errors": []}, "tags": [], '
            '"embed_text": "ci run org/repo abc123 success"}'
        )
    )

    embed_calls: list[tuple[int, str]] = []

    async def _rec(node_id, node_dict, source_label):
        embed_calls.append((node_id, source_label))

    server._embed_node = _rec  # type: ignore[method-assign]

    ok = PreflightDecision(proceed=True, reason="ok", loaded_models=["test-model"])
    with patch("hippo_brain.server.preflight_inference", new_callable=AsyncMock, return_value=ok):
        task = asyncio.create_task(server._enrichment_loop())
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert any(src == "workflow" for _, src in embed_calls)


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


# ---- Task 3: [reaper] config threading ----


def test_brain_server_stores_reaper_settings():
    """BrainServer must accept and store the reaper tuning knobs."""
    server = BrainServer(
        db_path=":memory:",
        embed_reaper_interval_secs=120,
        embed_reaper_batch_size=7,
        embed_orphan_stale_secs=600,
    )
    assert server.embed_reaper_interval_secs == 120
    assert server.embed_reaper_batch_size == 7
    assert server.embed_orphan_stale_secs == 600


# ---- Task 4: _embed_reaper_tick + _embed_reaper_loop ----


@pytest.mark.asyncio
async def test_embed_reaper_tick_reembeds_only_old_orphans(tmp_db):
    """The reaper re-embeds nodes older than the staleness window that lack a
    vector row; recent nodes and already-embedded nodes are left alone."""
    conn, db_path = tmp_db
    now_ms = int(time.time() * 1000)
    old = now_ms - 3_600_000  # 1h old
    recent = now_ms - 60_000  # 1m old — inside the staleness window
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS knowledge_vectors_rowids "
        "(rowid INTEGER PRIMARY KEY, id, chunk_id, chunk_offset);"
    )
    for nid, created in ((1, old), (2, recent), (3, old)):
        conn.execute(
            "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, "
            "created_at, updated_at) VALUES (?, ?, 'c', 'et', 'observation', ?, ?)",
            (nid, f"u{nid}", created, created),
        )
    # Node 3 already has a vector row.
    conn.execute("INSERT INTO knowledge_vectors_rowids (rowid) VALUES (3)")
    conn.commit()

    server = _make_server(str(db_path), embed_orphan_stale_secs=900, embed_reaper_batch_size=50)
    embedded: list[int] = []

    async def _rec(node_id, node_dict, source_label):
        embedded.append(node_id)

    server._embed_node = _rec  # type: ignore[method-assign]

    await server._embed_reaper_tick()

    assert embedded == [1]  # only the old, unembedded node


@pytest.mark.asyncio
async def test_embed_reaper_tick_survives_single_embed_failure(tmp_db):
    """One orphan's embed failure must not abort the rest of the sweep."""
    conn, db_path = tmp_db
    now_ms = int(time.time() * 1000)
    old = now_ms - 3_600_000
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS knowledge_vectors_rowids "
        "(rowid INTEGER PRIMARY KEY, id, chunk_id, chunk_offset);"
    )
    for nid in (1, 2):
        conn.execute(
            "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, "
            "created_at, updated_at) VALUES (?, ?, 'c', 'et', 'observation', ?, ?)",
            (nid, f"u{nid}", old, old),
        )
    conn.commit()

    server = _make_server(str(db_path), embed_orphan_stale_secs=900)
    seen: list[int] = []

    async def _rec(node_id, node_dict, source_label):
        seen.append(node_id)
        if node_id == 1:
            raise RuntimeError("embed boom")

    server._embed_node = _rec  # type: ignore[method-assign]

    # Node 1 raising must not abort the sweep — node 2 is still attempted, and
    # the tick itself does not propagate the failure.
    await server._embed_reaper_tick()
    assert seen == [1, 2]


@pytest.mark.asyncio
async def test_embed_reaper_tick_skips_when_paused(tmp_db):
    """A paused brain (hippo-bench isolation) must not run reaper embeds —
    they would issue inference calls and corrupt benchmark isolation."""
    conn, db_path = tmp_db
    now_ms = int(time.time() * 1000)
    old = now_ms - 3_600_000
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

    server = _make_server(str(db_path), embed_orphan_stale_secs=900)
    server._paused = True
    embedded: list[int] = []

    async def _rec(node_id, node_dict, source_label):
        embedded.append(node_id)

    server._embed_node = _rec  # type: ignore[method-assign]

    await server._embed_reaper_tick()
    assert embedded == []  # paused — no embeds issued


@pytest.mark.asyncio
async def test_embed_reaper_tick_propagates_non_missing_table_errors(tmp_db):
    """Only a missing shadow table is tolerated; a real operational error
    (locked DB, bad SQL) must surface, not be masked as a healthy idle reaper."""
    import sqlite3

    _, db_path = tmp_db
    server = _make_server(str(db_path), embed_orphan_stale_secs=900)

    class _LockedConn:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            pass

    server._get_conn = lambda: _LockedConn()  # type: ignore[method-assign]

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        await server._embed_reaper_tick()


def test_vault_export_endpoint_invokes_export(tmp_path, monkeypatch, tmp_db):
    from starlette.testclient import TestClient

    from hippo_brain.server import create_app

    captured = {}

    def fake_export(
        conn,
        out_dir,
        hippo_version,
        related_top_k,
        hub_degree_cap,
        hub_node_list_cap,
        shard_by,
        full,
    ):
        captured.update(out_dir=out_dir, top_k=related_top_k, cap=hub_degree_cap, full=full)
        return {"nodes": 3, "written": 3, "unchanged": 0, "deleted": 1}

    monkeypatch.setattr("hippo_brain.server.export_vault", fake_export)
    _, db_path = tmp_db
    app = create_app(db_path=str(db_path))
    with TestClient(app) as client:
        resp = client.post(
            "/vault/export",
            json={"out": str(tmp_path / "v"), "related_top_k": 5, "full": True},
        )
    assert resp.status_code == 200
    assert resp.json()["nodes"] == 3
    assert captured["out_dir"].endswith("/v") and captured["top_k"] == 5
    assert captured["full"] is True


def test_vault_export_requires_out(tmp_db):
    from starlette.testclient import TestClient

    from hippo_brain.server import create_app

    _, db_path = tmp_db
    app = create_app(db_path=str(db_path))
    with TestClient(app) as client:
        resp = client.post("/vault/export", json={})
    assert resp.status_code == 400
