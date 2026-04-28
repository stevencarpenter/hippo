import json
import re
import time
import uuid

from hippo_brain.entity_resolver import canonicalize, is_path_type, strip_worktree_prefix
from hippo_brain.models import EnrichmentResult, validate_enrichment_data
from hippo_brain.watchdog import DEFAULT_LOCK_TIMEOUT_MS

STALE_LOCK_TIMEOUT_MS = DEFAULT_LOCK_TIMEOUT_MS

# Shell commands treated as session-lifecycle / no-op noise when paired with
# no output and a sub-100ms duration. Kept small; the duration+output gate
# prevents false positives (a real `clear` that errored still gets enriched).
_TRIVIAL_SHELL_COMMANDS = frozenset(
    {
        "",
        "clear",
        "exit",
        "exec zsh",
        "exec bash",
        "exec fish",
        "exec sh",
        "true",
        ":",
    }
)
_TRIVIAL_SHELL_REGEX = re.compile(r"^exec\s+[\w/.\-]+$")
_SHELL_TRIVIAL_DURATION_MS = 100


def is_enrichment_eligible(event_dict: dict, source: str) -> tuple[bool, str]:
    """Return (eligible, reason) for an enrichment candidate.

    Ineligible events are session-lifecycle noise or empty work units that
    would pollute knowledge-node retrieval without adding signal. Reasons are
    human-readable and stored in the queue's error_message for observability.
    """
    if source == "shell":
        command = (event_dict.get("command") or "").strip()
        stdout = event_dict.get("stdout") or ""
        stderr = event_dict.get("stderr") or ""
        duration = event_dict.get("duration_ms") or 0
        trivial_cmd = (
            command in _TRIVIAL_SHELL_COMMANDS or _TRIVIAL_SHELL_REGEX.match(command) is not None
        )
        if trivial_cmd and not stdout and not stderr and duration < _SHELL_TRIVIAL_DURATION_MS:
            return (
                False,
                f"trivial shell command ({command!r}) with no output and {duration}ms duration",
            )
        return True, "eligible"

    if source == "claude":
        msg_count = event_dict.get("message_count") or 0
        tcj = event_dict.get("tool_calls_json")
        has_tool_calls = False
        if isinstance(tcj, str) and tcj:
            try:
                has_tool_calls = bool(json.loads(tcj))
            except json.JSONDecodeError:
                has_tool_calls = False
        elif tcj:
            has_tool_calls = bool(tcj)
        if msg_count < 3 and not has_tool_calls:
            return (
                False,
                f"claude session message_count={msg_count} < 3 and no tool_calls",
            )
        return True, "eligible"

    if source == "browser":
        dwell_ms = event_dict.get("dwell_ms") or 0
        if dwell_ms < 1000:
            return False, f"browser dwell_ms={dwell_ms} < 1000"
        return True, "eligible"

    if source == "workflow":
        # Workflow runs are infrequent and high-signal; no heuristic filter yet.
        return True, "eligible"

    return True, "unknown source, default eligible"


SHELL_ENTITY_TYPE_MAP = {
    "projects": "project",
    "tools": "tool",
    "files": "file",
    "services": "service",
    "errors": "concept",
    "env_vars": "env_var",
}

# Single source of truth for which entity types carry user-bindable identifiers
# (rendered on the RAG `Entities:` line). Adding a new type to any
# `*_ENTITY_TYPE_MAP` requires updating exactly one of these tuples; the
# taxonomy guard test (brain/tests/test_entity_taxonomy.py) fails otherwise.
IDENTIFIER_ENTITY_TYPES: tuple[str, ...] = ("tool", "file", "service", "project", "env_var")
NON_IDENTIFIER_ENTITY_TYPES: tuple[str, ...] = ("concept",)

# Stamped into `knowledge_nodes.enrichment_version` on every newly written
# node. Ratchets up whenever an enrichment-prompt or entity-taxonomy change
# makes older outputs structurally stale, so the re-enrich script's WHERE
# clause (`enrichment_version < TARGET_ENRICHMENT_VERSION`) naturally
# selects every legacy node for refresh.
#   v1 — original corpus
#   v2 — PRs #100/#105/#107 (verbatim preservation, identifier-dense
#        embed_text, design_decisions, worktree-stripped entity names)
#   v3 — adds the env_var entity bucket (issue #108 follow-up)
CURRENT_ENRICHMENT_VERSION: int = 3


