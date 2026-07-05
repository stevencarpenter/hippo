"""Inspectable evidence packets for agent retrieval (SNUG-123).

Stable citation objects attached to :class:`~hippo_brain.retrieval.SearchResult`
and MCP ``search_hybrid`` / ``_retrieve_filtered`` responses. Honors retrieval
eligibility by construction — only eligible linked rows are hydrated.

Operator debug: ``hippo-evidence-inspect <ref>`` prints the backing SQLite row.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hippo_brain.retrieval_eligibility import (
    agentic_session_eligible_sql,
    browser_event_eligible_sql,
    include_excluded_from_env,
    shell_event_eligible_sql,
    workflow_run_eligible_sql,
)
from hippo_brain.source_filters import source_kind_from_linked_id

_REF_RE = re.compile(r"^(shell|claude|codex|cursor|opencode|browser|workflow|memory)-(\d+)$")


@dataclass(frozen=True)
class EvidencePacket:
    """One inspectable citation backing a knowledge node."""

    ref: str
    source_kind: str
    table: str
    row_id: int
    timestamp_ms: int
    excerpt: str
    rank: int = 0
    retrieval_score: float | None = None
    session_id: str | None = None
    harness: str | None = None
    segment_index: int | None = None
    source_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_ref(ref: str) -> tuple[str, int]:
    match = _REF_RE.match(ref.strip())
    if not match:
        raise ValueError(f"invalid evidence ref: {ref!r}")
    return match.group(1), int(match.group(2))


def attach_retrieval_scores(packets: list[dict[str, Any]], score: float) -> list[dict[str, Any]]:
    rounded = round(max(0.0, min(1.0, score)), 4)
    return [{**pkt, "rank": rank, "retrieval_score": rounded} for rank, pkt in enumerate(packets)]


def _truncate(text: str | None, limit: int = 500) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def make_shell_packet(
    *,
    event_id: int,
    timestamp_ms: int,
    command: str | None,
    source_kind: str | None,
    ref: str | None = None,
) -> dict[str, Any]:
    logical_kind = "claude-tool" if source_kind == "claude-tool" else "shell"
    return EvidencePacket(
        ref=ref or f"shell-{event_id}",
        source_kind=logical_kind,
        table="events",
        row_id=event_id,
        timestamp_ms=timestamp_ms,
        excerpt=_truncate(command),
    ).to_dict()


def make_agentic_packet(
    *,
    row_id: int,
    harness: str,
    session_id: str,
    segment_index: int,
    timestamp_ms: int,
    summary_text: str | None,
    source_path: str | None,
    ref: str | None = None,
) -> dict[str, Any]:
    prefix = "claude" if harness == "claude-code" else harness
    logical_kind = source_kind_from_linked_id(f"{prefix}-{row_id}") or prefix
    return EvidencePacket(
        ref=ref or f"{prefix}-{row_id}",
        source_kind=logical_kind,
        table="agentic_sessions",
        row_id=row_id,
        timestamp_ms=timestamp_ms,
        excerpt=_truncate(summary_text),
        session_id=session_id or None,
        harness=harness,
        segment_index=segment_index,
        source_path=source_path or None,
    ).to_dict()


def make_browser_packet(
    *,
    event_id: int,
    timestamp_ms: int,
    title: str | None,
    url: str | None,
    domain: str | None,
) -> dict[str, Any]:
    excerpt = title or url or domain or ""
    if domain and title:
        excerpt = f"{domain} — {title}"
    return EvidencePacket(
        ref=f"browser-{event_id}",
        source_kind="browser",
        table="browser_events",
        row_id=event_id,
        timestamp_ms=timestamp_ms,
        excerpt=_truncate(excerpt),
    ).to_dict()


def make_workflow_packet(
    *,
    run_id: int,
    timestamp_ms: int,
    name: str | None,
    repo: str | None,
    conclusion: str | None,
) -> dict[str, Any]:
    parts = [p for p in (repo, name, conclusion) if p]
    excerpt = " / ".join(parts) if parts else f"workflow run {run_id}"
    return EvidencePacket(
        ref=f"workflow-{run_id}",
        source_kind="workflow",
        table="workflow_runs",
        row_id=run_id,
        timestamp_ms=timestamp_ms,
        excerpt=_truncate(excerpt),
    ).to_dict()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def inspect_evidence(
    conn: sqlite3.Connection,
    ref: str,
    *,
    include_excluded: bool = False,
) -> dict[str, Any]:
    """Load the backing SQLite row for an evidence ``ref`` (operator/debug)."""
    kind, row_id = parse_ref(ref)
    include_excluded = include_excluded or include_excluded_from_env()
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return _inspect_evidence_row(conn, kind, row_id, ref, include_excluded=include_excluded)
    finally:
        conn.row_factory = previous_factory


def _inspect_evidence_row(
    conn: sqlite3.Connection,
    kind: str,
    row_id: int,
    ref: str,
    *,
    include_excluded: bool,
) -> dict[str, Any]:
    if kind == "shell":
        row = conn.execute(
            f"""
            SELECT e.* FROM events e
            WHERE e.id = ?
              AND {shell_event_eligible_sql("e", include_excluded=include_excluded, conn=conn)}
            """,
            (row_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"no eligible events row for {ref}")
        return {"ref": ref, "table": "events", "row": _row_to_dict(row)}

    if kind in ("claude", "codex", "cursor", "opencode"):
        harness = "claude-code" if kind == "claude" else kind
        row = conn.execute(
            f"""
            SELECT asx.* FROM agentic_sessions asx
            WHERE asx.id = ? AND asx.harness = ?
              AND {agentic_session_eligible_sql("asx", include_excluded=include_excluded)}
            """,
            (row_id, harness),
        ).fetchone()
        if row is None:
            raise LookupError(f"no eligible agentic_sessions row for {ref}")
        return {"ref": ref, "table": "agentic_sessions", "row": _row_to_dict(row)}

    if kind == "browser":
        row = conn.execute(
            f"""
            SELECT be.* FROM browser_events be
            WHERE be.id = ?
              AND {browser_event_eligible_sql("be", include_excluded=include_excluded)}
            """,
            (row_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"no eligible browser_events row for {ref}")
        return {"ref": ref, "table": "browser_events", "row": _row_to_dict(row)}

    if kind == "workflow":
        row = conn.execute(
            f"""
            SELECT wr.* FROM workflow_runs wr
            WHERE wr.id = ?
              AND {workflow_run_eligible_sql("wr", include_excluded=include_excluded)}
            """,
            (row_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"no eligible workflow_runs row for {ref}")
        return {"ref": ref, "table": "workflow_runs", "row": _row_to_dict(row)}

    if kind == "memory":
        row = conn.execute(
            """
            SELECT mc.*, md.repository, md.source_path, md.state
            FROM memory_chunks mc
            JOIN memory_revisions mr ON mr.id = mc.revision_id
            JOIN memory_documents md ON md.id = mr.document_id
            WHERE mc.id = ? AND md.state = 'active' AND md.active_revision_id = mr.id
            """,
            (row_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"no active memory_chunks row for {ref}")
        return {"ref": ref, "table": "memory_chunks", "row": _row_to_dict(row)}

    raise ValueError(f"unsupported evidence kind: {kind!r}")


def _default_db_path() -> Path:
    data_home = Path.home() / ".local" / "share" / "hippo"
    return data_home / "hippo.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hippo-evidence-inspect",
        description="Print the raw SQLite row backing an evidence packet ref.",
    )
    parser.add_argument("ref", help="Evidence ref, e.g. shell-42 or claude-7")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to hippo.db (default: ~/.local/share/hippo/hippo.db)",
    )
    parser.add_argument(
        "--include-excluded",
        action="store_true",
        help="Include probe/in-flight rows (or set HIPPO_RETRIEVAL_INCLUDE_EXCLUDED=1)",
    )
    args = parser.parse_args(argv)

    db_path = args.db or _default_db_path()
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        payload = inspect_evidence(conn, args.ref, include_excluded=args.include_excluded)
    except (LookupError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        conn.close()

    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
