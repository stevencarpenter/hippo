"""Parse Claude Code session logs into segments for enrichment."""

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from hippo_brain.enrichment import (
    SHELL_ENTITY_TYPE_MAP,
    is_enrichment_eligible,
    upsert_entities,
)
from hippo_brain.models import EnrichmentResult
from hippo_brain.watchdog import DEFAULT_LOCK_TIMEOUT_MS

STALE_LOCK_TIMEOUT_MS = DEFAULT_LOCK_TIMEOUT_MS

# 5-minute gap between user prompts = task boundary
TASK_GAP_MS = 5 * 60 * 1000

CLAUDE_SYSTEM_PROMPT = """You are a developer activity analyst. You receive a summary of a Claude Code AI assistant session segment — what the developer asked and what the AI did on their behalf.

Produce structured enrichment data capturing the knowledge from this work session.

IMPORTANT: Be specific. Use actual file names, function names, error messages, and outcomes from the session data. Generic descriptions are unacceptable.

The embed_text field should read like a developer's work log entry — specific enough that searching for "embedding model configuration" or "clippy warning fix" would find it.

Output a JSON object with these fields:
- summary: Specific description of what was accomplished
- intent: The developer's goal (e.g., "feature development", "debugging", "refactoring", "configuration")
- outcome: One of "success", "partial", "failure", "unknown"
- key_decisions: List of decisions made and why
- problems_encountered: List of errors/failures and how they were resolved
- entities: An object with lists of extracted entities:
  - projects: Project names mentioned or inferred
  - tools: CLI tools and frameworks used
  - files: Specific files referenced (use actual paths)
  - services: Services interacted with (databases, APIs, etc.)
  - errors: Actual error messages encountered
- tags: Descriptive, specific tags
- embed_text: A detailed paragraph a developer would write in a work log. Specific file names, error messages, and outcomes. Optimized for semantic search.

Output ONLY valid JSON, no markdown fences or extra text."""


@dataclass
class SessionFile:
    path: Path
    project_dir: str  # encoded directory name
    session_id: str
    is_subagent: bool
    parent_session_id: str | None


@dataclass
class SessionSegment:
    session_id: str
    project_dir: str
    cwd: str
    git_branch: str | None
    segment_index: int
    start_time: int  # epoch ms
    end_time: int  # epoch ms
    user_prompts: list[str] = field(default_factory=list)
    assistant_texts: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    message_count: int = 0
    token_count: int = 0
    source_file: str = ""
    is_subagent: bool = False
    parent_session_id: str | None = None


