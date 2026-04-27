"""Parse GitHub Copilot (Codex) session logs into segments for enrichment.

Session files live at:
  ~/Library/Developer/Xcode/CodingAssistant/codex/sessions/<year>/<month>/<day>/rollout-*.jsonl

Format: newline-delimited JSON with typed payloads:
  - session_meta: session ID, cwd, model info
  - event_msg / user_message: user's prompt + Xcode project context
  - response_item / function_call: tool invocation
  - response_item / function_call_output: tool result
  - response_item / assistant message: AI response text
  - turn_context: per-turn metadata (model, cwd)
"""

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from hippo_brain.claude_sessions import SessionSegment
from hippo_brain.entity_resolver import strip_worktree_prefix

# 5-minute gap between user prompts = task boundary
TASK_GAP_MS = 5 * 60 * 1000

# Skip files still being written: require this much idle time since last mtime
# before we'll ingest them. Prevents partial reads of active sessions.
DEFAULT_MIN_IDLE_SECONDS = 60

_XCODE_STATUS_PATTERN = re.compile(
    r"The user (?:has (?:no )?(?:code selected|file currently open)|is currently inside this file:[^\n]*)\.?\n",
    re.IGNORECASE,
)


@dataclass
class CodexSessionFile:
    path: Path
    session_id: str
    project_dir: str  # slug (cwd basename or filename stem)


def iter_codex_session_files(
    codex_dir: Path,
    min_idle_seconds: float = DEFAULT_MIN_IDLE_SECONDS,
) -> list[CodexSessionFile]:
    """Discover all Codex session JSONL files.

    Files modified within ``min_idle_seconds`` are skipped — they may still be
    in flight and partial reads would freeze segments at the first observed
    state (insert_segment dedups on segment_index and would reject later,
    fuller versions).
    """
    results = []
    if not codex_dir.is_dir():
        return results

    sessions_dir = codex_dir / "sessions"
    if not sessions_dir.is_dir():
        return results

    cutoff = time.time() - min_idle_seconds if min_idle_seconds > 0 else None

    for jsonl in sorted(sessions_dir.rglob("*.jsonl")):
        if cutoff is not None:
            try:
                if jsonl.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue

        # Peek at session_meta to get the canonical session ID and cwd
        session_id = jsonl.stem  # fallback: filename without extension
        project_dir = jsonl.stem
        try:
            with open(jsonl) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if obj.get("type") == "session_meta":
                        payload = obj.get("payload", {})
                        session_id = payload.get("id", session_id)
                        cwd = payload.get("cwd", "")
                        if cwd:
                            project_dir = Path(cwd).name or project_dir
                        break
        except OSError, json.JSONDecodeError:
            pass

        results.append(CodexSessionFile(path=jsonl, session_id=session_id, project_dir=project_dir))

    return results


def _parse_ts(ts_str: str) -> int:
    """Parse ISO timestamp string to epoch milliseconds."""
    if not ts_str:
        return 0
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except ValueError:
        return 0


def _tool_summary(arguments_str: str) -> str:
    """Format a tool call into a short human-readable summary."""
    try:
        args = json.loads(arguments_str) if arguments_str else {}
    except json.JSONDecodeError:
        args = {}

    # Extract the most informative single argument value
    if "cmd" in args:
        return args["cmd"][:120]
    if "command" in args:
        return args["command"][:120]
    if "filePath" in args:
        return args["filePath"]
    if "path" in args:
        return args["path"]
    if "uri" in args:
        return args["uri"][:100]
    if "query" in args or "pattern" in args:
        return (args.get("query") or args.get("pattern", ""))[:80]

    # Fallback: first non-empty string value
    for v in args.values():
        if isinstance(v, str) and v:
            return v[:80]
    return arguments_str[:80] if arguments_str else ""


