"""Parse Opencode sessions for brain enrichment.

The daemon writes opencode sessions to the `agentic_sessions` table.
This module provides segment-building and claim logic for the brain's
enrichment pipeline, parallel to `claude_sessions.py` and `codex_sessions.py`.
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from hippo_brain.enrichment import (
    CURRENT_ENRICHMENT_VERSION,
    is_enrichment_eligible,
    upsert_entities,
)
from hippo_brain.entity_resolver import strip_worktree_prefix
from hippo_brain.models import EnrichmentResult
from hippo_brain.watchdog import DEFAULT_LOCK_TIMEOUT_MS

# 5-minute gap between sessions = distinct work units
TASK_GAP_MS = 5 * 60 * 1000

OPENCODE_ENRICHMENT_PROMPT = """You are a developer activity analyst. You receive a summary of an Opencode AI coding assistant session.
Opencode saves sessions as a SQLite database, and Hippo reads the session metadata, model info, agent type, and diff summaries.

Produce structured enrichment data capturing the knowledge from this work session.

IMPORTANT: Be specific. Use actual file names, function names, error messages, and outcomes.
The session may contain tool call summaries and snapshot diffs — extract the key technical decisions.

Output a JSON object with these fields:
- summary: Specific description of what was accomplished
- intent: One of "feature development", "debugging", "refactoring", "configuration", "maintenance"
- outcome: One of "success", "partial", "failure", "unknown"
- key_decisions: List of decisions made and why
- problems_encountered: List of errors/failures and how they were resolved
- entities: Tool/file/service identifiers extracted
- tags: Descriptive, specific tags
- embed_text: A detailed, identifier-dense paragraph optimized for keyword retrieval

Output ONLY valid JSON, no markdown fences or extra text."""


@dataclass
class OCSegment:
    """A parsed opencode session segment for enrichment."""
    session_id: str
    harness: str = "opencode"
    model: str = ""
    agent: str = ""
    project_dir: str = ""
    cwd: str = ""
    git_branch: str | None = None
    segment_index: int = 0
    start_time: int = 0  # epoch ms
    end_time: int = 0  # epoch ms
    slug: str = ""
    title: str = ""
    summary_text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    commit_messages: list[str] = field(default_factory=list)
    snapshot_diffs: dict | None = None
    message_count: int = 0
    token_count: int = 0
    source_file: str = ""


def iter_pending_opencode_segments(
    conn,
    max_claim_batch: int | None = None,
    stale_lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
) -> list[list[dict]]:
    """Claim pending opencode segments for enrichment.

    Returns a list of batches — each batch is a list of segment dicts ready
    for enrichment.
    """
    stale_before_ms = int(time.time() * 1000) - stale_lock_timeout_ms
    remaining = max_claim_batch if max_claim_batch is not None else -1

    cwd_groups = conn.execute(
        """
        SELECT cwd, COUNT(*) as cnt
        FROM agentic_sessions
        WHERE harness = 'opencode'
          AND enriched = 0
        GROUP BY cwd
        ORDER BY MIN(start_time) ASC
        """
    ).fetchall()

    all_batches = []
    for cwd, _ in cwd_groups:
        if remaining == 0:
            break
        limit = remaining if remaining > 0 else -1

        # Mark segments as enriched and get their IDs
        segment_ids = conn.execute(
            """
            UPDATE agentic_sessions
            SET enriched = 1
            WHERE id IN (
                SELECT id FROM agentic_sessions
                WHERE harness = 'opencode'
                  AND enriched = 0
                  AND cwd = ?
                ORDER BY start_time ASC
                LIMIT ?
            )
            RETURNING id
            """,
            (cwd, limit),
        ).fetchall()

        ids = [row[0] for row in segment_ids]
        if not ids:
            continue

        # Re-query for full segment data
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""
            SELECT id, session_id, harness, model, agent, slug, title,
                   start_time, end_time, summary_text, snapshot_diffs_json,
                   commit_messages_json, message_count, token_count
            FROM agentic_sessions
            WHERE id IN ({placeholders}) AND harness = 'opencode'
            ORDER BY start_time ASC
            """,
            ids,
        ).fetchall()

        segments = []
        for row in rows:
            sid, session_id, harness, model, agent, slug, title, _start, _end, summary_text, snap_json, commit_json, msg_count, tok_count = row

            diffs = json.loads(snap_json) if snap_json and snap_json != "null" else None
            commits = json.loads(commit_json) if commit_json else []

            segments.append({
                "id": sid,
                "session_id": session_id,
                "harness": harness,
                "model": model or "",
                "agent": agent or "",
                "cwd": cwd,
                "slug": slug or "",
                "title": title or "",
                "start_time": _start,
                "end_time": _end,
                "summary_text": summary_text,
                "snapshot_diffs": diffs,
                "commit_messages": commits,
                "message_count": msg_count or 0,
                "token_count": tok_count or 0,
            })

        segments = _skip_ineligible_opencode_segments(conn, segments)
        all_batches.extend([seg] for seg in segments)

    return all_batches


