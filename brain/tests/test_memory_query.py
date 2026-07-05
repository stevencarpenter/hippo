from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hippo_brain.auto_memory import ingest_memory_file, write_memory_knowledge_node
from hippo_brain.auto_memory_lifecycle import RevisionRetention, reconcile_configured_sources
from hippo_brain.memory_query import (
    MemoryQueryRequest,
    query_memory_current,
    run_memory_history_query,
)
from hippo_brain.models import EnrichmentResult
from hippo_brain.mcp import query_memory, query_memory_history


@pytest.fixture
def conn(tmp_db):
    connection, _path = tmp_db
    yield connection


def _publish(conn: sqlite3.Connection, revision_id: int, summary: str) -> None:
    result = EnrichmentResult(
        summary=summary,
        intent="memory",
        outcome="success",
        tags=["memory"],
        embed_text=summary,
    )
    node_id = write_memory_knowledge_node(
        conn, result, revision_id, "mock-model", now_ms=revision_id * 1000
    )
    assert node_id is not None


def _seed_memory_corpus(conn: sqlite3.Connection, tmp_path: Path) -> None:
    fixture_dir = (
        Path(__file__).parent.parent
        / "src"
        / "hippo_brain"
        / "_fixtures"
        / "auto_memory_spike"
        / "source"
    )
    for name in ("MEMORY.md", "feedback_candor.md", "project_architecture.md"):
        target = tmp_path / name
        target.write_text((fixture_dir / name).read_text(encoding="utf-8"))
        ingested = ingest_memory_file(conn, target, repository="hippo", now_ms=1000)
        _publish(conn, ingested.revision_id, f"summary for {name}")


