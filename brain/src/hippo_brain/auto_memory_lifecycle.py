"""Revision lifecycle, history, and deletion handling for Claude auto-memory."""

from __future__ import annotations

import difflib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hippo_brain.auto_memory_constants import (
    CHUNKER_NAME,
    CHUNKER_VERSION,
    DEFAULT_ABSENCE_CONFIRM_POLLS,
    DEFAULT_MAX_REVISION_AGE_DAYS,
    DEFAULT_MAX_REVISION_COUNT,
    _MAX_DIFF_CHARS,
)
from hippo_brain.vector_store import vec_table_available


@dataclass(frozen=True)
class RevisionRetention:
    max_count: int = DEFAULT_MAX_REVISION_COUNT
    max_age_ms: int = DEFAULT_MAX_REVISION_AGE_DAYS * 86_400_000
    absence_confirm_polls: int = DEFAULT_ABSENCE_CONFIRM_POLLS


def revision_retention_from_config(auto_memory: dict[str, Any]) -> RevisionRetention:
    """Parse revision retention settings from a config ``[auto_memory]`` table."""
    max_count = int(auto_memory.get("max_revision_count", DEFAULT_MAX_REVISION_COUNT))
    max_age_days = int(auto_memory.get("max_revision_age_days", DEFAULT_MAX_REVISION_AGE_DAYS))
    absence_polls = int(
        auto_memory.get("absence_confirm_polls", DEFAULT_ABSENCE_CONFIRM_POLLS)
    )
    return RevisionRetention(
        max_count=max(max_count, 1),
        max_age_ms=max(max_age_days, 1) * 86_400_000,
        absence_confirm_polls=max(absence_polls, 1),
    )


def _summarize_diff(old_redacted: str, new_redacted: str) -> tuple[str, str]:
    old_lines = old_redacted.splitlines(keepends=True)
    new_lines = new_redacted.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile="previous",
            tofile="current",
            lineterm="",
        )
    )
    diff_text = "".join(diff_lines)
    if len(diff_text) > _MAX_DIFF_CHARS:
        diff_text = diff_text[:_MAX_DIFF_CHARS] + "\n… (diff truncated)\n"
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    summary = f"{added} line(s) added, {removed} line(s) removed"
    return summary, diff_text


def finalize_superseded_revision(
    conn: sqlite3.Connection,
    revision_id: int,
    *,
    old_redacted: str,
    new_redacted: str,
) -> None:
    """Move superseded revision content into bounded summary/diff metadata."""
    summary, diff_text = _summarize_diff(old_redacted, new_redacted)
    conn.execute(
        "UPDATE memory_revisions SET summary = ?, diff_text = ?, redacted_content = NULL "
        "WHERE id = ?",
        (summary, diff_text, revision_id),
    )


def try_resolve_rename(
    conn: sqlite3.Connection,
    *,
    repository: str,
    logical_path: str,
    content_hash: str,
    source_path: str,
    observed_at: int,
) -> int | None:
    """Return an existing document id when an unambiguous rename is detected."""
    rows = conn.execute(
        "SELECT d.id, d.logical_path, d.source_path "
        "FROM memory_documents d "
        "JOIN memory_revisions r ON r.id = d.current_revision_id "
        "WHERE d.repository = ? AND d.logical_path != ? AND r.content_hash = ? "
        "AND d.state IN ('active', 'unavailable')",
        (repository, logical_path, content_hash),
    ).fetchall()
    if len(rows) != 1:
        return None
    document_id, old_logical_path, old_source_path = int(rows[0][0]), rows[0][1], rows[0][2]
    if Path(old_source_path).is_file():
        return None
    with conn:
        conn.execute(
            "UPDATE memory_documents SET logical_path = ?, source_path = ?, state = 'active', "
            "observed_at = ?, updated_at = ?, tombstoned_at = NULL WHERE id = ?",
            (logical_path, source_path, observed_at, observed_at, document_id),
        )
        revision_number = int(
            conn.execute(
                "SELECT COALESCE(MAX(revision_number), 0) + 1 FROM memory_revisions "
                "WHERE document_id = ?",
                (document_id,),
            ).fetchone()[0]
        )
        conn.execute(
            "INSERT INTO memory_revisions "
            "(document_id, revision_number, content_hash, source_hash, redacted_content, "
            " source_mtime_ms, source_size, change_kind, summary, chunker_name, "
            " chunker_version, chunker_config_json, enrichment_version, created_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, 'rename', ?, ?, ?, ?, 1, ?)",
            (
                document_id,
                revision_number,
                content_hash,
                content_hash,
                observed_at,
                0,
                f"Renamed from {old_logical_path!r} to {logical_path!r}",
                CHUNKER_NAME,
                CHUNKER_VERSION,
                json.dumps({"boundary": "heading", "retain_heading": True}, sort_keys=True),
                observed_at,
            ),
        )
    return document_id


