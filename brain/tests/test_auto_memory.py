from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hippo_brain.auto_memory import (
    claim_pending_memories,
    derive_repository_identity,
    ingest_memory_file,
    main,
    mark_memory_enrichment_failed,
    write_memory_knowledge_node,
)
from hippo_brain.models import EnrichmentResult
from hippo_brain.mcp_queries import search_knowledge_lexical
from hippo_brain.client import MockInferenceClient
from hippo_brain.server import BrainServer


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
CREATE TABLE knowledge_nodes (
    id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, content TEXT NOT NULL,
    embed_text TEXT NOT NULL, node_type TEXT NOT NULL, outcome TEXT, tags TEXT,
    enrichment_model TEXT, enrichment_version INTEGER NOT NULL,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE knowledge_node_memory_chunks (
    knowledge_node_id INTEGER NOT NULL, memory_chunk_id INTEGER NOT NULL,
    PRIMARY KEY(knowledge_node_id, memory_chunk_id)
);
CREATE TABLE knowledge_node_events (knowledge_node_id INTEGER, event_id INTEGER);
CREATE TABLE events (id INTEGER PRIMARY KEY, cwd TEXT, git_repo TEXT, git_branch TEXT);
CREATE TABLE knowledge_node_browser_events (
    knowledge_node_id INTEGER, browser_event_id INTEGER
);
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


def test_ingest_uses_clear_stable_fallback_outside_git(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    source = tmp_path / "memory" / "MEMORY.md"
    source.parent.mkdir()
    source.write_text("# Local\n\nOutside a Git checkout.\n")

    result = ingest_memory_file(conn, source)

    repository = conn.execute("SELECT repository FROM memory_documents").fetchone()[0]
    assert repository == f"local:{source.parent.resolve()}"
    assert ingest_memory_file(conn, source).document_uuid == result.document_uuid


def test_repository_identity_uses_sanitized_git_origin(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:sjcarpenter/hippo.git"],
        check=True,
    )
    source = repo / "MEMORY.md"
    source.write_text("# Git\n")

    assert derive_repository_identity(source) == "github.com/sjcarpenter/hippo"


def test_claim_and_complete_enrichment_publishes_provenance_atomically(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    source = tmp_path / "MEMORY.md"
    source.write_text("# Database\n\nUse WAL and a five-second busy timeout.\n")
    ingested = ingest_memory_file(conn, source, repository="hippo", now_ms=1000)

    claims = claim_pending_memories(conn, worker_id="test-worker", limit=10, now_ms=2000)

    assert len(claims) == 1
    assert claims[0]["revision_id"] == ingested.revision_id
    assert claims[0]["repository"] == "hippo"
    assert claims[0]["source_path"] == str(source.resolve())
    assert claims[0]["chunks"][0]["heading_path"] == "Database"
    assert (
        conn.execute(
            "SELECT status FROM memory_enrichment_queue WHERE revision_id = ?",
            (ingested.revision_id,),
        ).fetchone()[0]
        == "processing"
    )

    result = EnrichmentResult(
        summary="Hippo uses SQLite WAL with a five-second busy timeout.",
        intent="document database operation",
        outcome="success",
        tags=["sqlite", "operations"],
        embed_text="Hippo SQLite WAL busy timeout database operation",
    )
    node_id = write_memory_knowledge_node(
        conn, result, ingested.revision_id, "mock-model", now_ms=3000
    )

    node = conn.execute(
        "SELECT content, embed_text, enrichment_model FROM knowledge_nodes WHERE id = ?",
        (node_id,),
    ).fetchone()
    assert "Hippo uses SQLite WAL" in node[0]
    assert node[1] == result.embed_text
    assert node[2] == "mock-model"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM knowledge_node_memory_chunks WHERE knowledge_node_id = ?",
            (node_id,),
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT status FROM memory_enrichment_queue WHERE revision_id = ?",
            (ingested.revision_id,),
        ).fetchone()[0]
        == "done"
    )
    document = conn.execute(
        "SELECT active_revision_id, projection_status FROM memory_documents"
    ).fetchone()
    assert document == (ingested.revision_id, "ready")

    results = search_knowledge_lexical(
        conn,
        "busy timeout",
        source="claude-auto-memory",
        project="hippo",
    )
    assert len(results) == 1
    assert results[0]["source"] == "claude-auto-memory"
    assert results[0]["source_path"] == str(source.resolve())
    assert results[0]["repository"] == "hippo"
    assert results[0]["content_hash"]


def test_operator_command_ingests_synthetic_repository_and_rerun_is_noop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "hippo.db"
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    db.execute("PRAGMA user_version = 19")
    db.commit()
    db.close()
    source = tmp_path / "repo" / "MEMORY.md"
    source.parent.mkdir()
    source.write_text("# Build\n\nRun `mise run test`.\n")
    args = [
        "--db",
        str(db_path),
        "--file",
        str(source),
        "--repository",
        "synthetic/hippo",
    ]

    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["changed"] is True
    assert main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["changed"] is False
    assert second["document_uuid"] == first["document_uuid"]

    db = sqlite3.connect(db_path)
    try:
        assert db.execute("SELECT COUNT(*) FROM memory_documents").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_enrichment_queue").fetchone()[0] == 1
    finally:
        db.close()


async def test_brain_completes_memory_through_local_chat_and_sqlite_vec(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "hippo.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA user_version = 19")
    source = tmp_path / "MEMORY.md"
    source.write_text("# SQLite\n\nUse WAL.\n")
    ingest_memory_file(conn, source, repository="synthetic/hippo", now_ms=1000)
    claims = claim_pending_memories(conn, worker_id="test", now_ms=2000)
    conn.close()

    server = BrainServer(
        db_path=str(db_path),
        data_dir=str(tmp_path),
        enrichment_model="mock-model",
        embedding_model="text-embedding-mock",
    )
    server.client = MockInferenceClient()
    try:
        await server._enrich_memory_claims(claims)
        conn = sqlite3.connect(db_path)
        try:
            assert (
                conn.execute("SELECT status FROM memory_enrichment_queue").fetchone()[0] == "done"
            )
            assert conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0] == 1
            assert server._vector_db is not None
            assert (
                server._vector_db.execute("SELECT COUNT(*) FROM knowledge_vectors").fetchone()[0]
                == 1
            )
        finally:
            conn.close()
    finally:
        if server._vector_db is not None:
            server._vector_db.close()


