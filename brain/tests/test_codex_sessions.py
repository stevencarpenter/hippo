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