def prune_document_revisions(
    conn: sqlite3.Connection,
    document_id: int,
    retention: RevisionRetention,
    *,
    now_ms: int,
) -> int:
    """Delete bounded historical revisions without touching current/active rows."""
    row = conn.execute(
        "SELECT current_revision_id, active_revision_id FROM memory_documents WHERE id = ?",
        (document_id,),
    ).fetchone()
    if row is None:
        return 0
    protected = {
        int(row[0]) if row[0] is not None else None,
        int(row[1]) if row[1] is not None else None,
    }
    protected.discard(None)
    revisions = conn.execute(
        "SELECT id, revision_number, created_at FROM memory_revisions "
        "WHERE document_id = ? ORDER BY revision_number ASC",
        (document_id,),
    ).fetchall()
    cutoff = now_ms - retention.max_age_ms
    deletable: list[int] = []
    for rev_id, _revision_number, created_at in revisions:
        if rev_id in protected:
            continue
        if created_at < cutoff:
            deletable.append(int(rev_id))
    surviving = [
        int(rev_id)
        for rev_id, _revision_number, _created_at in revisions
        if int(rev_id) not in deletable and int(rev_id) not in protected
    ]
    while len(revisions) - len(deletable) > retention.max_count and surviving:
        deletable.append(surviving.pop(0))
    if not deletable:
        return 0
    with conn:
        conn.executemany(
            "DELETE FROM memory_revisions WHERE id = ?",
            [(rev_id,) for rev_id in deletable],
        )
    return len(deletable)


def query_memory_history(
    conn: sqlite3.Connection,
    *,
    repository: str | None = None,
    logical_path: str | None = None,
    document_uuid: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return bounded revision metadata for explicit history queries."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if not document_uuid and not (repository and logical_path):
        raise ValueError("document_uuid or repository+logical_path is required")
    clauses: list[str] = []
    params: list[Any] = []
    if document_uuid:
        clauses.append("d.uuid = ?")
        params.append(document_uuid)
    if repository:
        clauses.append("d.repository = ?")
        params.append(repository)
    if logical_path:
        clauses.append("d.logical_path = ?")
        params.append(logical_path)
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT d.uuid, d.repository, d.logical_path, d.source_path, d.state, "
        f"r.revision_number, r.content_hash, r.change_kind, r.summary, r.diff_text, "
        f"r.source_mtime_ms, r.created_at, r.enriched_at "
        f"FROM memory_revisions r "
        f"JOIN memory_documents d ON d.id = r.document_id "
        f"WHERE {where} "
        f"ORDER BY r.revision_number DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [
        {
            "document_uuid": row[0],
            "repository": row[1],
            "logical_path": row[2],
            "source_path": row[3],
            "document_state": row[4],
            "revision_number": row[5],
            "content_hash": row[6],
            "change_kind": row[7],
            "summary": row[8],
            "diff_text": row[9],
            "source_mtime_ms": row[10],
            "created_at": row[11],
            "enriched_at": row[12],
        }
        for row in rows
    ]


def _delete_projection_for_revision(conn: sqlite3.Connection, revision_id: int) -> None:
    old_node_id = conn.execute(
        "SELECT knmc.knowledge_node_id FROM knowledge_node_memory_chunks knmc "
        "JOIN memory_chunks mc ON mc.id = knmc.memory_chunk_id "
        "WHERE mc.revision_id = ? LIMIT 1",
        (revision_id,),
    ).fetchone()
    if old_node_id is None:
        return
    node_id = int(old_node_id[0])
    if vec_table_available(conn):
        conn.execute("DELETE FROM knowledge_vectors WHERE knowledge_node_id = ?", (node_id,))
    conn.execute("DELETE FROM knowledge_node_memory_chunks WHERE knowledge_node_id = ?", (node_id,))
    conn.execute("DELETE FROM knowledge_nodes WHERE id = ?", (node_id,))


