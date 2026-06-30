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


def test_supersede_cleans_up_old_node_and_swaps_active_revision(tmp_db, tmp_path: Path) -> None:
    # Runs on a sqlite-vec connection: supersede deletes the old node's vec0 vector
    # and write_memory_knowledge_node refuses to delete a node whose vector it
    # cannot clear (no silent orphan), so the connection must have vec loaded.
    from hippo_brain import vector_store

    _seeded, db_path = tmp_db
    conn = vector_store.open_conn(db_path)
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

    conn.close()


def test_claim_reclaims_stale_processing_revision(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """A revision orphaned in 'processing' by a crashed worker must be reclaimable.

    Without stale-lock reclaim, a brain crash between claim and completion strands
    the revision in 'processing' forever — never enriched, never retried.
    """
    source = tmp_path / "MEMORY.md"
    source.write_text("# A\n\nContent.\n")
    ingested = ingest_memory_file(conn, source, repository="hippo", now_ms=1000)

    # Worker 1 claims it, then "crashes" (row stuck in processing at t=2000).
    first = claim_pending_memories(conn, worker_id="w1", now_ms=2000)
    assert len(first) == 1
    assert (
        conn.execute(
            "SELECT status FROM memory_enrichment_queue WHERE revision_id = ?",
            (ingested.revision_id,),
        ).fetchone()[0]
        == "processing"
    )

    # A pending-only claim must NOT steal a fresh lock.
    assert claim_pending_memories(conn, worker_id="w2", now_ms=2500) == []

    # Once the lock is stale, a worker passing stale_lock_timeout_ms reclaims it.
    reclaimed = claim_pending_memories(
        conn, worker_id="w2", now_ms=2000 + 600_000 + 1, stale_lock_timeout_ms=600_000
    )
    assert len(reclaimed) == 1
    assert reclaimed[0].revision_id == ingested.revision_id
    row = conn.execute(
        "SELECT status, locked_by FROM memory_enrichment_queue WHERE revision_id = ?",
        (ingested.revision_id,),
    ).fetchone()
    assert row[0] == "processing"
    assert row[1] == "w2"


def test_out_of_order_revision_completion_does_not_revert_current_projection(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A superseded revision that finishes enriching late must not clobber the current node.

    Reproduces the race: rev1 and rev2 are both queued (file edited before rev1
    enriched); rev2 completes first and becomes the active projection; rev1 then
    completes out of order. The stale rev1 result must be discarded, not promoted
    over rev2's current node.
    """
    source = tmp_path / "MEMORY.md"
    source.write_text("# V1\n\nFirst version.\n")
    first = ingest_memory_file(conn, source, repository="hippo", now_ms=1000)

    source.write_text("# V2\n\nSecond version.\n")
    second = ingest_memory_file(conn, source, repository="hippo", now_ms=2000)
    assert first.revision_id != second.revision_id

    result_v2 = EnrichmentResult(
        summary="Second version.",
        intent="document updated state",
        outcome="success",
        tags=["v2"],
        embed_text="version two content",
    )
    second_node = write_memory_knowledge_node(
        conn, result_v2, second.revision_id, "mock-model", now_ms=3000
    )

    # rev1 (superseded) finishes enriching AFTER rev2 already published.
    result_v1 = EnrichmentResult(
        summary="First version.",
        intent="document initial state",
        outcome="success",
        tags=["v1"],
        embed_text="version one content",
    )
    stale_node = write_memory_knowledge_node(
        conn, result_v1, first.revision_id, "mock-model", now_ms=4000
    )

    # Superseded revision must not produce a competing projection node.
    assert stale_node is None, "stale revision should not publish a node"

    document = conn.execute(
        "SELECT current_revision_id, active_revision_id FROM memory_documents"
    ).fetchone()
    assert document == (second.revision_id, second.revision_id), (
        "active projection must stay on the current revision, not revert to the stale one"
    )

    assert (
        conn.execute(
            "SELECT COUNT(*) FROM knowledge_nodes WHERE id = ?", (second_node,)
        ).fetchone()[0]
        == 1
    ), "current node must survive a late stale-revision completion"

    # No orphan node for the stale revision.
    stale_links = conn.execute(
        "SELECT COUNT(*) FROM knowledge_node_memory_chunks knmc "
        "JOIN memory_chunks mc ON mc.id = knmc.memory_chunk_id "
        "WHERE mc.revision_id = ?",
        (first.revision_id,),
    ).fetchone()[0]
    assert stale_links == 0, "stale revision must not leave linked knowledge-node rows"

    # The stale revision's queue row is retired (not left pending / re-claimable).
    assert (
        conn.execute(
            "SELECT status FROM memory_enrichment_queue WHERE revision_id = ?",
            (first.revision_id,),
        ).fetchone()[0]
        == "done"
    )

    v2_search = search_knowledge_lexical(
        conn, "Second", source="claude-auto-memory", project="hippo"
    )
    assert len(v2_search) == 1


def test_supersede_deletes_old_node_vector(tmp_db, tmp_path: Path) -> None:
    """Superseding an old revision must delete its vec0 vector, not just the node.

    vec0 tables cannot FK-cascade and the embed reaper only heals
    nodes-missing-vectors, so a node deleted without its vector leaks an orphan
    vector forever. Runs on a sqlite-vec-loaded connection because the vec0
    virtual table only exists when the extension is loaded.
    """
    import sqlite_vec

    from hippo_brain import vector_store

    _conn, db_path = tmp_db
    vconn = vector_store.open_conn(db_path)
    try:
        source = tmp_path / "MEMORY.md"
        source.write_text("# V1\n\nFirst version.\n")
        first = ingest_memory_file(vconn, source, repository="hippo", now_ms=1000)
        result_v1 = EnrichmentResult(
            summary="First version.",
            intent="document",
            outcome="success",
            tags=["v1"],
            embed_text="version one",
        )
        first_node = write_memory_knowledge_node(
            vconn, result_v1, first.revision_id, "mock-model", now_ms=2000
        )

        # Simulate the background embed step having written this node's vector.
        vec = sqlite_vec.serialize_float32([0.1] * 768)
        vconn.execute(
            "INSERT INTO knowledge_vectors (knowledge_node_id, vec_knowledge, vec_command) "
            "VALUES (?, ?, ?)",
            (first_node, vec, vec),
        )
        vconn.commit()
        assert (
            vconn.execute(
                "SELECT COUNT(*) FROM knowledge_vectors WHERE knowledge_node_id = ?",
                (first_node,),
            ).fetchone()[0]
            == 1
        )

        source.write_text("# V2\n\nSecond version.\n")
        second = ingest_memory_file(vconn, source, repository="hippo", now_ms=3000)
        result_v2 = EnrichmentResult(
            summary="Second version.",
            intent="document",
            outcome="success",
            tags=["v2"],
            embed_text="version two",
        )
        write_memory_knowledge_node(vconn, result_v2, second.revision_id, "mock-model", now_ms=4000)

        assert (
            vconn.execute(
                "SELECT COUNT(*) FROM knowledge_vectors WHERE knowledge_node_id = ?",
                (first_node,),
            ).fetchone()[0]
            == 0
        ), "superseded node's vec0 vector must be deleted, not orphaned"
    finally:
        vconn.close()


def test_unchanged_file_skips_read_redact_and_git_identity(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll of an unchanged file must short-circuit before the expensive work.

    The current revision stores mtime+size; when both still match the file on
    disk, ingest must skip Git identity derivation, the full read, and the regex
    redact — proven here by sabotaging those and requiring they are never called.
    """
    import hippo_brain.auto_memory as am

    source = tmp_path / "MEMORY.md"
    source.write_text("# A\n\nOriginal content.\n")
    first = ingest_memory_file(conn, source, repository="hippo", now_ms=1000)
    assert first.changed is True

    def _should_not_run(*_args: object, **_kwargs: object):
        raise AssertionError("expensive re-ingest work must be skipped for an unchanged file")

    monkeypatch.setattr(am, "derive_repository_identity", _should_not_run)
    monkeypatch.setattr(am, "redact", _should_not_run)

    second = ingest_memory_file(conn, source, repository="hippo", now_ms=2000)

    assert second.changed is False
    assert second.revision_id == first.revision_id
    assert second.document_uuid == first.document_uuid


def test_poll_from_config_rejects_schema_version_skew(tmp_path: Path) -> None:
    """The recurring poller must fail with a clear schema-skew message on a pre-v19 DB.

    Without the guard the poller hits an opaque ``no such table: memory_documents``
    sqlite error every launchd tick during daemon/brain version skew.
    """
    from hippo_brain.auto_memory import poll_from_config

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "hippo.db"
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA user_version = 18")
    db.commit()
    db.close()

    source = tmp_path / "MEMORY.md"
    source.write_text("# X\n\nContent.\n")
    config = tmp_path / "config.toml"
    config.write_text(
        f'[storage]\ndata_dir = "{data_dir}"\n\n'
        "[auto_memory]\nenabled = true\n\n"
        f'[[auto_memory.sources]]\npath = "{source}"\nrepository = "hippo"\n'
    )

    with pytest.raises(RuntimeError, match="schema version"):
        poll_from_config(config)


def test_supersede_refuses_to_orphan_vector_on_vecless_connection(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """On a vec-less connection, superseding must refuse rather than delete the old
    node while silently skipping its (unreachable) vector — which would orphan it."""
    source = tmp_path / "MEMORY.md"
    source.write_text("# V1\n\nFirst.\n")
    first = ingest_memory_file(conn, source, repository="hippo", now_ms=1000)
    result_v1 = EnrichmentResult(
        summary="First.", intent="i", outcome="success", tags=["v1"], embed_text="one"
    )
    write_memory_knowledge_node(conn, result_v1, first.revision_id, "mock-model", now_ms=2000)

    source.write_text("# V2\n\nSecond.\n")
    second = ingest_memory_file(conn, source, repository="hippo", now_ms=3000)
    result_v2 = EnrichmentResult(
        summary="Second.", intent="i", outcome="success", tags=["v2"], embed_text="two"
    )
    with pytest.raises(RuntimeError, match="vec0 knowledge_vectors not reachable"):
        write_memory_knowledge_node(conn, result_v2, second.revision_id, "mock-model", now_ms=4000)


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


def test_idempotent_rewrite_resolves_node_by_uuid_not_stale_lastrowid(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """An ignored INSERT OR IGNORE must resolve the node id by uuid, not lastrowid.

    After an INSERT OR IGNORE that is *ignored*, SQLite leaves last_insert_rowid()
    pointing at the previous successful insert on the connection — a stale,
    cross-table rowid. We force that rowid away from the target node by writing a
    second document's node first (advancing the connection's last rowid), then
    re-write the first revision. Resolving via cursor.lastrowid would return the
    second node's link rowid; resolving via uuid returns the correct node.
    """
    result = EnrichmentResult(
        summary="x",
        intent="document",
        outcome="success",
        tags=["x"],
        embed_text="x",
    )

    src_a = tmp_path / "A.md"
    src_a.write_text("# A\n\nAlpha.\n")
    ing_a = ingest_memory_file(conn, src_a, repository="hippo", now_ms=1000)
    node_a = write_memory_knowledge_node(conn, result, ing_a.revision_id, "mock-model", now_ms=2000)

    # A second document + node advances the connection's last_insert_rowid past node_a,
    # so a stale-lastrowid read would no longer coincidentally equal node_a's id.
    src_b = tmp_path / "B.md"
    src_b.write_text("# B\n\nBeta.\n")
    ing_b = ingest_memory_file(conn, src_b, repository="hippo", now_ms=3000)
    node_b = write_memory_knowledge_node(conn, result, ing_b.revision_id, "mock-model", now_ms=4000)
    assert node_b != node_a

    # Idempotent re-write of revision A: the INSERT OR IGNORE is ignored, so the id
    # must come from the uuid lookup, not the (now-advanced) lastrowid.
    again = write_memory_knowledge_node(conn, result, ing_a.revision_id, "mock-model", now_ms=5000)
    assert again == node_a


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
