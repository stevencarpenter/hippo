import json
import re
import time
import uuid

from hippo_brain.models import EnrichmentResult, validate_enrichment_data

STALE_LOCK_TIMEOUT_MS = 5 * 60 * 1000

SYSTEM_PROMPT = """You are a developer activity analyst. You receive a sequence of shell command events from a single work session and produce structured enrichment data.

Events are labeled with who executed them: "developer (human)" for commands the user typed,
or "Claude Code (AI agent)" for commands executed by an AI coding assistant. Reflect this
distinction in your summary — attribute actions to the correct actor.

IMPORTANT: Be specific. Use actual file names, function names, error messages, and outcomes from the event data. Generic descriptions like "edited a Rust file" are unacceptable. Instead say "added build.rs to hippo-daemon that embeds git metadata via cargo:rustc-env".

The embed_text field should read like a developer's work log entry — specific enough that searching for "embedding model configuration" or "clippy warning fix" would find it.

Output a JSON object with these fields:
- summary: Specific description of what was accomplished (not what tools were used)
- intent: The developer's goal (e.g., "testing", "debugging", "deploying", "refactoring")
- outcome: One of "success", "partial", "failure", "unknown"
- key_decisions: List of decisions made and why (e.g., "Chose build.rs over vergen crate for zero dependencies")
- problems_encountered: List of errors/failures and how they were resolved
- entities: An object with lists of extracted entities:
  - projects: Project names mentioned or inferred
  - tools: CLI tools used (cargo, npm, git, docker, etc.)
  - files: Specific files referenced (use actual paths from the events)
  - services: Services interacted with (databases, APIs, etc.)
  - errors: Actual error messages encountered (not generic descriptions)
- tags: Descriptive, specific tags (not "success" or "editing")
- embed_text: A detailed paragraph a developer would write in a work log. Specific file names, error messages, and outcomes. Optimized for semantic search.

Output ONLY valid JSON, no markdown fences or extra text."""


def _actor_label(shell: str) -> str:
    if shell in ("claude-code", "claude"):
        return "Claude Code (AI agent)"
    return "developer (human)"


def build_enrichment_prompt(events: list[dict]) -> str:
    """Format events into the user prompt template."""
    lines = []
    for i, ev in enumerate(events, 1):
        actor = _actor_label(ev.get("shell", ""))
        parts = [f"Event {i} (executed by {actor}):"]
        parts.append(f"  command: {ev.get('command', '')}")
        parts.append(f"  exit_code: {ev.get('exit_code', '')}")
        parts.append(f"  duration_ms: {ev.get('duration_ms', '')}")
        parts.append(f"  cwd: {ev.get('cwd', '')}")
        if ev.get("git_branch"):
            parts.append(f"  git_branch: {ev['git_branch']}")
        if ev.get("git_commit"):
            parts.append(f"  git_commit: {ev['git_commit']}")
        if ev.get("git_repo"):
            parts.append(f"  git_repo: {ev['git_repo']}")
        if ev.get("stdout"):
            parts.append(f"  stdout:\n{ev['stdout']}")
        if ev.get("stderr"):
            parts.append(f"  stderr:\n{ev['stderr']}")
        lines.append("\n".join(parts))
    return "\n\n".join(lines)


def parse_enrichment_response(raw: str) -> EnrichmentResult:
    """Strip markdown code fences if present, parse JSON, return dataclass."""
    text = raw.strip()
    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    data = json.loads(text)
    return validate_enrichment_data(data)