def iter_session_files(claude_projects_dir: Path) -> list[SessionFile]:
    """Discover all Claude session JSONL files."""
    results = []
    if not claude_projects_dir.is_dir():
        return results

    for project_dir in sorted(claude_projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        project_name = project_dir.name

        # Main session files
        for jsonl in sorted(project_dir.glob("*.jsonl")):
            session_id = jsonl.stem
            results.append(
                SessionFile(
                    path=jsonl,
                    project_dir=project_name,
                    session_id=session_id,
                    is_subagent=False,
                    parent_session_id=None,
                )
            )

        # Subagent files: <session-uuid>/subagents/<agent-id>.jsonl
        for subagent_dir in sorted(project_dir.glob("*/subagents")):
            parent_session_id = subagent_dir.parent.name
            for jsonl in sorted(subagent_dir.glob("*.jsonl")):
                results.append(
                    SessionFile(
                        path=jsonl,
                        project_dir=project_name,
                        session_id=jsonl.stem,
                        is_subagent=True,
                        parent_session_id=parent_session_id,
                    )
                )

    return results


def _parse_timestamp(ts: str) -> int:
    """Parse ISO timestamp to epoch ms."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _extract_user_text(msg: dict | str) -> str | None:
    """Extract human-typed text from a user message, filtering system content."""
    if isinstance(msg, str):
        if msg.strip().startswith("<"):
            return None
        return msg.strip() or None

    content = msg.get("content", [])
    if isinstance(content, str):
        if content.strip().startswith("<"):
            return None
        return content.strip() or None

    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text and not text.startswith("<"):
                    texts.append(text)
        return "\n".join(texts) if texts else None

    return None


def _extract_tool_summary(block: dict) -> dict | None:
    """Extract a concise summary from a tool_use content block."""
    name = block.get("name", "")
    inp = block.get("input", {})
    if not name:
        return None

    summary = ""
    if name == "Bash":
        summary = inp.get("command", "")[:200]
    elif name in ("Read", "Write"):
        summary = inp.get("file_path", "")
    elif name == "Edit":
        summary = inp.get("file_path", "")
    elif name == "Grep":
        pattern = inp.get("pattern", "")
        path = inp.get("path", "")
        summary = f"{pattern}" + (f" in {path}" if path else "")
    elif name == "Glob":
        summary = inp.get("pattern", "")
    elif name == "Agent":
        summary = inp.get("description", "")[:100]
    else:
        # Generic: stringify first key
        for k, v in inp.items():
            summary = f"{k}={str(v)[:80]}"
            break

    return {"name": name, "summary": summary}


def _extract_assistant_text(msg: dict) -> tuple[list[str], list[dict]]:
    """Extract text excerpts and tool calls from an assistant message."""
    texts = []
    tools = []
    content = msg.get("content", [])
    if not isinstance(content, list):
        return texts, tools

    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text", "").strip()
            if text and len(text) > 20:
                # Truncate long reasoning blocks
                texts.append(text[:300])
        elif block.get("type") == "tool_use":
            tool = _extract_tool_summary(block)
            if tool:
                tools.append(tool)

    return texts, tools


def extract_segments(
    session_file: SessionFile, max_prompt_chars: int = 12000
) -> list[SessionSegment]:
    """Stream a session JSONL and segment at task boundaries.

    Segments split when:
    - Time gap > 5 min between consecutive user prompts
    - Accumulated content exceeds max_prompt_chars
    """
    segments: list[SessionSegment] = []
    current: SessionSegment | None = None
    current_chars = 0
    last_user_time = 0

    with open(session_file.path) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type", "")
            timestamp_str = entry.get("timestamp", "")
            ts = _parse_timestamp(timestamp_str) if timestamp_str else 0

            # Skip noise
            if entry_type in (
                "file-history-snapshot",
                "progress",
                "queue-operation",
                "last-prompt",
            ):
                continue

            cwd = entry.get("cwd", "")
            git_branch = entry.get("gitBranch")

            # Initialize first segment
            if current is None and entry_type in ("user", "assistant", "system"):
                current = SessionSegment(
                    session_id=session_file.session_id,
                    project_dir=session_file.project_dir,
                    cwd=cwd or "",
                    git_branch=git_branch,
                    segment_index=len(segments),
                    start_time=ts or int(time.time() * 1000),
                    end_time=ts or int(time.time() * 1000),
                    source_file=str(session_file.path),
                    is_subagent=session_file.is_subagent,
                    parent_session_id=session_file.parent_session_id,
                )

            if current is None:
                continue

            # Check for segment boundary on user messages
            if entry_type == "user" and last_user_time > 0 and ts > 0:
                gap = ts - last_user_time
                if gap > TASK_GAP_MS or current_chars > max_prompt_chars:
                    # Finalize current segment if it has content
                    if current.user_prompts or current.tool_calls or current.assistant_texts:
                        segments.append(current)
                    current = SessionSegment(
                        session_id=session_file.session_id,
                        project_dir=session_file.project_dir,
                        cwd=cwd or current.cwd,
                        git_branch=git_branch or current.git_branch,
                        segment_index=len(segments),
                        start_time=ts,
                        end_time=ts,
                        source_file=str(session_file.path),
                        is_subagent=session_file.is_subagent,
                        parent_session_id=session_file.parent_session_id,
                    )
                    current_chars = 0

            # Update end time
            if ts > 0:
                current.end_time = max(current.end_time, ts)
            current.message_count += 1

            # Update cwd if available (it can change within a session)
            if cwd:
                current.cwd = cwd

            # Extract content based on type
            if entry_type == "user":
                if ts > 0:
                    last_user_time = ts
                msg = entry.get("message", entry.get("content", ""))
                text = _extract_user_text(msg)
                if text:
                    current.user_prompts.append(text[:500])
                    current_chars += len(text[:500])

                # Count tokens from usage
                if isinstance(msg, dict):
                    usage = msg.get("usage", {})
                    if isinstance(usage, dict):
                        current.token_count += usage.get("input_tokens", 0)

            elif entry_type == "assistant":
                msg = entry.get("message", {})
                if isinstance(msg, dict):
                    texts, tools = _extract_assistant_text(msg)
                    current.assistant_texts.extend(texts)
                    current.tool_calls.extend(tools)
                    current_chars += sum(len(t) for t in texts)
                    current_chars += sum(len(t.get("summary", "")) for t in tools)

                    # Count tokens from usage
                    usage = msg.get("usage", {})
                    if isinstance(usage, dict):
                        current.token_count += usage.get("output_tokens", 0)

    # Finalize last segment
    if current is not None and (
        current.user_prompts or current.tool_calls or current.assistant_texts
    ):
        segments.append(current)

    return segments


def build_claude_enrichment_prompt(segments: list[SessionSegment]) -> str:
    """Format session segments into the enrichment prompt."""
    parts = []
    for seg in segments:
        header = f"Claude Code session segment (project: {seg.cwd}, branch: {seg.git_branch or 'unknown'})"
        if seg.start_time and seg.end_time:
            start = datetime.fromtimestamp(seg.start_time / 1000).strftime("%Y-%m-%d %H:%M")
            end = datetime.fromtimestamp(seg.end_time / 1000).strftime("%H:%M")
            header += f"\nDuration: {start} - {end}"
        if seg.is_subagent:
            header += "\n(This is a subagent session — spawned to handle a delegated task)"

        lines = [header, ""]

        if seg.user_prompts:
            lines.append("User requests:")
            for i, prompt in enumerate(seg.user_prompts, 1):
                lines.append(f'  {i}. "{prompt}"')
            lines.append("")

        if seg.tool_calls:
            lines.append("Work performed:")
            for tc in seg.tool_calls:
                lines.append(f"  - {tc['name']}: {tc['summary']}")
            lines.append("")

        if seg.assistant_texts:
            lines.append("Assistant reasoning (excerpts):")
            # Limit to most informative excerpts
            for text in seg.assistant_texts[:10]:
                lines.append(f'  - "{text}"')
            lines.append("")

        parts.append("\n".join(lines))

    return "\n---\n\n".join(parts)


def ensure_claude_tables(conn) -> None:
    """Ensure Claude session tables exist (Python-side migration for v2 → v3)."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= 3:
        return

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS claude_sessions (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            project_dir TEXT NOT NULL,
            cwd TEXT NOT NULL,
            git_branch TEXT,
            segment_index INTEGER NOT NULL,
            start_time INTEGER NOT NULL,
            end_time INTEGER NOT NULL,
            summary_text TEXT NOT NULL,
            tool_calls_json TEXT,
            user_prompts_json TEXT,
            message_count INTEGER NOT NULL,
            token_count INTEGER,
            source_file TEXT NOT NULL,
            is_subagent INTEGER NOT NULL DEFAULT 0,
            parent_session_id TEXT,
            enriched INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
            UNIQUE (session_id, segment_index)
        );
        CREATE TABLE IF NOT EXISTS knowledge_node_claude_sessions (
            knowledge_node_id INTEGER NOT NULL REFERENCES knowledge_nodes (id),
            claude_session_id INTEGER NOT NULL REFERENCES claude_sessions (id),
            PRIMARY KEY (knowledge_node_id, claude_session_id)
        );
        CREATE TABLE IF NOT EXISTS claude_enrichment_queue (
            id INTEGER PRIMARY KEY,
            claude_session_id INTEGER NOT NULL UNIQUE REFERENCES claude_sessions (id),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'processing', 'done', 'failed', 'skipped')),
            priority INTEGER NOT NULL DEFAULT 5,
            retry_count INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 5,
            error_message TEXT,
            locked_at INTEGER,
            locked_by TEXT,
            created_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
            updated_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
        );
        CREATE INDEX IF NOT EXISTS idx_claude_sessions_cwd ON claude_sessions (cwd);
        CREATE INDEX IF NOT EXISTS idx_claude_sessions_session ON claude_sessions (session_id);
        CREATE INDEX IF NOT EXISTS idx_claude_queue_pending ON claude_enrichment_queue (status, priority)
            WHERE status = 'pending';
        PRAGMA user_version = 3;
        """
    )


def insert_segment(conn, segment: SessionSegment) -> int | None:
    """Insert a session segment and queue it for enrichment. Returns segment id or None if duplicate."""
    summary_text = build_claude_enrichment_prompt([segment])
    tool_calls_json = json.dumps(segment.tool_calls)
    user_prompts_json = json.dumps(segment.user_prompts)
    now_ms = int(time.time() * 1000)

    try:
        cursor = conn.execute(
            """
            INSERT INTO claude_sessions
                (session_id, project_dir, cwd, git_branch, segment_index,
                 start_time, end_time, summary_text, tool_calls_json,
                 user_prompts_json, message_count, token_count, source_file,
                 is_subagent, parent_session_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                segment.session_id,
                segment.project_dir,
                segment.cwd,
                segment.git_branch,
                segment.segment_index,
                segment.start_time,
                segment.end_time,
                summary_text,
                tool_calls_json,
                user_prompts_json,
                segment.message_count,
                segment.token_count,
                segment.source_file,
                1 if segment.is_subagent else 0,
                segment.parent_session_id,
                now_ms,
            ),
        )
        segment_id = cursor.lastrowid

        conn.execute(
            "INSERT INTO claude_enrichment_queue (claude_session_id, created_at) VALUES (?, ?)",
            (segment_id, now_ms),
        )
        conn.commit()
        return segment_id
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            return None
        raise