def test_supersede_cleans_up_old_node_and_swaps_active_revision(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    source = tmp_path / "MEMORY.md"
    source.write_text("# V1\n\nFirst version.\n")
    first = ingest_memory_file(conn, source, repository="hippo", now_ms=1000)
    assert first.changed is True and first.revision_number == 1

    result_v1 = EnrichmentResult(
        summary="First version.",
        intent="document initial state",
        outcome="success",
        tags=["v1"],
        embed_text="version one content",
    )
    first_node = write_memory_knowledge_node(
        conn, result_v1, first.revision_id, "mock-model", now_ms=2000
    )

    source.write_text("# V2\n\nSecond version.\n")
    second = ingest_memory_file(conn, source, repository="hippo", now_ms=3000)
    assert second.changed is True and second.revision_number == 2

    assert first.revision_id != second.revision_id
    assert first.document_uuid == second.document_uuid

    result_v2 = EnrichmentResult(
        summary="Second version.",
        intent="document updated state",
        outcome="success",
        tags=["v2"],
        embed_text="version two content",
    )
    second_node = write_memory_knowledge_node(
        conn, result_v2, second.revision_id, "mock-model", now_ms=4000
    )

    assert first_node != second_node

    document = conn.execute(
        "SELECT current_revision_id, active_revision_id FROM memory_documents"
    ).fetchone()
    assert document == (second.revision_id, second.revision_id)

    old_node_exists = conn.execute(
        "SELECT COUNT(*) FROM knowledge_nodes WHERE id = ?", (first_node,)
    ).fetchone()[0]
    assert old_node_exists == 0, "old knowledge node should be deleted"

    old_links = conn.execute(
        "SELECT COUNT(*) FROM knowledge_node_memory_chunks WHERE knowledge_node_id = ?",
        (first_node,),
    ).fetchone()[0]
    assert old_links == 0, "old knowledge_node_memory_chunks should be deleted"

    new_node = conn.execute(
        "SELECT content FROM knowledge_nodes WHERE id = ?", (second_node,)
    ).fetchone()
    assert new_node is not None
    assert "Second version" in new_node[0]

    new_links = conn.execute(
        "SELECT COUNT(*) FROM knowledge_node_memory_chunks WHERE knowledge_node_id = ?",
        (second_node,),
    ).fetchone()[0]
    assert new_links == 1

    v1_search = search_knowledge_lexical(
        conn, "First", source="claude-auto-memory", project="hippo"
    )
    assert len(v1_search) == 0, "old revision content should not be searchable"

    v2_search = search_knowledge_lexical(
        conn, "Second", source="claude-auto-memory", project="hippo"
    )
    assert len(v2_search) == 1
    assert v2_search[0]["outcome"] == "success"


def test_idempotent_write_same_revision_reuses_node(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    source = tmp_path / "MEMORY.md"
    source.write_text("# Build\n\nCargo.\n")
    ingested = ingest_memory_file(conn, source, repository="hippo", now_ms=1000)

    result = EnrichmentResult(
        summary="Cargo build.",
        intent="document",
        outcome="success",
        tags=["build"],
        embed_text="cargo build",
    )
    first_node = write_memory_knowledge_node(
        conn, result, ingested.revision_id, "mock-model", now_ms=2000
    )
    second_node = write_memory_knowledge_node(
        conn, result, ingested.revision_id, "mock-model", now_ms=3000
    )

    assert first_node == second_node
    assert conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0] == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM knowledge_node_memory_chunks WHERE knowledge_node_id = ?",
            (first_node,),
        ).fetchone()[0]
        == 1
    )


def test_enrichment_failure_keeps_old_projection_while_retry_pending(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    source = tmp_path / "MEMORY.md"
    source.write_text("# Stable\n\nReliable content.\n")
    first = ingest_memory_file(conn, source, repository="hippo", now_ms=1000)
    assert first.revision_number == 1

    result_v1 = EnrichmentResult(
        summary="Stable.",
        intent="document",
        outcome="success",
        tags=["stable"],
        embed_text="stable content",
    )
    write_memory_knowledge_node(conn, result_v1, first.revision_id, "mock-model", now_ms=2000)

    source.write_text("# Updated\n\nNew content after a bad enrichment.\n")
    second = ingest_memory_file(conn, source, repository="hippo", now_ms=3000)
    assert second.revision_number == 2

    mark_memory_enrichment_failed(
        conn, second.revision_id, "simulated enrichment failure", now_ms=4000
    )

    document = conn.execute(
        "SELECT active_revision_id, projection_status, current_revision_id FROM memory_documents"
    ).fetchone()
    assert document[0] == first.revision_id
    assert document[1] == "stale"
    assert document[2] == second.revision_id

    results = search_knowledge_lexical(conn, "stable", source="claude-auto-memory", project="hippo")
    assert len(results) == 1
    assert results[0]["outcome"] == "success"

    retry_status = conn.execute(
        "SELECT status, retry_count FROM memory_enrichment_queue WHERE revision_id = ?",
        (second.revision_id,),
    ).fetchone()
    assert retry_status[0] == "pending"
    assert retry_status[1] == 1