# Batching contract:
# Events are claimed from the queue in priority/creation order and fetched
# in timestamp order. Batches are not coherence-grouped by session or repo;
# the batch size is configured via enrichment_batch_size. One failed batch
# retries all claimed events together up to max_retries.
def claim_pending_events(conn, batch_size: int, worker_id: str) -> list[dict]:
    """Atomically claim pending events from the enrichment queue."""
    now_ms = int(time.time() * 1000)
    stale_before_ms = now_ms - STALE_LOCK_TIMEOUT_MS
    cursor = conn.execute(
        """
        UPDATE enrichment_queue
        SET status     = 'processing',
            locked_at  = ?,
            locked_by  = ?,
            updated_at = ?
        WHERE id IN (SELECT id
                     FROM enrichment_queue
                     WHERE status = 'pending'
                        OR (
                         status = 'processing'
                             AND COALESCE(locked_at, 0) <= ?
                         )
                     ORDER BY priority, created_at
                     LIMIT ?)
        RETURNING event_id
        """,
        (now_ms, worker_id, now_ms, stale_before_ms, batch_size),
    )
    event_ids = [row[0] for row in cursor.fetchall()]
    conn.commit()

    if not event_ids:
        return []

    placeholders = ",".join("?" * len(event_ids))
    cursor = conn.execute(
        f"""
        SELECT id, session_id, timestamp, command, exit_code, duration_ms,
               cwd, hostname, shell, git_repo, git_branch, git_commit, git_dirty
        FROM events
        WHERE id IN ({placeholders})
        ORDER BY timestamp ASC
        """,
        event_ids,
    )

    events = []
    for row in cursor.fetchall():
        events.append(
            {
                "id": row[0],
                "session_id": row[1],
                "timestamp": row[2],
                "command": row[3],
                "exit_code": row[4],
                "duration_ms": row[5],
                "cwd": row[6],
                "hostname": row[7],
                "shell": row[8],
                "git_repo": row[9],
                "git_branch": row[10],
                "git_commit": row[11],
                "git_dirty": row[12],
            }
        )
    return events


def claim_pending_events_by_session(
    conn, max_per_chunk: int, worker_id: str, stale_secs: int = 120
) -> list[list[dict]]:
    """Claim pending events grouped by session. Returns list of event chunks.

    Only processes sessions where the last event is older than stale_secs.
    Long sessions are split into chunks at time gaps > 60s or at max_per_chunk.
    """
    now_ms = int(time.time() * 1000)
    stale_threshold_ms = now_ms - (stale_secs * 1000)
    stale_lock_ms = now_ms - STALE_LOCK_TIMEOUT_MS

    cursor = conn.execute(
        """
        SELECT e.session_id, COUNT(*) as cnt
        FROM enrichment_queue eq
        JOIN events e ON eq.event_id = e.id
        WHERE eq.status = 'pending'
           OR (eq.status = 'processing' AND COALESCE(eq.locked_at, 0) <= ?)
        GROUP BY e.session_id
        HAVING MAX(e.timestamp) < ?
        ORDER BY MIN(e.timestamp) ASC
        """,
        (stale_lock_ms, stale_threshold_ms),
    )
    sessions = cursor.fetchall()

    all_chunks = []
    for session_id, _ in sessions:
        cursor = conn.execute(
            """
            UPDATE enrichment_queue
            SET status = 'processing', locked_at = ?, locked_by = ?, updated_at = ?
            WHERE id IN (
                SELECT eq.id FROM enrichment_queue eq
                JOIN events e ON eq.event_id = e.id
                WHERE e.session_id = ?
                  AND (eq.status = 'pending'
                       OR (eq.status = 'processing' AND COALESCE(eq.locked_at, 0) <= ?))
            )
            RETURNING event_id
            """,
            (now_ms, worker_id, now_ms, session_id, stale_lock_ms),
        )
        event_ids = [row[0] for row in cursor.fetchall()]
        conn.commit()

        if not event_ids:
            continue

        placeholders = ",".join("?" * len(event_ids))
        cursor = conn.execute(
            f"""
            SELECT id, session_id, timestamp, command, exit_code, duration_ms,
                   cwd, hostname, shell, git_repo, git_branch, git_commit, git_dirty,
                   stdout, stderr
            FROM events
            WHERE id IN ({placeholders})
            ORDER BY timestamp ASC
            """,
            event_ids,
        )

        events = []
        for row in cursor.fetchall():
            events.append(
                {
                    "id": row[0],
                    "session_id": row[1],
                    "timestamp": row[2],
                    "command": row[3],
                    "exit_code": row[4],
                    "duration_ms": row[5],
                    "cwd": row[6],
                    "hostname": row[7],
                    "shell": row[8],
                    "git_repo": row[9],
                    "git_branch": row[10],
                    "git_commit": row[11],
                    "git_dirty": row[12],
                    "stdout": row[13],
                    "stderr": row[14],
                }
            )

        chunks = _chunk_events(events, max_per_chunk)
        all_chunks.extend(chunks)

    return all_chunks