def upsert_entities(conn, node_id: int, entities_dict, entity_type_map: dict, now_ms: int):
    """Upsert entities and link to a knowledge node. Shared across all enrichment sources."""
    all_entities = entities_dict if isinstance(entities_dict, dict) else {}
    entity_ids = []
    for key, entity_type in entity_type_map.items():
        for name in all_entities.get(key, []):
            # For path-type entities, strip worktree prefix from the display
            # name too — not just the canonical key. `canonical` deduplicates
            # correctly, but `name` is what `mcp__hippo__get_entities` and the
            # UI surface, and an ephemeral `.claude/worktrees/<X>/` prefix
            # here means the first write from inside a worktree poisons the
            # display name forever. Non-path entity types (errors stored as
            # `concept`, etc.) are left verbatim — their values may legitimately
            # contain `.claude/worktrees/...` substrings inside stack traces or
            # diagnostic messages, and rewriting those would lose information
            # while creating a name/canonical divergence.
            # On conflict, repair an existing polluted name with the new clean
            # one; otherwise leave it alone (avoid churn for stable rows).
            display_name = strip_worktree_prefix(name) if is_path_type(entity_type) else name
            canonical = canonicalize(entity_type, name)
            cursor = conn.execute(
                """
                INSERT INTO entities (type, name, canonical, first_seen, last_seen, created_at)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (type, canonical) DO
                UPDATE SET last_seen = excluded.last_seen,
                           name = CASE
                               WHEN name LIKE '%.claude/worktrees/%'
                                   THEN excluded.name
                               ELSE name
                           END
                RETURNING id
                """,
                (entity_type, display_name, canonical, now_ms, now_ms, now_ms),
            )
            entity_ids.append(cursor.fetchone()[0])
    if entity_ids:
        conn.executemany(
            "INSERT INTO knowledge_node_entities (knowledge_node_id, entity_id) "
            "VALUES (?, ?) ON CONFLICT DO NOTHING",
            [(node_id, eid) for eid in entity_ids],
        )


SYSTEM_PROMPT = """You are a developer activity analyst. You receive a sequence of shell command events from a single work session and produce structured enrichment data.

Events are labeled with who executed them: "developer (human)" for commands the user typed,
or "Claude Code (AI agent)" for commands executed by an AI coding assistant. Reflect this
distinction in your summary — attribute actions to the correct actor.

IMPORTANT: Be specific. Use actual file names, function names, error messages, and outcomes from the event data. Generic descriptions like "edited a Rust file" are unacceptable. Instead say "added build.rs to hippo-daemon that embeds git metadata via cargo:rustc-env".

VERBATIM PRESERVATION RULE: In every text field (summary, intent, key_decisions, problems_encountered, design_decisions, embed_text), reproduce the following kinds of tokens EXACTLY as they appeared in the source events. Do NOT paraphrase, normalize, or guess at them:
  - Environment variable names (UPPERCASE_WITH_UNDERSCORES, e.g. HIPPO_FORCE)
  - Constants and ALL_CAPS identifiers matching [A-Z][A-Z0-9_]{2,}
  - Semantic versions matching \\d+\\.\\d+\\.\\d+ (e.g. 0.0.26, 2.20.0)
  - Package@version pairs (e.g. "python-multipart 0.0.26", "pygments@2.20.0")
  - Symbol names: function, method, class, struct, trait, type, and constant identifiers
  - CLI flag names (--no-verify, -uall, --release)
  - File paths and command names
If you are unsure of an exact name or version, OMIT it rather than guess. A hallucinated identifier is worse than a missing one — a future agent can re-read the source events to recover what was missed, but cannot un-believe a wrong name.

The embed_text field is what powers semantic search over this work session. It MUST be identifier-dense: include every symbol name, file path, package name, version string, and CLI command that appears in the source events. Density of identifiers beats prose elegance — a future agent will search this field by keyword, not read it aloud. A good embed_text reads like a tag soup of the actual technical content (e.g. "drain_brain pgrep launchctl uv run wrapper crates/hippo-daemon/src/install.rs parse_launchctl_pid"), not like a polished paragraph.

Output a JSON object with these fields:
- summary: Specific description of what was accomplished (not what tools were used)
- intent: The developer's goal (e.g., "testing", "debugging", "deploying", "refactoring")
- outcome: One of "success", "partial", "failure", "unknown"
- key_decisions: List of decisions made and why (e.g., "Chose build.rs over vergen crate for zero dependencies")
- design_decisions: List of "considered X, chose Y, reason Z" structured decisions when the events show an alternative was evaluated and rejected. Each entry is an object with keys "considered" (the abandoned approach), "chosen" (what was picked), and "reason" (why the chosen approach won). Empty list if no alternatives were weighed.
- problems_encountered: List of errors/failures and how they were resolved
- entities: An object with lists of extracted entities:
  - projects: Project names mentioned or inferred
  - tools: CLI tools used (cargo, npm, git, docker, etc.)
  - files: Specific files referenced (use actual paths from the events)
  - services: Services interacted with (databases, APIs, etc.)
  - errors: Actual error messages encountered (not generic descriptions)
  - env_vars: Environment variable names referenced or required (UPPERCASE_WITH_UNDERSCORES, e.g. HIPPO_PROJECT_ROOTS, RUST_LOG, PATH). Include vars that the events read, set, exported, unset, or whose absence caused a failure. Use the exact verbatim name — do not lowercase, abbreviate, or guess.
- tags: Descriptive, specific tags (not "success" or "editing")
- embed_text: A detailed, identifier-dense paragraph (see rule above). Optimized for keyword retrieval, not prose.

Output ONLY valid JSON, no markdown fences or extra text."""