def claim_pending_claude_segments(
    conn,
    worker_id: str,
    max_claim_batch: int | None = None,
    stale_lock_timeout_ms: int = STALE_LOCK_TIMEOUT_MS,
) -> list[list[dict]]:
    """Claim pending Claude segments. Each segment becomes its own batch (1:1 enrichment).

    `max_claim_batch` caps total segments claimed across all cwd groups so
    one cycle can't drain the entire backlog. `None` disables the cap.
    """
    now_ms = int(time.time() * 1000)
    stale_before_ms = now_ms - stale_lock_timeout_ms
    remaining = max_claim_batch if max_claim_batch is not None else -1

    # Get pending segments grouped by cwd
    cursor = conn.execute(
        """
        SELECT cs.cwd, COUNT(*) as cnt
        FROM claude_enrichment_queue ceq
        JOIN claude_sessions cs ON ceq.claude_session_id = cs.id
        WHERE ceq.status = 'pending'
           OR (ceq.status = 'processing' AND COALESCE(ceq.locked_at, 0) <= ?)
        GROUP BY cs.cwd
        ORDER BY MIN(cs.start_time) ASC
        """,
        (stale_before_ms,),
    )
    cwd_groups = cursor.fetchall()

    all_batches = []
    for cwd, _ in cwd_groups:
        if remaining == 0:
            break
        limit = remaining if remaining > 0 else -1
        cursor = conn.execute(
            """
            UPDATE claude_enrichment_queue
            SET status = 'processing', locked_at = ?, locked_by = ?, updated_at = ?
            WHERE id IN (
                SELECT ceq.id FROM claude_enrichment_queue ceq
                JOIN claude_sessions cs ON ceq.claude_session_id = cs.id
                WHERE cs.cwd = ?
                  AND (ceq.status = 'pending'
                       OR (ceq.status = 'processing' AND COALESCE(ceq.locked_at, 0) <= ?))
                ORDER BY cs.start_time ASC, ceq.id ASC
                LIMIT ?
            )
            RETURNING claude_session_id
            """,
            (now_ms, worker_id, now_ms, cwd, stale_before_ms, limit),
        )
        segment_ids = [row[0] for row in cursor.fetchall()]
        conn.commit()
        if remaining > 0:
            remaining -= len(segment_ids)

        if not segment_ids:
            continue

        placeholders = ",".join("?" * len(segment_ids))
        cursor = conn.execute(
            f"""
            SELECT id, session_id, project_dir, cwd, git_branch, segment_index,
                   start_time, end_time, summary_text, tool_calls_json,
                   user_prompts_json, message_count, token_count, is_subagent
            FROM claude_sessions
            WHERE id IN ({placeholders})
            ORDER BY start_time ASC
            """,
            segment_ids,
        )

        segments = []
        for row in cursor.fetchall():
            segments.append(
                {
                    "id": row[0],
                    "session_id": row[1],
                    "project_dir": row[2],
                    "cwd": row[3],
                    "git_branch": row[4],
                    "segment_index": row[5],
                    "start_time": row[6],
                    "end_time": row[7],
                    "summary_text": row[8],
                    "tool_calls_json": row[9],
                    "user_prompts_json": row[10],
                    "message_count": row[11],
                    "token_count": row[12],
                    "is_subagent": row[13],
                }
            )

        segments = _skip_ineligible_claude_segments(conn, segments)

        # One segment = one knowledge node for maximum search granularity
        all_batches.extend([seg] for seg in segments)

    return all_batches


