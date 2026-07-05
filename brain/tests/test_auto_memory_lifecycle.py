from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hippo_brain.auto_memory import (
    RevisionRetention,
    ingest_memory_file,
    query_memory_history,
    reconcile_configured_sources,
    write_memory_knowledge_node,
)
from hippo_brain.models import EnrichmentResult
from hippo_brain.mcp_queries import search_knowledge_lexical


@pytest.fixture
def conn(tmp_db):
    connection, _path = tmp_db
    yield connection


def _publish(conn: sqlite3.Connection, revision_id: int, summary: str) -> int:
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
    return node_id


def test_update_clears_superseded_redacted_content_and_stores_diff(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    source = tmp_path / "MEMORY.md"
    source.write_text("# One\n\nFirst body.\n")
    first = ingest_memory_file(conn, source, repository="hippo", now_ms=1000)

    source.write_text("# One\n\nSecond body with more detail.\n")
    second = ingest_memory_file(conn, source, repository="hippo", now_ms=2000)

    assert second.revision_number == 2
    old = conn.execute(
        "SELECT redacted_content, summary, diff_text FROM memory_revisions WHERE id = ?",
        (first.revision_id,),
    ).fetchone()
    assert old[0] is None
    assert "added" in old[1]
    assert "Second body" in old[2]
    current = conn.execute(
        "SELECT redacted_content FROM memory_revisions WHERE id = ?",
        (second.revision_id,),
    ).fetchone()[0]
    assert "Second body" in current


def test_history_query_returns_revision_metadata(conn: sqlite3.Connection, tmp_path: Path) -> None:
    source = tmp_path / "MEMORY.md"
    source.write_text("# Alpha\n\none\n")
    ingest_memory_file(conn, source, repository="hippo", now_ms=1000)
    source.write_text("# Alpha\n\ntwo\n")
    ingest_memory_file(conn, source, repository="hippo", now_ms=2000)

    history = query_memory_history(conn, repository="hippo", logical_path="MEMORY.md", limit=10)
    assert len(history) == 2
    assert history[0]["revision_number"] == 2
    assert history[0]["change_kind"] == "update"
    assert history[1]["change_kind"] == "create"
    assert "one" in (history[1]["diff_text"] or "")


def test_unambiguous_rename_preserves_document_identity(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    old_path = tmp_path / "notes.md"
    old_path.write_text("# Stable\n\nunchanged\n")
    first = ingest_memory_file(
        conn, old_path, repository="hippo", logical_path="notes.md", now_ms=1000
    )

    old_path.unlink()
    new_path = tmp_path / "reference.md"
    new_path.write_text("# Stable\n\nunchanged\n")
    second = ingest_memory_file(
        conn, new_path, repository="hippo", logical_path="reference.md", now_ms=2000
    )

    assert second.document_uuid == first.document_uuid
    assert second.changed is False
    rename = conn.execute(
        "SELECT change_kind FROM memory_revisions WHERE document_id = ? "
        "ORDER BY revision_number DESC LIMIT 1",
        (first.document_id,),
    ).fetchone()[0]
    assert rename == "rename"


def test_ambiguous_same_content_creates_distinct_documents(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    left = tmp_path / "a.md"
    right = tmp_path / "b.md"
    body = "# Shared\n\nsame text\n"
    left.write_text(body)
    right.write_text(body)

    first = ingest_memory_file(conn, left, repository="hippo", logical_path="a.md")
    second = ingest_memory_file(conn, right, repository="hippo", logical_path="b.md")

    assert first.document_uuid != second.document_uuid
    assert conn.execute("SELECT COUNT(*) FROM memory_documents").fetchone()[0] == 2


def test_missing_source_becomes_unavailable_then_tombstoned(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    source = tmp_path / "MEMORY.md"
    source.write_text("# Gone\n\nsoon\n")
    ingested = ingest_memory_file(conn, source, repository="hippo", now_ms=1000)
    _publish(conn, ingested.revision_id, "gone soon")
    source.unlink()

    retention = RevisionRetention(absence_confirm_polls=2)
    sources = [{"path": str(source), "repository": "hippo", "logical_path": "MEMORY.md"}]

    reconcile_configured_sources(conn, sources, retention=retention, now_ms=2000)
    state = conn.execute(
        "SELECT state FROM memory_documents WHERE id = ?", (ingested.document_id,)
    ).fetchone()[0]
    assert state == "unavailable"
    assert (
        search_knowledge_lexical(conn, "gone", source="claude-auto-memory", project="hippo") == []
    )

    reconcile_configured_sources(conn, sources, retention=retention, now_ms=3000)
    state = conn.execute(
        "SELECT state FROM memory_documents WHERE id = ?", (ingested.document_id,)
    ).fetchone()[0]
    assert state == "tombstoned"
    history = query_memory_history(conn, repository="hippo", logical_path="MEMORY.md", limit=5)
    assert history[0]["change_kind"] == "delete"


def test_revision_pruning_respects_count(conn: sqlite3.Connection, tmp_path: Path) -> None:
    source = tmp_path / "MEMORY.md"
    retention = RevisionRetention(max_count=2, max_age_ms=10_000_000)
    for index in range(3):
        source.write_text(f"# Rev {index}\n\nbody {index}\n")
        ingest_memory_file(
            conn, source, repository="hippo", now_ms=1000 + index, retention=retention
        )

    count = conn.execute(
        "SELECT COUNT(*) FROM memory_revisions WHERE document_id = ?",
        (conn.execute("SELECT id FROM memory_documents").fetchone()[0],),
    ).fetchone()[0]
    assert count == 2


def test_full_lifecycle_add_update_history_rename_delete(tmp_db, tmp_path: Path) -> None:
    from hippo_brain import vector_store

    _seeded, db_path = tmp_db
    conn = vector_store.open_conn(db_path)
    path = tmp_path / "MEMORY.md"
    path.write_text("# Start\n\nv1\n")
    first = ingest_memory_file(conn, path, repository="hippo", now_ms=1000)
    _publish(conn, first.revision_id, "start v1")

    path.write_text("# Start\n\nv2\n")
    second = ingest_memory_file(conn, path, repository="hippo", now_ms=2000)
    _publish(conn, second.revision_id, "start v2")
    assert len(query_memory_history(conn, repository="hippo", logical_path="MEMORY.md")) >= 2

    path.unlink()
    renamed = tmp_path / "ARCHIVE.md"
    renamed.write_text("# Start\n\nv2\n")
    third = ingest_memory_file(
        conn, renamed, repository="hippo", logical_path="ARCHIVE.md", now_ms=3000
    )
    assert third.document_uuid == first.document_uuid

    sources = [{"path": str(renamed), "repository": "hippo", "logical_path": "ARCHIVE.md"}]
    renamed.unlink()
    reconcile_configured_sources(
        conn, sources, retention=RevisionRetention(absence_confirm_polls=1), now_ms=4000
    )
    assert (
        conn.execute(
            "SELECT state FROM memory_documents WHERE id = ?", (first.document_id,)
        ).fetchone()[0]
        == "tombstoned"
    )
    assert (
        search_knowledge_lexical(conn, "start", source="claude-auto-memory", project="hippo") == []
    )
