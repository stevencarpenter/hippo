"""Read-only auto-memory query API for agents and humans (SNUG-137).

Current projected memory is the default; revision history is explicit and bounded.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hippo_brain.auto_memory_categories import (
    list_document_categories,
    list_document_links,
    validate_memory_category_filter,
)
from hippo_brain.auto_memory_constants import SOURCE_KIND
from hippo_brain.auto_memory_lifecycle import query_memory_history
from hippo_brain.mcp_queries import MAX_LIMIT, parse_since
from hippo_brain.source_filters import table_exists

MAX_EXCERPT_CHARS = 600


@dataclass(frozen=True)
class MemoryQueryRequest:
    query: str = ""
    repository: str = ""
    category: str = ""
    logical_path: str = ""
    document_uuid: str = ""
    since: str = ""
    source_kind: str = SOURCE_KIND
    limit: int = 20
    offset: int = 0
    include_non_queryable: bool = False
    include_source_path: bool = False


def resolve_limit(limit: int, *, default: int = 20) -> int:
    if limit <= 0:
        raise ValueError("limit must be greater than 0")
    return min(limit, MAX_LIMIT)


def _truncate(text: str, max_len: int = MAX_EXCERPT_CHARS) -> str:
    if len(text) <= max_len:
        return text
    return text[: max(max_len - 1, 1)] + "…"


def _categories_payload(conn: sqlite3.Connection, document_id: int) -> list[dict[str, Any]]:
    return [
        {
            "category": item.category,
            "source": item.source,
            "confidence": item.confidence,
            "model": item.model,
            "enrichment_version": item.enrichment_version,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
        for item in list_document_categories(conn, document_id)
    ]


def _links_payload(conn: sqlite3.Connection, document_id: int) -> list[dict[str, Any]]:
    return [
        {
            "target_logical_path": link["target_logical_path"],
            "anchor_text": link["anchor_text"],
            "resolution": link["resolution"],
            "target_document_id": link["target_document_id"],
        }
        for link in list_document_links(conn, document_id)
    ]


def _shape_chunk_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    include_source_path: bool,
) -> dict[str, Any]:
    document_id = int(row["document_id"])
    payload: dict[str, Any] = {
        "document_uuid": row["document_uuid"],
        "repository": row["repository"],
        "logical_path": row["logical_path"],
        "document_state": row["document_state"],
        "projection_status": row["projection_status"],
        "source_kind": row["source_kind"],
        "revision_number": row["revision_number"],
        "content_hash": row["content_hash"],
        "chunk_id": row["chunk_id"],
        "chunk_ordinal": row["chunk_ordinal"],
        "heading_path": row["heading_path"] or "",
        "source_mtime_ms": row["source_mtime_ms"],
        "revision_created_at": row["revision_created_at"],
        "enriched_at": row["enriched_at"],
        "observed_at": row["observed_at"],
        "knowledge_node_uuid": row["knowledge_node_uuid"] or "",
        "evidence_excerpt": _truncate(row["chunk_content"] or ""),
        "memory_categories": _categories_payload(conn, document_id),
        "memory_links": _links_payload(conn, document_id),
    }
    if include_source_path:
        payload["source_path"] = row["source_path"]
    return payload


def _shape_status_row(row: sqlite3.Row, *, include_source_path: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "document_uuid": row["document_uuid"],
        "repository": row["repository"],
        "logical_path": row["logical_path"],
        "document_state": row["document_state"],
        "projection_status": row["projection_status"],
        "source_kind": row["source_kind"],
        "observed_at": row["observed_at"],
        "updated_at": row["updated_at"],
        "last_error": row["last_error"],
        "evidence_excerpt": "",
    }
    if include_source_path:
        payload["source_path"] = row["source_path"]
    return payload


def _build_current_filters(req: MemoryQueryRequest) -> tuple[str, list[Any]]:
    clauses: list[str] = ["d.source_kind = ?"]
    params: list[Any] = [req.source_kind or SOURCE_KIND]

    if req.include_non_queryable:
        pass  # status stubs are appended separately; chunk query stays strict.
    else:
        clauses.append("d.state = 'active'")
        clauses.append("d.active_revision_id IS NOT NULL")

    if req.repository:
        clauses.append("d.repository = ?")
        params.append(req.repository)
    if req.logical_path:
        clauses.append("d.logical_path = ?")
        params.append(req.logical_path)
    if req.document_uuid:
        clauses.append("d.uuid = ?")
        params.append(req.document_uuid)

    since_ms = parse_since(req.since)
    if since_ms:
        clauses.append("d.updated_at >= ?")
        params.append(since_ms)

    if req.category:
        validate_memory_category_filter(req.category)
        clauses.append(
            "EXISTS (SELECT 1 FROM memory_document_categories mdc "
            "WHERE mdc.document_id = d.id AND mdc.category = ?)"
        )
        params.append(req.category)

    if req.query:
        pattern = f"%{req.query}%"
        clauses.append(
            "(mc.content LIKE ? OR COALESCE(r.summary, '') LIKE ? "
            "OR COALESCE(r.diff_text, '') LIKE ?)"
        )
        params.extend([pattern, pattern, pattern])

    return " AND ".join(clauses), params


def query_memory_current(
    conn: sqlite3.Connection,
    req: MemoryQueryRequest,
) -> dict[str, Any]:
    """Return bounded current auto-memory chunks (or status stubs when non-queryable)."""
    conn.row_factory = sqlite3.Row
    if not table_exists(conn, "memory_documents"):
        return {
            "view": "current",
            "results": [],
            "limit": req.limit,
            "offset": req.offset,
            "truncated": False,
        }

    limit = resolve_limit(req.limit, default=20)
    offset = max(req.offset, 0)

    where_sql, params = _build_current_filters(req)

    sql = (
        "SELECT d.id AS document_id, d.uuid AS document_uuid, d.repository, "
        "d.logical_path, d.source_path, d.state AS document_state, "
        "d.projection_status, d.source_kind, d.observed_at, "
        "r.revision_number, r.content_hash, r.source_mtime_ms, r.created_at AS revision_created_at, "
        "r.enriched_at, mc.id AS chunk_id, mc.ordinal AS chunk_ordinal, "
        "mc.heading_path, mc.content AS chunk_content, kn.uuid AS knowledge_node_uuid "
        "FROM memory_documents d "
        "JOIN memory_revisions r ON r.id = d.active_revision_id "
        "JOIN memory_chunks mc ON mc.revision_id = r.id "
        "LEFT JOIN knowledge_node_memory_chunks knmc ON knmc.memory_chunk_id = mc.id "
        "LEFT JOIN knowledge_nodes kn ON kn.id = knmc.knowledge_node_id "
        f"WHERE {where_sql} "
        "ORDER BY d.updated_at DESC, mc.ordinal ASC LIMIT ? OFFSET ?"
    )
    rows = conn.execute(sql, (*params, limit + 1, offset)).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    results: list[dict[str, Any]] = [
        _shape_chunk_row(conn, row, include_source_path=req.include_source_path) for row in rows
    ]

    if req.include_non_queryable and not results:
        status_where, status_params = _build_status_filters(req)
        status_sql = (
            "SELECT d.id AS document_id, d.uuid AS document_uuid, d.repository, "
            "d.logical_path, d.source_path, d.state AS document_state, "
            "d.projection_status, d.source_kind, d.observed_at, d.updated_at, d.last_error "
            "FROM memory_documents d "
            f"WHERE {status_where} "
            "AND (d.state != 'active' OR d.projection_status NOT IN ('ready', 'stale') "
            "OR d.active_revision_id IS NULL) "
            "ORDER BY d.updated_at DESC LIMIT ? OFFSET ?"
        )
        status_rows = conn.execute(status_sql, (*status_params, limit + 1, offset)).fetchall()
        truncated = len(status_rows) > limit
        status_rows = status_rows[:limit]
        results = [
            _shape_status_row(row, include_source_path=req.include_source_path)
            for row in status_rows
        ]
        return {
            "view": "current",
            "results": results,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
        }

    return {
        "view": "current",
        "results": results,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
    }


def _build_status_filters(req: MemoryQueryRequest) -> tuple[str, list[Any]]:
    clauses: list[str] = ["d.source_kind = ?"]
    params: list[Any] = [req.source_kind or SOURCE_KIND]
    if req.repository:
        clauses.append("d.repository = ?")
        params.append(req.repository)
    if req.logical_path:
        clauses.append("d.logical_path = ?")
        params.append(req.logical_path)
    if req.document_uuid:
        clauses.append("d.uuid = ?")
        params.append(req.document_uuid)
    since_ms = parse_since(req.since)
    if since_ms:
        clauses.append("d.updated_at >= ?")
        params.append(since_ms)
    if req.category:
        validate_memory_category_filter(req.category)
        clauses.append(
            "EXISTS (SELECT 1 FROM memory_document_categories mdc "
            "WHERE mdc.document_id = d.id AND mdc.category = ?)"
        )
        params.append(req.category)
    return " AND ".join(clauses), params


def run_memory_history_query(
    conn: sqlite3.Connection,
    *,
    repository: str = "",
    logical_path: str = "",
    document_uuid: str = "",
    limit: int = 50,
    include_source_path: bool = False,
) -> dict[str, Any]:
    """Explicit bounded revision history (never mixed into current results)."""
    limit = resolve_limit(limit, default=50)
    conn.row_factory = sqlite3.Row
    history = query_memory_history(
        conn,
        repository=repository or None,
        logical_path=logical_path or None,
        document_uuid=document_uuid or None,
        limit=limit,
    )
    results: list[dict[str, Any]] = []
    for row in history:
        item = {
            "document_uuid": row["document_uuid"],
            "repository": row["repository"],
            "logical_path": row["logical_path"],
            "document_state": row["document_state"],
            "revision_number": row["revision_number"],
            "content_hash": row["content_hash"],
            "change_kind": row["change_kind"],
            "summary": row["summary"],
            "diff_text": row["diff_text"],
            "source_mtime_ms": row["source_mtime_ms"],
            "revision_created_at": row["created_at"],
            "enriched_at": row["enriched_at"],
        }
        if include_source_path:
            item["source_path"] = row["source_path"]
        results.append(item)
    return {
        "view": "history",
        "results": results,
        "limit": limit,
        "offset": 0,
        "truncated": len(results) >= limit,
    }


def _default_db_path() -> Path:
    return Path.home() / ".local" / "share" / "hippo" / "hippo.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hippo-memory-query",
        description="Query Claude auto-memory documents (current or explicit history).",
    )
    parser.add_argument("query", nargs="?", default="", help="Optional text filter")
    parser.add_argument("--repository", default="")
    parser.add_argument("--category", default="")
    parser.add_argument("--logical-path", default="")
    parser.add_argument("--document-uuid", default="")
    parser.add_argument("--since", default="", help="Freshness window like 24h or 7d")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--history", action="store_true", help="Return revision history")
    parser.add_argument(
        "--include-non-queryable",
        action="store_true",
        help="Include pending/failed/unavailable status stubs",
    )
    parser.add_argument(
        "--include-source-path",
        action="store_true",
        help="Include absolute local paths (local diagnostics only)",
    )
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)

    db_path = args.db or _default_db_path()
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if args.history:
            if not args.document_uuid and not (args.repository and args.logical_path):
                parser.error(
                    "--history requires --document-uuid or --repository and --logical-path"
                )
            payload = run_memory_history_query(
                conn,
                repository=args.repository,
                logical_path=args.logical_path,
                document_uuid=args.document_uuid,
                limit=args.limit,
                include_source_path=args.include_source_path,
            )
        else:
            req = MemoryQueryRequest(
                query=args.query,
                repository=args.repository,
                category=args.category,
                logical_path=args.logical_path,
                document_uuid=args.document_uuid,
                since=args.since,
                limit=args.limit,
                offset=args.offset,
                include_non_queryable=args.include_non_queryable,
                include_source_path=args.include_source_path,
            )
            payload = query_memory_current(conn, req)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        conn.close()

    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