def _skip_ineligible_opencode_segments(
    conn,
    segments: list[dict],
) -> list[dict]:
    """Mark ineligible segments, return eligible ones."""
    eligible = []
    now_ms = int(time.time() * 1000)
    for seg in segments:
        ok, reason = is_enrichment_eligible(seg, "opencode")
        if ok:
            eligible.append(seg)
        else:
            conn.execute(
                "UPDATE agentic_sessions SET enriched = 1 WHERE id = ?",
                (seg["id"],),
            )
    conn.commit()
    return eligible


def build_opencode_enrichment_prompt(segments: list[OCSegment]) -> str:
    """Format opencode segments into an enrichment prompt."""
    parts = []
    for seg in segments:
        cwd = strip_worktree_prefix(seg.cwd)
        header = (
            f"Opencode session segment (project: {cwd}, slug: {seg.slug})"
        )
        if seg.start_time and seg.end_time:
            start = datetime.fromtimestamp(seg.start_time / 1000).strftime("%Y-%m-%d %H:%M")
            end = datetime.fromtimestamp(seg.end_time / 1000).strftime("%H:%M")
            header += f"\nDuration: {start} - {end}"
        if seg.agent:
            header += f"\nAgent: {seg.agent}"
        if seg.model:
            header += f"\nModel: {seg.model}"

        lines = [header]

        if seg.snapshot_diffs:
            lines.append("")
            lines.append("Snapshot diffs:")
            diff = seg.snapshot_diffs
            lines.append(f"  +{diff.get('additions', 0)}/-{diff.get('deletions', 0)} lines, {diff.get('files', 0)} files")

        if seg.commit_messages:
            lines.append("")
            lines.append("Commit messages:")
            for cm in seg.commit_messages[:5]:
                lines.append(f"  {cm}")

        parts.append("\n".join(lines))

    return "\n---\n\n".join(parts)


def insert_opencode_segment(conn, segment: OCSegment) -> int | None:
    """Insert a parsed opencode segment. Returns segment id or None."""
    summary_text = build_opencode_enrichment_prompt([segment])
    now_ms = int(time.time() * 1000)

    try:
        cursor = conn.execute(
            """
            INSERT INTO opencode_segments
                (session_id, model, agent, cwd, slug, title,
                 start_time, end_time, summary_text, tool_calls_json,
                 commit_messages_json, snapshot_diffs, message_count,
                 token_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                segment.session_id,
                segment.model,
                segment.agent,
                segment.cwd,
                segment.slug,
                segment.title,
                segment.start_time,
                segment.end_time,
                summary_text,
                json.dumps(segment.tool_calls),
                json.dumps(segment.commit_messages),
                json.dumps(segment.snapshot_diffs) if segment.snapshot_diffs else None,
                segment.message_count,
                segment.token_count,
                now_ms,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            return None
        raise


def write_opencode_knowledge_node(
    conn,
    result: EnrichmentResult,
    segment_ids: list[int],
    model_name: str,
) -> int:
    """Insert knowledge node linked to opencode session metadata."""
    node_uuid = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)
    content = json.dumps({
        "summary": result.summary,
        "intent": result.intent,
        "outcome": result.outcome,
        "entities": result.entities,
        "tags": result.tags,
        "key_decisions": result.key_decisions,
        "problems_encountered": result.problems_encountered,
        "design_decisions": result.design_decisions,
    })
    tags_json = json.dumps(result.tags)

    conn.execute("BEGIN")
    try:
        cursor = conn.execute(
            """
            INSERT INTO knowledge_nodes (uuid, content, embed_text, node_type, outcome,
                                         tags, enrichment_model, enrichment_version,
                                         created_at, updated_at)
            VALUES (?, ?, ?, 'observation', ?, ?, ?, ?, ?, ?)
            """,
            (
                node_uuid,
                content,
                result.embed_text,
                result.outcome,
                tags_json,
                model_name,
                CURRENT_ENRICHMENT_VERSION,
                now_ms,
                now_ms,
            ),
        )
        node_id = cursor.lastrowid

        # Link knowledge node to opencode session (agentic_sessions table)
        placeholders = ",".join("?" * len(segment_ids))
        conn.execute(
            f"""
            INSERT INTO knowledge_node_agentic_sessions
                (knowledge_node_id, agentic_session_id)
            VALUES ({", ".join("?" for _ in segment_ids)});
            """,
            (node_id, *segment_ids),
        )

        # Mark segments as enriched
        placeholders = ",".join("?" * len(segment_ids))
        conn.execute(
            f"UPDATE agentic_sessions SET enriched = 1 WHERE id IN ({placeholders})",
            segment_ids,
        )

        upsert_entities(conn, node_id, result.entities, {}, now_ms)

        conn.commit()
        return node_id
    except Exception:
        conn.rollback()
        raise