def _actor_label(shell: str) -> str:
    if shell in ("claude-code", "claude"):
        return "Claude Code (AI agent)"
    return "developer (human)"


def build_enrichment_prompt(events: list[dict], browser_context: str = "") -> str:
    """Format events into the user prompt template.

    `cwd` is normalized to strip Claude Code worktree segments
    (`.claude/worktrees/<X>/`) so the LLM sees the parent-repo path rather
    than the ephemeral agent worktree subdirectory. The raw value is
    preserved in the events table.
    """
    lines = []
    for i, ev in enumerate(events, 1):
        actor = _actor_label(ev.get("shell", ""))
        parts = [f"Event {i} (executed by {actor}):"]
        parts.append(f"  command: {ev.get('command', '')}")
        parts.append(f"  exit_code: {ev.get('exit_code', '')}")
        parts.append(f"  duration_ms: {ev.get('duration_ms', '')}")
        parts.append(f"  cwd: {strip_worktree_prefix(ev.get('cwd', ''))}")
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
    prompt = "\n\n".join(lines)
    if browser_context:
        prompt += "\n\n" + browser_context
    return prompt


def parse_enrichment_response(raw: str) -> EnrichmentResult:
    """Strip markdown code fences if present, parse JSON, return dataclass."""
    if not raw:
        raise ValueError("model returned empty response")
    text = raw.strip()
    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    # strict=False permits raw control characters (e.g. unescaped \n) inside
    # string values. JSON spec disallows them, but local LLMs routinely emit
    # them inside multi-line `summary`/`embed_text` values; rejecting these
    # responses sends the retry loop into a hot loop of full-model inferences.
    data = json.loads(text, strict=False)
    return validate_enrichment_data(data)


