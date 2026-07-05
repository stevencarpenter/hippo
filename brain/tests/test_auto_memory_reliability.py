"""Reliability and operations tests for Claude auto-memory (SNUG-138)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hippo_brain.auto_memory import (
    claim_pending_memories,
    ingest_memory_file,
    mark_memory_enrichment_failed,
    write_memory_knowledge_node,
)
from hippo_brain.auto_memory_constants import PROBE_REPOSITORY
from hippo_brain.auto_memory_health import (
    bump_watcher_heartbeat,
    record_reconcile_failure,
    replay_failed_enrichments,
    snapshot_health,
)
from hippo_brain.auto_memory_probe import probe_fixture_dir, run_probe
from hippo_brain.auto_memory_reconcile import reconcile_sources
from hippo_brain.memory_query import MemoryQueryRequest, query_memory_current
from hippo_brain.models import EnrichmentResult
from hippo_brain.mcp_queries import search_knowledge_lexical


@pytest.fixture
def conn(tmp_db):
    connection, _path = tmp_db
    yield connection


def test_probe_fixture_ingests_and_excludes_from_memory_query(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    fixture_root = probe_fixture_dir(tmp_path)
    result = run_probe(conn, fixture_root=fixture_root)
    assert result["ok"] is True
    assert result.get("lag_ms") is not None

    current = query_memory_current(conn, MemoryQueryRequest(query="Synthetic"))
    assert all(r["repository"] != PROBE_REPOSITORY for r in current["results"])

    rows = conn.execute(
        "SELECT COUNT(*) FROM memory_documents WHERE repository = ?",
        (PROBE_REPOSITORY,),
    ).fetchone()[0]
    assert rows == 1


def test_probe_does_not_bump_ingest_health_or_enqueue(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    before = conn.execute(
        "SELECT last_event_ts, consecutive_failures FROM source_health WHERE source = 'claude-auto-memory'"
    ).fetchone()
    fixture_root = probe_fixture_dir(tmp_path)
    run_probe(conn, fixture_root=fixture_root)
    after = conn.execute(
        "SELECT last_event_ts, consecutive_failures FROM source_health WHERE source = 'claude-auto-memory'"
    ).fetchone()
    assert after == before
    probe_queue = conn.execute(
        """
        SELECT COUNT(*) FROM memory_enrichment_queue meq
        JOIN memory_revisions mr ON mr.id = meq.revision_id
        JOIN memory_documents md ON md.id = mr.document_id
        WHERE md.repository = ?
        """,
        (PROBE_REPOSITORY,),
    ).fetchone()[0]
    assert probe_queue == 0


def test_probe_rows_excluded_from_lexical_search(conn: sqlite3.Connection, tmp_path: Path) -> None:
    fixture_root = probe_fixture_dir(tmp_path)
    run_probe(conn, fixture_root=fixture_root)
    probe_rev = conn.execute(
        "SELECT current_revision_id FROM memory_documents WHERE repository = ?",
        (PROBE_REPOSITORY,),
    ).fetchone()[0]
    write_memory_knowledge_node(
        conn,
        EnrichmentResult(
            summary="Synthetic probe",
            intent="probe",
            outcome="success",
            tags=["probe"],
            embed_text="Synthetic auto-memory probe content",
        ),
        int(probe_rev),
        "mock-model",
        now_ms=3000,
    )
    conn.commit()
    assert search_knowledge_lexical(conn, "Synthetic", source="claude-auto-memory") == []
    assert search_knowledge_lexical(conn, "Synthetic auto-memory", source="claude-auto-memory") == []


def test_health_snapshot_separates_watcher_queue_and_projection(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    bump_watcher_heartbeat(conn, now_ms=1000)
    source = tmp_path / "MEMORY.md"
    source.write_text("# Health\n\nsnapshot dimensions.\n")
    ingest_memory_file(conn, source, repository="hippo", now_ms=2000)
    snap = snapshot_health(conn, now_ms=3000)
    assert snap.watcher_last_heartbeat_ms == 1000
    assert snap.ingest_last_event_ms is not None
    assert snap.pending_enrichment >= 1
    assert snap.failed_enrichment == 0


def test_reconcile_failure_increments_ingest_health(
    conn: sqlite3.Connection,
) -> None:
    record_reconcile_failure(conn, "simulated reconcile error", now_ms=5000)
    row = conn.execute(
        "SELECT consecutive_failures, last_error_msg FROM source_health WHERE source = 'claude-auto-memory'"
    ).fetchone()
    assert row[0] == 1
    assert "simulated" in row[1]


def test_replay_resets_failed_enrichment_rows(conn: sqlite3.Connection, tmp_path: Path) -> None:
    source = tmp_path / "MEMORY.md"
    source.write_text("# Replay\n\nfailed then replayed.\n")
    ingested = ingest_memory_file(conn, source, repository="hippo", now_ms=1000)
    mark_memory_enrichment_failed(conn, ingested.revision_id, "boom", now_ms=2000)
    conn.execute(
        "UPDATE memory_enrichment_queue SET status = 'failed' WHERE revision_id = ?",
        (ingested.revision_id,),
    )
    conn.commit()
    replayed = replay_failed_enrichments(conn, limit=10)
    assert replayed == 1
    status = conn.execute(
        "SELECT status, retry_count FROM memory_enrichment_queue WHERE revision_id = ?",
        (ingested.revision_id,),
    ).fetchone()
    assert status == ("pending", 0)


def test_reconcile_bumps_watcher_and_returns_health_summary(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    source = tmp_path / "MEMORY.md"
    source.write_text("# Reconcile\n\nhealth summary.\n")
    summary = reconcile_sources(
        conn,
        [{"path": str(source), "repository": "hippo"}],
        now_ms=4000,
        require_stable=False,
    )
    assert "health" in summary
    assert summary["health"]["watcher_last_heartbeat_ms"] == 4000


def test_crash_recovery_requeues_stale_processing_revision(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    source = tmp_path / "MEMORY.md"
    source.write_text("# Crash\n\nmid-enrichment interrupt.\n")
    ingested = ingest_memory_file(conn, source, repository="hippo", now_ms=1000)
    claims = claim_pending_memories(conn, worker_id="worker-a", limit=1, now_ms=2000)
    assert len(claims) == 1
    reclaimed = claim_pending_memories(
        conn,
        worker_id="worker-b",
        limit=1,
        now_ms=2000 + 600_000 + 1,
        stale_lock_timeout_ms=600_000,
    )
    assert len(reclaimed) == 1
    assert reclaimed[0].revision_id == ingested.revision_id
