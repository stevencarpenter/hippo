"""Parse Opencode sessions for brain enrichment.

The daemon writes opencode sessions to the `agentic_sessions` table and
enqueues a `pending` row in `agentic_enrichment_queue` per ingested session.
This module provides the claim → eligibility-filter → knowledge-node-write
flow that the brain's enrichment loop consumes, parallel to
`claude_sessions.py`.
"""

import json
import time
import uuid
from datetime import datetime

from hippo_brain.enrichment import (
    CURRENT_ENRICHMENT_VERSION,
    is_enrichment_eligible,
    upsert_entities,
)
from hippo_brain.entity_resolver import strip_worktree_prefix
from hippo_brain.models import EnrichmentResult
from hippo_brain.watchdog import DEFAULT_LOCK_TIMEOUT_MS as STALE_LOCK_TIMEOUT_MS

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


def claim_pending_opencode_segments(
    conn,
    worker_id: str,
    max_claim_batch: int | None = None,
    stale_lock_timeout_ms: int = STALE_LOCK_TIMEOUT_MS,
) -> list[list[dict]]:
    """Claim pending opencode segments via `agentic_enrichment_queue`.

    Mirrors `claim_pending_claude_segments`: groups by cwd, flips
    `status = 'processing'` with a worker-scoped lock, returns one batch per
    segment (1:1 enrichment for maximum search granularity). Excludes
    `probe_tag IS NOT NULL` rows (AP-6).
    """
    now_ms = int(time.time() * 1000)
    stale_before_ms = now_ms - stale_lock_timeout_ms
    remaining = max_claim_batch if max_claim_batch is not None else -1

    cwd_groups = conn.execute(
        """
        SELECT s.cwd, COUNT(*) AS cnt
        FROM agentic_enrichment_queue q
        JOIN agentic_sessions s ON q.session_id = s.id
        WHERE (q.status = 'pending'
           OR (q.status = 'processing' AND COALESCE(q.locked_at, 0) <= ?))
          AND s.harness = 'opencode'
          AND s.probe_tag IS NULL
        GROUP BY s.cwd
        ORDER BY MIN(s.start_time) ASC
        """,
        (stale_before_ms,),
    ).fetchall()

    all_batches: list[list[dict]] = []
    for cwd, _ in cwd_groups:
        if remaining == 0:
            break
        limit = remaining if remaining > 0 else -1
        cursor = conn.execute(
            """
            UPDATE agentic_enrichment_queue
            SET status = 'processing', locked_at = ?, locked_by = ?, updated_at = ?
            WHERE id IN (
                SELECT q.id FROM agentic_enrichment_queue q
                JOIN agentic_sessions s ON q.session_id = s.id
                WHERE s.cwd = ?
                  AND s.harness = 'opencode'
                  AND s.probe_tag IS NULL
                  AND (q.status = 'pending'
                       OR (q.status = 'processing' AND COALESCE(q.locked_at, 0) <= ?))
                ORDER BY s.start_time ASC, q.id ASC
                LIMIT ?
            )
            RETURNING session_id
            """,
            (now_ms, worker_id, now_ms, cwd, stale_before_ms, limit),
        )
        segment_ids = [row[0] for row in cursor.fetchall()]
        conn.commit()
        if not segment_ids:
            continue

        placeholders = ",".join("?" * len(segment_ids))
        rows = conn.execute(
            f"""
            SELECT id, session_id, harness, model, agent, slug, title,
                   start_time, end_time, summary_text, snapshot_diffs_json,
                   commit_messages_json, message_count, token_count, cwd
            FROM agentic_sessions
            WHERE id IN ({placeholders}) AND harness = 'opencode' AND probe_tag IS NULL
            ORDER BY start_time ASC
            """,
            segment_ids,
        ).fetchall()

        segments = []
        for row in rows:
            (
                sid,
                session_id,
                harness,
                model,
                agent,
                slug,
                title,
                start_time,
                end_time,
                summary_text,
                snap_json,
                commit_json,
                msg_count,
                tok_count,
                row_cwd,
            ) = row
            diffs = json.loads(snap_json) if snap_json and snap_json != "null" else None
            commits = json.loads(commit_json) if commit_json else []
            segments.append(
                {
                    "id": sid,
                    "session_id": session_id,
                    "harness": harness,
                    "model": model or "",
                    "agent": agent or "",
                    "cwd": row_cwd,
                    "slug": slug or "",
                    "title": title or "",
                    "start_time": start_time,
                    "end_time": end_time,
                    "summary_text": summary_text,
                    "snapshot_diffs": diffs,
                    "commit_messages": commits,
                    "message_count": msg_count or 0,
                    "token_count": tok_count or 0,
                }
            )

        segments = _skip_ineligible_opencode_segments(conn, segments)
        # Decrement the global cap by the number of *eligible* segments only.
        # If 10 rows were claimed but 8 were ineligible, only 2 enrichable
        # batches reach the LLM — `max_claim_batch` should reflect what the
        # caller actually gets, not what we scanned.
        if remaining > 0:
            remaining -= len(segments)
        all_batches.extend([seg] for seg in segments)

    return all_batches


