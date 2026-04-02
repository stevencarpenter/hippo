"""Pure query functions for the Hippo MCP server.

Each function takes a sqlite3.Connection and returns a list of dicts.
No MCP framework dependency — reusable from CLI, HTTP, or MCP.
"""

import json
import re
import sqlite3
import time


MAX_LIMIT = 100


def shape_semantic_results(hits: list[dict]) -> list[dict]:
    """Transform raw LanceDB search hits into the spec-compliant result shape.

    Strips internal fields (vector arrays, session_id, enrichment_model) and
    maps to the canonical schema: score, summary, intent, outcome, tags,
    embed_text, cwd, git_branch.
    """
    results = []
    for hit in hits:
        try:
            tags = (
                json.loads(hit.get("tags", "[]"))
                if isinstance(hit.get("tags"), str)
                else hit.get("tags", [])
            )
        except json.JSONDecodeError, TypeError:
            tags = []
        results.append(
            {
                "score": round(1.0 - hit.get("_distance", 0.0), 4),
                "summary": hit.get("summary", ""),
                "intent": "",
                "outcome": hit.get("outcome", ""),
                "tags": tags,
                "embed_text": hit.get("embed_text", ""),
                "cwd": hit.get("cwd", ""),
                "git_branch": hit.get("git_branch", ""),
            }
        )
    return results


def parse_since(since: str) -> int:
    """Parse a duration string like '24h', '7d', '30m' into an epoch-ms threshold.

    Returns 0 if the string is empty or unparseable (meaning no time filter).
    """
    if not since:
        return 0
    match = re.match(r"^(\d+)(h|d|m)$", since.strip())
    if not match:
        return 0
    value = int(match.group(1))
    unit = match.group(2)
    unit_ms = {"h": 3600 * 1000, "d": 24 * 3600 * 1000, "m": 60 * 1000}[unit]
    now_ms = int(time.time() * 1000)
    return now_ms - (value * unit_ms)


def search_knowledge_lexical(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict]:
    """Lexical (LIKE) search over knowledge_nodes."""
    if query:
        pattern = f"%{query}%"
        rows = conn.execute(
            """
            SELECT id, uuid, content, embed_text, outcome, tags, created_at
            FROM knowledge_nodes
            WHERE content LIKE ? OR embed_text LIKE ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (pattern, pattern, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, uuid, content, embed_text, outcome, tags, created_at
            FROM knowledge_nodes
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    results = []
    for row in rows:
        node_id, uuid, content_str, embed_text, outcome, tags_str, created_at = row
        try:
            content = json.loads(content_str)
        except json.JSONDecodeError, TypeError:
            content = {}
        try:
            tags = json.loads(tags_str) if tags_str else []
        except json.JSONDecodeError, TypeError:
            tags = []

        results.append(
            {
                "score": None,
                "summary": content.get("summary", ""),
                "intent": content.get("intent", ""),
                "outcome": outcome or "",
                "tags": tags,
                "embed_text": embed_text or "",
                "cwd": "",
                "git_branch": "",
            }
        )
    return results


def search_events_impl(
    conn: sqlite3.Connection,
    query: str = "",
    source: str = "all",
    since: str = "",
    project: str = "",
    limit: int = 20,
) -> list[dict]:
    """Search raw events across shell, claude, and browser sources."""
    since_ms = parse_since(since)
    results = []

    if source in ("shell", "all"):
        results.extend(_search_shell_events(conn, query, since_ms, project, limit))
    if source in ("claude", "all"):
        results.extend(_search_claude_events(conn, query, since_ms, project, limit))
    if source in ("browser", "all"):
        results.extend(_search_browser_events(conn, query, since_ms, limit))

    results.sort(key=lambda r: r["timestamp"], reverse=True)
    return results[:limit] if source == "all" else results


def _search_shell_events(
    conn: sqlite3.Connection, query: str, since_ms: int, project: str, limit: int
) -> list[dict]:
    conditions = []
    params: list = []

    if query:
        conditions.append("command LIKE ?")
        params.append(f"%{query}%")
    if since_ms:
        conditions.append("timestamp >= ?")
        params.append(since_ms)
    if project:
        conditions.append("cwd LIKE ?")
        params.append(f"%{project}%")

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"""
        SELECT timestamp, command, exit_code, duration_ms, cwd, git_branch
        FROM events
        {where}
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    return [
        {
            "source": "shell",
            "timestamp": row[0],
            "summary": row[1] or "",
            "cwd": row[4] or "",
            "detail": f"exit={row[2]} duration={row[3]}ms",
            "git_branch": row[5] or "",
        }
        for row in rows
    ]


def _search_claude_events(
    conn: sqlite3.Connection, query: str, since_ms: int, project: str, limit: int
) -> list[dict]:
    conditions = []
    params: list = []

    if query:
        conditions.append("summary_text LIKE ?")
        params.append(f"%{query}%")
    if since_ms:
        conditions.append("start_time >= ?")
        params.append(since_ms)
    if project:
        conditions.append("cwd LIKE ?")
        params.append(f"%{project}%")

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"""
        SELECT start_time, summary_text, cwd, git_branch, message_count,
               tool_calls_json
        FROM claude_sessions
        {where}
        ORDER BY start_time DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    results = []
    for row in rows:
        tool_count = 0
        if row[5]:
            try:
                tool_count = len(json.loads(row[5]))
            except json.JSONDecodeError, TypeError:
                pass
        results.append(
            {
                "source": "claude",
                "timestamp": row[0],
                "summary": row[1] or "",
                "cwd": row[2] or "",
                "detail": f"messages={row[4]} tools={tool_count}",
                "git_branch": row[3] or "",
            }
        )
    return results


def _search_browser_events(
    conn: sqlite3.Connection, query: str, since_ms: int, limit: int
) -> list[dict]:
    conditions = []
    params: list = []

    if query:
        conditions.append("(url LIKE ? OR title LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])
    if since_ms:
        conditions.append("timestamp >= ?")
        params.append(since_ms)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"""
        SELECT timestamp, url, title, domain, dwell_ms, scroll_depth
        FROM browser_events
        {where}
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    return [
        {
            "source": "browser",
            "timestamp": row[0],
            "summary": f"{row[3]} — {row[2] or row[1]}",
            "cwd": "",
            "detail": f"dwell={row[4]}ms scroll={int((row[5] or 0) * 100)}%",
            "git_branch": "",
        }
        for row in rows
    ]


def get_entities_impl(
    conn: sqlite3.Connection,
    entity_type: str = "",
    query: str = "",
    limit: int = 50,
) -> list[dict]:
    """List entities from the knowledge graph."""
    conditions = []
    params: list = []

    if entity_type:
        conditions.append("type = ?")
        params.append(entity_type)
    if query:
        conditions.append("name LIKE ?")
        params.append(f"%{query}%")

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"""
        SELECT type, name, canonical, first_seen, last_seen
        FROM entities
        {where}
        ORDER BY last_seen DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    return [
        {
            "type": row[0],
            "name": row[1],
            "canonical": row[2] or "",
            "first_seen": row[3],
            "last_seen": row[4],
        }
        for row in rows
    ]
