"""Tests for Codex (Xcode GitHub Copilot) session log parsing."""

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from hippo_brain.claude_sessions import ensure_claude_tables, insert_segment
from hippo_brain.codex_sessions import (
    DEFAULT_MIN_IDLE_SECONDS,
    _extract_user_text_from_codex_message,
    _parse_ts,
    build_codex_enrichment_summary,
    extract_codex_segments,
    iter_codex_session_files,
)
from hippo_brain.codex_sessions import CodexSessionFile


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _session_meta(session_id: str = "sess-1", cwd: str = "/projects/demo") -> dict:
    return {
        "type": "session_meta",
        "timestamp": "2026-04-18T12:00:00Z",
        "payload": {"id": session_id, "cwd": cwd},
    }


def _user_msg(text: str, ts: str = "2026-04-18T12:00:01Z") -> dict:
    return {
        "type": "event_msg",
        "timestamp": ts,
        "payload": {"type": "user_message", "message": text},
    }


def test_iter_codex_session_files_empty_when_no_sessions_dir():
    with tempfile.TemporaryDirectory() as tmp:
        codex_dir = Path(tmp)
        # no sessions/ subdir
        assert iter_codex_session_files(codex_dir) == []


def test_iter_codex_session_files_returns_empty_when_codex_dir_missing():
    # A nonexistent path must not raise.
    assert iter_codex_session_files(Path("/definitely/not/a/real/codex/dir")) == []


def test_iter_codex_session_files_skips_recently_modified():
    """Files modified within min_idle_seconds must be skipped to avoid
    ingesting still-being-written sessions (segment_index dedup would
    otherwise permanently freeze them at the first observed state)."""
    with tempfile.TemporaryDirectory() as tmp:
        codex_dir = Path(tmp)
        sessions = codex_dir / "sessions" / "2026" / "04" / "18"
        jsonl = sessions / "rollout-1.jsonl"
        _write_jsonl(
            jsonl,
            [_session_meta("sess-live", "/projects/live"), _user_msg("hi")],
        )
        # Freshly written: within idle window => skipped.
        assert iter_codex_session_files(codex_dir, min_idle_seconds=60) == []

        # Backdate mtime beyond the window => included.
        past = time.time() - 120
        os.utime(jsonl, (past, past))
        result = iter_codex_session_files(codex_dir, min_idle_seconds=60)
        assert len(result) == 1
        assert result[0].session_id == "sess-live"
        assert result[0].project_dir == "live"


def test_iter_codex_session_files_default_idle_is_nonzero():
    assert DEFAULT_MIN_IDLE_SECONDS > 0


def test_iter_codex_session_files_tolerates_malformed_json():
    """A corrupt JSONL must not crash discovery of siblings."""
    with tempfile.TemporaryDirectory() as tmp:
        codex_dir = Path(tmp)
        sessions = codex_dir / "sessions"
        bad = sessions / "bad.jsonl"
        good = sessions / "good.jsonl"
        bad.parent.mkdir(parents=True)
        bad.write_text("{not valid json\n")
        _write_jsonl(good, [_session_meta("sess-good", "/projects/good")])

        past = time.time() - 120
        os.utime(bad, (past, past))
        os.utime(good, (past, past))

        files = iter_codex_session_files(codex_dir, min_idle_seconds=60)
        ids = {f.session_id for f in files}
        # bad.jsonl falls back to stem; good.jsonl reads session_meta.
        assert "sess-good" in ids


def test_extract_codex_segments_empty_file():
    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp) / "empty.jsonl"
        empty.write_text("")
        sf = CodexSessionFile(path=empty, session_id="sess-empty", project_dir="empty")
        assert extract_codex_segments(sf) == []


def test_extract_codex_segments_skips_malformed_lines():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "mixed.jsonl"
        with path.open("w") as f:
            f.write("not-json\n")
            f.write(json.dumps(_session_meta()) + "\n")
            f.write("{still bad\n")
            f.write(json.dumps(_user_msg("hello codex")) + "\n")
        sf = CodexSessionFile(path=path, session_id="sess-1", project_dir="demo")
        segments = extract_codex_segments(sf)
        assert len(segments) == 1
        assert segments[0].user_prompts == ["hello codex"]
        assert segments[0].source == "codex"