def _chunk_events(events: list[dict], max_size: int) -> list[list[dict]]:
    """Split events into chunks at time gaps > 60s or at max_size."""
    if len(events) <= max_size:
        return [events]

    TIME_GAP_MS = 60_000
    chunks = []
    current = [events[0]]

    for ev in events[1:]:
        prev_ts = current[-1]["timestamp"]
        gap = ev["timestamp"] - prev_ts
        if gap > TIME_GAP_MS or len(current) >= max_size:
            chunks.append(current)
            current = [ev]
        else:
            current.append(ev)

    if current:
        chunks.append(current)
    return chunks


def write_knowledge_node(
    conn, result: EnrichmentResult, event_ids: list[int], model_name: str
) -> int:
    """Insert knowledge node, link to events, upsert entities, mark queue done.

    All inserts run inside an explicit transaction so a mid-write failure
    leaves no partial state in the database.
    """
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
            VALUES (?, ?, ?, 'observation', ?, ?, ?, 1, ?, ?)
            """,
            (
                node_uuid,
                content,
                result.embed_text,
                result.outcome,
                tags_json,
                model_name,
                now_ms,
                now_ms,
            ),
        )
        node_id = cursor.lastrowid

        # Link to events
        for event_id in event_ids:
            conn.execute(
                "INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (?, ?)",
                (node_id, event_id),
            )

        # Upsert entities
        all_entities = result.entities if isinstance(result.entities, dict) else {}
        entity_type_map = {
            "projects": "project",
            "tools": "tool",
            "files": "file",
            "services": "service",
            "errors": "concept",
        }
        for key, entity_type in entity_type_map.items():
            for name in all_entities.get(key, []):
                canonical = name.lower().strip()
                cursor = conn.execute(
                    """
                    INSERT INTO entities (type, name, canonical, first_seen, last_seen, created_at)
                    VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (type, canonical) DO
                    UPDATE SET
                        last_seen = excluded.last_seen
                        RETURNING id
                    """,
                    (entity_type, name, canonical, now_ms, now_ms, now_ms),
                )
                entity_id = cursor.fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO knowledge_node_entities (knowledge_node_id, entity_id)
                    VALUES (?, ?) ON CONFLICT DO NOTHING
                    """,
                    (node_id, entity_id),
                )

        # Mark events as enriched
        placeholders = ",".join("?" * len(event_ids))
        conn.execute(
            f"UPDATE events SET enriched = 1 WHERE id IN ({placeholders})",
            event_ids,
        )

        # Mark queue entries done
        conn.execute(
            f"""
            UPDATE enrichment_queue SET status = 'done', updated_at = ?
            WHERE event_id IN ({placeholders})
            """,
            [now_ms, *event_ids],
        )

        conn.commit()
        return node_id
    except Exception:
        conn.rollback()
        raise


def mark_queue_failed(conn, event_ids: list[int], error: str) -> None:
    """Increment retry_count; reset to pending if retries remain, failed if exhausted."""
    now_ms = int(time.time() * 1000)
    for event_id in event_ids:
        conn.execute(
            """
            UPDATE enrichment_queue
            SET retry_count   = retry_count + 1,
                error_message = ?,
                locked_at     = NULL,
                locked_by     = NULL,
                updated_at    = ?,
                status        = CASE
                                    WHEN retry_count + 1 >= max_retries THEN 'failed'
                                    ELSE 'pending'
                    END
            WHERE event_id = ?
            """,
            (error, now_ms, event_id),
        )
    conn.commit()
