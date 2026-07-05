"""Auto-memory capture health: watcher, ingest, queue, and projection signals (SNUG-138)."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from hippo_brain.auto_memory_constants import PROBE_REPOSITORY, SOURCE_KIND, WATCHER_SOURCE
from hippo_brain.source_filters import table_exists

WATCHER_STALE_MS = 15 * 60 * 1000
PROJECTION_STALE_MS = 30 * 60 * 1000
RECONCILE_FAILURE_THRESHOLD = 3


@dataclass(frozen=True)
class AutoMemoryHealthSnapshot:
    watcher_last_heartbeat_ms: int | None
    ingest_last_event_ms: int | None
    ingest_consecutive_failures: int
    pending_enrichment: int
    failed_enrichment: int
    stale_projection_count: int
    orphan_chunk_links: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "watcher_last_heartbeat_ms": self.watcher_last_heartbeat_ms,
            "ingest_last_event_ms": self.ingest_last_event_ms,
            "ingest_consecutive_failures": self.ingest_consecutive_failures,
            "pending_enrichment": self.pending_enrichment,
            "failed_enrichment": self.failed_enrichment,
            "stale_projection_count": self.stale_projection_count,
            "orphan_chunk_links": self.orphan_chunk_links,
        }


def _queue_counts(conn: sqlite3.Connection) -> tuple[int, int]:
    if not table_exists(conn, "memory_enrichment_queue"):
        return 0, 0
    pending = conn.execute(
        """
        SELECT COUNT(*) FROM memory_enrichment_queue meq
        JOIN memory_revisions mr ON mr.id = meq.revision_id
        JOIN memory_documents md ON md.id = mr.document_id
        WHERE meq.status = 'pending' AND md.repository != ?
        """,
        (PROBE_REPOSITORY,),
    ).fetchone()[0]
    failed = conn.execute(
        """
        SELECT COUNT(*) FROM memory_enrichment_queue meq
        JOIN memory_revisions mr ON mr.id = meq.revision_id
        JOIN memory_documents md ON md.id = mr.document_id
        WHERE meq.status = 'failed' AND md.repository != ?
        """,
        (PROBE_REPOSITORY,),
    ).fetchone()[0]
    return int(pending or 0), int(failed or 0)


def _stale_projection_count(conn: sqlite3.Connection, *, now_ms: int) -> int:
    if not table_exists(conn, "memory_documents"):
        return 0
    cutoff = now_ms - PROJECTION_STALE_MS
    row = conn.execute(
        """
        SELECT COUNT(*) FROM memory_documents
        WHERE state = 'active'
          AND projection_status = 'pending'
          AND repository != ?
          AND updated_at < ?
        """,
        (PROBE_REPOSITORY, cutoff),
    ).fetchone()
    return int(row[0] or 0)


def _orphan_chunk_links(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "knowledge_node_memory_chunks"):
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*) FROM memory_chunks mc
        LEFT JOIN knowledge_node_memory_chunks knmc ON knmc.memory_chunk_id = mc.id
        JOIN memory_revisions mr ON mr.id = mc.revision_id
        JOIN memory_documents md ON md.id = mr.document_id
        WHERE md.active_revision_id = mr.id
          AND md.state = 'active'
          AND md.repository != ?
          AND knmc.knowledge_node_id IS NULL
        """,
        (PROBE_REPOSITORY,),
    ).fetchone()
    return int(row[0] or 0)


def _health_row(conn: sqlite3.Connection, source: str) -> dict[str, Any] | None:
    if not table_exists(conn, "source_health"):
        return None
    row = conn.execute(
        """
        SELECT last_event_ts, last_success_ts, last_error_msg, consecutive_failures,
               last_heartbeat_ts, updated_at
        FROM source_health WHERE source = ?
        """,
        (source,),
    ).fetchone()
    if row is None:
        return None
    keys = (
        "last_event_ts",
        "last_success_ts",
        "last_error_msg",
        "consecutive_failures",
        "last_heartbeat_ts",
        "updated_at",
    )
    return dict(zip(keys, row, strict=True))


def snapshot_health(conn: sqlite3.Connection, *, now_ms: int | None = None) -> AutoMemoryHealthSnapshot:
    """Read auto-memory health dimensions from SQLite (no brain HTTP)."""
    now_ms = now_ms or int(time.time() * 1000)
    watcher = _health_row(conn, WATCHER_SOURCE) or {}
    ingest = _health_row(conn, SOURCE_KIND) or {}
    pending, failed = _queue_counts(conn)
    return AutoMemoryHealthSnapshot(
        watcher_last_heartbeat_ms=watcher.get("last_heartbeat_ts"),
        ingest_last_event_ms=ingest.get("last_event_ts"),
        ingest_consecutive_failures=int(ingest.get("consecutive_failures") or 0),
        pending_enrichment=pending,
        failed_enrichment=failed,
        stale_projection_count=_stale_projection_count(conn, now_ms=now_ms),
        orphan_chunk_links=_orphan_chunk_links(conn),
    )


def bump_watcher_heartbeat(conn: sqlite3.Connection, *, now_ms: int | None = None) -> None:
    now_ms = now_ms or int(time.time() * 1000)
    conn.execute(
        """
        INSERT OR IGNORE INTO source_health (source, updated_at)
        VALUES (?, ?)
        """,
        (WATCHER_SOURCE, now_ms),
    )
    conn.execute(
        """
        UPDATE source_health
        SET last_heartbeat_ts = ?, last_success_ts = ?, consecutive_failures = 0,
            last_error_msg = NULL, last_error_ts = NULL, updated_at = ?
        WHERE source = ?
        """,
        (now_ms, now_ms, now_ms, WATCHER_SOURCE),
    )


def record_reconcile_failure(
    conn: sqlite3.Connection,
    message: str,
    *,
    now_ms: int | None = None,
) -> None:
    now_ms = now_ms or int(time.time() * 1000)
    conn.execute(
        """
        INSERT OR IGNORE INTO source_health (source, updated_at)
        VALUES (?, ?)
        """,
        (SOURCE_KIND, now_ms),
    )
    conn.execute(
        """
        UPDATE source_health
        SET consecutive_failures = consecutive_failures + 1,
            last_error_msg = ?, last_error_ts = ?, updated_at = ?
        WHERE source = ?
        """,
        (message[:500], now_ms, now_ms, SOURCE_KIND),
    )


def replay_failed_enrichments(conn: sqlite3.Connection, *, limit: int = 50) -> int:
    """Operator-safe replay: reset failed queue rows to pending with bounded retries."""
    if not table_exists(conn, "memory_enrichment_queue"):
        return 0
    now_ms = int(time.time() * 1000)
    cursor = conn.execute(
        """
        UPDATE memory_enrichment_queue
        SET status = 'pending', retry_count = 0, updated_at = ?
        WHERE id IN (
            SELECT meq.id FROM memory_enrichment_queue meq
            JOIN memory_revisions mr ON mr.id = meq.revision_id
            JOIN memory_documents md ON md.id = mr.document_id
            WHERE meq.status = 'failed' AND md.repository != ?
            LIMIT ?
        )
        """,
        (now_ms, PROBE_REPOSITORY, max(limit, 1)),
    )
    return int(cursor.rowcount or 0)