def test_extract_codex_segments_without_session_meta():
    """A JSONL missing session_meta should still parse; the caller's
    fallback project_dir / session_id are preserved."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "no-meta.jsonl"
        _write_jsonl(path, [_user_msg("solo prompt")])
        sf = CodexSessionFile(path=path, session_id="fallback-id", project_dir="fallback-proj")
        segments = extract_codex_segments(sf)
        assert len(segments) == 1
        assert segments[0].session_id == "fallback-id"
        assert segments[0].project_dir == "fallback-proj"


def test_extract_codex_segments_skips_developer_role_injection():
    """Injected developer-role messages are framework scaffolding, not user
    intent. They must not create segments or appear in user_prompts."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "dev.jsonl"
        _write_jsonl(
            path,
            [
                _session_meta(),
                {
                    "type": "response_item",
                    "timestamp": "2026-04-18T12:00:02Z",
                    "payload": {
                        "role": "developer",
                        "content": [{"type": "output_text", "text": "system setup"}],
                    },
                },
                _user_msg("real ask"),
            ],
        )
        sf = CodexSessionFile(path=path, session_id="sess-1", project_dir="demo")
        segments = extract_codex_segments(sf)
        assert len(segments) == 1
        assert segments[0].user_prompts == ["real ask"]
        # developer message was skipped before the user message so it didn't
        # increment counts for that segment.
        assert "system setup" not in " ".join(segments[0].assistant_texts)


def test_extract_user_text_strips_xcode_context():
    """The Xcode-injected project-context preamble must be stripped so the
    user's real request is what gets enriched."""
    message = (
        "Project structure:\n"
        "  MyApp/\n"
        "    ContentView.swift\n"
        "The user is currently inside this file: ContentView.swift\n"
        "The user has no code selected.\n"
        "Rename all instances of `foo` to `bar`."
    )
    assert (
        _extract_user_text_from_codex_message(message) == "Rename all instances of `foo` to `bar`."
    )


def test_extract_user_text_falls_back_to_last_paragraph():
    message = "Project structure: x\n\nPlease help with this bug."
    # Xcode status regex won't match; falls back to last paragraph.
    assert _extract_user_text_from_codex_message(message) == "Please help with this bug."


def test_extract_user_text_returns_raw_when_no_markers():
    message = "Just a bare request."
    assert _extract_user_text_from_codex_message(message) == "Just a bare request."


def test_codex_segment_inserted_with_codex_prompt_builder():
    """insert_segment must route source='codex' segments through the codex
    enrichment builder so the LLM doesn't frame them as Claude sessions."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    conn = sqlite3.connect(db_path)
    try:
        # Preconditions ensure_claude_tables checks for (knowledge_nodes ref)
        conn.execute("CREATE TABLE knowledge_nodes (id INTEGER PRIMARY KEY)")
        conn.commit()
        ensure_claude_tables(conn)

        path = Path(tempfile.mkdtemp()) / "codex.jsonl"
        _write_jsonl(
            path,
            [_session_meta("sess-cdx", "/projects/demo"), _user_msg("do the thing")],
        )
        sf = CodexSessionFile(path=path, session_id="sess-cdx", project_dir="demo")
        segments = extract_codex_segments(sf)
        assert segments and segments[0].source == "codex"

        seg_id = insert_segment(conn, segments[0])
        assert seg_id is not None
        summary = conn.execute(
            "SELECT summary_text FROM claude_sessions WHERE id = ?", (seg_id,)
        ).fetchone()[0]
        # The codex builder's header must be present; Claude's "Claude Code"
        # header must not, so enrichment sees the correct source.
        assert "GitHub Copilot (Codex) session" in summary
    finally:
        conn.close()
        Path(db_path).unlink(missing_ok=True)


def test_build_codex_enrichment_summary_empty_list():
    assert build_codex_enrichment_summary([]) == ""


# ---------------------------------------------------------------------------
# _tool_summary: malformed-args resilience
# ---------------------------------------------------------------------------


def _fn_call(
    name: str,
    arguments,
    ts: str = "2026-04-18T12:00:02Z",
) -> dict:
    """Build a response_item / function_call JSONL entry."""
    return {
        "type": "response_item",
        "timestamp": ts,
        "payload": {"type": "function_call", "name": name, "arguments": arguments},
    }


def test_tool_summary_malformed_json_falls_back_to_raw():
    """_tool_summary must not raise on invalid JSON args; the raw arg string
    (truncated) is used as the summary so the enrichment prompt still has
    something meaningful."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad-args.jsonl"
        raw_args = "this is not json at all"
        _write_jsonl(
            path,
            [
                _session_meta(),
                _user_msg("go"),
                _fn_call("Bash", raw_args),
            ],
        )
        sf = CodexSessionFile(path=path, session_id="s", project_dir="demo")
        segments = extract_codex_segments(sf)
        assert len(segments) == 1
        assert segments[0].tool_calls == [{"name": "Bash", "summary": raw_args[:80]}]