def claim_pending_events_by_session(
    conn,
    max_per_chunk: int,
    worker_id: str,
    stale_secs: int = 120,
    max_claim_batch: int | None = None,
    stale_lock_timeout_ms: int = STALE_LOCK_TIMEOUT_MS,
) -> list[list[dict]]:
    """Claim pending events grouped by session. Returns list of event chunks.

    Only processes sessions where the last event is older than stale_secs.
    Long sessions are split into chunks at time gaps > 60s or at max_per_chunk.

    `max_claim_batch` caps the total events claimed per invocation across all
    sessions, so one cycle can't claim the entire backlog. When a session has
    more events than the remaining budget, the remainder is left `pending`
    for the next cycle. `None` means no cap.
    """
    now_ms = int(time.time() * 1000)
    stale_threshold_ms = now_ms - (stale_secs * 1000)
    stale_lock_ms = now_ms - stale_lock_timeout_ms
    remaining = max_claim_batch if max_claim_batch is not None else -1

    cursor = conn.execute(
        """
        SELECT e.session_id, COUNT(*) as cnt
        FROM enrichment_queue eq
        JOIN events e ON eq.event_id = e.id
        WHERE (eq.status = 'pending'
           OR (eq.status = 'processing' AND COALESCE(eq.locked_at, 0) <= ?))
          AND e.probe_tag IS NULL
        GROUP BY e.session_id
        HAVING MAX(e.timestamp) < ?
        ORDER BY MIN(e.timestamp) ASC
        """,
        (stale_lock_ms, stale_threshold_ms),
    )
    sessions = cursor.fetchall()

    all_chunks = []
    for session_id, _ in sessions:
        if remaining == 0:
            break
        limit = remaining if remaining > 0 else -1
        cursor = conn.execute(
            """
            UPDATE enrichment_queue
            SET status = 'processing', locked_at = ?, locked_by = ?, updated_at = ?
            WHERE id IN (
                SELECT eq.id FROM enrichment_queue eq
                JOIN events e ON eq.event_id = e.id
                WHERE e.session_id = ?
                  AND e.probe_tag IS NULL
                  AND (eq.status = 'pending'
                       OR (eq.status = 'processing' AND COALESCE(eq.locked_at, 0) <= ?))
                ORDER BY e.timestamp ASC, eq.id ASC
                LIMIT ?
            )
            RETURNING event_id
            """,
            (now_ms, worker_id, now_ms, session_id, stale_lock_ms, limit),
        )
        event_ids = [row[0] for row in cursor.fetchall()]
        conn.commit()
        if remaining > 0:
            remaining -= len(event_ids)

        if not event_ids:
            continue

        placeholders = ",".join("?" * len(event_ids))
        rows = conn.execute(
            f"""SELECT id, session_id, timestamp, command, exit_code, duration_ms,
                       cwd, hostname, shell, git_repo, git_branch, git_commit, git_dirty,
                       stdout, stderr
                FROM events WHERE id IN ({placeholders}) AND probe_tag IS NULL
                ORDER BY timestamp ASC, id ASC""",
            event_ids,
        ).fetchall()
        events = [
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
            for row in rows
        ]
        events.sort(key=lambda e: e["timestamp"])
        events = _skip_ineligible_shell_events(conn, events)

        if not events:
            continue

        chunks = _chunk_events(events, max_per_chunk)
        all_chunks.extend(chunks)

    return all_chunks


def _skip_ineligible_shell_events(conn, events: list[dict]) -> list[dict]:
    """Mark ineligible shell events as skipped in the queue, return the rest."""
    eligible = []
    now_ms = int(time.time() * 1000)
    for ev in events:
        ok, reason = is_enrichment_eligible(ev, "shell")
        if ok:
            eligible.append(ev)
        else:
            conn.execute(
                "UPDATE enrichment_queue "
                "SET status = 'skipped', error_message = ?, "
                "    locked_at = NULL, locked_by = NULL, updated_at = ? "
                "WHERE event_id = ?",
                (reason, now_ms, ev["id"]),
            )
            conn.execute(
                "UPDATE events SET enriched = 1 WHERE id = ?",
                (ev["id"],),
            )
    if len(eligible) != len(events):
        conn.commit()
    return eligible


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

        # Link to events
        conn.executemany(
            "INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (?, ?)",
            [(node_id, eid) for eid in event_ids],
        )

        # Upsert entities
        upsert_entities(conn, node_id, result.entities, SHELL_ENTITY_TYPE_MAP, now_ms)

        # Mark events as enriched
        conn.executemany(
            "UPDATE events SET enriched = 1 WHERE id = ?",
            [(eid,) for eid in event_ids],
        )

        # Mark queue entries done
        conn.executemany(
            "UPDATE enrichment_queue SET status = 'done', updated_at = ? WHERE event_id = ?",
            [(now_ms, eid) for eid in event_ids],
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
