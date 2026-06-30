"""Read-only ingestion for Claude Code auto-memory Markdown files."""

from __future__ import annotations

import argparse
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
    unchanged = conn.execute(
        "SELECT d.id, d.uuid, r.id, r.revision_number "
        "FROM memory_documents d JOIN memory_revisions r ON r.id = d.current_revision_id "
        "WHERE d.source_kind = ? AND d.source_path = ? AND d.logical_path = ? "
        "AND d.state = 'active' AND r.source_mtime_ms = ? AND r.source_size = ?",
        (SOURCE_KIND, resolved, identity_path, int(stat.st_mtime * 1000), stat.st_size),
    ).fetchone()
    if unchanged is not None:
        doc_id, doc_uuid, rev_id, rev_num = unchanged
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


def build_memory_enrichment_prompt(claim: MemoryClaim) -> str:
    """Render one claimed, already-redacted memory revision for enrichment."""
    header = (
        f"Claude Code auto-memory\nRepository: {claim.repository}\n"
        f"Path: {claim.logical_path}\nContent hash: {claim.content_hash}"
    )
    body = "\n\n---\n\n".join(chunk["content"] for chunk in claim.chunks)
    return f"{header}\n\n{body}"


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
        cursor = conn.execute(
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
        if cursor.lastrowid:
            node_id = int(cursor.lastrowid)
        else:
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
) -> int:
    """Ingest every configured auto-memory source. Returns count of changed revisions."""
    changed = 0
    for source in sources:
        path = source.get("path")
        if not path:
            continue
        result = ingest_memory_file(
            conn,
            path,
            repository=source.get("repository"),
            logical_path=source.get("logical_path"),
        )
        if result.changed:
            changed += 1
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
        return poll_sources(conn, sources)
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
    conn = _open_db(args.db)
    try:
        version = _schema_version(conn)
        if version != EXPECTED_SCHEMA_VERSION:
            parser.error(
                f"database schema version must be {EXPECTED_SCHEMA_VERSION}, found {version}"
            )
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