def tombstone_document(
    conn: sqlite3.Connection,
    document_id: int,
    *,
    now_ms: int,
) -> None:
    """Tombstone a confirmed-missing document while retaining bounded history."""
    row = conn.execute(
        "SELECT active_revision_id, current_revision_id, repository, logical_path, state "
        "FROM memory_documents WHERE id = ?",
        (document_id,),
    ).fetchone()
    if row is None or row[4] == "tombstoned":
        return
    active_revision_id, _current_revision_id, repository, logical_path, _state = row
    with conn:
        if active_revision_id is not None:
            _delete_projection_for_revision(conn, int(active_revision_id))
        revision_number = int(
            conn.execute(
                "SELECT COALESCE(MAX(revision_number), 0) + 1 FROM memory_revisions "
                "WHERE document_id = ?",
                (document_id,),
            ).fetchone()[0]
        )
        cursor = conn.execute(
            "INSERT INTO memory_revisions "
            "(document_id, revision_number, content_hash, source_hash, redacted_content, "
            " source_mtime_ms, source_size, change_kind, summary, chunker_name, "
            " chunker_version, chunker_config_json, enrichment_version, created_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, 'delete', ?, ?, ?, ?, 1, ?)",
            (
                document_id,
                revision_number,
                "",
                "",
                now_ms,
                0,
                f"Document {logical_path!r} removed from {repository!r}",
                CHUNKER_NAME,
                CHUNKER_VERSION,
                json.dumps({"boundary": "heading", "retain_heading": True}, sort_keys=True),
                now_ms,
            ),
        )
        delete_revision_id = int(cursor.lastrowid)
        conn.execute(
            "UPDATE memory_documents SET state = 'tombstoned', tombstoned_at = ?, "
            "active_revision_id = NULL, projection_status = 'stale', observed_at = ?, "
            "updated_at = ?, current_revision_id = ? WHERE id = ?",
            (now_ms, now_ms, now_ms, delete_revision_id, document_id),
        )


def reconcile_missing_source(
    conn: sqlite3.Connection,
    document_id: int,
    *,
    now_ms: int,
    absence_confirm_polls: int,
) -> bool:
    """Advance absence handling; tombstone after configured confirmation polls."""
    row = conn.execute("SELECT state FROM memory_documents WHERE id = ?", (document_id,)).fetchone()
    if row is None:
        return False
    state = row[0]
    if state == "tombstoned":
        return True
    if state == "active":
        conn.execute(
            "UPDATE memory_documents SET state = 'unavailable', observed_at = ?, updated_at = ? "
            "WHERE id = ?",
            (now_ms, now_ms, document_id),
        )
        if absence_confirm_polls <= 1:
            tombstone_document(conn, document_id, now_ms=now_ms)
            return True
        return False
    if state == "unavailable":
        tombstone_document(conn, document_id, now_ms=now_ms)
        return True
    return False


def reconcile_configured_sources(
    conn: sqlite3.Connection,
    sources: list[dict[str, Any]],
    *,
    retention: RevisionRetention,
    now_ms: int | None = None,
) -> int:
    """Mark configured-but-missing files unavailable, then tombstone after confirmation."""
    observed_at = now_ms if now_ms is not None else int(time.time() * 1000)
    tombstoned = 0
    configured_paths = {
        str(Path(source["path"]).expanduser().resolve()) for source in sources if source.get("path")
    }
    for source_path in sorted(configured_paths):
        if Path(source_path).is_file():
            continue
        rows = conn.execute(
            "SELECT id FROM memory_documents WHERE source_path = ? AND state != 'tombstoned'",
            (source_path,),
        ).fetchall()
        for (document_id,) in rows:
            if reconcile_missing_source(
                conn,
                int(document_id),
                now_ms=observed_at,
                absence_confirm_polls=retention.absence_confirm_polls,
            ):
                prune_document_revisions(conn, int(document_id), retention, now_ms=observed_at)
                tombstoned += 1
    return tombstoned
