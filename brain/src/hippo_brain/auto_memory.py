"""Read-only ingestion for Claude Code auto-memory Markdown files."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sqlite3
import subprocess
import time
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hippo_brain.markdown_chunking import MarkdownChunk, markdown_heading_chunks
from hippo_brain.models import EnrichmentResult
from hippo_brain.redaction import redact
from hippo_brain.schema_version import EXPECTED_SCHEMA_VERSION
from hippo_brain.vector_store import vec_table_available

SOURCE_KIND = "claude-auto-memory"
CHUNKER_NAME = "markdown-headings"
CHUNKER_VERSION = 1
_IDENTITY_NAMESPACE = uuid.UUID("0fc25921-9c30-4c16-85da-b489ea81f087")
DEFAULT_MAX_REVISION_COUNT = 20
DEFAULT_MAX_REVISION_AGE_DAYS = 90
DEFAULT_ABSENCE_CONFIRM_POLLS = 2
_MAX_DIFF_CHARS = 16_384

MEMORY_ENRICHMENT_SYSTEM_PROMPT = """\
You enrich Claude Code auto-memory Markdown into structured knowledge for a local \
personal knowledge base.

The input is already redacted. Produce a JSON object with:
- summary: one concise sentence of what this memory documents
- intent: why this memory exists or what problem it addresses
- outcome: one of success, failure, partial, unknown
- entities: object with keys projects, tools, files, services, errors (each a list of strings)
- tags: short topical tags
- key_decisions: list of notable decisions or conventions captured
- problems_encountered: list of problems or pitfalls documented
- design_decisions: list of objects with decision, rationale, alternatives (may be empty)
- embed_text: a dense paragraph optimized for semantic search (include repository context)

