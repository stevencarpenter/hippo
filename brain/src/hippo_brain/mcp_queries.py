"""Pure query functions for the Hippo MCP server.

Each function takes a sqlite3.Connection and returns a list of dicts.
No MCP framework dependency — reusable from CLI, HTTP, or MCP.
"""

import json
import re
import sqlite3
import time

from hippo_brain.models import CIAnnotation, CIJob, CIStatus, Lesson


MAX_LIMIT = 100
MAX_ANNOTATIONS_PER_JOB = 10


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
    query_pat = f"%{query}%" if query else None
    since_val = since_ms if since_ms else None
    project_pat = f"%{project}%" if project else None
    rows = conn.execute(
        """
        SELECT timestamp, command, exit_code, duration_ms, cwd, git_branch
        FROM events
        WHERE (? IS NULL OR command LIKE ?)
          AND (? IS NULL OR timestamp >= ?)
          AND (? IS NULL OR cwd LIKE ?)
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (query_pat, query_pat, since_val, since_val, project_pat, project_pat, limit),
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
    query_pat = f"%{query}%" if query else None
    since_val = since_ms if since_ms else None
    project_pat = f"%{project}%" if project else None
    rows = conn.execute(
        """
        SELECT start_time, summary_text, cwd, git_branch, message_count,
               tool_calls_json
        FROM claude_sessions
        WHERE (? IS NULL OR summary_text LIKE ?)
          AND (? IS NULL OR start_time >= ?)
          AND (? IS NULL OR cwd LIKE ?)
        ORDER BY start_time DESC
        LIMIT ?
        """,
        (query_pat, query_pat, since_val, since_val, project_pat, project_pat, limit),
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
    query_pat = f"%{query}%" if query else None
    since_val = since_ms if since_ms else None
    rows = conn.execute(
        """
        SELECT timestamp, url, title, domain, dwell_ms, scroll_depth
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
    type_val = entity_type if entity_type else None
    query_pat = f"%{query}%" if query else None
    rows = conn.execute(
        """
        SELECT type, name, canonical, first_seen, last_seen
        FROM entities
        WHERE (? IS NULL OR type = ?)
          AND (? IS NULL OR name LIKE ?)
        ORDER BY last_seen DESC
        LIMIT ?
        """,
        (type_val, type_val, query_pat, query_pat, limit),
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
                ORDER BY started_at DESC LIMIT 1
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
                ORDER BY started_at DESC LIMIT 1
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
