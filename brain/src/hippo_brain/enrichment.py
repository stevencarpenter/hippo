import json
import re
import time
import uuid

from hippo_brain.models import EnrichmentResult

SYSTEM_PROMPT = """You are a developer activity analyst. You receive shell command events and produce structured enrichment data.

For each batch of events, output a JSON object with these fields:
- summary: A concise description of what the developer was doing
- intent: The developer's goal (e.g., "testing", "debugging", "deploying", "refactoring")
- outcome: One of "success", "partial", "failure", "unknown"
- entities: An object with lists of extracted entities:
  - projects: Project names mentioned or inferred
  - tools: CLI tools used (cargo, npm, git, docker, etc.)
  - files: Specific files referenced
  - services: Services interacted with (databases, APIs, etc.)
  - errors: Error types or messages encountered
- relationships: A list of {from, to, relationship} objects describing entity relationships
- tags: A list of descriptive tags
- embed_text: A natural language summary optimized for semantic search

Output ONLY valid JSON, no markdown fences or extra text."""


def build_enrichment_prompt(events: list[dict]) -> str:
    """Format events into the user prompt template."""
    lines = []
    for i, ev in enumerate(events, 1):
        parts = [f"Event {i}:"]
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
    return EnrichmentResult(
        summary=data.get("summary", ""),
        intent=data.get("intent", ""),
        outcome=data.get("outcome", "unknown"),
        entities=data.get("entities", {}),
        relationships=data.get("relationships", []),
        tags=data.get("tags", []),
        embed_text=data.get("embed_text", ""),
    )


def claim_pending_events(conn, batch_size: int, worker_id: str) -> list[dict]:
    """Atomically claim pending events from the enrichment queue."""
    now_ms = int(time.time() * 1000)
    cursor = conn.execute(
        """
        UPDATE enrichment_queue
        SET status = 'processing', locked_at = ?, locked_by = ?,
            updated_at = ?
        WHERE id IN (
            SELECT id FROM enrichment_queue
            WHERE status = 'pending'
            ORDER BY priority ASC, created_at ASC
            LIMIT ?
        )
        RETURNING event_id
        """,
        (now_ms, worker_id, now_ms, batch_size),
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
        """,
        event_ids,
    )

    events = []
    for row in cursor.fetchall():
        events.append({
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
        })
    return events


def write_knowledge_node(
    conn, result: EnrichmentResult, event_ids: list[int], model_name: str
) -> int:
    """Insert knowledge node, link to events, upsert entities, mark queue done."""
    node_uuid = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)
    content = json.dumps({
        "summary": result.summary,
        "intent": result.intent,
        "outcome": result.outcome,
        "entities": result.entities,
        "relationships": result.relationships,
        "tags": result.tags,
    })
    tags_json = json.dumps(result.tags)

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
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (type, canonical) DO UPDATE SET
                    last_seen = excluded.last_seen
                RETURNING id
                """,
                (entity_type, name, canonical, now_ms, now_ms, now_ms),
            )
            entity_id = cursor.fetchone()[0]
            conn.execute(
                """
                INSERT INTO knowledge_node_entities (knowledge_node_id, entity_id)
                VALUES (?, ?)
                ON CONFLICT DO NOTHING
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


def mark_queue_failed(conn, event_ids: list[int], error: str):
    """Increment retry_count; reset to pending if retries remain, failed if exhausted."""
    now_ms = int(time.time() * 1000)
    for event_id in event_ids:
        conn.execute(
            """
            UPDATE enrichment_queue
            SET retry_count = retry_count + 1,
                error_message = ?,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = ?,
                status = CASE
                    WHEN retry_count + 1 >= max_retries THEN 'failed'
                    ELSE 'pending'
                END
            WHERE event_id = ?
            """,
            (error, now_ms, event_id),
        )
    conn.commit()