def test_tool_summary_prefers_cmd_then_command_then_filepath():
    """Summary extraction must follow the documented key priority order —
    each key yields the expected (truncated) value."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "priority.jsonl"
        _write_jsonl(
            path,
            [
                _session_meta(),
                _user_msg("go"),
                _fn_call("A", json.dumps({"cmd": "cargo test", "command": "nope"})),
                _fn_call("B", json.dumps({"command": "ls -la", "path": "/nope"})),
                _fn_call("C", json.dumps({"filePath": "/src/main.rs"})),
                _fn_call("D", json.dumps({"path": "/src/lib.rs"})),
                _fn_call("E", json.dumps({"uri": "file:///tmp/x"})),
                _fn_call("F", json.dumps({"query": "fn main"})),
                _fn_call("G", json.dumps({"other_key": "fallback str"})),
                _fn_call("H", json.dumps({"num": 42})),  # no string values
            ],
        )
        sf = CodexSessionFile(path=path, session_id="s", project_dir="demo")
        segments = extract_codex_segments(sf)
        tools = {tc["name"]: tc["summary"] for tc in segments[0].tool_calls}
        assert tools["A"] == "cargo test"  # cmd wins over command
        assert tools["B"] == "ls -la"  # command wins over path
        assert tools["C"] == "/src/main.rs"
        assert tools["D"] == "/src/lib.rs"
        assert tools["E"] == "file:///tmp/x"
        assert tools["F"] == "fn main"
        assert tools["G"] == "fallback str"  # falls back to first string value
        # All values non-string: no key/string match; fallback truncates the
        # raw JSON args string to 80 chars so the prompt still shows something.
        assert tools["H"] == '{"num": 42}'


def test_tool_summary_dict_arguments_are_json_encoded():
    """Codex sometimes emits `arguments` as a dict (not a JSON string). It
    must be re-encoded so _tool_summary can still extract a key."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "dict-args.jsonl"
        _write_jsonl(
            path,
            [
                _session_meta(),
                _user_msg("go"),
                {
                    "type": "response_item",
                    "timestamp": "2026-04-18T12:00:02Z",
                    "payload": {
                        "type": "function_call",
                        "name": "Bash",
                        "arguments": {"cmd": "echo hi"},
                    },
                },
            ],
        )
        sf = CodexSessionFile(path=path, session_id="s", project_dir="demo")
        segments = extract_codex_segments(sf)
        assert segments[0].tool_calls == [{"name": "Bash", "summary": "echo hi"}]