Output ONLY valid JSON, no markdown fences or explanation."""


@dataclass(frozen=True)
class IngestResult:
    document_id: int
    document_uuid: str
    revision_id: int
    revision_number: int
    changed: bool
    chunk_count: int


@dataclass(frozen=True)
class RevisionRetention:
    max_count: int = DEFAULT_MAX_REVISION_COUNT
    max_age_ms: int = DEFAULT_MAX_REVISION_AGE_DAYS * 86_400_000
    absence_confirm_polls: int = DEFAULT_ABSENCE_CONFIRM_POLLS


@dataclass(frozen=True)
class MemoryClaim:
    revision_id: int
    document_id: int
    revision_number: int
    content_hash: str
    captured_at: int
    document_uuid: str
    repository: str
    logical_path: str
    source_path: str
    chunks: tuple[dict[str, Any], ...]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def derive_repository_identity(source_path: Path | str, explicit: str | None = None) -> str:
    """Return an explicit identity, a sanitized Git remote, or a documented local fallback."""
    if explicit and explicit.strip():
        return explicit.strip()
    directory = Path(source_path).expanduser().resolve().parent
    try:
        root_result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        root = Path(root_result.stdout.strip()).resolve()
        remote_result = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        remote = remote_result.stdout.strip()
        if remote:
            if ":" in remote and "://" not in remote:
                host, path = remote.split(":", 1)
                host = host.rsplit("@", 1)[-1]
            else:
                parsed = urlsplit(remote)
                host = parsed.hostname or "git"
                path = parsed.path
            clean_path = path.strip("/")
            if clean_path.endswith(".git"):
                clean_path = clean_path[:-4]
            if clean_path:
                return f"{host}/{clean_path}"
        return f"local-git:{root}"
    except FileNotFoundError, subprocess.SubprocessError, OSError, ValueError:
        return f"local:{directory}"


def _chunks(markdown: str) -> list[MarkdownChunk]:
    return markdown_heading_chunks(markdown)


def revision_retention_from_config(auto_memory: dict[str, Any]) -> RevisionRetention:
    """Parse revision retention settings from a config ``[auto_memory]`` table."""
    max_count = int(auto_memory.get("max_revision_count", DEFAULT_MAX_REVISION_COUNT))
    max_age_days = int(auto_memory.get("max_revision_age_days", DEFAULT_MAX_REVISION_AGE_DAYS))
    absence_polls = int(auto_memory.get("absence_confirm_polls", DEFAULT_ABSENCE_CONFIRM_POLLS))
    return RevisionRetention(
        max_count=max(max_count, 1),
        max_age_ms=max(max_age_days, 1) * 86_400_000,
        absence_confirm_polls=max(absence_polls, 1),
    )


def _summarize_diff(old_redacted: str, new_redacted: str) -> tuple[str, str]:
    """Build a bounded summary and unified diff from redacted content only."""
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


def _finalize_superseded_revision(
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


def _try_resolve_rename(
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
    active_revision_id, current_revision_id, repository, logical_path, _state = row
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


def ingest_memory_file(
    conn: sqlite3.Connection,
    source_path: Path | str,
    *,
    repository: str | None = None,
    logical_path: str | None = None,
    now_ms: int | None = None,
    retention: RevisionRetention | None = None,
) -> IngestResult:
    """Read, redact, version, chunk, and enqueue one explicit memory file.

    The source is never opened for writing. Only redacted text and hashes of
    redacted text cross the SQLite durability boundary.
    """
    path = Path(source_path).expanduser()
    if not path.is_file():
        raise ValueError(f"auto-memory source must be an explicit regular file: {path}")

    stat = path.stat()
    resolved = str(path.resolve())
    identity_path = logical_path or path.name

    # Fast path: a poll fires every 60s, but memory files rarely change. When the
    # on-disk mtime(ms)+size still match the document's current revision, the
    # content is unchanged, so skip the Git identity derivation (two subprocesses),
    # the full read, the regex redact, and the double hash entirely. mtime+size is
    # the same cheap proxy the codex/cursor pollers use; logical_path is resolved
    # without Git so this lookup stays cheap. Any real edit changes mtime or size
    # and falls through to the authoritative content-hash path below.
    #
    # The authoritative document identity is (source_kind, repository, logical_path),
    # but the fast path keys on source_path to avoid the Git derivation. When an
    # explicit repository is configured we additionally scope by it — cheaply, no
    # Git — so two sources sharing one path but declaring different repositories
    # don't alias onto the first document and silently suppress the second. When
    # the repository is auto-derived (None), the same path yields the same identity,
    # so the path-keyed match is already unambiguous.
    explicit_repo = repository.strip() if repository and repository.strip() else None
    unchanged = conn.execute(
        "SELECT d.id, d.uuid, r.id, r.revision_number, r.source_hash "
        "FROM memory_documents d JOIN memory_revisions r ON r.id = d.current_revision_id "
        "WHERE d.source_kind = ? AND d.source_path = ? AND d.logical_path = ? "
        "AND (? IS NULL OR d.repository = ?) "
        "AND d.state = 'active' AND r.source_mtime_ms = ? AND r.source_size = ?",
        (
            SOURCE_KIND,
            resolved,
            identity_path,
            explicit_repo,
            explicit_repo,
            int(stat.st_mtime * 1000),
            stat.st_size,
        ),
    ).fetchone()
    if unchanged is not None:
        doc_id, doc_uuid, rev_id, rev_num, stored_source_hash = unchanged
        source_text = path.read_text(encoding="utf-8")
        if _sha256(source_text) == stored_source_hash:
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM memory_chunks WHERE revision_id = ?", (rev_id,)
            ).fetchone()[0]
            return IngestResult(
                int(doc_id), doc_uuid, int(rev_id), int(rev_num), False, int(chunk_count)
            )

    repository_identity = derive_repository_identity(path, repository)

    source = path.read_text(encoding="utf-8")
    source_hash = _sha256(source)
    redacted = redact(source)
    content_hash = _sha256(redacted)
    observed_at = now_ms if now_ms is not None else int(time.time() * 1000)
    document_uuid = str(
        uuid.uuid5(_IDENTITY_NAMESPACE, f"{SOURCE_KIND}\0{repository_identity}\0{identity_path}")
    )
    retention_policy = retention or RevisionRetention()

    renamed_document_id = _try_resolve_rename(
        conn,
        repository=repository_identity,
        logical_path=identity_path,
        content_hash=content_hash,
        source_path=resolved,
        observed_at=observed_at,
    )
    if renamed_document_id is not None:
        row = conn.execute(
            "SELECT d.uuid, d.current_revision_id, r.revision_number FROM memory_documents d "
            "JOIN memory_revisions r ON r.id = d.current_revision_id WHERE d.id = ?",
            (renamed_document_id,),
        ).fetchone()
        if row is not None:
            doc_uuid, revision_id, revision_number = row[0], int(row[1]), int(row[2])
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM memory_chunks WHERE revision_id = ?", (revision_id,)
            ).fetchone()[0]
            prune_document_revisions(
                conn, renamed_document_id, retention_policy, now_ms=observed_at
            )
            return IngestResult(
                renamed_document_id,
                doc_uuid,
                revision_id,
                revision_number,
                False,
                int(chunk_count),
            )

    with conn:
        row = conn.execute(
            "SELECT id, current_revision_id FROM memory_documents "
            "WHERE source_kind = ? AND repository = ? AND logical_path = ?",
            (SOURCE_KIND, repository_identity, identity_path),
        ).fetchone()
        if row is None:
            cursor = conn.execute(
                "INSERT INTO memory_documents "
                "(uuid, source_kind, repository, logical_path, source_path, state, "
                " projection_status, observed_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', 'pending', ?, ?, ?)",
                (
                    document_uuid,
                    SOURCE_KIND,
                    repository_identity,
                    identity_path,
                    resolved,
                    observed_at,
                    observed_at,
                    observed_at,
                ),
            )
            document_id = int(cursor.lastrowid)
            current_revision_id = None
        else:
            document_id, current_revision_id = int(row[0]), row[1]
            conn.execute(
                "UPDATE memory_documents SET source_path = ?, state = 'active', "
                "observed_at = ?, updated_at = ?, tombstoned_at = NULL WHERE id = ?",
                (resolved, observed_at, observed_at, document_id),
            )

        if current_revision_id is not None:
            current = conn.execute(
                "SELECT id, revision_number, content_hash, redacted_content FROM memory_revisions "
                "WHERE id = ?",
                (current_revision_id,),
            ).fetchone()
            if current is not None and current[2] == content_hash:
                chunk_count = conn.execute(
                    "SELECT COUNT(*) FROM memory_chunks WHERE revision_id = ?", (current[0],)
                ).fetchone()[0]
                return IngestResult(
                    document_id,
                    document_uuid,
                    int(current[0]),
                    int(current[1]),
                    False,
                    int(chunk_count),
                )

        previous_redacted: str | None = None
        if current_revision_id is not None:
            previous = conn.execute(
                "SELECT redacted_content FROM memory_revisions WHERE id = ?",
                (current_revision_id,),
            ).fetchone()
            if previous is not None and previous[0]:
                previous_redacted = previous[0]

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
            " source_mtime_ms, source_size, change_kind, chunker_name, chunker_version, "
            " chunker_config_json, enrichment_version, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                document_id,
                revision_number,
                content_hash,
                source_hash,
                redacted,
                int(stat.st_mtime * 1000),
                stat.st_size,
                "create" if revision_number == 1 else "update",
                CHUNKER_NAME,
                CHUNKER_VERSION,
                json.dumps({"boundary": "heading", "retain_heading": True}, sort_keys=True),
                observed_at,
            ),
        )
        revision_id = int(cursor.lastrowid)
        chunks = _chunks(redacted)
        conn.executemany(
            "INSERT INTO memory_chunks "
            "(revision_id, ordinal, heading_path, start_offset, end_offset, content, "
            " content_hash, token_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    revision_id,
                    chunk.ordinal,
                    chunk.heading_path,
                    chunk.start_offset,
                    chunk.end_offset,
                    chunk.content,
                    _sha256(chunk.content),
                    len(chunk.content.split()),
                    observed_at,
                )
                for chunk in chunks
            ],
        )
        conn.execute(
            "INSERT INTO memory_enrichment_queue "
            "(revision_id, status, priority, retry_count, max_retries, enqueued_at, updated_at) "
            "VALUES (?, 'pending', 5, 0, 5, ?, ?)",
            (revision_id, observed_at, observed_at),
        )
        conn.execute(
            "UPDATE memory_documents SET current_revision_id = ?, projection_status = 'pending', "
            "last_error = NULL, updated_at = ? WHERE id = ?",
            (revision_id, observed_at, document_id),
        )
        conn.execute(
            "UPDATE source_health SET last_event_ts = ?, last_success_ts = ?, "
            "last_error_msg = NULL, last_error_ts = NULL, "
            "consecutive_failures = 0, updated_at = ? WHERE source = ?",
            (observed_at, observed_at, observed_at, SOURCE_KIND),
        )
        if previous_redacted is not None and current_revision_id is not None:
            _finalize_superseded_revision(
                conn,
                int(current_revision_id),
                old_redacted=previous_redacted,
                new_redacted=redacted,
            )

    prune_document_revisions(conn, document_id, retention_policy, now_ms=observed_at)

    return IngestResult(
        document_id,
        document_uuid,
        revision_id,
        revision_number,
        True,
        len(chunks),
    )


def claim_pending_memories(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    limit: int = 10,
    now_ms: int | None = None,
    stale_lock_timeout_ms: int | None = None,
) -> list[MemoryClaim]:
    """Atomically claim pending memory revisions and load their redacted chunks.

    When ``stale_lock_timeout_ms`` is set, revisions stuck in ``processing`` with
    a ``locked_at`` older than the timeout are reclaimed too — recovering locks
    orphaned by a crashed worker (the sibling agentic/shell claims do the same).
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    claimed_at = now_ms if now_ms is not None else int(time.time() * 1000)
    # `claimable` is a fixed predicate (no user input); the same fragment gates
    # both the SELECT and the UPDATE so a row can never be selected but not locked.
    if stale_lock_timeout_ms is not None:
        stale_before = claimed_at - stale_lock_timeout_ms
        claimable = (
            "(status = 'pending' OR (status = 'processing' AND COALESCE(locked_at, 0) <= ?))"
        )
        claimable_params: tuple[int, ...] = (stale_before,)
    else:
        claimable = "status = 'pending'"
        claimable_params = ()
    conn.execute("BEGIN IMMEDIATE")
    try:
        revision_ids = [
            int(row[0])
            for row in conn.execute(
                f"SELECT revision_id FROM memory_enrichment_queue "  # noqa: S608
                f"WHERE {claimable} ORDER BY priority, enqueued_at LIMIT ?",
                (*claimable_params, limit),
            ).fetchall()
        ]
        if revision_ids:
            placeholders = ",".join("?" for _ in revision_ids)
            conn.execute(
                f"UPDATE memory_enrichment_queue SET status = 'processing', locked_at = ?, "  # noqa: S608
                f"locked_by = ?, updated_at = ? WHERE revision_id IN ({placeholders}) "
                f"AND {claimable}",
                (claimed_at, worker_id, claimed_at, *revision_ids, *claimable_params),
            )
            conn.execute(
                f"UPDATE memory_documents SET projection_status = 'processing', updated_at = ? "
                f"WHERE current_revision_id IN ({placeholders})",
                (claimed_at, *revision_ids),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    claims: list[MemoryClaim] = []
    for revision_id in revision_ids:
        row = conn.execute(
            "SELECT r.id, r.document_id, r.revision_number, r.content_hash, r.created_at, "
            "d.uuid, d.repository, d.logical_path, d.source_path "
            "FROM memory_revisions r JOIN memory_documents d ON d.id = r.document_id "
            "WHERE r.id = ?",
            (revision_id,),
        ).fetchone()
        if row is None:
            continue
        chunks = tuple(
            {
                "id": int(chunk[0]),
                "ordinal": int(chunk[1]),
                "heading_path": chunk[2],
                "content": chunk[3],
                "content_hash": chunk[4],
            }
            for chunk in conn.execute(
                "SELECT id, ordinal, heading_path, content, content_hash "
                "FROM memory_chunks WHERE revision_id = ? ORDER BY ordinal",
                (revision_id,),
            ).fetchall()
        )
        claims.append(
            MemoryClaim(
                revision_id=int(row[0]),
                document_id=int(row[1]),
                revision_number=int(row[2]),
                content_hash=row[3],
                captured_at=int(row[4]),
                document_uuid=row[5],
                repository=row[6],
                logical_path=row[7],
                source_path=row[8],
                chunks=chunks,
            )
        )
    return claims


def render_memory_enrichment_input(
    repository: str, logical_path: str, content_hash: str, chunk_texts: list[str]
) -> str:
    """Render the enrichment user message for a memory revision.

    Single source of truth for the input format: the live enrichment loop
    (via build_memory_enrichment_prompt) and the training exporter
    (training.py) both go through here so exported examples match what the
    model saw at enrichment time.
    """
    header = (
        f"Claude Code auto-memory\nRepository: {repository}\n"
        f"Path: {logical_path}\nContent hash: {content_hash}"
    )
    body = "\n\n---\n\n".join(chunk_texts)
    return f"{header}\n\n{body}"


def build_memory_enrichment_prompt(claim: MemoryClaim) -> str:
    """Render one claimed, already-redacted memory revision for enrichment."""
    return render_memory_enrichment_input(
        claim.repository,
        claim.logical_path,
        claim.content_hash,
        [chunk["content"] for chunk in claim.chunks],
    )


def write_memory_knowledge_node(
    conn: sqlite3.Connection,
    result: EnrichmentResult,
    revision_id: int,
    model_name: str,
    *,
    now_ms: int | None = None,
) -> int | None:
    """Publish a memory projection and mark its queue item done in one transaction.

    Idempotent: if a knowledge node already exists for this revision (e.g. after a
    previous successful enrichment whose embedding step failed), it reuses the existing
    node. When superseding an older revision, the old knowledge node is cleaned up
    atomically within the same transaction.

    Returns the published knowledge-node id, or ``None`` when the revision was
    superseded by a newer one before its enrichment finished. A stale result is
    discarded rather than promoted: publishing it would delete the current node
    and revert the projection to outdated content.
    """
    completed_at = now_ms if now_ms is not None else int(time.time() * 1000)
    revision = conn.execute(
        "SELECT r.document_id, r.content_hash, d.uuid, d.repository, d.logical_path, "
        "d.source_path, r.created_at FROM memory_revisions r "
        "JOIN memory_documents d ON d.id = r.document_id WHERE r.id = ?",
        (revision_id,),
    ).fetchone()
    if revision is None:
        raise ValueError(f"memory revision does not exist: {revision_id}")
    document_id, content_hash, document_uuid, repository, logical_path, source_path, captured_at = (
        revision
    )
    node_uuid = str(uuid.uuid5(_IDENTITY_NAMESPACE, f"projection\0{document_uuid}\0{revision_id}"))
    content = json.dumps(
        {
            "summary": result.summary,
            "intent": result.intent,
            "outcome": result.outcome,
            "entities": result.entities,
            "tags": result.tags,
            "key_decisions": result.key_decisions,
            "problems_encountered": result.problems_encountered,
            "design_decisions": result.design_decisions,
            "source": {
                "kind": SOURCE_KIND,
                "repository": repository,
                "logical_path": logical_path,
                "source_path": source_path,
                "content_hash": content_hash,
                "captured_at": captured_at,
            },
        },
        sort_keys=True,
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        current_revision_id = conn.execute(
            "SELECT current_revision_id FROM memory_documents WHERE id = ?",
            (document_id,),
        ).fetchone()[0]
        if current_revision_id is not None and current_revision_id != revision_id:
            # The document advanced to a newer revision before this enrichment
            # finished. Promoting now would delete the current node and revert the
            # projection to stale content, so discard the stale result: record that
            # the revision was enriched (history) and retire its queue row without
            # minting or promoting a node. Checked inside BEGIN IMMEDIATE so a
            # concurrent ingest cannot move current_revision_id between here and
            # the promote below.
            conn.execute(
                "UPDATE memory_revisions SET summary = ?, enrichment_model = ?, "
                "enriched_at = ? WHERE id = ?",
                (result.summary, model_name, completed_at, revision_id),
            )
            conn.execute(
                "UPDATE memory_enrichment_queue SET status = 'done', locked_at = NULL, "
                "locked_by = NULL, error_message = NULL, updated_at = ? WHERE revision_id = ?",
                (completed_at, revision_id),
            )
            conn.commit()
            return None
        conn.execute(
            "INSERT OR IGNORE INTO knowledge_nodes "
            "(uuid, content, embed_text, node_type, outcome, tags, enrichment_model, "
            "enrichment_version, created_at, updated_at) "
            "VALUES (?, ?, ?, 'observation', ?, ?, ?, 1, ?, ?)",
            (
                node_uuid,
                content,
                result.embed_text,
                result.outcome,
                json.dumps(result.tags),
                model_name,
                completed_at,
                completed_at,
            ),
        )
        # Resolve the node id by its deterministic uuid, never via cursor.lastrowid:
        # after an INSERT OR IGNORE that was *ignored* (the idempotent re-run), SQLite
        # leaves last_insert_rowid() pointing at the previous successful insert on this
        # connection, so lastrowid would be a stale (cross-table) rowid rather than 0.
        # The truthiness check would then skip the fallback SELECT and return the wrong
        # id. A uuid lookup is correct whether the row was just inserted or already existed.
        node_id = conn.execute(
            "SELECT id FROM knowledge_nodes WHERE uuid = ?", (node_uuid,)
        ).fetchone()[0]
        conn.execute(
            "DELETE FROM knowledge_node_memory_chunks WHERE knowledge_node_id = ?",
            (node_id,),
        )
        conn.execute(
            "INSERT INTO knowledge_node_memory_chunks (knowledge_node_id, memory_chunk_id) "
            "SELECT ?, id FROM memory_chunks WHERE revision_id = ?",
            (node_id, revision_id),
        )
        old_active = conn.execute(
            "SELECT active_revision_id FROM memory_documents WHERE id = ?",
            (document_id,),
        ).fetchone()[0]
        if old_active is not None and old_active != revision_id:
            old_node_id = conn.execute(
                "SELECT knmc.knowledge_node_id FROM knowledge_node_memory_chunks knmc "
                "JOIN memory_chunks mc ON mc.id = knmc.memory_chunk_id "
                "WHERE mc.revision_id = ? LIMIT 1",
                (old_active,),
            ).fetchone()
            if old_node_id is not None:
                # vec0 has no FK cascade and the embed reaper only heals
                # nodes-missing-vectors, so deleting the superseded node without its
                # vector would orphan the vector forever. Refuse rather than orphan
                # (mirrors claude_sessions.replace_prior_agentic_nodes); the prod
                # enrichment conn always loads sqlite-vec via _get_conn.
                if not vec_table_available(conn):
                    raise RuntimeError(
                        "write_memory_knowledge_node: vec0 knowledge_vectors not reachable; "
                        "refusing to delete the superseded node to avoid an orphan vector "
                        "(load sqlite-vec on this connection)"
                    )
                conn.execute(
                    "DELETE FROM knowledge_node_memory_chunks WHERE knowledge_node_id = ?",
                    (old_node_id[0],),
                )
                conn.execute(
                    "DELETE FROM knowledge_vectors WHERE knowledge_node_id = ?",
                    (old_node_id[0],),
                )
                conn.execute(
                    "DELETE FROM knowledge_nodes WHERE id = ?",
                    (old_node_id[0],),
                )
        conn.execute(
            "UPDATE memory_revisions SET summary = ?, enrichment_model = ?, enriched_at = ? "
            "WHERE id = ?",
            (result.summary, model_name, completed_at, revision_id),
        )
        conn.execute(
            "UPDATE memory_enrichment_queue SET status = 'done', locked_at = NULL, "
            "locked_by = NULL, error_message = NULL, updated_at = ? WHERE revision_id = ?",
            (completed_at, revision_id),
        )
        conn.execute(
            "UPDATE memory_documents SET active_revision_id = ?, projection_status = 'ready', "
            "last_error = NULL, updated_at = ? WHERE id = ?",
            (revision_id, completed_at, document_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return node_id


def mark_memory_enrichment_failed(
    conn: sqlite3.Connection,
    revision_id: int,
    error: str,
    *,
    now_ms: int | None = None,
) -> None:
    """Release a failed claim, preserving the last-known-good projection."""
    failed_at = now_ms if now_ms is not None else int(time.time() * 1000)
    with conn:
        conn.execute(
            "UPDATE memory_enrichment_queue SET retry_count = retry_count + 1, "
            "status = CASE WHEN retry_count + 1 >= max_retries THEN 'failed' ELSE 'pending' END, "
            "error_message = ?, locked_at = NULL, locked_by = NULL, updated_at = ? "
            "WHERE revision_id = ?",
            (error, failed_at, revision_id),
        )
        conn.execute(
            "UPDATE memory_documents SET projection_status = CASE "
            "WHEN active_revision_id IS NULL THEN 'failed' ELSE 'stale' END, "
            "last_error = ?, updated_at = ? WHERE current_revision_id = ?",
            (error, failed_at, revision_id),
        )


def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def poll_sources(
    conn: sqlite3.Connection,
    sources: list[dict[str, Any]],
    *,
    retention: RevisionRetention | None = None,
) -> int:
    """Ingest every configured auto-memory source. Returns count of changed revisions."""
    retention_policy = retention or RevisionRetention()
    changed = 0
    for source in sources:
        path = source.get("path")
        if not path:
            continue
        if not Path(path).is_file():
            continue
        result = ingest_memory_file(
            conn,
            path,
            repository=source.get("repository"),
            logical_path=source.get("logical_path"),
            retention=retention_policy,
        )
        if result.changed:
            changed += 1
    reconcile_configured_sources(conn, sources, retention=retention_policy)
    return changed


def poll_from_config(config_path: Path | None = None) -> int:
    """Load config and poll all enabled auto-memory sources."""
    path = config_path or Path.home() / ".config" / "hippo" / "config.toml"
    if not path.is_file():
        return 0
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    auto_memory = config.get("auto_memory", {})
    if not auto_memory.get("enabled", False):
        return 0
    sources: list[dict[str, Any]] = []
    for source in auto_memory.get("sources", []):
        if not isinstance(source, dict):
            continue
        sources.append(
            {
                "path": str(Path(source.get("path", "")).expanduser()),
                "repository": source.get("repository"),
                "logical_path": source.get("logical_path"),
            }
        )
    if not sources:
        return 0
    storage = config.get("storage", {})
    data_dir = Path(
        storage.get("data_dir", Path.home() / ".local" / "share" / "hippo")
    ).expanduser()
    db_path = data_dir / "hippo.db"
    conn = _open_db(db_path)
    try:
        version = _schema_version(conn)
        if version != EXPECTED_SCHEMA_VERSION:
            raise RuntimeError(
                f"auto-memory poll requires schema version {EXPECTED_SCHEMA_VERSION}, "
                f"found {version}; the daemon migrates hippo.db on startup. "
                "Run `hippo doctor` to check daemon/brain version alignment."
            )
        return poll_sources(
            conn,
            sources,
            retention=revision_retention_from_config(auto_memory),
        )
    finally:
        conn.close()


def poll_main(argv: list[str] | None = None) -> int:
    """Poll all enabled auto-memory sources from config (launchd / hippo auto-memory-poll)."""
    parser = argparse.ArgumentParser(description="Poll configured Claude auto-memory sources.")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Hippo config.toml (defaults to ~/.config/hippo/config.toml)",
    )
    args = parser.parse_args(argv)
    changed = poll_from_config(args.config)
    print(json.dumps({"changed": changed}, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Ingest one explicitly configured Claude auto-memory file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, help="Memory Markdown file")
    parser.add_argument(
        "--repository",
        help="Stable repository identity; defaults to sanitized Git origin or local path",
    )
    parser.add_argument(
        "--logical-path",
        default=None,
        help="Path relative to the repository memory root (defaults to filename)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path.home() / ".local" / "share" / "hippo" / "hippo.db",
        help="Hippo SQLite database",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="Print bounded revision history instead of ingesting",
    )
    args = parser.parse_args(argv)
    conn = _open_db(args.db)
    try:
        version = _schema_version(conn)
        if version != EXPECTED_SCHEMA_VERSION:
            parser.error(
                f"database schema version must be {EXPECTED_SCHEMA_VERSION}, found {version}"
            )
        if args.history:
            if not args.repository or not args.logical_path:
                parser.error("--history requires --repository and --logical-path")
            history = query_memory_history(
                conn,
                repository=args.repository,
                logical_path=args.logical_path,
                limit=50,
            )
            print(json.dumps(history, sort_keys=True))
            return 0
        if args.file is None:
            parser.error("--file is required unless --history is set")
        result = ingest_memory_file(
            conn,
            args.file,
            repository=args.repository,
            logical_path=args.logical_path,
        )
    finally:
        conn.close()
    print(json.dumps(result.__dict__, sort_keys=True))
    return 0
