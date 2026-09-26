"""Frozen source-backed summaries for advisory enrichment fidelity review."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from hippo_brain.bench.knowledge_probe import open_readonly
from hippo_brain.decision_capture import external_path
from hippo_brain.evidence_packets import _inspect_evidence_row, parse_ref
from hippo_brain.jev import canonical, digest, redact_state

VERSION = "claim-packets-v2"
LINKS = (
    ("knowledge_node_events", "event_id", "shell"),
    ("knowledge_node_agentic_sessions", "agentic_session_id", "agentic"),
    ("knowledge_node_browser_events", "browser_event_id", "browser"),
    ("knowledge_node_workflow_runs", "run_id", "workflow"),
    ("knowledge_node_memory_chunks", "memory_chunk_id", "memory"),
)
TEXT_FIELDS = (
    "command",
    "stdout",
    "stderr",
    "summary_text",
    "user_prompts_json",
    "tool_calls_json",
    "extracted_text",
    "title",
    "raw_json",
    "annotations_json",
    "content",
)
CONTEXT_FIELDS = (
    "session_id",
    "harness",
    "segment_index",
    "cwd",
    "project_dir",
    "git_branch",
    "repo",
    "head_sha",
    "head_branch",
    "url",
    "repository",
    "timestamp",
    "start_time",
    "started_at",
    "end_time",
    "completed_at",
    "created_at",
    "exit_code",
    "status",
    "conclusion",
    "stdout_truncated",
    "stderr_truncated",
)


def write_artifact(path: Path, value: Any) -> None:
    """Write a private immutable artifact outside repository/production trees."""
    path = external_path(path)
    body = (canonical(value) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".claim-", suffix=".tmp") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
        # link publishes a complete file atomically and refuses an existing destination.
        os.link(stream.name, path)


def create_directory(path: Path) -> Path:
    path = external_path(path)
    path.mkdir(parents=True, mode=0o700)
    return path


def _refs(conn: sqlite3.Connection, node_id: int) -> list[str]:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    refs = []
    for table, column, kind in LINKS:
        if table not in tables:
            continue
        for (row_id,) in conn.execute(
            f"SELECT {column} FROM {table} WHERE knowledge_node_id=? ORDER BY {column}",
            (node_id,),
        ):
            prefix = kind
            if kind == "agentic":
                row = conn.execute(
                    "SELECT harness FROM agentic_sessions WHERE id=?", (row_id,)
                ).fetchone()
                prefix = ("claude" if row[0] == "claude-code" else row[0]) if row else "missing"
            refs.append(f"{prefix}-{row_id}")
    return refs


def make_packet(conn: sqlite3.Connection, node_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT uuid, content, created_at FROM knowledge_nodes WHERE id=?", (node_id,)
    ).fetchone()
    if row is None:
        raise LookupError("knowledge node disappeared")
    problems = []
    try:
        content = json.loads(row[1])
        summary = content.get("summary") if isinstance(content, dict) else None
    except TypeError, ValueError:
        summary = None
    if not isinstance(summary, str) or not summary.strip():
        summary = ""
        problems.append("missing_summary")
    claim = redact_state(summary)
    if len(claim) > 4000:
        claim = claim[:4000]
        problems.append("truncated_claim")
    refs = _refs(conn, node_id)
    if not refs:
        problems.append("no_linked_sources")
    if len(refs) > 8:
        problems.append("truncated_source_set")
    sources = []
    for ref in refs[:8]:
        previous_factory = conn.row_factory
        try:
            conn.row_factory = sqlite3.Row
            kind, source_id = parse_ref(ref)
            # Do not honor the operator include-excluded environment override.
            original = _inspect_evidence_row(conn, kind, source_id, ref, include_excluded=False)[
                "row"
            ]
            if kind == "workflow":
                tables = {
                    r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if {"workflow_jobs", "workflow_annotations"} <= tables:
                    annotations = conn.execute(
                        "SELECT a.*, j.name AS job_name, j.status AS job_status, "
                        "j.conclusion AS job_conclusion FROM workflow_annotations a "
                        "JOIN workflow_jobs j ON j.id=a.job_id WHERE j.run_id=? "
                        "ORDER BY a.id LIMIT 101",
                        (source_id,),
                    ).fetchall()
                    if len(annotations) > 100:
                        problems.append(f"truncated_annotations:{ref}")
                    original["annotations_json"] = canonical([dict(a) for a in annotations[:100]])
                else:
                    problems.append(f"missing_workflow_annotations:{ref}")
        except LookupError, ValueError:
            problems.append(f"unavailable_source:{ref}")
            continue
        finally:
            conn.row_factory = previous_factory
        fields = {}
        for name in ("stdout_truncated", "stderr_truncated"):
            if original.get(name):
                problems.append(f"capture_truncated:{ref}:{name}")
        for name in (*CONTEXT_FIELDS, *TEXT_FIELDS):
            value = original.get(name)
            if value is None:
                continue
            if name.endswith("_json") and isinstance(value, str):
                try:
                    decoded = json.loads(value)
                    canonical(decoded)
                    value = decoded
                except ValueError:
                    problems.append(f"invalid_json_field:{ref}:{name}")
            clean = redact_state(value)
            bounded = clean if isinstance(clean, str) else canonical(clean)
            if len(bounded) > 6000:
                clean = bounded[:6000]
                problems.append(f"truncated_field:{ref}:{name}")
            fields[name] = clean
        if not any(fields.get(name) for name in TEXT_FIELDS):
            problems.append(f"empty_source:{ref}")
        sources.append({"ref": ref, "revision_hash": digest(original), "fields": fields})
    state = {"claim": claim, "sources": sources}
    if len(canonical(state).encode()) > 100_000:
        problems.append("oversized_state")
    body = {
        "version": VERSION,
        "node_uuid": row[0],
        "node_revision_hash": digest(row[1]),
        "created_at_ms": row[2],
        "source_refs": refs,
        "state": state,
        "problems": problems,
    }
    return {"packet_hash": digest(body), **body}


def validate_packet(packet: dict[str, Any]) -> None:
    if not isinstance(packet, dict) or packet.get("version") != VERSION:
        raise ValueError("unsupported claim packet")
    body = {key: value for key, value in packet.items() if key != "packet_hash"}
    if packet.get("packet_hash") != digest(body):
        raise ValueError("claim packet changed")
    state = packet.get("state")
    if (
        not isinstance(state, dict)
        or set(state) != {"claim", "sources"}
        or not isinstance(state["claim"], str)
        or not isinstance(state["sources"], list)
        or not isinstance(packet.get("problems"), list)
    ):
        raise ValueError("invalid claim packet state")


def prepare(database: Path, out: Path, *, limit: int = 50) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    with closing(open_readonly(database)) as conn:
        conn.execute("BEGIN")
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM knowledge_nodes ORDER BY created_at DESC, uuid LIMIT ?", (limit,)
            )
        ]
        packets = [make_packet(conn, node_id) for node_id in ids]
    manifest = {
        "version": VERSION,
        "created_at_ms": int(time.time() * 1000),
        "selection": "most_recent_nodes; descriptive cohort, not a representative holdout",
        "limit": limit,
        "packets_hash": digest(packets),
        "count": len(packets),
        "blocked": sum(bool(p["problems"]) for p in packets),
    }
    root = create_directory(out)
    write_artifact(root / "packets.json", packets)
    write_artifact(root / "manifest.json", manifest)
    return manifest


def load_packets(root: Path) -> list[dict[str, Any]]:
    manifest = json.loads((root / "manifest.json").read_text())
    packets = json.loads((root / "packets.json").read_text())
    if (
        manifest.get("version") != VERSION
        or not isinstance(packets, list)
        or manifest.get("packets_hash") != digest(packets)
        or manifest.get("count") != len(packets)
    ):
        raise ValueError("frozen packet manifest mismatch")
    seen = set()
    for packet in packets:
        validate_packet(packet)
        if packet["packet_hash"] in seen:
            raise ValueError("duplicate claim packet")
        seen.add(packet["packet_hash"])
    return packets


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    print(canonical(prepare(args.database, args.out, limit=args.limit)))


if __name__ == "__main__":
    main()