def test_tool_summary_skipped_when_name_missing():
    """If a function_call has no name, it must not pollute tool_calls even
    though the summary could be extracted."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "noname.jsonl"
        _write_jsonl(
            path,
            [
                _session_meta(),
                _user_msg("go"),
                {
                    "type": "response_item",
                    "timestamp": "2026-04-18T12:00:02Z",
                    "payload": {
                        "type": "function_call",
                        "arguments": json.dumps({"cmd": "ignored"}),
                    },
                },
            ],
        )
        sf = CodexSessionFile(path=path, session_id="s", project_dir="demo")
        segments = extract_codex_segments(sf)
        assert segments[0].tool_calls == []


# ---------------------------------------------------------------------------
# turn_context: cwd updates mid-session
# ---------------------------------------------------------------------------


def test_turn_context_updates_current_segment_cwd():
    """turn_context entries that follow a user message must update the
    active segment's cwd — the enrichment prompt uses cwd as the project
    label, so stale cwds would misattribute work."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cwd.jsonl"
        _write_jsonl(
            path,
            [
                _session_meta("s1", "/projects/first"),
                _user_msg("first ask", ts="2026-04-18T12:00:01Z"),
                {
                    "type": "turn_context",
                    "timestamp": "2026-04-18T12:00:02Z",
                    "payload": {"cwd": "/projects/second"},
                },
                # Still within the 5-min window: same segment, new cwd.
                _user_msg("second ask", ts="2026-04-18T12:00:30Z"),
            ],
        )
        sf = CodexSessionFile(path=path, session_id="s1", project_dir="first")
        segments = extract_codex_segments(sf)
        assert len(segments) == 1
        assert segments[0].cwd == "/projects/second"
        assert segments[0].user_prompts == ["first ask", "second ask"]


def test_turn_context_before_any_user_sets_session_cwd():
    """turn_context arriving before the first user message must still
    update the session cwd used when the first segment is opened."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "early-ctx.jsonl"
        _write_jsonl(
            path,
            [
                _session_meta("s1", "/projects/orig"),
                {
                    "type": "turn_context",
                    "timestamp": "2026-04-18T12:00:00.5Z",
                    "payload": {"cwd": "/projects/override"},
                },
                _user_msg("hello", ts="2026-04-18T12:00:01Z"),
            ],
        )
        sf = CodexSessionFile(path=path, session_id="s1", project_dir="orig")
        segments = extract_codex_segments(sf)
        assert len(segments) == 1
        assert segments[0].cwd == "/projects/override"


# ---------------------------------------------------------------------------
# Segment boundaries
# ---------------------------------------------------------------------------


def test_segment_boundary_on_time_gap():
    """User messages separated by more than TASK_GAP_MS (5 minutes) must
    split into separate segments; segment_index increments monotonically."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "gap.jsonl"
        _write_jsonl(
            path,
            [
                _session_meta(),
                _user_msg("task 1", ts="2026-04-18T12:00:00Z"),
                # 10-minute gap
                _user_msg("task 2", ts="2026-04-18T12:10:00Z"),
            ],
        )
        sf = CodexSessionFile(path=path, session_id="s", project_dir="demo")
        segments = extract_codex_segments(sf)
        assert len(segments) == 2
        assert segments[0].user_prompts == ["task 1"]
        assert segments[1].user_prompts == ["task 2"]
        assert segments[0].segment_index == 0
        assert segments[1].segment_index == 1


def test_segment_boundary_on_char_cap():
    """Segments must split once accumulated content exceeds max_prompt_chars
    so the enrichment prompt stays within budget."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cap.jsonl"
        big = "x" * 400  # user_prompts store up to 500 chars
        _write_jsonl(
            path,
            [
                _session_meta(),
                _user_msg(big, ts="2026-04-18T12:00:00Z"),
                _user_msg(big, ts="2026-04-18T12:00:10Z"),
                _user_msg(big, ts="2026-04-18T12:00:20Z"),
                # With max_prompt_chars=600, the second user message already
                # pushes current_chars > cap; the third triggers a split.
                _user_msg("last", ts="2026-04-18T12:00:30Z"),
            ],
        )
        sf = CodexSessionFile(path=path, session_id="s", project_dir="demo")
        segments = extract_codex_segments(sf, max_prompt_chars=600)
        # Split must have occurred: more than one segment.
        assert len(segments) >= 2
        # The final "last" prompt is in a later segment than the first big one.
        first_seg_prompts = segments[0].user_prompts
        all_prompts = [p for seg in segments for p in seg.user_prompts]
        assert "last" in all_prompts
        assert "last" not in first_seg_prompts


# ---------------------------------------------------------------------------
# Response item parsing
# ---------------------------------------------------------------------------


def test_response_item_function_call_extracts_tools():
    """function_call and custom_tool_call payloads must produce tool_calls
    entries with name + summary."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fn.jsonl"
        _write_jsonl(
            path,
            [
                _session_meta(),
                _user_msg("do"),
                _fn_call("Bash", json.dumps({"cmd": "cargo test"})),
                {
                    "type": "response_item",
                    "timestamp": "2026-04-18T12:00:03Z",
                    "payload": {
                        "type": "custom_tool_call",
                        "tool_name": "Xref",
                        "input": json.dumps({"query": "fn main"}),
                    },
                },
            ],
        )
        sf = CodexSessionFile(path=path, session_id="s", project_dir="demo")
        segments = extract_codex_segments(sf)
        assert len(segments) == 1
        tools = segments[0].tool_calls
        assert {"name": "Bash", "summary": "cargo test"} in tools
        assert {"name": "Xref", "summary": "fn main"} in tools


