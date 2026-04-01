"""Browser event enrichment: queue claiming, prompt building, knowledge node writing."""

import json
import time
import uuid

from hippo_brain.models import EnrichmentResult

STALE_LOCK_TIMEOUT_MS = 5 * 60 * 1000

BROWSER_SYSTEM_PROMPT = """You are a developer activity analyst. You receive a sequence of web pages a developer visited during a browsing session.

Extract what they were researching, learning, or investigating. Focus on technical topics and how pages relate to each other (e.g., a search query leading to documentation).

IMPORTANT: Be specific. Use actual page titles, URLs, technical concepts, and search queries from the data. Generic descriptions like "browsed some pages" are unacceptable.

The embed_text field should read like a developer's research log — specific enough that searching for "Rust Display trait implementation" or "cargo proc-macro error" would find it.

Output a JSON object with these fields:
- summary: Specific description of what was researched or learned
- intent: The developer's goal (e.g., "research", "debugging", "learning", "reference")
- outcome: One of "success", "partial", "failure", "unknown"
- key_decisions: List of decisions informed by the research
- problems_encountered: List of obstacles or dead ends
- entities: An object with lists of extracted entities:
  - projects: Project names mentioned or inferred
  - tools: Technologies, frameworks, languages referenced
  - files: Specific files referenced
  - services: Services or APIs referenced
  - errors: Error messages being researched
- tags: Descriptive, specific tags
- embed_text: A detailed paragraph describing the research session. Specific topics, search queries, and sources. Optimized for semantic search.

Output ONLY valid JSON, no markdown fences or extra text."""