def test_current_query_defaults_to_projected_active_memory(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed_memory_corpus(conn, tmp_path)
    out = query_memory_current(conn, MemoryQueryRequest(limit=50))
    assert out["view"] == "current"
    assert len(out["results"]) >= 3
    for row in out["results"]:
        assert row["document_state"] == "active"
        assert row["projection_status"] in ("ready", "stale", "pending", "processing")
        assert row["repository"] == "hippo"
        assert row["evidence_excerpt"]
        assert "source_path" not in row
        assert row["content_hash"]
        assert row["chunk_id"] is not None


def test_repository_and_category_filters(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _seed_memory_corpus(conn, tmp_path)
    feedback = query_memory_current(
        conn,
        MemoryQueryRequest(repository="hippo", category="feedback", limit=10),
    )
    assert feedback["results"]
    assert all("feedback" in row["logical_path"] for row in feedback["results"])
    assert all(
        any(cat["category"] == "feedback" for cat in row["memory_categories"])
        for row in feedback["results"]
    )


def test_text_query_matches_chunk_content(conn: sqlite3.Connection, tmp_path: Path) -> None:
    path = tmp_path / "MEMORY.md"
    path.write_text("# Busy\n\nSQLite busy_timeout matters.\n")
    ingested = ingest_memory_file(conn, path, repository="hippo", now_ms=1000)
    _publish(conn, ingested.revision_id, "busy timeout note")

    hits = query_memory_current(conn, MemoryQueryRequest(query="busy_timeout", limit=5))
    assert len(hits["results"]) == 1
    assert "busy_timeout" in hits["results"][0]["evidence_excerpt"]


def test_history_is_explicit_and_separate(conn: sqlite3.Connection, tmp_path: Path) -> None:
    path = tmp_path / "MEMORY.md"
    path.write_text("# One\n\nalpha\n")
    first = ingest_memory_file(conn, path, repository="hippo", now_ms=1000)
    _publish(conn, first.revision_id, "alpha")
    path.write_text("# One\n\nbeta\n")
    ingest_memory_file(conn, path, repository="hippo", now_ms=2000)

    current = query_memory_current(
        conn, MemoryQueryRequest(repository="hippo", logical_path="MEMORY.md")
    )
    assert len(current["results"]) >= 1
    assert "alpha" in current["results"][0]["evidence_excerpt"]

    history = run_memory_history_query(
        conn, repository="hippo", logical_path="MEMORY.md", limit=10
    )
    assert history["view"] == "history"
    assert len(history["results"]) == 2
    assert history["results"][0]["revision_number"] == 2
    assert "source_path" not in history["results"][0]


def test_include_source_path_is_opt_in(conn: sqlite3.Connection, tmp_path: Path) -> None:
    path = tmp_path / "MEMORY.md"
    path.write_text("# Path\n\nsecret path test\n")
    ingested = ingest_memory_file(conn, path, repository="hippo", now_ms=1000)
    _publish(conn, ingested.revision_id, "path test")

    row = query_memory_current(
        conn,
        MemoryQueryRequest(
            repository="hippo",
            logical_path="MEMORY.md",
            include_source_path=True,
        ),
    )["results"][0]
    assert str(path) in row["source_path"]


def test_non_queryable_status_stubs(conn: sqlite3.Connection, tmp_path: Path) -> None:
    path = tmp_path / "MEMORY.md"
    path.write_text("# Pending\n\nnot enriched yet\n")
    ingest_memory_file(conn, path, repository="hippo", now_ms=1000)

    default = query_memory_current(
        conn, MemoryQueryRequest(repository="hippo", logical_path="MEMORY.md")
    )
    assert default["results"] == []

    with_status = query_memory_current(
        conn,
        MemoryQueryRequest(
            repository="hippo",
            logical_path="MEMORY.md",
            include_non_queryable=True,
        ),
    )
    assert len(with_status["results"]) == 1
    assert with_status["results"][0]["projection_status"] == "pending"
    assert with_status["results"][0]["evidence_excerpt"] == ""


def test_unavailable_document_excluded_from_default_query(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    path = tmp_path / "MEMORY.md"
    path.write_text("# Gone\n\ndata\n")
    ingested = ingest_memory_file(conn, path, repository="hippo", now_ms=1000)
    _publish(conn, ingested.revision_id, "gone")
    path.unlink()

    reconcile_configured_sources(
        conn,
        [{"path": str(path), "repository": "hippo", "logical_path": "MEMORY.md"}],
        retention=RevisionRetention(absence_confirm_polls=2),
        now_ms=2000,
    )
    assert query_memory_current(conn, MemoryQueryRequest(query="data"))["results"] == []

    diagnostic = query_memory_current(
        conn,
        MemoryQueryRequest(query="data", include_non_queryable=True),
    )
    assert diagnostic["results"]
    assert diagnostic["results"][0]["document_state"] == "unavailable"


def test_memory_query_does_not_return_agentic_session_rows(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    path = tmp_path / "MEMORY.md"
    path.write_text("# Memory only\n\nauto memory corpus\n")
    ingested = ingest_memory_file(conn, path, repository="hippo", now_ms=1000)
    _publish(conn, ingested.revision_id, "auto memory corpus")

    conn.execute(
        "INSERT INTO agentic_sessions "
        "(session_id, harness, segment_index, start_time, end_time, cwd, project_dir, "
        "summary_text, message_count, tool_calls_json, probe_tag) "
        "VALUES ('sess-1', 'claude-code', 0, 1000, 2000, '/tmp/hippo', '/tmp/hippo', "
        "'session log about cargo', 1, '[]', NULL)"
    )
    conn.execute(
        "INSERT INTO knowledge_nodes (uuid, content, embed_text, outcome, tags, created_at) "
        "VALUES ('sess-node', '{\"summary\":\"session log about cargo\"}', "
        "'session log about cargo', '', '[]', 1500)"
    )
    session_node_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    session_id = conn.execute("SELECT id FROM agentic_sessions").fetchone()[0]
    conn.execute(
        "INSERT INTO knowledge_node_agentic_sessions (knowledge_node_id, agentic_session_id) "
        "VALUES (?, ?)",
        (session_node_id, session_id),
    )
    conn.commit()

    hits = query_memory_current(conn, MemoryQueryRequest(query="memory", limit=10))
    assert hits["results"]
    assert all("session log" not in row["evidence_excerpt"] for row in hits["results"])
    assert all(row["repository"] == "hippo" for row in hits["results"])


def test_cli_and_mcp_match_for_fixture(conn: sqlite3.Connection, tmp_path: Path, tmp_db) -> None:
    _seed_memory_corpus(conn, tmp_path)
    _conn, db_path = tmp_db

    cli = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(Path(__file__).parent.parent),
            "hippo-memory-query",
            "architecture",
            "--repository",
            "hippo",
            "--category",
            "project",
            "--db",
            str(db_path),
            "--limit",
            "5",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(Path(__file__).parent.parent.parent),
    )
    cli_payload = json.loads(cli.stdout)

    from hippo_brain.mcp import _state

    _state.db_path = str(db_path)
    mcp_payload = asyncio.run(
        query_memory(
            query="architecture",
            repository="hippo",
            category="project",
            limit=5,
        )
    )
    assert "error" not in mcp_payload
    assert cli_payload["results"] == mcp_payload["results"]

    cli_history = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(Path(__file__).parent.parent),
            "hippo-memory-query",
            "--history",
            "--repository",
            "hippo",
            "--logical-path",
            "MEMORY.md",
            "--db",
            str(db_path),
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(Path(__file__).parent.parent.parent),
    )
    hist_cli = json.loads(cli_history.stdout)

    hist_mcp = asyncio.run(
        query_memory_history(repository="hippo", logical_path="MEMORY.md", limit=50)
    )
    assert "error" not in hist_mcp
    assert hist_cli["results"] == hist_mcp["results"]