def test_response_item_assistant_output_text_is_extracted():
    """Assistant response items with list-of-blocks content must contribute
    output_text blocks to assistant_texts (truncated at 300 chars).
    Non-output_text blocks must be ignored."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "asst.jsonl"
        long_text = "y" * 400
        _write_jsonl(
            path,
            [
                _session_meta(),
                _user_msg("hi"),
                {
                    "type": "response_item",
                    "timestamp": "2026-04-18T12:00:02Z",
                    "payload": {
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "first reply"},
                            {"type": "reasoning", "text": "should be skipped"},
                            {"type": "output_text", "text": long_text},
                            {"type": "output_text", "text": ""},  # empty -> skip
                            "not-a-dict-block",  # malformed -> skip
                        ],
                    },
                },
            ],
        )
        sf = CodexSessionFile(path=path, session_id="s", project_dir="demo")
        segments = extract_codex_segments(sf)
        assert len(segments) == 1
        texts = segments[0].assistant_texts
        assert texts[0] == "first reply"
        assert texts[1] == "y" * 300  # truncated
        assert "should be skipped" not in " ".join(texts)
        # Only the two non-empty output_text blocks were kept.
        assert len(texts) == 2


def test_response_item_assistant_ignores_non_list_content():
    """If assistant content is not a list (e.g. a string), nothing is
    extracted — the parser must not crash."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "asst-str.jsonl"
        _write_jsonl(
            path,
            [
                _session_meta(),
                _user_msg("hi"),
                {
                    "type": "response_item",
                    "timestamp": "2026-04-18T12:00:02Z",
                    "payload": {"role": "assistant", "content": "bare string content"},
                },
            ],
        )
        sf = CodexSessionFile(path=path, session_id="s", project_dir="demo")
        segments = extract_codex_segments(sf)
        assert segments[0].assistant_texts == []


def test_user_message_empty_string_is_skipped():
    """A user_message with an empty message field must not open a segment
    nor increment counts — it's a noise entry."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "empty-msg.jsonl"
        _write_jsonl(
            path,
            [
                _session_meta(),
                _user_msg("", ts="2026-04-18T12:00:01Z"),
                _user_msg("real", ts="2026-04-18T12:00:02Z"),
            ],
        )
        sf = CodexSessionFile(path=path, session_id="s", project_dir="demo")
        segments = extract_codex_segments(sf)
        assert len(segments) == 1
        assert segments[0].user_prompts == ["real"]


# ---------------------------------------------------------------------------
# _parse_ts: direct coverage of fallback branches
# ---------------------------------------------------------------------------


def test_parse_ts_empty_string_returns_zero():
    """Missing or empty timestamps must return 0 (not raise) so downstream
    segment-time logic treats them as "unknown"."""
    assert _parse_ts("") == 0


def test_parse_ts_malformed_string_returns_zero():
    """An unparseable timestamp string must fall back to 0 instead of
    raising — codex JSONL lines in the wild have been observed with
    garbage values here."""
    assert _parse_ts("not-a-timestamp") == 0


def test_parse_ts_valid_iso_roundtrip():
    """Well-formed ISO-8601 timestamps convert to epoch milliseconds."""
    from datetime import datetime, timezone

    ts = "2026-04-18T12:00:00Z"
    expected = int(datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert _parse_ts(ts) == expected


# ---------------------------------------------------------------------------
# Misc defensive paths (blank lines, non-dict payload)
# ---------------------------------------------------------------------------


def test_extract_codex_segments_skips_blank_lines_in_body():
    """Blank/whitespace-only lines within a JSONL (common when sessions
    are appended by flush) must be silently skipped."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "blanks.jsonl"
        with path.open("w") as f:
            f.write("\n")  # leading blank
            f.write(json.dumps(_session_meta()) + "\n")
            f.write("   \n")  # whitespace-only
            f.write(json.dumps(_user_msg("hello")) + "\n")
            f.write("\n")  # trailing blank
        sf = CodexSessionFile(path=path, session_id="s", project_dir="demo")
        segments = extract_codex_segments(sf)
        assert len(segments) == 1
        assert segments[0].user_prompts == ["hello"]