def claim_pending_browser_events(conn, worker_id: str, stale_secs: int = 60) -> list[list[dict]]:
    """Atomically claim pending browser events and return them grouped into time-based chunks.

    Only claims events whose timestamp is older than stale_secs (to avoid
    processing events from an active browsing session).
    """
    now_ms = int(time.time() * 1000)
    stale_threshold_ms = now_ms - (stale_secs * 1000)
    stale_lock_ms = now_ms - STALE_LOCK_TIMEOUT_MS

    cursor = conn.execute(
        """
        UPDATE browser_enrichment_queue
        SET status     = 'processing',
            locked_at  = ?,
            locked_by  = ?,
            updated_at = ?
        WHERE id IN (
            SELECT beq.id
            FROM browser_enrichment_queue beq
            JOIN browser_events be ON beq.browser_event_id = be.id
            WHERE (beq.status = 'pending'
                   OR (beq.status = 'processing'
                       AND COALESCE(beq.locked_at, 0) <= ?))
              AND be.timestamp < ?
            ORDER BY beq.priority, beq.created_at
        )
        RETURNING browser_event_id
        """,
        (now_ms, worker_id, now_ms, stale_lock_ms, stale_threshold_ms),
    )
    event_ids = [row[0] for row in cursor.fetchall()]
    conn.commit()

    if not event_ids:
        return []

    placeholders = ",".join("?" * len(event_ids))
    cursor = conn.execute(
        f"""
        SELECT id, timestamp, url, title, domain, dwell_ms,
               scroll_depth, extracted_text, search_query, referrer
        FROM browser_events
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
                "timestamp": row[1],
                "url": row[2],
                "title": row[3],
                "domain": row[4],
                "dwell_ms": row[5],
                "scroll_depth": row[6],
                "extracted_text": row[7],
                "search_query": row[8],
                "referrer": row[9],
            }
        )

    return _chunk_by_time_gap(events)


def _chunk_by_time_gap(events: list[dict], gap_ms: int = 300_000) -> list[list[dict]]:
    """Split events into chunks at time gaps > gap_ms."""
    if not events:
        return []

    chunks = []
    current = [events[0]]

    for ev in events[1:]:
        prev_ts = current[-1]["timestamp"]
        if ev["timestamp"] - prev_ts > gap_ms:
            chunks.append(current)
            current = [ev]
        else:
            current.append(ev)

    if current:
        chunks.append(current)
    return chunks


def build_browser_enrichment_prompt(events: list[dict]) -> str:
    """Format browser events into the user prompt for LLM enrichment."""
    lines = []
    for i, ev in enumerate(events, 1):
        parts = [f"Page {i}:"]
        parts.append(f"  url: {ev.get('url', '')}")
        parts.append(f"  title: {ev.get('title', '')}")
        parts.append(f"  domain: {ev.get('domain', '')}")

        dwell_ms = ev.get("dwell_ms", 0) or 0
        dwell_s = dwell_ms / 1000.0
        scroll = ev.get("scroll_depth")
        time_scroll = f"  time spent: {dwell_s:.1f}s"
        if scroll is not None:
            time_scroll += f", scrolled: {int(scroll * 100)}%"
        parts.append(time_scroll)

        search_query = ev.get("search_query")
        if search_query:
            parts.append(f"  search query: {search_query}")

        extracted = ev.get("extracted_text")
        if extracted:
            excerpt = extracted[:2000]
            parts.append(f"  content excerpt: {excerpt}")

        lines.append("\n".join(parts))
    return "\n\n".join(lines)


def get_correlated_browser_events(
    conn, session_start_ms: int, session_end_ms: int, window_ms: int = 300_000
) -> list[dict]:
    """Fetch browser events that overlap with a shell session time window.

    Used by shell enrichment to inject browser context into prompts.
    """
    cursor = conn.execute(
        """
        SELECT id, timestamp, url, title, domain, dwell_ms,
               scroll_depth, extracted_text, search_query, referrer
        FROM browser_events
        WHERE timestamp BETWEEN ? AND ?
        ORDER BY timestamp ASC
        """,
        (session_start_ms - window_ms, session_end_ms + window_ms),
    )

    events = []
    for row in cursor.fetchall():
        events.append(
            {
                "id": row[0],
                "timestamp": row[1],
                "url": row[2],
                "title": row[3],
                "domain": row[4],
                "dwell_ms": row[5],
                "scroll_depth": row[6],
                "extracted_text": row[7],
                "search_query": row[8],
                "referrer": row[9],
            }
        )
    return events


def format_browser_context_for_shell_prompt(browser_events: list[dict]) -> str:
    """Format correlated browser events as context text for shell enrichment prompts."""
    if not browser_events:
        return ""

    lines = ["Browser Activity (concurrent):"]
    for ev in browser_events:
        domain = ev.get("domain", "")
        title = ev.get("title", "")
        dwell_ms = ev.get("dwell_ms", 0) or 0
        dwell_s = dwell_ms / 1000.0
        scroll = ev.get("scroll_depth")

        entry = f'  {domain} - "{title}" (read {dwell_s:.1f}s'
        if scroll is not None:
            entry += f", {int(scroll * 100)}% scroll"
        entry += ")"
        lines.append(entry)

        search_query = ev.get("search_query")
        if search_query:
            lines.append(f'  Search query: "{search_query}"')

    return "\n".join(lines)


def write_browser_knowledge_node(
    conn, result: EnrichmentResult, event_ids: list[int], model_name: str
) -> int:
    """Insert knowledge node, link to browser events, upsert entities, mark queue done.

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

        # Link to browser events
        for event_id in event_ids:
            conn.execute(
                "INSERT INTO knowledge_node_browser_events (knowledge_node_id, browser_event_id) VALUES (?, ?)",
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
                    UPDATE SET last_seen = excluded.last_seen
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

        # Mark browser events as enriched
        placeholders = ",".join("?" * len(event_ids))
        conn.execute(
            f"UPDATE browser_events SET enriched = 1 WHERE id IN ({placeholders})",
            event_ids,
        )

        # Mark queue entries done
        conn.execute(
            f"""
            UPDATE browser_enrichment_queue SET status = 'done', updated_at = ?
            WHERE browser_event_id IN ({placeholders})
            """,
            [now_ms, *event_ids],
        )

        conn.commit()
        return node_id
    except Exception:
        conn.rollback()
        raise


def mark_browser_queue_failed(conn, event_ids: list[int], error: str) -> None:
    """Increment retry_count; reset to pending if retries remain, failed if exhausted."""
    now_ms = int(time.time() * 1000)
    for event_id in event_ids:
        conn.execute(
            """
            UPDATE browser_enrichment_queue
            SET retry_count   = retry_count + 1,
                error_message = ?,
                locked_at     = NULL,
                locked_by     = NULL,
                updated_at    = ?,
                status        = CASE
                                    WHEN retry_count + 1 >= max_retries THEN 'failed'
                                    ELSE 'pending'
                                END
            WHERE browser_event_id = ?
            """,
            (error, now_ms, event_id),
        )
    conn.commit()