def _skip_ineligible_claude_segments(conn, segments: list[dict]) -> list[dict]:
    """Mark ineligible Claude session segments as skipped, return the rest."""
    eligible = []
    now_ms = int(time.time() * 1000)
    for seg in segments:
        ok, reason = is_enrichment_eligible(seg, "claude")
        if ok:
            eligible.append(seg)
        else:
            conn.execute(
                "UPDATE claude_enrichment_queue "
                "SET status = 'skipped', error_message = ?, "
                "    locked_at = NULL, locked_by = NULL, updated_at = ? "
                "WHERE claude_session_id = ?",
                (reason, now_ms, seg["id"]),
            )
            conn.execute(
                "UPDATE claude_sessions SET enriched = 1 WHERE id = ?",
                (seg["id"],),
            )
    if len(eligible) != len(segments):
        conn.commit()
    return eligible


def write_claude_knowledge_node(
    conn, result: EnrichmentResult, segment_ids: list[int], model_name: str
) -> int:
    """Insert knowledge node linked to Claude session segments."""
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

        # Link to claude sessions
        conn.executemany(
            "INSERT INTO knowledge_node_claude_sessions (knowledge_node_id, claude_session_id) VALUES (?, ?)",
            [(node_id, sid) for sid in segment_ids],
        )

        # Mark segments as enriched
        placeholders = ",".join("?" * len(segment_ids))
        conn.execute(
            f"UPDATE claude_sessions SET enriched = 1 WHERE id IN ({placeholders})",
            segment_ids,
        )

        # Mark queue entries done
        conn.execute(
            f"""
            UPDATE claude_enrichment_queue SET status = 'done', updated_at = ?
            WHERE claude_session_id IN ({placeholders})
            """,
            [now_ms, *segment_ids],
        )

        # Upsert entities
        upsert_entities(conn, node_id, result.entities, SHELL_ENTITY_TYPE_MAP, now_ms)

        conn.commit()
        return node_id
    except Exception:
        conn.rollback()
        raise


def mark_claude_queue_failed(conn, segment_ids: list[int], error: str) -> None:
    """Increment retry_count; reset to pending if retries remain, failed if exhausted."""
    now_ms = int(time.time() * 1000)
    for seg_id in segment_ids:
        conn.execute(
            """
            UPDATE claude_enrichment_queue
            SET retry_count   = retry_count + 1,
                error_message = ?,
                locked_at     = NULL,
                locked_by     = NULL,
                updated_at    = ?,
                status        = CASE
                                    WHEN retry_count + 1 >= max_retries THEN 'failed'
                                    ELSE 'pending'
                                END
            WHERE claude_session_id = ?
            """,
            (error, now_ms, seg_id),
        )
    conn.commit()
