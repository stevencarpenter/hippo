from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hippo_brain.auto_memory import ingest_memory_file


SCHEMA = """
CREATE TABLE memory_documents (
    id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE,
    source_kind TEXT NOT NULL, repository TEXT NOT NULL, logical_path TEXT NOT NULL,
    source_path TEXT NOT NULL, current_revision_id INTEGER, active_revision_id INTEGER,
    state TEXT NOT NULL, projection_status TEXT NOT NULL, last_error TEXT,
    observed_at INTEGER NOT NULL, tombstoned_at INTEGER,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    UNIQUE(source_kind, repository, logical_path)
);
CREATE TABLE memory_revisions (
    id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL, revision_number INTEGER NOT NULL,
    content_hash TEXT NOT NULL, source_hash TEXT NOT NULL, redacted_content TEXT,
    source_mtime_ms INTEGER NOT NULL, source_size INTEGER NOT NULL,
    change_kind TEXT NOT NULL, summary TEXT, diff_text TEXT, chunker_name TEXT NOT NULL,
    chunker_version INTEGER NOT NULL, chunker_config_json TEXT NOT NULL,
    enrichment_model TEXT, enrichment_version INTEGER NOT NULL,
    enriched_at INTEGER, created_at INTEGER NOT NULL,
    UNIQUE(document_id, revision_number)
);
CREATE TABLE memory_chunks (
    id INTEGER PRIMARY KEY, revision_id INTEGER NOT NULL, ordinal INTEGER NOT NULL,
    heading_path TEXT NOT NULL, start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL,
    content TEXT NOT NULL, content_hash TEXT NOT NULL, token_count INTEGER NOT NULL,
    created_at INTEGER NOT NULL, UNIQUE(revision_id, ordinal)
);
CREATE TABLE memory_enrichment_queue (
    id INTEGER PRIMARY KEY, revision_id INTEGER NOT NULL UNIQUE, status TEXT NOT NULL,
    priority INTEGER NOT NULL, retry_count INTEGER NOT NULL, max_retries INTEGER NOT NULL,
    error_message TEXT, locked_at INTEGER, locked_by TEXT,
    enqueued_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE source_health (
    source TEXT PRIMARY KEY, last_event_ts INTEGER, last_success_ts INTEGER,
    last_error TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
INSERT INTO source_health(source, updated_at) VALUES ('claude-auto-memory', 0);
"""


@pytest.fixture
def conn() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    try:
        yield db
    finally:
        db.close()


def test_ingest_redacts_before_persist_and_chunks_markdown(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    source = tmp_path / "MEMORY.md"
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    source.write_text(f"# Build\n\nUse cargo.\n\n## Auth\n\nToken: {secret}\n")

    result = ingest_memory_file(conn, source, repository="hippo")

    assert result.changed is True
    assert result.revision_number == 1
    persisted = conn.execute(
        "SELECT redacted_content FROM memory_revisions WHERE id = ?", (result.revision_id,)
    ).fetchone()[0]
    assert secret not in persisted
    assert "[REDACTED]" in persisted
    chunks = conn.execute(
        "SELECT ordinal, heading_path, content FROM memory_chunks "
        "WHERE revision_id = ? ORDER BY ordinal",
        (result.revision_id,),
    ).fetchall()
    assert chunks == [
        (0, "Build", "# Build\n\nUse cargo."),
        (1, "Build > Auth", "## Auth\n\nToken: [REDACTED]"),
    ]
    assert conn.execute("SELECT COUNT(*) FROM memory_enrichment_queue").fetchone()[0] == 1


def test_ingest_has_stable_identity_and_unchanged_file_is_noop(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    source = tmp_path / "MEMORY.md"
    source.write_text("# Decision\n\nKeep source read-only.\n")

    first = ingest_memory_file(conn, source, repository="hippo")
    second = ingest_memory_file(conn, source, repository="hippo")

    assert first.document_uuid == second.document_uuid
    assert second.changed is False
    assert second.revision_id == first.revision_id
    assert conn.execute("SELECT COUNT(*) FROM memory_documents").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM memory_enrichment_queue").fetchone()[0] == 1


def test_ingest_requires_an_explicit_regular_file(conn: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="regular file"):
        ingest_memory_file(conn, tmp_path, repository="hippo")
