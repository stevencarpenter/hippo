"""Read-only ingestion for Claude Code auto-memory Markdown files."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hippo_brain.models import EnrichmentResult
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
    repository: str | None = None,
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
    repository_identity = derive_repository_identity(path, repository)

    source = path.read_text(encoding="utf-8")
    redacted = redact(source)
    content_hash = _sha256(redacted)
    stat = path.stat()
    observed_at = now_ms if now_ms is not None else int(time.time() * 1000)
    identity_path = logical_path or path.name
    document_uuid = str(
        uuid.uuid5(_IDENTITY_NAMESPACE, f"{SOURCE_KIND}\0{repository_identity}\0{identity_path}")
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


def claim_pending_memories(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    limit: int = 10,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Atomically claim pending memory revisions and load their redacted chunks."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    claimed_at = now_ms if now_ms is not None else int(time.time() * 1000)
    conn.execute("BEGIN IMMEDIATE")
    try:
        revision_ids = [
            int(row[0])
            for row in conn.execute(
                "SELECT revision_id FROM memory_enrichment_queue "
                "WHERE status = 'pending' ORDER BY priority, enqueued_at LIMIT ?",
                (limit,),
            ).fetchall()
        ]
        if revision_ids:
            placeholders = ",".join("?" for _ in revision_ids)
            conn.execute(
                f"UPDATE memory_enrichment_queue SET status = 'processing', locked_at = ?, "
                f"locked_by = ?, updated_at = ? WHERE revision_id IN ({placeholders}) "
                "AND status = 'pending'",
                (claimed_at, worker_id, claimed_at, *revision_ids),
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

    claims: list[dict[str, Any]] = []
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
        chunks = [
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
        ]
        claims.append(
            {
                "revision_id": int(row[0]),
                "document_id": int(row[1]),
                "revision_number": int(row[2]),
                "content_hash": row[3],
                "captured_at": int(row[4]),
                "document_uuid": row[5],
                "repository": row[6],
                "logical_path": row[7],
                "source_path": row[8],
                "chunks": chunks,
            }
        )
    return claims


def build_memory_enrichment_prompt(claim: dict[str, Any]) -> str:
    """Render one claimed, already-redacted memory revision for enrichment."""
    header = (
        f"Claude Code auto-memory\nRepository: {claim['repository']}\n"
        f"Path: {claim['logical_path']}\nContent hash: {claim['content_hash']}"
    )
    body = "\n\n---\n\n".join(chunk["content"] for chunk in claim["chunks"])
    return f"{header}\n\n{body}"


def write_memory_knowledge_node(
    conn: sqlite3.Connection,
    result: EnrichmentResult,
    revision_id: int,
    model_name: str,
    *,
    now_ms: int | None = None,
) -> int:
    """Publish a memory projection and mark its queue item done in one transaction."""
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
        cursor = conn.execute(
            "INSERT INTO knowledge_nodes "
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
        node_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO knowledge_node_memory_chunks (knowledge_node_id, memory_chunk_id) "
            "SELECT ?, id FROM memory_chunks WHERE revision_id = ?",
            (node_id, revision_id),
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


def main(argv: list[str] | None = None) -> int:
    """Ingest one explicitly configured Claude auto-memory file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True, help="Memory Markdown file")
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
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != 19:
            parser.error(f"database schema version must be 19, found {version}")
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
