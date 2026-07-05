"""Read-only ingestion for Claude Code auto-memory Markdown files."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import tomllib
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from hippo_brain.auto_memory_constants import (
    SOURCE_KIND,
    _IDENTITY_NAMESPACE,
)
from hippo_brain.auto_memory_categories import replace_model_categories
from hippo_brain.auto_memory_lifecycle import (
    RevisionRetention,
    query_memory_history,
    reconcile_configured_sources,
    revision_retention_from_config,
)
from hippo_brain.auto_memory_ingest import (
    IngestResult,
    derive_repository_identity,
    ingest_memory_file,
)
from hippo_brain.auto_memory_reconcile import (
    ReconcileConfig,
    document_absence_outcome,
    reconcile_config_from_dict,
    reconcile_source,
    reconcile_sources,
    load_sources_from_config,
)
from hippo_brain.models import EnrichmentResult
from hippo_brain.schema_version import EXPECTED_SCHEMA_VERSION
from hippo_brain.vector_store import vec_table_available

__all__ = [
    "IngestResult",
    "MemoryClaim",
    "derive_repository_identity",
    "ingest_memory_file",
]

MEMORY_ENRICHMENT_SYSTEM_PROMPT = """\
You enrich Claude Code auto-memory Markdown into structured knowledge for a local \
personal knowledge base.

The input is already redacted. Produce a JSON object with:
- summary: one concise sentence of what this memory documents
- intent: why this memory exists or what problem it addresses
- outcome: one of success, failure, partial, unknown
- entities: object with keys projects, tools, files, services, errors (each a list of strings)
- tags: short topical tags
- memory_categories: zero or more of user, feedback, project, reference inferred from content (never index)
- key_decisions: list of notable decisions or conventions captured
- problems_encountered: list of problems or pitfalls documented
- design_decisions: list of objects with decision, rationale, alternatives (may be empty)
- embed_text: a dense paragraph optimized for semantic search (include repository context)

Output ONLY valid JSON, no markdown fences or explanation."""


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
        replace_model_categories(
            conn,
            document_id,
            result.memory_categories,
            model_name=model_name,
            now_ms=completed_at,
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
    reconcile: ReconcileConfig | None = None,
) -> int:
    """Reconcile every configured auto-memory source. Returns changed revision count."""
    summary = reconcile_sources(
        conn,
        sources,
        retention=retention,
        reconcile=reconcile,
        require_stable=True,
    )
    return int(summary["changed"])


def reconcile_from_config(
    config_path: Path | None = None,
    *,
    require_stable: bool = True,
) -> dict[str, Any]:
    """Load config and reconcile all enabled auto-memory sources."""
    path = config_path or Path.home() / ".config" / "hippo" / "config.toml"
    if not path.is_file():
        return {"changed": 0, "pending_enrichment": 0, "failed_enrichment": 0, "sources": []}
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    auto_memory = config.get("auto_memory", {})
    if not auto_memory.get("enabled", False):
        return {"changed": 0, "pending_enrichment": 0, "failed_enrichment": 0, "sources": []}
    sources = load_sources_from_config(config)
    if not sources:
        return {"changed": 0, "pending_enrichment": 0, "failed_enrichment": 0, "sources": []}
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
                f"auto-memory reconcile requires schema version {EXPECTED_SCHEMA_VERSION}, "
                f"found {version}; the daemon migrates hippo.db on startup. "
                "Run `hippo doctor` to check daemon/brain version alignment."
            )
        return reconcile_sources(
            conn,
            sources,
            retention=revision_retention_from_config(auto_memory),
            reconcile=reconcile_config_from_dict(auto_memory),
            require_stable=require_stable,
        )
    finally:
        conn.close()


def poll_from_config(config_path: Path | None = None) -> int:
    """Backward-compatible poll entry returning only the changed count."""
    return int(reconcile_from_config(config_path).get("changed", 0))


def poll_main(argv: list[str] | None = None) -> int:
    """Periodic reconciliation fallback (launchd / hippo auto-memory-poll)."""
    parser = argparse.ArgumentParser(description="Reconcile configured Claude auto-memory sources.")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Hippo config.toml (defaults to ~/.config/hippo/config.toml)",
    )
    args = parser.parse_args(argv)
    summary = reconcile_from_config(args.config, require_stable=True)
    print(json.dumps(summary, sort_keys=True))
    return 0


def reconcile_file_main(argv: list[str] | None = None) -> int:
    """Reconcile one configured file after an FSEvents notification."""
    parser = argparse.ArgumentParser(description="Reconcile one Claude auto-memory file.")
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Hippo config.toml (defaults to ~/.config/hippo/config.toml)",
    )
    args = parser.parse_args(argv)
    config_path = args.config or Path.home() / ".config" / "hippo" / "config.toml"
    with config_path.open("rb") as handle:
        config = tomllib.load(handle)
    auto_memory = config.get("auto_memory", {})
    sources = load_sources_from_config(config)
    resolved = str(args.file.expanduser().resolve())
    source = next(
        (s for s in sources if str(Path(s["path"]).expanduser().resolve()) == resolved),
        {"path": resolved, "repository": None, "logical_path": args.file.name},
    )
    storage = config.get("storage", {})
    data_dir = Path(
        storage.get("data_dir", Path.home() / ".local" / "share" / "hippo")
    ).expanduser()
    conn = _open_db(data_dir / "hippo.db")
    try:
        if _schema_version(conn) != EXPECTED_SCHEMA_VERSION:
            parser.error(f"database schema version must be {EXPECTED_SCHEMA_VERSION}")
        retention = revision_retention_from_config(auto_memory)
        observed_at = int(time.time() * 1000)
        result = reconcile_source(
            conn,
            source,
            retention=retention,
            reconcile=reconcile_config_from_dict(auto_memory),
            require_stable=True,
            now_ms=observed_at,
        )
        if result.outcome == "missing":
            reconcile_configured_sources(conn, [source], retention=retention, now_ms=observed_at)
            result = replace(result, outcome=document_absence_outcome(conn, result.path))
    finally:
        conn.close()
    print(
        json.dumps(
            {
                "path": result.path,
                "outcome": result.outcome,
                "changed": result.changed,
                "revision_id": result.revision_id,
                "projection_status": result.projection_status,
                "pending_enrichment": result.pending_enrichment,
                "failed_enrichment": result.failed_enrichment,
            },
            sort_keys=True,
        )
    )
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
