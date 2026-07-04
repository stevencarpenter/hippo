"""Training data export for fine-tuning enrichment models.

Exports knowledge nodes from all enrichment sources (shell, Claude, OpenCode,
browser, workflow, auto-memory) as JSONL chat-format pairs suitable for LoRA
fine-tuning with mlx-lm.

Each example contains:
  - system: source-specific enrichment prompt (matches what the brain sends)
  - user: reconstructed enrichment input (events, session data, URLs, CI runs)
  - assistant: full validated JSON output the enrichment model produced
"""

import json
import random
from pathlib import Path

# Source-specific system prompts — imported from the enrichment modules so
# they stay in sync with the live pipeline.  The auto-memory prompt is
# truncated to its system-role text only (the enrichment loop appends
# document chunks as the user message).
from hippo_brain.enrichment import SYSTEM_PROMPT as SHELL_SYSTEM_PROMPT
from hippo_brain.claude_sessions import CLAUDE_SYSTEM_PROMPT
from hippo_brain.browser_enrichment import BROWSER_SYSTEM_PROMPT
from hippo_brain.workflow_enrichment import WORKFLOW_SYSTEM_PROMPT
from hippo_brain.auto_memory import (
    MEMORY_ENRICHMENT_SYSTEM_PROMPT as MEMORY_SYSTEM_PROMPT,
    render_memory_enrichment_input,
)


def _write_jsonl(path: Path, examples: list[dict]) -> None:
    with open(path, "w") as f:
        for ex in examples:
            f.write(json.dumps(ex) + "\n")


