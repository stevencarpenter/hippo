"""Change-outcome enrichment: join workflow_runs to co-temporal shell/claude events.

For each completed workflow run in the enrichment queue:
1. Find co-temporal shell events (same SHA or within ±15min window)
2. Find co-temporal Claude sessions (overlapping time window)
3. Fetch top failing annotations
4. Build a prompt → call LLM → write a knowledge node
5. Link the node to the run, shell events, and Claude sessions
6. For each failing annotation, call lessons.upsert_cluster
"""

import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path

from hippo_brain.client import InferenceClient
from hippo_brain.enrichment import parse_enrichment_response
from hippo_brain.lessons import ClusterKey, upsert_cluster
from hippo_brain.watchdog import DEFAULT_LOCK_TIMEOUT_MS

logger = logging.getLogger("hippo_brain")

CORRELATION_WINDOW_MS = 15 * 60 * 1000  # ±15 minutes

WORKFLOW_SYSTEM_PROMPT = """You are a CI/CD activity analyst. You receive a summary of a GitHub Actions workflow run and the developer activity around it.

Produce structured enrichment data capturing what changed, whether it succeeded, and — if it failed — the root cause and a one-line fix suggestion.

Be specific: use actual tool names, rule IDs, file paths, and error messages from the run data. Generic descriptions are unacceptable. If you are unsure of an exact identifier, omit it rather than guess.

Output a JSON object with these fields:
- summary: Specific description of what the run did and its outcome (include root cause + one-line fix if it failed)
- intent: The goal of the change under test (e.g., "ci debugging", "feature development", "dependency bump")
- outcome: One of "success", "partial", "failure", "unknown"
- key_decisions: List of notable decisions (empty list if none)
- problems_encountered: List of failures/errors and how they were (or should be) resolved
- entities: An object with lists of strings: projects, tools, files, services, errors, env_vars
- tags: Descriptive, specific tags
- embed_text: A detailed, identifier-dense paragraph optimized for keyword retrieval (tool names, rule IDs, file paths, error strings)

Output ONLY valid JSON, no markdown fences or extra text."""


