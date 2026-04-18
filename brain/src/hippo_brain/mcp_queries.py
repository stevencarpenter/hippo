"""Pure query functions for the Hippo MCP server.

Each function takes a sqlite3.Connection and returns a list of dicts.
No MCP framework dependency — reusable from CLI, HTTP, or MCP.
"""

import json
import re
import sqlite3
import time
from datetime import datetime, timezone

from hippo_brain.models import CIAnnotation, CIJob, CIStatus, Lesson


MAX_LIMIT = 100
MAX_ANNOTATIONS_PER_JOB = 10


def _safe_json_loads(value, default):
    """Decode JSON, returning ``default`` when the value is not a JSON string.

    Catches both ``TypeError`` (non-str input, e.g. ``None``) and
    ``ValueError`` (which is the base class of ``json.JSONDecodeError``).
    """
    if not isinstance(value, (str, bytes, bytearray)):
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def _knowledge_node_links(
    conn: sqlite3.Connection, knowledge_node_id: int
) -> tuple[list[int], list[int], list[int]]:
    """Return (event_ids, claude_session_ids, browser_event_ids) linked to a node."""
    event_ids = [
        r[0]
        for r in conn.execute(
            "SELECT event_id FROM knowledge_node_events WHERE knowledge_node_id = ?",
            (knowledge_node_id,),
        ).fetchall()
    ]
    claude_ids = [
        r[0]
        for r in conn.execute(
            "SELECT claude_session_id FROM knowledge_node_claude_sessions "
            "WHERE knowledge_node_id = ?",
            (knowledge_node_id,),
        ).fetchall()
    ]
    browser_ids = [
        r[0]
        for r in conn.execute(
            "SELECT browser_event_id FROM knowledge_node_browser_events "
            "WHERE knowledge_node_id = ?",
            (knowledge_node_id,),
        ).fetchall()
    ]
    return event_ids, claude_ids, browser_ids


def _lookup_knowledge_uuid(conn: sqlite3.Connection, knowledge_node_id: int) -> str:
    row = conn.execute(
        "SELECT uuid FROM knowledge_nodes WHERE id = ?", (knowledge_node_id,)
    ).fetchone()
    return row[0] if row else ""