def _build_shell_user_message(events: list[tuple]) -> str:
    """Reconstruct the user prompt the brain sends for shell enrichment."""
    parts = []
    for i, (cmd, exit_code, duration_ms, cwd, git_branch, shell) in enumerate(events, 1):
        actor = (
            "Claude Code (AI agent)" if shell in ("claude-code", "claude") else "developer (human)"
        )
        lines = [f"Event {i} (executed by {actor}):"]
        lines.append(f"  command: {cmd}")
        lines.append(f"  exit_code: {exit_code}")
        lines.append(f"  duration_ms: {duration_ms}")
        lines.append(f"  cwd: {cwd or ''}")
        if git_branch:
            lines.append(f"  git_branch: {git_branch}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _build_agentic_user_message(sessions: list[tuple]) -> str:
    """Reconstruct the user prompt for Claude/OpenCode session enrichment."""
    parts = []
    for i, (
        summary_text,
        tool_calls_json,
        user_prompts_json,
        cwd,
        git_branch,
        model,
        agent,
        start_time,
        end_time,
        message_count,
    ) in enumerate(sessions, 1):
        lines = [f"Segment {i}:"]
        lines.append(f"  summary: {summary_text}")
        if user_prompts_json:
            try:
                prompts = json.loads(user_prompts_json)
                if prompts:
                    lines.append(f"  user_prompts: {json.dumps(prompts[:3])}")
            except json.JSONDecodeError, TypeError:
                pass
        if tool_calls_json:
            try:
                calls = json.loads(tool_calls_json)
                if calls:
                    lines.append(f"  tool_calls ({len(calls)}): {json.dumps(calls[:5])}")
            except json.JSONDecodeError, TypeError:
                pass
        if cwd:
            lines.append(f"  cwd: {cwd}")
        if git_branch:
            lines.append(f"  git_branch: {git_branch}")
        if model:
            lines.append(f"  model: {model}")
        if agent:
            lines.append(f"  agent: {agent}")
        if message_count:
            lines.append(f"  message_count: {message_count}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _build_browser_user_message(events: list[tuple]) -> str:
    """Reconstruct the user prompt for browser history enrichment."""
    parts = []
    for i, (url, title, domain, dwell_ms, scroll_depth, search_query) in enumerate(events, 1):
        lines = [f"Page {i}:"]
        lines.append(f"  url: {url}")
        if title:
            lines.append(f"  title: {title}")
        lines.append(f"  domain: {domain}")
        dwell_s = (dwell_ms or 0) / 1000.0
        time_line = f"  time spent: {dwell_s:.1f}s"
        if scroll_depth is not None:
            time_line += f", scrolled: {int(scroll_depth * 100)}%"
        lines.append(time_line)
        if search_query:
            lines.append(f"  search query: {search_query}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _build_workflow_user_message(run: tuple) -> str:
    """Reconstruct the user prompt for CI workflow enrichment."""
    (
        repo,
        head_sha,
        head_branch,
        event,
        status,
        conclusion,
        started_at,
        completed_at,
        html_url,
        actor,
    ) = run
    lines = ["Workflow run:"]
    lines.append(f"  repo: {repo}")
    lines.append(f"  sha: {head_sha}")
    if head_branch:
        lines.append(f"  branch: {head_branch}")
    lines.append(f"  event: {event}")
    lines.append(f"  status: {status}")
    if conclusion:
        lines.append(f"  conclusion: {conclusion}")
    if html_url:
        lines.append(f"  url: {html_url}")
    if actor:
        lines.append(f"  actor: {actor}")
    return "\n".join(lines)


def export_training_data(
    conn,
    output_dir: str | Path,
    since_ms: int | None = None,
    min_events: int = 1,
) -> dict:
    """Export knowledge nodes as JSONL conversation pairs for fine-tuning.

    Exports nodes from all enrichment sources (shell, Claude/OpenCode agentic
    sessions, browser history, CI workflow runs, auto-memory).  Each example
    includes the source-appropriate system prompt, a reconstructed user prompt
    matching the live enrichment input format, and the full validated JSON
    output the enrichment model produced.

    Returns stats dict with total, train, valid, test counts and per-source
    breakdown.

    ``min_events`` applies to the multi-event sources (shell, agentic,
    browser).  Workflow and auto-memory nodes are single-input by
    construction (one run / one revision per node), so they are exported
    regardless of ``min_events`` rather than vanishing when it is > 1.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    examples: list[dict] = []
    source_counts: dict[str, int] = {}

    # ── 1. Shell enrichment nodes ──────────────────────────────────────
    shell_sql = """
        SELECT kn.id, kn.content
        FROM knowledge_nodes kn
        WHERE kn.outcome IN ('success', 'partial')
          AND kn.node_type = 'observation'
          AND kn.id IN (SELECT DISTINCT knowledge_node_id FROM knowledge_node_events)
          AND kn.id NOT IN (SELECT DISTINCT knowledge_node_id FROM knowledge_node_agentic_sessions)
          AND kn.id NOT IN (SELECT DISTINCT knowledge_node_id FROM knowledge_node_browser_events)
          AND kn.id NOT IN (SELECT DISTINCT knowledge_node_id FROM knowledge_node_workflow_runs)
    """
    shell_params: list = []
    if since_ms is not None:
        shell_sql += " AND kn.created_at >= ?"
        shell_params.append(since_ms)

    for node_id, content in conn.execute(shell_sql, shell_params).fetchall():
        events = conn.execute(
            """SELECT e.command, e.exit_code, e.duration_ms, e.cwd, e.git_branch, e.shell
               FROM events e
               JOIN knowledge_node_events kne ON kne.event_id = e.id
               WHERE kne.knowledge_node_id = ?
                 AND e.probe_tag IS NULL
               ORDER BY e.timestamp ASC""",
            (node_id,),
        ).fetchall()
        if len(events) < min_events:
            continue
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError, TypeError:
            continue
        user_msg = _build_shell_user_message(events)
        assistant_msg = json.dumps(parsed, ensure_ascii=False)
        examples.append(
            {
                "messages": [
                    {"role": "system", "content": SHELL_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": assistant_msg},
                ]
            }
        )
        source_counts["shell"] = source_counts.get("shell", 0) + 1

    # ── 2. Agentic session enrichment nodes (Claude + OpenCode) ────────
    agentic_sql = """
        SELECT kn.id, kn.content
        FROM knowledge_nodes kn
        WHERE kn.outcome IN ('success', 'partial')
          AND kn.node_type = 'observation'
          AND kn.id IN (SELECT DISTINCT knowledge_node_id FROM knowledge_node_agentic_sessions)
    """
    agentic_params: list = []
    if since_ms is not None:
        agentic_sql += " AND kn.created_at >= ?"
        agentic_params.append(since_ms)

    for node_id, content in conn.execute(agentic_sql, agentic_params).fetchall():
        sessions = conn.execute(
            """SELECT as_.summary_text, as_.tool_calls_json, as_.user_prompts_json,
                      as_.cwd, as_.git_branch, as_.model, as_.agent,
                      as_.start_time, as_.end_time, as_.message_count
               FROM agentic_sessions as_
               JOIN knowledge_node_agentic_sessions knas ON knas.agentic_session_id = as_.id
               WHERE knas.knowledge_node_id = ?
                 AND as_.probe_tag IS NULL
               ORDER BY as_.start_time ASC""",
            (node_id,),
        ).fetchall()
        if len(sessions) < min_events:
            continue
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError, TypeError:
            continue
        user_msg = _build_agentic_user_message(sessions)
        assistant_msg = json.dumps(parsed, ensure_ascii=False)
        examples.append(
            {
                "messages": [
                    {"role": "system", "content": CLAUDE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": assistant_msg},
                ]
            }
        )
        source_counts["agentic"] = source_counts.get("agentic", 0) + 1

    # ── 3. Browser enrichment nodes ────────────────────────────────────
    browser_sql = """
        SELECT kn.id, kn.content
        FROM knowledge_nodes kn
        WHERE kn.outcome IN ('success', 'partial')
          AND kn.node_type = 'observation'
          AND kn.id IN (SELECT DISTINCT knowledge_node_id FROM knowledge_node_browser_events)
    """
    browser_params: list = []
    if since_ms is not None:
        browser_sql += " AND kn.created_at >= ?"
        browser_params.append(since_ms)

    for node_id, content in conn.execute(browser_sql, browser_params).fetchall():
        events = conn.execute(
            """SELECT be.url, be.title, be.domain, be.dwell_ms, be.scroll_depth, be.search_query
               FROM browser_events be
               JOIN knowledge_node_browser_events knbe ON knbe.browser_event_id = be.id
               WHERE knbe.knowledge_node_id = ?
                 AND be.probe_tag IS NULL
               ORDER BY be.timestamp ASC""",
            (node_id,),
        ).fetchall()
        if len(events) < min_events:
            continue
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError, TypeError:
            continue
        user_msg = _build_browser_user_message(events)
        assistant_msg = json.dumps(parsed, ensure_ascii=False)
        examples.append(
            {
                "messages": [
                    {"role": "system", "content": BROWSER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": assistant_msg},
                ]
            }
        )
        source_counts["browser"] = source_counts.get("browser", 0) + 1

    # ── 4. Workflow (CI) enrichment nodes ──────────────────────────────
    # No outcome filter here, deliberately: for workflow nodes the outcome
    # column holds the raw GitHub conclusion (success/failure/cancelled/…),
    # not the LLM's success/partial vocab (see workflow_enrichment.py), and
    # enriched *failed* runs — root cause + fix summaries — are exactly the
    # training signal we want to keep.
    workflow_sql = """
        SELECT kn.id, kn.content
        FROM knowledge_nodes kn
        WHERE kn.node_type = 'change_outcome'
          AND kn.id IN (SELECT DISTINCT knowledge_node_id FROM knowledge_node_workflow_runs)
    """
    wf_params: list = []
    if since_ms is not None:
        workflow_sql += " AND kn.created_at >= ?"
        wf_params.append(since_ms)

    for node_id, content in conn.execute(workflow_sql, wf_params).fetchall():
        run_row = conn.execute(
            """SELECT wr.repo, wr.head_sha, wr.head_branch, wr.event, wr.status,
                      wr.conclusion, wr.started_at, wr.completed_at, wr.html_url, wr.actor
               FROM workflow_runs wr
               JOIN knowledge_node_workflow_runs knwr ON knwr.run_id = wr.id
               WHERE knwr.knowledge_node_id = ?
               LIMIT 1""",
            (node_id,),
        ).fetchone()
        if run_row is None:
            continue
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError, TypeError:
            continue
        user_msg = _build_workflow_user_message(run_row)
        assistant_msg = json.dumps(parsed, ensure_ascii=False)
        examples.append(
            {
                "messages": [
                    {"role": "system", "content": WORKFLOW_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": assistant_msg},
                ]
            }
        )
        source_counts["workflow"] = source_counts.get("workflow", 0) + 1

    # ── 5. Auto-memory enrichment nodes ────────────────────────────────
    memory_sql = """
        SELECT DISTINCT kn.id, kn.content
        FROM knowledge_nodes kn
        JOIN knowledge_node_memory_chunks knmc ON knmc.knowledge_node_id = kn.id
        JOIN memory_chunks mc ON mc.id = knmc.memory_chunk_id
        WHERE kn.outcome IN ('success', 'partial')
          AND kn.node_type = 'observation'
    """
    mem_params: list = []
    if since_ms is not None:
        memory_sql += " AND kn.created_at >= ?"
        mem_params.append(since_ms)

    for node_id, content in conn.execute(memory_sql, mem_params).fetchall():
        chunk_rows = conn.execute(
            """SELECT d.repository, d.logical_path, mr.content_hash, mc.content
               FROM memory_chunks mc
               JOIN memory_revisions mr ON mr.id = mc.revision_id
               JOIN memory_documents d ON d.id = mr.document_id
               JOIN knowledge_node_memory_chunks knmc ON knmc.memory_chunk_id = mc.id
               WHERE knmc.knowledge_node_id = ?
               ORDER BY mc.revision_id ASC, mc.ordinal ASC""",
            (node_id,),
        ).fetchall()
        chunk_texts = [row[3] for row in chunk_rows if (row[3] or "").strip()]
        if not chunk_texts:
            continue
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError, TypeError:
            continue
        repository, logical_path, content_hash = chunk_rows[0][:3]
        user_msg = render_memory_enrichment_input(
            repository, logical_path, content_hash, chunk_texts
        )
        assistant_msg = json.dumps(parsed, ensure_ascii=False)
        examples.append(
            {
                "messages": [
                    {"role": "system", "content": MEMORY_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": assistant_msg},
                ]
            }
        )
        source_counts["auto-memory"] = source_counts.get("auto-memory", 0) + 1

    # ── Split & write ──────────────────────────────────────────────────
    if not examples:
        return {"total": 0, "train": 0, "valid": 0, "test": 0, "sources": {}}

    random.shuffle(examples)
    n = len(examples)
    train_end = max(1, int(n * 0.8))
    valid_end = max(train_end + 1, train_end + int(n * 0.1))

    train = examples[:train_end]
    valid = examples[train_end:valid_end]
    test = examples[valid_end:]

    _write_jsonl(output_dir / "train.jsonl", train)
    _write_jsonl(output_dir / "valid.jsonl", valid)
    _write_jsonl(output_dir / "test.jsonl", test)

    return {
        "total": n,
        "train": len(train),
        "valid": len(valid),
        "test": len(test),
        "sources": source_counts,
    }