def _skip_ineligible_opencode_segments(conn, segments: list[dict]) -> list[dict]:
    """Mark ineligible opencode segments as skipped in the queue; return the rest."""
    eligible = []
    now_ms = int(time.time() * 1000)
    for seg in segments:
        ok, reason = is_enrichment_eligible(seg, "opencode")
        if ok:
            eligible.append(seg)
        else:
            conn.execute(
                "UPDATE agentic_enrichment_queue "
                "SET status = 'skipped', error_message = ?, "
                "    locked_at = NULL, locked_by = NULL, updated_at = ? "
                "WHERE session_id = ?",
                (reason, now_ms, seg["id"]),
            )
            conn.execute(
                "UPDATE agentic_sessions SET enriched = 1 WHERE id = ?",
                (seg["id"],),
            )
    if len(eligible) != len(segments):
        conn.commit()
    return eligible


def build_opencode_enrichment_prompt(segments: list[dict]) -> str:
    """Format opencode segment dicts into an enrichment prompt body."""
    parts = []
    for seg in segments:
        cwd = strip_worktree_prefix(seg.get("cwd", ""))
        header = f"Opencode session segment (project: {cwd}, slug: {seg.get('slug', '')})"
        if seg.get("start_time") and seg.get("end_time"):
            start = datetime.fromtimestamp(seg["start_time"] / 1000).strftime("%Y-%m-%d %H:%M")
            end = datetime.fromtimestamp(seg["end_time"] / 1000).strftime("%H:%M")
            header += f"\nDuration: {start} - {end}"
        if seg.get("agent"):
            header += f"\nAgent: {seg['agent']}"
        if seg.get("model"):
            header += f"\nModel: {seg['model']}"

        lines = [header]
        diffs = seg.get("snapshot_diffs")
        if diffs:
            lines.append("")
            lines.append("Snapshot diffs:")
            lines.append(
                f"  +{diffs.get('additions', 0)}/-{diffs.get('deletions', 0)} lines, "
                f"{diffs.get('files', 0)} files"
            )
        commits = seg.get("commit_messages") or []
        if commits:
            lines.append("")
            lines.append("Commit messages:")
            for cm in commits[:5]:
                lines.append(f"  {cm}")
        parts.append("\n".join(lines))
    return "\n---\n\n".join(parts)


def write_opencode_knowledge_node(
    conn,
    result: EnrichmentResult,
    segment_ids: list[int],
    model_name: str,
) -> int:
    """Insert knowledge node, link to opencode session(s), mark queue done."""
    node_uuid = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)
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
        }
    )
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

        # Link knowledge node to each opencode session segment. One row per
        # link (junction table is 2 columns; never collapse into a single
        # VALUES row).
        conn.executemany(
            "INSERT INTO knowledge_node_agentic_sessions "
            "(knowledge_node_id, agentic_session_id) VALUES (?, ?)",
            [(node_id, sid) for sid in segment_ids],
        )

        # Mark segments as enriched only after the knowledge node has been
        # written — if anything fails, the BEGIN/rollback below keeps the
        # segments in their pre-enriched state.
        placeholders = ",".join("?" * len(segment_ids))
        conn.execute(
            f"UPDATE agentic_sessions SET enriched = 1 WHERE id IN ({placeholders})",
            segment_ids,
        )

        # Close out the queue entry.
        conn.execute(
            f"UPDATE agentic_enrichment_queue SET status = 'done', updated_at = ? "
            f"WHERE session_id IN ({placeholders})",
            [now_ms, *segment_ids],
        )

        upsert_entities(conn, node_id, result.entities, {}, now_ms)

        conn.commit()
        return node_id
    except Exception:
        conn.rollback()
        raise


def mark_opencode_queue_failed(conn, segment_ids: list[int], error: str) -> None:
    """Increment retry_count on the queue entries; flip to 'failed' once exhausted."""
    now_ms = int(time.time() * 1000)
    for seg_id in segment_ids:
        conn.execute(
            """
            UPDATE agentic_enrichment_queue
            SET retry_count   = retry_count + 1,
                error_message = ?,
                locked_at     = NULL,
                locked_by     = NULL,
                updated_at    = ?,
                status        = CASE
                                    WHEN retry_count + 1 >= max_retries THEN 'failed'
                                    ELSE 'pending'
                                END
            WHERE session_id = ?
            """,
            (error, now_ms, seg_id),
        )
    conn.commit()