def shape_semantic_results(hits: list[dict], conn: sqlite3.Connection | None = None) -> list[dict]:
    """Transform raw LanceDB search hits into the spec-compliant result shape.

    Strips internal fields (vector arrays, session_id, enrichment_model) and
    maps to the canonical schema. When ``conn`` is provided, augments each
    result with ``uuid`` and linked event/session/browser ids so agents can
    follow up.
    """
    results = []
    for hit in hits:
        raw_tags = hit.get("tags", [])
        if isinstance(raw_tags, str):
            tags = _safe_json_loads(raw_tags, [])
        else:
            tags = raw_tags or []

        node_id = hit.get("id")
        uuid = ""
        event_ids: list[int] = []
        claude_ids: list[int] = []
        browser_ids: list[int] = []
        if conn is not None and node_id is not None:
            uuid = _lookup_knowledge_uuid(conn, node_id)
            event_ids, claude_ids, browser_ids = _knowledge_node_links(conn, node_id)

        results.append(
            {
                "uuid": uuid,
                "score": round(1.0 - hit.get("_distance", 0.0), 4),
                "summary": hit.get("summary", ""),
                "intent": "",
                "outcome": hit.get("outcome", ""),
                "tags": tags,
                "embed_text": hit.get("embed_text", ""),
                "cwd": hit.get("cwd", ""),
                "git_branch": hit.get("git_branch", ""),
                "captured_at": hit.get("captured_at", 0),
                "linked_event_ids": event_ids,
                "linked_claude_session_ids": claude_ids,
                "linked_browser_event_ids": browser_ids,
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


def _build_knowledge_filter_clause(
    project: str,
    since_ms: int,
    source: str,
    branch: str,
) -> tuple[str, list]:
    """Compose a WHERE-fragment + params for knowledge_nodes filter columns.

    Filters that depend on linked events/sessions are pushed down via EXISTS
    subqueries so the lexical SELECT stays a single statement.
    """
    clauses: list[str] = []
    params: list = []

    if since_ms:
        clauses.append("kn.created_at >= ?")
        params.append(since_ms)

    if project:
        like = f"%{project}%"
        clauses.append(
            "(EXISTS (SELECT 1 FROM knowledge_node_events kne "
            "  JOIN events e ON e.id = kne.event_id "
            "  WHERE kne.knowledge_node_id = kn.id "
            "    AND (e.cwd LIKE ? OR e.git_repo LIKE ?))"
            " OR EXISTS (SELECT 1 FROM knowledge_node_claude_sessions kncs "
            "  JOIN claude_sessions cs ON cs.id = kncs.claude_session_id "
            "  WHERE kncs.knowledge_node_id = kn.id "
            "    AND (cs.cwd LIKE ? OR cs.project_dir LIKE ?)))"
        )
        params.extend([like, like, like, like])

    if branch:
        clauses.append(
            "(EXISTS (SELECT 1 FROM knowledge_node_events kne "
            "  JOIN events e ON e.id = kne.event_id "
            "  WHERE kne.knowledge_node_id = kn.id AND e.git_branch = ?)"
            " OR EXISTS (SELECT 1 FROM knowledge_node_claude_sessions kncs "
            "  JOIN claude_sessions cs ON cs.id = kncs.claude_session_id "
            "  WHERE kncs.knowledge_node_id = kn.id AND cs.git_branch = ?))"
        )
        params.extend([branch, branch])

    if source:
        source_table = {
            "shell": "knowledge_node_events",
            "claude": "knowledge_node_claude_sessions",
            "browser": "knowledge_node_browser_events",
            "workflow": "knowledge_node_workflow_runs",
        }.get(source)
        if source_table:
            clauses.append(
                f"EXISTS (SELECT 1 FROM {source_table} link WHERE link.knowledge_node_id = kn.id)"
            )

    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    return where, params


def search_knowledge_lexical(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    project: str = "",
    since: str = "",
    source: str = "",
    branch: str = "",
) -> list[dict]:
    """Lexical (LIKE) search over knowledge_nodes with optional filter pushdown."""
    since_ms = parse_since(since)
    where_extra, extra_params = _build_knowledge_filter_clause(
        project=project, since_ms=since_ms, source=source, branch=branch
    )

    if query:
        pattern = f"%{query}%"
        sql = (
            "SELECT kn.id, kn.uuid, kn.content, kn.embed_text, kn.outcome, kn.tags, "
            "       kn.created_at "
            "FROM knowledge_nodes kn "
            "WHERE (kn.content LIKE ? OR kn.embed_text LIKE ?)"
            f"{where_extra} "
            "ORDER BY kn.created_at DESC LIMIT ?"
        )
        params = [pattern, pattern, *extra_params, limit]
    else:
        sql = (
            "SELECT kn.id, kn.uuid, kn.content, kn.embed_text, kn.outcome, kn.tags, "
            "       kn.created_at "
            "FROM knowledge_nodes kn "
            "WHERE 1=1"
            f"{where_extra} "
            "ORDER BY kn.created_at DESC LIMIT ?"
        )
        params = [*extra_params, limit]

    rows = conn.execute(sql, params).fetchall()

    results = []
    for row in rows:
        node_id, uuid, content_str, embed_text, outcome, tags_str, created_at = row
        content = _safe_json_loads(content_str, {})
        tags = _safe_json_loads(tags_str, []) if tags_str else []
        event_ids, claude_ids, browser_ids = _knowledge_node_links(conn, node_id)

        results.append(
            {
                "uuid": uuid,
                "score": None,
                "summary": content.get("summary", ""),
                "intent": content.get("intent", ""),
                "outcome": outcome or "",
                "tags": tags,
                "embed_text": embed_text or "",
                "cwd": "",
                "git_branch": "",
                "captured_at": created_at,
                "linked_event_ids": event_ids,
                "linked_claude_session_ids": claude_ids,
                "linked_browser_event_ids": browser_ids,
            }
        )
    return results


def search_events_impl(
    conn: sqlite3.Connection,
    query: str = "",
    source: str = "all",
    since: str = "",
    project: str = "",
    branch: str = "",
    limit: int = 20,
) -> list[dict]:
    """Search raw events across shell, claude, and browser sources."""
    since_ms = parse_since(since)
    results = []

    if source in ("shell", "all"):
        results.extend(_search_shell_events(conn, query, since_ms, project, branch, limit))
    if source in ("claude", "all"):
        results.extend(_search_claude_events(conn, query, since_ms, project, branch, limit))
    if source in ("browser", "all"):
        # Branch is meaningless for browser events — silently ignored.
        results.extend(_search_browser_events(conn, query, since_ms, limit))

    results.sort(key=lambda r: r["timestamp"], reverse=True)
    return results[:limit] if source == "all" else results


def _search_shell_events(
    conn: sqlite3.Connection,
    query: str,
    since_ms: int,
    project: str,
    branch: str,
    limit: int,
) -> list[dict]:
    query_pat = f"%{query}%" if query else None
    since_val = since_ms if since_ms else None
    project_pat = f"%{project}%" if project else None
    branch_val = branch or None
    rows = conn.execute(
        """
        SELECT id, timestamp, command, exit_code, duration_ms, cwd, git_branch
        FROM events
        WHERE (? IS NULL OR command LIKE ?)
          AND (? IS NULL OR timestamp >= ?)
          AND (? IS NULL OR cwd LIKE ?)
          AND (? IS NULL OR git_branch = ?)
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (
            query_pat,
            query_pat,
            since_val,
            since_val,
            project_pat,
            project_pat,
            branch_val,
            branch_val,
            limit,
        ),
    ).fetchall()

    return [
        {
            "id": row[0],
            "source": "shell",
            "timestamp": row[1],
            "summary": row[2] or "",
            "cwd": row[5] or "",
            "detail": f"exit={row[3]} duration={row[4]}ms",
            "git_branch": row[6] or "",
        }
        for row in rows
    ]


def _search_claude_events(
    conn: sqlite3.Connection,
    query: str,
    since_ms: int,
    project: str,
    branch: str,
    limit: int,
) -> list[dict]:
    query_pat = f"%{query}%" if query else None
    since_val = since_ms if since_ms else None
    project_pat = f"%{project}%" if project else None
    branch_val = branch or None
    rows = conn.execute(
        """
        SELECT id, start_time, summary_text, cwd, git_branch, message_count,
               tool_calls_json
        FROM claude_sessions
        WHERE (? IS NULL OR summary_text LIKE ?)
          AND (? IS NULL OR start_time >= ?)
          AND (? IS NULL OR cwd LIKE ?)
          AND (? IS NULL OR git_branch = ?)
        ORDER BY start_time DESC
        LIMIT ?
        """,
        (
            query_pat,
            query_pat,
            since_val,
            since_val,
            project_pat,
            project_pat,
            branch_val,
            branch_val,
            limit,
        ),
    ).fetchall()

    results = []
    for row in rows:
        tool_count = 0
        if row[6]:
            tool_count = len(_safe_json_loads(row[6], []))
        results.append(
            {
                "id": row[0],
                "source": "claude",
                "timestamp": row[1],
                "summary": row[2] or "",
                "cwd": row[3] or "",
                "detail": f"messages={row[5]} tools={tool_count}",
                "git_branch": row[4] or "",
            }
        )
    return results


def _search_browser_events(
    conn: sqlite3.Connection, query: str, since_ms: int, limit: int
) -> list[dict]:
    query_pat = f"%{query}%" if query else None
    since_val = since_ms if since_ms else None
    rows = conn.execute(
        """
        SELECT id, timestamp, url, title, domain, dwell_ms, scroll_depth
        FROM browser_events
        WHERE (? IS NULL OR (url LIKE ? OR title LIKE ?))
          AND (? IS NULL OR timestamp >= ?)
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (query_pat, query_pat, query_pat, since_val, since_val, limit),
    ).fetchall()

    return [
        {
            "id": row[0],
            "source": "browser",
            "timestamp": row[1],
            "summary": f"{row[4]} — {row[3] or row[2]}",
            "cwd": "",
            "detail": f"dwell={row[5]}ms scroll={int((row[6] or 0) * 100)}%",
            "git_branch": "",
        }
        for row in rows
    ]


def get_entities_impl(
    conn: sqlite3.Connection,
    entity_type: str = "",
    query: str = "",
    limit: int = 50,
    project: str = "",
    since: str = "",
) -> list[dict]:
    """List entities from the knowledge graph.

    ``project`` filters to entities co-occurring with knowledge nodes whose
    linked events/sessions match the project substring. ``since`` limits to
    entities whose ``last_seen`` is at or after the parsed threshold.
    """
    type_val = entity_type if entity_type else None
    query_pat = f"%{query}%" if query else None
    since_ms = parse_since(since)

    project_clause = ""
    project_params: list = []
    if project:
        like = f"%{project}%"
        project_clause = (
            " AND EXISTS ("
            "   SELECT 1 FROM knowledge_node_entities kne "
            "   JOIN knowledge_nodes kn ON kn.id = kne.knowledge_node_id "
            "   WHERE kne.entity_id = entities.id AND ("
            "     EXISTS (SELECT 1 FROM knowledge_node_events kne2 "
            "             JOIN events e ON e.id = kne2.event_id "
            "             WHERE kne2.knowledge_node_id = kn.id "
            "               AND (e.cwd LIKE ? OR e.git_repo LIKE ?))"
            "     OR EXISTS (SELECT 1 FROM knowledge_node_claude_sessions kncs "
            "                JOIN claude_sessions cs ON cs.id = kncs.claude_session_id "
            "                WHERE kncs.knowledge_node_id = kn.id "
            "                  AND (cs.cwd LIKE ? OR cs.project_dir LIKE ?))"
            "   )"
            " )"
        )
        project_params = [like, like, like, like]

    since_clause = ""
    since_params: list = []
    if since_ms:
        since_clause = " AND last_seen >= ?"
        since_params = [since_ms]

    sql = (
        "SELECT type, name, canonical, first_seen, last_seen "
        "FROM entities "
        "WHERE (? IS NULL OR type = ?) "
        "  AND (? IS NULL OR name LIKE ?)"
        f"{project_clause}{since_clause} "
        "ORDER BY last_seen DESC LIMIT ?"
    )
    params = [type_val, type_val, query_pat, query_pat, *project_params, *since_params, limit]
    rows = conn.execute(sql, params).fetchall()

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


def list_projects_impl(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """Return distinct (git_repo, cwd_root) pairs ordered by most recent activity.

    ``cwd_root`` is the cwd as recorded on the event; we de-duplicate at the
    SQL layer and sort by MAX(timestamp) DESC. Both shell events and claude
    sessions contribute. Browser events have no project, so they are skipped.
    """
    rows = conn.execute(
        """
        SELECT git_repo, cwd_root, MAX(last_seen) AS last_seen FROM (
            SELECT git_repo, cwd AS cwd_root, MAX(timestamp) AS last_seen
            FROM events
            WHERE cwd IS NOT NULL AND cwd != ''
            GROUP BY git_repo, cwd
            UNION ALL
            SELECT NULL AS git_repo, project_dir AS cwd_root,
                   MAX(start_time) AS last_seen
            FROM claude_sessions
            WHERE project_dir IS NOT NULL AND project_dir != ''
            GROUP BY project_dir
        )
        GROUP BY git_repo, cwd_root
        ORDER BY last_seen DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    return [
        {
            "git_repo": row[0] or "",
            "cwd_root": row[1] or "",
            "last_seen": row[2] or 0,
        }
        for row in rows
    ]


def _format_iso(ts_ms: int) -> str:
    if not ts_ms:
        return ""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def format_context_block(query: str, results: list[dict]) -> str:
    """Render a list of SearchResult-shaped dicts as a Markdown context block.

    Designed to be pasted into an agent prompt verbatim. Keeps each entry
    short — full text retrieval should use ``search_knowledge`` directly.
    """
    if not results:
        return f"# Hippo context for: {query}\n\n_No relevant knowledge found._\n"

    lines = [f"# Hippo context for: {query}", ""]
    for i, r in enumerate(results, 1):
        score = r.get("score")
        score_str = f" (score: {score:.2f})" if isinstance(score, (int, float)) else ""
        lines.append(f"## [{i}] {r.get('summary', '') or '(no summary)'}{score_str}")
        if r.get("outcome"):
            lines.append(f"- **Outcome:** {r['outcome']}")
        if r.get("cwd"):
            lines.append(f"- **CWD:** `{r['cwd']}`")
        if r.get("git_branch"):
            lines.append(f"- **Branch:** `{r['git_branch']}`")
        ts = r.get("captured_at", 0)
        if ts:
            lines.append(f"- **When:** {_format_iso(ts)}")
        if r.get("uuid"):
            lines.append(f"- **uuid:** `{r['uuid']}`")
        embed = r.get("embed_text") or ""
        if embed:
            snippet = embed if len(embed) <= 600 else embed[:597] + "…"
            lines.append("")
            lines.append(snippet)
        lines.append("")
    return "\n".join(lines)


def get_lessons_impl(
    db_path: str,
    repo: str | None = None,
    path: str | None = None,
    tool: str | None = None,
    limit: int = 10,
) -> list[Lesson]:
    """Return distilled past-mistake lessons matching the filters.

    Ordered by occurrences DESC, then last_seen_at DESC.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, repo, tool, rule_id, path_prefix, summary, fix_hint,
                      occurrences, first_seen_at, last_seen_at
               FROM lessons
               WHERE (? IS NULL OR repo = ?)
                 AND (? IS NULL OR tool = ?)
                 AND (? IS NULL OR ? LIKE path_prefix || '%')
               ORDER BY occurrences DESC, last_seen_at DESC
               LIMIT ?""",
            (repo, repo, tool, tool, path, path, min(limit, MAX_LIMIT)),
        ).fetchall()
    finally:
        conn.close()

    return [
        Lesson(
            id=r["id"],
            repo=r["repo"],
            tool=r["tool"],
            rule_id=r["rule_id"],
            path_prefix=r["path_prefix"],
            summary=r["summary"],
            fix_hint=r["fix_hint"],
            occurrences=r["occurrences"],
            first_seen_at=r["first_seen_at"],
            last_seen_at=r["last_seen_at"],
        )
        for r in rows
    ]


def get_ci_status_impl(
    db_path: str,
    repo: str,
    sha: str | None = None,
    branch: str | None = None,
) -> CIStatus | None:
    """Return the most recent workflow run for (repo, sha|branch) with jobs and annotations.

    If `sha` is given, prefer the latest run on that SHA.
    If `branch` is given (no SHA), return the latest run on that branch.
    Returns None if no matching run exists.
    Raises ValueError if neither sha nor branch is provided.
    """
    if not sha and not branch:
        raise ValueError("must supply sha or branch")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if sha:
            cur = conn.execute(
                """
                SELECT id, repo, head_sha, head_branch, status, conclusion,
                       started_at, completed_at, html_url
                FROM workflow_runs
                WHERE repo = ? AND head_sha = ?
                ORDER BY COALESCE(started_at, last_seen_at) DESC LIMIT 1
                """,
                (repo, sha),
            )
        else:
            cur = conn.execute(
                """
                SELECT id, repo, head_sha, head_branch, status, conclusion,
                       started_at, completed_at, html_url
                FROM workflow_runs
                WHERE repo = ? AND head_branch = ?
                ORDER BY COALESCE(started_at, last_seen_at) DESC LIMIT 1
                """,
                (repo, branch),
            )

        row = cur.fetchone()
        if row is None:
            return None

        status = CIStatus(
            run_id=row["id"],
            repo=row["repo"],
            head_sha=row["head_sha"],
            head_branch=row["head_branch"],
            status=row["status"],
            conclusion=row["conclusion"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            html_url=row["html_url"],
        )

        jobs_cur = conn.execute(
            """
            SELECT id, name, conclusion, started_at, completed_at
            FROM workflow_jobs WHERE run_id = ? ORDER BY started_at
            """,
            (row["id"],),
        )
        for j in jobs_cur.fetchall():
            job = CIJob(
                id=j["id"],
                name=j["name"],
                conclusion=j["conclusion"],
                started_at=j["started_at"],
                completed_at=j["completed_at"],
            )
            ann_cur = conn.execute(
                """
                SELECT level, tool, rule_id, path, start_line, message
                FROM workflow_annotations WHERE job_id = ? LIMIT ?
                """,
                (j["id"], MAX_ANNOTATIONS_PER_JOB),
            )
            for a in ann_cur.fetchall():
                job.annotations.append(
                    CIAnnotation(
                        level=a["level"],
                        tool=a["tool"],
                        rule_id=a["rule_id"],
                        path=a["path"],
                        start_line=a["start_line"],
                        message=a["message"],
                    )
                )
            status.jobs.append(job)

        return status
    finally:
        conn.close()