def extract_codex_segments(
    session_file: CodexSessionFile, max_prompt_chars: int = 12000
) -> list[SessionSegment]:
    """Parse a Codex session JSONL into SessionSegment objects.

    Segments split at 5-minute gaps between user messages or when accumulated
    content exceeds max_prompt_chars.
    """
    segments: list[SessionSegment] = []
    current: SessionSegment | None = None
    current_chars = 0
    last_user_time = 0
    session_cwd = ""

    with open(session_file.path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = obj.get("type", "")
            ts = _parse_ts(obj.get("timestamp", ""))
            payload = obj.get("payload", {})
            if not isinstance(payload, dict):
                continue

            # session_meta: grab canonical cwd
            if entry_type == "session_meta":
                session_cwd = payload.get("cwd", "")
                continue

            # turn_context: may update cwd
            if entry_type == "turn_context":
                cwd = payload.get("cwd", "")
                if cwd:
                    session_cwd = cwd
                if current is not None and cwd:
                    current.cwd = cwd
                continue

            payload_type = payload.get("type", "")
            payload_role = payload.get("role", "")

            # Skip developer/system injection messages
            if payload_role == "developer":
                continue

            # ---- User message: event_msg user_message ----
            if entry_type == "event_msg" and payload_type == "user_message":
                message = payload.get("message", "")
                if not message:
                    continue

                # Strip Xcode project context prefix (ends before actual user text)
                # The context block ends at a blank line before the real request
                user_text = _extract_user_text_from_codex_message(message)

                # Segment boundary check
                if last_user_time > 0 and ts > 0:
                    gap = ts - last_user_time
                    if gap > TASK_GAP_MS or current_chars > max_prompt_chars:
                        if current is not None and (
                            current.user_prompts or current.tool_calls or current.assistant_texts
                        ):
                            segments.append(current)
                        current = None
                        current_chars = 0

                if current is None:
                    cwd = session_cwd or str(session_file.path.parent)
                    current = SessionSegment(
                        session_id=session_file.session_id,
                        project_dir=session_file.project_dir,
                        cwd=cwd,
                        git_branch=None,
                        segment_index=len(segments),
                        start_time=ts or int(time.time() * 1000),
                        end_time=ts or int(time.time() * 1000),
                        source_file=str(session_file.path),
                        source="codex",
                    )

                if ts > 0:
                    last_user_time = ts
                    current.end_time = max(current.end_time, ts)

                current.message_count += 1
                if user_text:
                    current.user_prompts.append(user_text[:500])
                    current_chars += len(user_text[:500])
                continue

            if current is None:
                continue

            # Update end time
            if ts > 0:
                current.end_time = max(current.end_time, ts)
            current.message_count += 1

            # ---- Tool calls ----
            if entry_type == "response_item" and payload_type in (
                "function_call",
                "custom_tool_call",
            ):
                name = payload.get("name", payload.get("tool_name", ""))
                arguments_str = payload.get("arguments", payload.get("input", ""))
                if isinstance(arguments_str, dict):
                    arguments_str = json.dumps(arguments_str)
                summary = _tool_summary(arguments_str)
                if name:
                    current.tool_calls.append({"name": name, "summary": summary})
                    current_chars += len(summary)
                continue

            # ---- Assistant text responses ----
            if entry_type == "response_item" and payload_role == "assistant":
                content = payload.get("content") or []
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "output_text":
                            text = block.get("text", "")
                            if text:
                                current.assistant_texts.append(text[:300])
                                current_chars += len(text[:300])
                continue

    # Finalize last segment
    if current is not None and (
        current.user_prompts or current.tool_calls or current.assistant_texts
    ):
        segments.append(current)

    return segments


def _extract_user_text_from_codex_message(message: str) -> str:
    """Strip Xcode-injected project context from the user message.

    Codex user_message lines are formatted as:
      Project structure:\\n...\\nThe user is currently inside...\\n
      \\nThe user has [no] code selected.\\n<ACTUAL REQUEST>

    The actual user text follows the last Xcode status line.
    """
    # Strip "The user has [no] code selected.\n" and similar Xcode status lines
    # then take whatever follows as the actual user text
    matches = list(_XCODE_STATUS_PATTERN.finditer(message))
    if matches:
        last_match = matches[-1]
        candidate = message[last_match.end() :].strip()
        if candidate:
            return candidate[:500]

    # Fallback: last paragraph after a blank line
    idx = message.rfind("\n\n")
    if idx != -1:
        candidate = message[idx + 2 :].strip()
        if candidate and not candidate.startswith("Project structure:"):
            return candidate[:500]

    return message[:500]


def build_codex_enrichment_summary(segments: list[SessionSegment]) -> str:
    """Format Codex segments into a summary for the enrichment prompt.

    The segment `cwd` is normalized via `strip_worktree_prefix` so the LLM
    sees the parent-repo path rather than the ephemeral
    `.claude/worktrees/<X>/` subdirectory created by parallel agents.
    """
    parts = []
    for seg in segments:
        cwd = strip_worktree_prefix(seg.cwd)
        header = f"GitHub Copilot (Codex) session (project: {cwd})"
        if seg.start_time and seg.end_time:
            start = datetime.fromtimestamp(seg.start_time / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M"
            )
            end = datetime.fromtimestamp(seg.end_time / 1000, tz=timezone.utc).strftime("%H:%M")
            header += f"\nDuration: {start} – {end} UTC"

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
            lines.append("Assistant responses (excerpts):")
            for text in seg.assistant_texts[:5]:
                lines.append(f'  - "{text}"')
            lines.append("")

        parts.append("\n".join(lines))

    return "\n---\n\n".join(parts)
