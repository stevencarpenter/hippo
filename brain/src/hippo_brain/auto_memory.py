"""Read-only ingestion for Claude Code auto-memory Markdown files."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from hippo_brain.redaction import redact

SOURCE_KIND = "claude-auto-memory"
CHUNKER_NAME = "markdown-headings"
CHUNKER_VERSION = 1
_IDENTITY_NAMESPACE = uuid.UUID("0fc25921-9c30-4c16-85da-b489ea81f087")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class IngestResult:
    document_id: int
    document_uuid: str
    revision_id: int
    revision_number: int
    changed: bool
    chunk_count: int


@dataclass(frozen=True)
class _Chunk:
    ordinal: int
    heading_path: str
    start_offset: int
    end_offset: int
    content: str


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunks(markdown: str) -> list[_Chunk]:
    """Split Markdown at headings while retaining deterministic heading paths."""
    matches = list(_HEADING.finditer(markdown))
    if not matches:
        content = markdown.strip()
        return [_Chunk(0, "", 0, len(markdown), content)] if content else []

    chunks: list[_Chunk] = []
    headings: list[str] = []
    for ordinal, match in enumerate(matches):
        level = len(match.group(1))
        title = match.group(2).strip()
        headings = headings[: level - 1]
        headings.append(title)
        start = match.start()
        end = matches[ordinal + 1].start() if ordinal + 1 < len(matches) else len(markdown)
        content = markdown[start:end].strip()
        if content:
            chunks.append(
                _Chunk(
                    ordinal=len(chunks),
                    heading_path=" > ".join(headings),
                    start_offset=start,
                    end_offset=end,
                    content=content,
                )
            )
    return chunks


def ingest_memory_file(
    conn: sqlite3.Connection,
    source_path: Path | str,
    *,
    repository: str,
    logical_path: str | None = None,
    now_ms: int | None = None,
) -> IngestResult:
    """Read, redact, version, chunk, and enqueue one explicit memory file.

    The source is never opened for writing. Only redacted text and hashes of
    redacted text cross the SQLite durability boundary.
    """
    path = Path(source_path).expanduser()
    if not path.is_file():
        raise ValueError(f"auto-memory source must be an explicit regular file: {path}")
    if not repository.strip():
        raise ValueError("repository must not be empty")

    source = path.read_text(encoding="utf-8")
    redacted = redact(source)
    content_hash = _sha256(redacted)
    stat = path.stat()
    observed_at = now_ms if now_ms is not None else int(time.time() * 1000)
    identity_path = logical_path or path.name
    document_uuid = str(
        uuid.uuid5(_IDENTITY_NAMESPACE, f"{SOURCE_KIND}\0{repository}\0{identity_path}")
    )

    with conn:
        row = conn.execute(
            "SELECT id, current_revision_id FROM memory_documents "
            "WHERE source_kind = ? AND repository = ? AND logical_path = ?",
            (SOURCE_KIND, repository, identity_path),
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
                    repository,
                    identity_path,
                    str(path.resolve()),
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
                (str(path.resolve()), observed_at, observed_at, document_id),
            )

        if current_revision_id is not None:
            current = conn.execute(
                "SELECT id, revision_number, content_hash FROM memory_revisions WHERE id = ?",
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
                content_hash,
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
            "UPDATE source_health SET last_event_ts = ?, last_success_ts = ?, last_error = NULL, "
            "consecutive_failures = 0, updated_at = ? WHERE source = ?",
            (observed_at, observed_at, observed_at, SOURCE_KIND),
        )

    return IngestResult(
        document_id,
        document_uuid,
        revision_id,
        revision_number,
        True,
        len(chunks),
    )
