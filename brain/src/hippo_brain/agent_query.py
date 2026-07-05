"""Compact agent-oriented query API over evidence packets (SNUG-124).

Single entry surface for agent moves: what is known, show evidence, recent
changes, and prior decisions — each returns a bounded answer plus evidence
packets and lightweight source-health freshness hints.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from hippo_brain.mcp_queries import MAX_LIMIT, parse_since
from hippo_brain.retrieval import Filters, SearchResult, search
from hippo_brain.retrieval_eligibility import include_excluded_from_env
from hippo_brain.source_freshness import freshness_for_evidence_packets
from hippo_brain.source_filters import CLAUDE_AUTO_MEMORY_SOURCE

AGENT_QUERY_MODES = frozenset({"known", "evidence", "recent", "decisions"})
AGENT_QUERY_SOURCES = frozenset(
    {"", "shell", "claude", "browser", "workflow", CLAUDE_AUTO_MEMORY_SOURCE}
)

DEFAULT_LIMIT = 10
MAX_ANSWER_CHARS = 2000
MAX_SUMMARY_CHARS = 400
DECISIONS_CANDIDATE_MULTIPLIER = 3


@dataclass(frozen=True)
class AgentQueryRequest:
    query: str
    mode: str = "known"
    source: str = ""
    since: str = ""
    project: str = ""
    branch: str = ""
    limit: int = DEFAULT_LIMIT
    include_excluded: bool = False


def clamp_limit(limit: int) -> int:
    if limit <= 0:
        return 0
    return min(limit, MAX_LIMIT)


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max(max_len - 1, 1)] + "…"


def _filters_from_request(req: AgentQueryRequest) -> Filters:
    since_ms = parse_since(req.since)
    return Filters(
        project=req.project or None,
        since_ms=since_ms or None,
        source=req.source or None,
        branch=req.branch or None,
        include_excluded=req.include_excluded or include_excluded_from_env(),
    )


def _retrieval_mode(req: AgentQueryRequest) -> str:
    if req.mode == "recent":
        return "recent"
    return "hybrid"


def _compact_hit(result: SearchResult, *, include_decisions: bool) -> dict[str, Any]:
    hit: dict[str, Any] = {
        "uuid": result.uuid,
        "score": result.score,
        "summary": _truncate(result.summary or "", MAX_SUMMARY_CHARS),
        "captured_at": result.captured_at,
        "cwd": result.cwd,
        "git_branch": result.git_branch,
        "outcome": result.outcome,
        "evidence": list(result.evidence),
    }
    if include_decisions and result.design_decisions:
        hit["design_decisions"] = list(result.design_decisions)
    return hit


def _compose_known_answer(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "No matching knowledge found."
    lines = [f"- {_truncate(h.get('summary', ''), 300)}" for h in hits[:5]]
    return _truncate("\n".join(lines), MAX_ANSWER_CHARS)


def _compose_evidence_answer(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "No evidence found for this query."
    total_packets = sum(len(h.get("evidence") or []) for h in hits)
    return (
        f"Found {len(hits)} knowledge hit(s) with {total_packets} evidence packet(s). "
        "See hits[].evidence for inspectable citations."
    )


def _compose_decisions_answer(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "No prior design decisions matched this query."
    count = sum(len(h.get("design_decisions") or []) for h in hits)
    return f"Found {len(hits)} knowledge node(s) documenting {count} design decision(s)."


def _compose_answer(mode: str, hits: list[dict[str, Any]]) -> str:
    if mode == "evidence":
        return _compose_evidence_answer(hits)
    if mode == "decisions":
        return _compose_decisions_answer(hits)
    if not hits:
        if mode == "recent":
            return "No recent knowledge matched this topic."
        return "No matching knowledge found."
    return _compose_known_answer(hits)


def run_agent_query(
    conn: sqlite3.Connection,
    req: AgentQueryRequest,
    query_vec: Sequence[float] | None = None,
    *,
    backend=None,
) -> dict[str, Any]:
    """Execute a compact agent query and return a bounded response dict."""
    if req.mode not in AGENT_QUERY_MODES:
        raise ValueError(f"unknown mode: {req.mode!r}")
    if req.source not in AGENT_QUERY_SOURCES:
        raise ValueError(f"unsupported source filter: {req.source!r}")

    limit = clamp_limit(req.limit)
    if limit == 0:
        return {
            "mode": req.mode,
            "query": req.query,
            "answer": "limit must be greater than 0",
            "hits": [],
            "freshness": {},
            "limit": 0,
            "truncated": False,
        }

    filters = _filters_from_request(req)
    fetch_limit = limit
    if req.mode == "decisions":
        fetch_limit = min(limit * DECISIONS_CANDIDATE_MULTIPLIER, MAX_LIMIT)

    results = search(
        conn,
        req.query,
        query_vec,
        filters,
        mode=_retrieval_mode(req),
        limit=fetch_limit,
        backend=backend,
    )
    if req.mode == "decisions":
        results = [r for r in results if r.design_decisions]

    truncated = len(results) > limit
    results = results[:limit]
    include_decisions = req.mode == "decisions"
    hits = [_compact_hit(r, include_decisions=include_decisions) for r in results]

    all_packets: list[dict[str, Any]] = []
    for hit in hits:
        all_packets.extend(hit.get("evidence") or [])

    return {
        "mode": req.mode,
        "query": req.query,
        "answer": _compose_answer(req.mode, hits),
        "hits": hits,
        "freshness": freshness_for_evidence_packets(conn, all_packets),
        "limit": limit,
        "truncated": truncated,
    }


def _default_db_path() -> Path:
    return Path.home() / ".local" / "share" / "hippo" / "hippo.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hippo-agent-query",
        description="Compact agent query API (answer + evidence packets).",
    )
    parser.add_argument("query", help="Natural language query")
    parser.add_argument(
        "--mode",
        choices=sorted(AGENT_QUERY_MODES),
        default="known",
        help="known | evidence | recent | decisions",
    )
    parser.add_argument(
        "--source", default="", help="shell|claude|browser|workflow|claude-auto-memory"
    )
    parser.add_argument("--since", default="", help="Time window like 24h, 7d")
    parser.add_argument("--project", default="")
    parser.add_argument("--branch", default="")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--include-excluded", action="store_true")
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)

    req = AgentQueryRequest(
        query=args.query,
        mode=args.mode,
        source=args.source,
        since=args.since,
        project=args.project,
        branch=args.branch,
        limit=args.limit,
        include_excluded=args.include_excluded,
    )

    db_path = args.db or _default_db_path()
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 1

    try:
        from hippo_brain.vector_store import open_conn

        conn = open_conn(str(db_path))
    except ImportError:
        conn = sqlite3.connect(str(db_path))

    try:
        payload = run_agent_query(conn, req)
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