def enrich_one(
    db_path: str,
    run_id: int,
    inference: InferenceClient,
    query_model: str,
    *,
    path_prefix_segments: int = 2,
    min_occurrences: int = 2,
) -> None:
    """Enrich a single workflow run: create knowledge node + update lessons."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            return

        now = int(time.time() * 1000)
        started = run["started_at"] or now

        # Co-temporal shell events: prefer exact SHA match; only fall back to time
        # window for push-related commands from the same repo.
        head_sha = run["head_sha"]
        repo_name = run["repo"]

        shell_rows = conn.execute(
            """SELECT id, command, git_commit FROM events
               WHERE git_commit = ?
                 AND probe_tag IS NULL
               LIMIT 20""",
            (head_sha,),
        ).fetchall()

        if not shell_rows:
            shell_rows = conn.execute(
                """SELECT id, command, git_commit FROM events
                   WHERE timestamp BETWEEN ? AND ?
                     AND command LIKE '%git push%'
                     AND (git_repo IS NULL OR git_repo = ?)
                     AND probe_tag IS NULL
                   LIMIT 20""",
                (
                    started - CORRELATION_WINDOW_MS,
                    started + CORRELATION_WINDOW_MS,
                    repo_name,
                ),
            ).fetchall()

        # Co-temporal Claude sessions
        claude_rows = conn.execute(
            """SELECT id, session_id, summary_text FROM claude_sessions
               WHERE start_time <= ? AND end_time >= ?
                 AND probe_tag IS NULL
               LIMIT 10""",
            (
                started + CORRELATION_WINDOW_MS,
                started - CORRELATION_WINDOW_MS,
            ),
        ).fetchall()

        # Top failing annotations (up to 10)
        ann_rows = conn.execute(
            """SELECT a.tool, a.rule_id, a.path, a.start_line, a.message
               FROM workflow_annotations a
               JOIN workflow_jobs j ON j.id = a.job_id
               WHERE j.run_id = ? AND a.level = 'failure'
               ORDER BY a.id LIMIT 10""",
            (run_id,),
        ).fetchall()

        # Build and run enrichment prompt
        prompt = _build_prompt(run, shell_rows, claude_rows, ann_rows)
        summary = inference.complete(model=query_model, prompt=prompt, max_tokens=300)

        node_uuid = str(uuid.uuid4())
        title = f"{repo_name}@{head_sha[:7]} — {run['conclusion']}"
        # Write knowledge node
        cur = conn.execute(
            """INSERT INTO knowledge_nodes
               (uuid, content, embed_text, node_type, outcome, created_at, updated_at)
               VALUES (?, ?, ?, 'change_outcome', ?, ?, ?)""",
            (
                node_uuid,
                summary,
                title,
                run["conclusion"],
                now,
                now,
            ),
        )
        node_id = cur.lastrowid

        # Link to workflow run
        conn.execute(
            "INSERT INTO knowledge_node_workflow_runs (knowledge_node_id, run_id) VALUES (?,?)",
            (node_id, run_id),
        )

        # Link to shell events
        for s in shell_rows:
            conn.execute(
                "INSERT OR IGNORE INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (?,?)",
                (node_id, s["id"]),
            )

        # Link to Claude sessions
        for c in claude_rows:
            conn.execute(
                "INSERT OR IGNORE INTO knowledge_node_claude_sessions (knowledge_node_id, claude_session_id) VALUES (?,?)",
                (node_id, c["id"]),
            )

        # Mark run enriched + queue done
        conn.execute("UPDATE workflow_runs SET enriched = 1 WHERE id = ?", (run_id,))
        conn.execute(
            "UPDATE workflow_enrichment_queue SET status='done', updated_at=? WHERE run_id=?",
            (now, run_id),
        )
        conn.commit()

        # Lesson clustering for each failing annotation
        for a in ann_rows:
            path_prefix = _path_prefix(a["path"], path_prefix_segments)
            upsert_cluster(
                db_path,
                ClusterKey(
                    repo=repo_name,
                    tool=a["tool"] or "",
                    rule_id=a["rule_id"] or "",
                    path_prefix=path_prefix or "",
                ),
                min_occurrences=min_occurrences,
                summary_fn=lambda k: f"{k.tool}:{k.rule_id} in {k.path_prefix}",
                now_ms=now,
            )

    finally:
        conn.close()


async def enrich_one_async(
    db_path: str,
    run_id: int,
    inference: InferenceClient,
    query_model: str,
    *,
    path_prefix_segments: int = 2,
    min_occurrences: int = 2,
) -> tuple[int, dict] | None:
    """Async wrapper around enrich_one for use in the enrichment scheduler.

    Returns ``(node_id, node_dict)`` for the created knowledge node so the
    caller can schedule embedding, or ``None`` when ``run_id`` does not exist.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            return

        now = int(time.time() * 1000)
        started = run["started_at"] or now

        # Co-temporal shell events: prefer exact SHA match; only fall back to time
        # window for push-related commands from the same repo.
        head_sha = run["head_sha"]
        repo_name = run["repo"]

        shell_rows = conn.execute(
            """SELECT id, command, git_commit FROM events
               WHERE git_commit = ?
                 AND probe_tag IS NULL
               LIMIT 20""",
            (head_sha,),
        ).fetchall()

        if not shell_rows:
            shell_rows = conn.execute(
                """SELECT id, command, git_commit FROM events
                   WHERE timestamp BETWEEN ? AND ?
                     AND command LIKE '%git push%'
                     AND (git_repo IS NULL OR git_repo = ?)
                     AND probe_tag IS NULL
                   LIMIT 20""",
                (
                    started - CORRELATION_WINDOW_MS,
                    started + CORRELATION_WINDOW_MS,
                    repo_name,
                ),
            ).fetchall()

        # Co-temporal Claude sessions
        claude_rows = conn.execute(
            """SELECT id, session_id, summary_text FROM claude_sessions
               WHERE start_time <= ? AND end_time >= ?
                 AND probe_tag IS NULL
               LIMIT 10""",
            (
                started + CORRELATION_WINDOW_MS,
                started - CORRELATION_WINDOW_MS,
            ),
        ).fetchall()

        # Top failing annotations (up to 10)
        ann_rows = conn.execute(
            """SELECT a.tool, a.rule_id, a.path, a.start_line, a.message
               FROM workflow_annotations a
               JOIN workflow_jobs j ON j.id = a.job_id
               WHERE j.run_id = ? AND a.level = 'failure'
               ORDER BY a.id LIMIT 10""",
            (run_id,),
        ).fetchall()

        # Build prompt and call LLM (async). The system prompt demands a JSON
        # object; parse_enrichment_response validates it (and repairs stray
        # escapes). Storing the raw reply — as this path used to — let a
        # reasoning model's chain-of-thought land in `content`, producing
        # invalid-JSON nodes. On un-parseable output it raises, so no garbage
        # node is written and the caller marks the run failed.
        prompt = _build_prompt(run, shell_rows, claude_rows, ann_rows)
        raw = await inference.chat(
            messages=[
                {"role": "system", "content": WORKFLOW_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            model=query_model,
            max_tokens=600,
        )
        result = parse_enrichment_response(raw)
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

        node_uuid = str(uuid.uuid4())
        title = f"{repo_name}@{head_sha[:7]} — {run['conclusion']}"
        # Write knowledge node. `outcome` column keeps the authoritative GitHub
        # conclusion; embed_text stays the title (repo@sha) for retrieval.
        cur = conn.execute(
            """INSERT INTO knowledge_nodes
               (uuid, content, embed_text, node_type, outcome, enrichment_model,
                created_at, updated_at)
               VALUES (?, ?, ?, 'change_outcome', ?, ?, ?, ?)""",
            (
                node_uuid,
                content,
                title,
                run["conclusion"],
                query_model,
                now,
                now,
            ),
        )
        node_id = cur.lastrowid

        # Link to workflow run
        conn.execute(
            "INSERT INTO knowledge_node_workflow_runs (knowledge_node_id, run_id) VALUES (?,?)",
            (node_id, run_id),
        )

        # Link to shell events
        for s in shell_rows:
            conn.execute(
                "INSERT OR IGNORE INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (?,?)",
                (node_id, s["id"]),
            )

        # Link to Claude sessions
        for c in claude_rows:
            conn.execute(
                "INSERT OR IGNORE INTO knowledge_node_claude_sessions (knowledge_node_id, claude_session_id) VALUES (?,?)",
                (node_id, c["id"]),
            )

        # Mark run enriched + queue done
        conn.execute("UPDATE workflow_runs SET enriched = 1 WHERE id = ?", (run_id,))
        conn.execute(
            "UPDATE workflow_enrichment_queue SET status='done', updated_at=? WHERE run_id=?",
            (now, run_id),
        )
        conn.commit()

        # Lesson clustering for each failing annotation (sync — opens its own
        # connection). Best-effort: the node and queue 'done' status are already
        # committed above, so a clustering failure must not propagate — that would
        # mark the run failed and re-enrich it into a duplicate node.
        try:
            for a in ann_rows:
                path_prefix = _path_prefix(a["path"], path_prefix_segments)
                upsert_cluster(
                    db_path,
                    ClusterKey(
                        repo=repo_name,
                        tool=a["tool"] or "",
                        rule_id=a["rule_id"] or "",
                        path_prefix=path_prefix or "",
                    ),
                    min_occurrences=min_occurrences,
                    summary_fn=lambda k: f"{k.tool}:{k.rule_id} in {k.path_prefix}",
                    now_ms=now,
                )
        except Exception:
            logger.warning(
                "lesson clustering failed for workflow run %d; node already persisted",
                run_id,
                exc_info=True,
            )

        assert node_id is not None  # INSERT always populates lastrowid
        return node_id, {"id": node_id, "embed_text": title, "commands_raw": ""}
    finally:
        conn.close()


def claim_pending_workflow_runs(
    conn: sqlite3.Connection,
    worker_id: str,
    stale_lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    max_claim_batch: int | None = None,
) -> list[int]:
    """Atomically claim pending workflow enrichment queue entries.

    Returns a list of run_ids ready for enrichment. `max_claim_batch` caps the
    number of runs claimed per invocation; `None` disables the cap.
    """
    now_ms = int(time.time() * 1000)
    stale_lock_ms = now_ms - stale_lock_timeout_ms
    claim_limit = max_claim_batch if max_claim_batch is not None else -1

    cursor = conn.execute(
        """
        UPDATE workflow_enrichment_queue
        SET status    = 'processing',
            locked_at = ?,
            locked_by = ?,
            updated_at = ?
        WHERE run_id IN (
            SELECT run_id FROM workflow_enrichment_queue
            WHERE status = 'pending'
               OR (status = 'processing' AND COALESCE(locked_at, 0) <= ?)
            ORDER BY priority, enqueued_at
            LIMIT ?
        )
        RETURNING run_id
        """,
        (now_ms, worker_id, now_ms, stale_lock_ms, claim_limit),
    )
    run_ids = [row[0] for row in cursor.fetchall()]
    conn.commit()
    return run_ids


def mark_workflow_queue_failed(conn: sqlite3.Connection, run_id: int, error: str) -> None:
    """Increment retry_count; reset to pending if retries remain, failed if exhausted."""
    now_ms = int(time.time() * 1000)
    conn.execute(
        """
        UPDATE workflow_enrichment_queue
        SET retry_count   = retry_count + 1,
            error_message = ?,
            locked_at     = NULL,
            locked_by     = NULL,
            updated_at    = ?,
            status        = CASE
                                WHEN retry_count + 1 >= max_retries THEN 'failed'
                                ELSE 'pending'
                            END
        WHERE run_id = ?
        """,
        (error, now_ms, run_id),
    )
    conn.commit()


def _path_prefix(path: str | None, segments: int) -> str:
    """Extract the first N path segments as a prefix string."""
    if not path:
        return ""
    parts = Path(path).parts
    return str(Path(*parts[:segments])) + "/" if len(parts) >= segments else path


def _build_prompt(run, shell_rows, claude_rows, ann_rows) -> str:
    parts = [
        f"Workflow run: {run['repo']} @ {run['head_sha'][:7]}",
        f"Branch: {run['head_branch'] or 'unknown'}",
        f"Status: {run['status']}  Conclusion: {run['conclusion']}",
        "",
    ]
    if ann_rows:
        parts.append(f"Annotations ({len(ann_rows)}):")
        for a in ann_rows:
            parts.append(
                f"  - [{a['tool'] or '?'}:{a['rule_id'] or '?'}] "
                f"{a['path']}:{a['start_line']}: {a['message']}"
            )
    parts.append(f"\nCo-temporal shell events: {len(shell_rows)}")
    parts.append(f"Co-temporal Claude sessions: {len(claude_rows)}")
    parts.append(
        "\nSummarize what changed, whether it succeeded, and if it "
        "failed, the root cause and one-line fix suggestion."
    )
    return "\n".join(parts)
