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


@pytest.fixture
def conn(tmp_db):
    connection, _path = tmp_db
    yield connection


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
    assert claims[0].revision_id == ingested.revision_id
    assert claims[0].repository == "hippo"
    assert claims[0].source_path == str(source.resolve())
    assert claims[0].chunks[0]["heading_path"] == "Database"
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
    tmp_db, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _conn, db_path = tmp_db
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
    tmp_db,
    tmp_path: Path,
) -> None:
    conn, db_path = tmp_db
    source = tmp_path / "MEMORY.md"
    source.write_text("# SQLite\n\nUse WAL.\n")
    ingest_memory_file(conn, source, repository="synthetic/hippo", now_ms=1000)
    claims = claim_pending_memories(conn, worker_id="test", now_ms=2000)

    server = BrainServer(
        db_path=str(db_path),
        data_dir=str(tmp_path),
        enrichment_model="mock-model",
        embedding_model="text-embedding-mock",
    )
    server.client = MockInferenceClient()
    try:
        await server._enrich_memory_claims(claims)
        verify = sqlite3.connect(db_path)
        try:
            assert (
                verify.execute("SELECT status FROM memory_enrichment_queue").fetchone()[0] == "done"
            )
            assert verify.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0] == 1
            assert server._vector_db is not None
            assert (
                server._vector_db.execute("SELECT COUNT(*) FROM knowledge_vectors").fetchone()[0]
                == 1
            )
        finally:
            verify.close()
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