def test_iter_codex_session_files_skips_blank_lines_in_meta_scan():
    """iter_codex_session_files' session_meta peek loop must tolerate
    blank lines at the top of a JSONL without aborting the scan."""
    with tempfile.TemporaryDirectory() as tmp:
        codex_dir = Path(tmp)
        sessions = codex_dir / "sessions"
        jsonl = sessions / "rollout-blank.jsonl"
        sessions.mkdir(parents=True)
        with jsonl.open("w") as f:
            f.write("\n")
            f.write("   \n")
            f.write(json.dumps(_session_meta("sess-blank", "/projects/blank")) + "\n")
        past = time.time() - 120
        os.utime(jsonl, (past, past))
        result = iter_codex_session_files(codex_dir, min_idle_seconds=60)
        assert len(result) == 1
        assert result[0].session_id == "sess-blank"


def test_response_item_before_user_message_is_ignored():
    """Response items (assistant/tool) that appear before any user message
    must be dropped — there's no segment yet, and silently extending a
    None segment would crash."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "orphan.jsonl"
        _write_jsonl(
            path,
            [
                _session_meta(),
                # assistant response with no preceding user message
                {
                    "type": "response_item",
                    "timestamp": "2026-04-18T12:00:00Z",
                    "payload": {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "orphan"}],
                    },
                },
                _user_msg("real"),
            ],
        )
        sf = CodexSessionFile(path=path, session_id="s", project_dir="demo")
        segments = extract_codex_segments(sf)
        assert len(segments) == 1
        assert segments[0].user_prompts == ["real"]
        # The orphan assistant text must NOT appear — it had no segment to
        # attach to.
        assert segments[0].assistant_texts == []


def test_extract_codex_segments_skips_entries_with_non_dict_payload():
    """Entries whose `payload` is not a dict (string, list, null) must be
    skipped — the parser only knows how to read dict payloads."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad-payload.jsonl"
        with path.open("w") as f:
            f.write(json.dumps(_session_meta()) + "\n")
            # string payload — must be skipped, not crash
            f.write(json.dumps({"type": "event_msg", "payload": "oops"}) + "\n")
            # list payload — must be skipped, not crash
            f.write(json.dumps({"type": "response_item", "payload": ["a", "b"]}) + "\n")
            f.write(json.dumps(_user_msg("real")) + "\n")
        sf = CodexSessionFile(path=path, session_id="s", project_dir="demo")
        segments = extract_codex_segments(sf)
        assert len(segments) == 1
        assert segments[0].user_prompts == ["real"]


def test_build_codex_enrichment_summary_includes_tools_and_assistant():
    """build_codex_enrichment_summary renders tool_calls and
    assistant_texts sections when present — the enrichment prompt relies
    on these labels to frame the turn."""
    from hippo_brain.claude_sessions import SessionSegment

    seg = SessionSegment(
        session_id="s1",
        project_dir="demo",
        cwd="/projects/demo",
        git_branch=None,
        segment_index=0,
        start_time=1711612800000,
        end_time=1711614600000,
        user_prompts=["fix the thing"],
        assistant_texts=["Sure, here's how."],
        tool_calls=[{"name": "Bash", "summary": "cargo test"}],
        message_count=3,
        source="codex",
    )
    out = build_codex_enrichment_summary([seg])
    assert "GitHub Copilot (Codex) session" in out
    assert "Work performed:" in out
    assert "Bash: cargo test" in out
    assert "Assistant responses (excerpts):" in out
    assert "Sure, here's how." in out
