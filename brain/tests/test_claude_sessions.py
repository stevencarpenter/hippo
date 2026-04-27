"""Tests for Claude Code session log parsing and enrichment."""

import json
import sqlite3
import tempfile
from pathlib import Path

from hippo_brain.claude_sessions import (
    SessionFile,
    SessionSegment,
    build_claude_enrichment_prompt,
    claim_pending_claude_segments,
    ensure_claude_tables,
    extract_segments,
    insert_segment,
    iter_session_files,
    mark_claude_queue_failed,
    write_claude_knowledge_node,
)
from hippo_brain.models import EnrichmentResult


def _make_jsonl_line(entry_type, **kwargs):
    """Create a JSONL line for testing."""
    base = {"type": entry_type, "timestamp": kwargs.pop("timestamp", "2026-03-28T12:00:00.000Z")}
    base.update(kwargs)
    return json.dumps(base)


def _user_msg(text, timestamp="2026-03-28T12:00:00.000Z", cwd="/projects/test"):
    return _make_jsonl_line(
        "user",
        timestamp=timestamp,
        cwd=cwd,
        message={"content": [{"type": "text", "text": text}]},
    )


def _assistant_msg(text="", tools=None, timestamp="2026-03-28T12:01:00.000Z", cwd="/projects/test"):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for tool in tools or []:
        content.append({"type": "tool_use", "name": tool["name"], "input": tool.get("input", {})})
    return _make_jsonl_line(
        "assistant",
        timestamp=timestamp,
        cwd=cwd,
        message={
            "content": content,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        },
    )


def _write_session_file(tmp_dir, project, session_id, lines, subagent=False, parent=None):
    """Write a session JSONL file to a temp directory."""
    project_dir = Path(tmp_dir) / project
    if subagent and parent:
        session_dir = project_dir / parent / "subagents"
    else:
        session_dir = project_dir
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


class TestIterSessionFiles:
    def test_discovers_main_sessions(self, tmp_path):
        _write_session_file(tmp_path, "project-a", "sess-1", [_user_msg("hello")])
        _write_session_file(tmp_path, "project-a", "sess-2", [_user_msg("world")])

        files = iter_session_files(tmp_path)
        assert len(files) == 2
        assert all(not f.is_subagent for f in files)

    def test_discovers_subagent_sessions(self, tmp_path):
        _write_session_file(tmp_path, "project-a", "sess-1", [_user_msg("main")])
        _write_session_file(
            tmp_path,
            "project-a",
            "agent-1",
            [_user_msg("sub")],
            subagent=True,
            parent="sess-1",
        )

        files = iter_session_files(tmp_path)
        assert len(files) == 2
        subs = [f for f in files if f.is_subagent]
        assert len(subs) == 1
        assert subs[0].parent_session_id == "sess-1"

    def test_empty_directory(self, tmp_path):
        files = iter_session_files(tmp_path)
        assert files == []

    def test_nonexistent_directory(self, tmp_path):
        files = iter_session_files(tmp_path / "nonexistent")
        assert files == []


class TestExtractSegments:
    def test_single_segment(self, tmp_path):
        lines = [
            _user_msg("Fix the bug", timestamp="2026-03-28T12:00:00.000Z"),
            _assistant_msg(
                "Looking at the code...",
                tools=[{"name": "Read", "input": {"file_path": "/src/main.rs"}}],
                timestamp="2026-03-28T12:01:00.000Z",
            ),
        ]
        path = _write_session_file(tmp_path, "proj", "s1", lines)
        sf = SessionFile(
            path=path,
            project_dir="proj",
            session_id="s1",
            is_subagent=False,
            parent_session_id=None,
        )

        segments = extract_segments(sf)
        assert len(segments) == 1
        assert segments[0].session_id == "s1"
        assert len(segments[0].user_prompts) == 1
        assert "Fix the bug" in segments[0].user_prompts[0]
        assert len(segments[0].tool_calls) == 1
        assert segments[0].tool_calls[0]["name"] == "Read"

    def test_splits_on_time_gap(self, tmp_path):
        lines = [
            _user_msg("Task 1", timestamp="2026-03-28T12:00:00.000Z"),
            _assistant_msg("Done 1", timestamp="2026-03-28T12:01:00.000Z"),
            # 10-minute gap
            _user_msg("Task 2", timestamp="2026-03-28T12:11:00.000Z"),
            _assistant_msg("Done 2", timestamp="2026-03-28T12:12:00.000Z"),
        ]
        path = _write_session_file(tmp_path, "proj", "s1", lines)
        sf = SessionFile(
            path=path,
            project_dir="proj",
            session_id="s1",
            is_subagent=False,
            parent_session_id=None,
        )

        segments = extract_segments(sf)
        assert len(segments) == 2
        assert "Task 1" in segments[0].user_prompts[0]
        assert "Task 2" in segments[1].user_prompts[0]
        assert segments[0].segment_index == 0
        assert segments[1].segment_index == 1

    def test_skips_noise_types(self, tmp_path):
        lines = [
            _make_jsonl_line("file-history-snapshot"),
            _make_jsonl_line("progress"),
            _user_msg("Real prompt"),
            _make_jsonl_line("queue-operation", operation="enqueue"),
            _assistant_msg("Response"),
        ]
        path = _write_session_file(tmp_path, "proj", "s1", lines)
        sf = SessionFile(
            path=path,
            project_dir="proj",
            session_id="s1",
            is_subagent=False,
            parent_session_id=None,
        )

        segments = extract_segments(sf)
        assert len(segments) == 1
        assert segments[0].message_count == 2  # only user + assistant counted

    def test_filters_system_content_from_user(self, tmp_path):
        lines = [
            _make_jsonl_line(
                "user",
                timestamp="2026-03-28T12:00:00.000Z",
                cwd="/test",
                message="<local-command-caveat>System stuff</local-command-caveat>",
            ),
            _user_msg("Real human prompt"),
            _assistant_msg("Response"),
        ]
        path = _write_session_file(tmp_path, "proj", "s1", lines)
        sf = SessionFile(
            path=path,
            project_dir="proj",
            session_id="s1",
            is_subagent=False,
            parent_session_id=None,
        )

        segments = extract_segments(sf)
        assert len(segments) == 1
        assert len(segments[0].user_prompts) == 1
        assert "Real human prompt" in segments[0].user_prompts[0]

    def test_extracts_tool_summaries(self, tmp_path):
        lines = [
            _user_msg("Do stuff"),
            _assistant_msg(
                "",
                tools=[
                    {"name": "Bash", "input": {"command": "cargo test"}},
                    {"name": "Edit", "input": {"file_path": "/src/lib.rs"}},
                    {"name": "Grep", "input": {"pattern": "fn main", "path": "/src"}},
                ],
            ),
        ]
        path = _write_session_file(tmp_path, "proj", "s1", lines)
        sf = SessionFile(
            path=path,
            project_dir="proj",
            session_id="s1",
            is_subagent=False,
            parent_session_id=None,
        )

        segments = extract_segments(sf)
        tools = segments[0].tool_calls
        assert len(tools) == 3
        assert tools[0] == {"name": "Bash", "summary": "cargo test"}
        assert tools[1] == {"name": "Edit", "summary": "/src/lib.rs"}
        assert tools[2] == {"name": "Grep", "summary": "fn main in /src"}

    def test_empty_session(self, tmp_path):
        lines = [
            _make_jsonl_line("file-history-snapshot"),
            _make_jsonl_line("progress"),
        ]
        path = _write_session_file(tmp_path, "proj", "s1", lines)
        sf = SessionFile(
            path=path,
            project_dir="proj",
            session_id="s1",
            is_subagent=False,
            parent_session_id=None,
        )

        segments = extract_segments(sf)
        assert len(segments) == 0


class TestBuildClaudeEnrichmentPrompt:
    def test_formats_segment(self):
        seg = SessionSegment(
            session_id="s1",
            project_dir="proj",
            cwd="/projects/hippo",
            git_branch="main",
            segment_index=0,
            start_time=1711612800000,
            end_time=1711614600000,
            user_prompts=["Fix the enrichment bug"],
            assistant_texts=["Looking at enrichment.py..."],
            tool_calls=[
                {"name": "Read", "summary": "/src/enrichment.py"},
                {"name": "Edit", "summary": "/src/enrichment.py"},
            ],
            message_count=10,
        )

        prompt = build_claude_enrichment_prompt([seg])
        assert "/projects/hippo" in prompt
        assert "main" in prompt
        assert "Fix the enrichment bug" in prompt
        assert "Read: /src/enrichment.py" in prompt
        assert "Looking at enrichment.py" in prompt

    def test_strips_worktree_from_cwd(self):
        """Issue #98 F1c: Claude segment cwd from an agent worktree must be
        normalized to the parent repo path before reaching the LLM.
        """
        seg = SessionSegment(
            session_id="s1",
            project_dir="proj",
            cwd="/projects/hippo/.claude/worktrees/agent-ac83d4d3/crates/hippo-core",
            git_branch="main",
            segment_index=0,
            start_time=1711612800000,
            end_time=1711614600000,
            user_prompts=["test"],
            message_count=1,
        )
        prompt = build_claude_enrichment_prompt([seg])
        assert "/projects/hippo/crates/hippo-core" in prompt
        assert ".claude/worktrees" not in prompt
        assert "agent-ac83d4d3" not in prompt


class TestInsertAndClaim:
    def test_insert_segment(self, tmp_db):
        db_conn, _ = tmp_db
        seg = SessionSegment(
            session_id="test-session",
            project_dir="proj",
            cwd="/projects/test",
            git_branch="main",
            segment_index=0,
            start_time=1000,
            end_time=2000,
            user_prompts=["Hello"],
            tool_calls=[{"name": "Bash", "summary": "echo hi"}],
            message_count=5,
            source_file="/tmp/test.jsonl",
        )

        seg_id = insert_segment(db_conn, seg)
        assert seg_id is not None
        assert seg_id > 0

        # Verify in database
        row = db_conn.execute(
            "SELECT session_id, cwd FROM claude_sessions WHERE id = ?", (seg_id,)
        ).fetchone()
        assert row == ("test-session", "/projects/test")

        # Verify queue entry
        queue = db_conn.execute(
            "SELECT status FROM claude_enrichment_queue WHERE claude_session_id = ?",
            (seg_id,),
        ).fetchone()
        assert queue[0] == "pending"

    def test_insert_duplicate_skipped(self, tmp_db):
        db_conn, _ = tmp_db
        seg = SessionSegment(
            session_id="dup-session",
            project_dir="proj",
            cwd="/test",
            git_branch=None,
            segment_index=0,
            start_time=1000,
            end_time=2000,
            user_prompts=["test"],
            message_count=1,
            source_file="/tmp/test.jsonl",
        )

        first = insert_segment(db_conn, seg)
        assert first is not None
        second = insert_segment(db_conn, seg)
        assert second is None

    def test_claim_groups_by_cwd(self, tmp_db):
        db_conn, _ = tmp_db
        for i, cwd in enumerate(["/proj-a", "/proj-a", "/proj-b"]):
            seg = SessionSegment(
                session_id=f"s{i}",
                project_dir="p",
                cwd=cwd,
                git_branch=None,
                segment_index=0,
                start_time=1000 + i * 1000,
                end_time=2000 + i * 1000,
                user_prompts=[f"prompt {i}"],
                tool_calls=[{"name": "Read", "summary": "foo"}],
                message_count=5,
                source_file="/tmp/test.jsonl",
            )
            insert_segment(db_conn, seg)

        batches = claim_pending_claude_segments(db_conn, "test-worker")
        # Each segment becomes its own batch (1:1 enrichment)
        assert len(batches) == 3
        total_segments = sum(len(b) for b in batches)
        assert total_segments == 3

    def test_write_claude_knowledge_node(self, tmp_db):
        db_conn, _ = tmp_db
        seg = SessionSegment(
            session_id="kn-session",
            project_dir="proj",
            cwd="/test",
            git_branch="main",
            segment_index=0,
            start_time=1000,
            end_time=2000,
            user_prompts=["test"],
            message_count=1,
            source_file="/tmp/test.jsonl",
        )
        seg_id = insert_segment(db_conn, seg)

        result = EnrichmentResult(
            summary="Test summary",
            intent="testing",
            outcome="success",
            entities={
                "projects": ["test"],
                "tools": ["cargo"],
                "files": [],
                "services": [],
                "errors": [],
            },
            tags=["test"],
            embed_text="Test embed text for search",
            key_decisions=["chose testing approach"],
            problems_encountered=[],
        )

        node_id = write_claude_knowledge_node(db_conn, result, [seg_id], "test-model")
        assert node_id > 0

        # Verify knowledge node
        row = db_conn.execute(
            "SELECT embed_text FROM knowledge_nodes WHERE id = ?", (node_id,)
        ).fetchone()
        assert row[0] == "Test embed text for search"

        # Verify link table
        link = db_conn.execute(
            "SELECT claude_session_id FROM knowledge_node_claude_sessions WHERE knowledge_node_id = ?",
            (node_id,),
        ).fetchone()
        assert link[0] == seg_id

        # Verify segment marked enriched
        enriched = db_conn.execute(
            "SELECT enriched FROM claude_sessions WHERE id = ?", (seg_id,)
        ).fetchone()
        assert enriched[0] == 1

        # Verify queue done
        status = db_conn.execute(
            "SELECT status FROM claude_enrichment_queue WHERE claude_session_id = ?",
            (seg_id,),
        ).fetchone()
        assert status[0] == "done"

    def test_write_claude_knowledge_node_persists_design_decisions(self, tmp_db):
        """Protect the Claude-session writer's content JSON shape.

        This function overlaps with PR #101's content-hash propagation changes,
        so pinning `design_decisions` here helps catch a bad conflict
        resolution that keeps one change but drops the other.
        """
        db_conn, _ = tmp_db
        seg = SessionSegment(
            session_id="kn-session-design",
            project_dir="proj",
            cwd="/test",
            git_branch="main",
            segment_index=0,
            start_time=1000,
            end_time=2000,
            user_prompts=["test"],
            message_count=1,
            source_file="/tmp/test.jsonl",
        )
        seg_id = insert_segment(db_conn, seg)

        result = EnrichmentResult(
            summary="Test summary",
            intent="testing",
            outcome="success",
            entities={
                "projects": ["test"],
                "tools": ["cargo"],
                "files": [],
                "services": [],
                "errors": [],
            },
            tags=["test"],
            embed_text="Test embed text for search",
            key_decisions=["chose testing approach"],
            problems_encountered=[],
            design_decisions=[
                {
                    "considered": "plain prose summary only",
                    "chosen": "structured design_decisions field",
                    "reason": "better why-X-over-Y recall",
                }
            ],
        )

        node_id = write_claude_knowledge_node(db_conn, result, [seg_id], "test-model")
        row = db_conn.execute(
            "SELECT content FROM knowledge_nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        content = json.loads(row[0])
        assert content["design_decisions"] == [
            {
                "considered": "plain prose summary only",
                "chosen": "structured design_decisions field",
                "reason": "better why-X-over-Y recall",
            }
        ]

        # Verify entities created
        entity = db_conn.execute(
            "SELECT name FROM entities WHERE type = 'project' AND canonical = 'test'"
        ).fetchone()
        assert entity is not None

    def test_mark_claude_queue_failed(self, tmp_db):
        db_conn, _ = tmp_db
        seg = SessionSegment(
            session_id="fail-session",
            project_dir="proj",
            cwd="/test",
            git_branch=None,
            segment_index=0,
            start_time=1000,
            end_time=2000,
            user_prompts=["test"],
            message_count=1,
            source_file="/tmp/test.jsonl",
        )
        seg_id = insert_segment(db_conn, seg)

        mark_claude_queue_failed(db_conn, [seg_id], "test error")

        row = db_conn.execute(
            "SELECT status, retry_count, error_message FROM claude_enrichment_queue WHERE claude_session_id = ?",
            (seg_id,),
        ).fetchone()
        assert row[0] == "pending"  # still pending, retry_count < max_retries
        assert row[1] == 1
        assert row[2] == "test error"


class TestEnsureClaudeTables:
    def test_migrates_v2_to_v3(self):
        """ensure_claude_tables upgrades a v2 database."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db_path = Path(tmp.name)

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 2")
        # Minimal v2 schema
        conn.execute(
            "CREATE TABLE knowledge_nodes (id INTEGER PRIMARY KEY, uuid TEXT, content TEXT, embed_text TEXT)"
        )
        conn.commit()

        ensure_claude_tables(conn)

        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 3

        # Verify tables exist
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "claude_sessions" in tables
        assert "knowledge_node_claude_sessions" in tables
        assert "claude_enrichment_queue" in tables

        conn.close()
        db_path.unlink(missing_ok=True)

    def test_no_op_on_v3(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db_path = Path(tmp.name)

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 3")
        conn.commit()

        ensure_claude_tables(conn)  # should not raise
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 3

        conn.close()
        db_path.unlink(missing_ok=True)


class TestClaudeEligibilityFilter:
    def test_short_segment_no_tools_is_skipped(self, tmp_db):
        db_conn, _ = tmp_db
        seg = SessionSegment(
            session_id="noise-s",
            project_dir="p",
            cwd="/proj",
            git_branch=None,
            segment_index=0,
            start_time=1000,
            end_time=2000,
            user_prompts=["hi"],
            tool_calls=[],
            message_count=1,
            source_file="/tmp/test.jsonl",
        )
        insert_segment(db_conn, seg)

        batches = claim_pending_claude_segments(db_conn, "test-worker")
        assert batches == []

        row = db_conn.execute(
            "SELECT status, error_message FROM claude_enrichment_queue"
        ).fetchone()
        assert row[0] == "skipped"
        assert "message_count=1" in row[1]

    def test_segment_with_tools_survives(self, tmp_db):
        db_conn, _ = tmp_db
        seg = SessionSegment(
            session_id="real-s",
            project_dir="p",
            cwd="/proj",
            git_branch=None,
            segment_index=0,
            start_time=1000,
            end_time=2000,
            user_prompts=["hi"],
            tool_calls=[{"name": "Edit", "summary": "foo.py"}],
            message_count=1,
            source_file="/tmp/test.jsonl",
        )
        insert_segment(db_conn, seg)

        batches = claim_pending_claude_segments(db_conn, "test-worker")
        assert len(batches) == 1


class TestContentHashPropagation:
    """Tests for T-A.5: brain reads and writes content_hash / last_enriched_content_hash."""

    _RESULT = EnrichmentResult(
        summary="Summary",
        intent="testing",
        outcome="success",
        entities={"projects": [], "tools": [], "files": [], "services": [], "errors": []},
        tags=["test"],
        embed_text="embed text",
        key_decisions=[],
        problems_encountered=[],
    )

    def _make_seg(self, session_id, *, tool_calls=None):
        return SessionSegment(
            session_id=session_id,
            project_dir="p",
            cwd="/proj",
            git_branch=None,
            segment_index=0,
            start_time=1000,
            end_time=2000,
            user_prompts=["hi"],
            tool_calls=tool_calls or [{"name": "Read", "summary": "foo.py"}],
            message_count=5,
            source_file="/tmp/test.jsonl",
        )

    def test_claim_pending_segments_returns_content_hash(self, tmp_db):
        """claim_pending_claude_segments returns content_hash from the DB row."""
        db_conn, _ = tmp_db
        seg_id = insert_segment(db_conn, self._make_seg("hash-claim-s"))
        # Simulate daemon writing the hash after insert.
        db_conn.execute(
            "UPDATE claude_sessions SET content_hash = ? WHERE id = ?",
            ("abc123", seg_id),
        )
        db_conn.commit()

        batches = claim_pending_claude_segments(db_conn, "test-worker")
        assert len(batches) == 1
        segment = batches[0][0]
        assert segment["content_hash"] == "abc123"

    def test_enrichment_writes_last_enriched_content_hash(self, tmp_db):
        """Successful enrichment writes last_enriched_content_hash = content_hash."""
        db_conn, _ = tmp_db
        seg_id = insert_segment(db_conn, self._make_seg("hash-write-s"))
        db_conn.execute(
            "UPDATE claude_sessions SET content_hash = ? WHERE id = ?",
            ("abc123", seg_id),
        )
        db_conn.commit()

        write_claude_knowledge_node(
            db_conn,
            self._RESULT,
            [seg_id],
            "test-model",
            content_hashes=["abc123"],
        )

        row = db_conn.execute(
            "SELECT last_enriched_content_hash FROM claude_sessions WHERE id = ?",
            (seg_id,),
        ).fetchone()
        assert row[0] == "abc123"

    def test_enrichment_failure_does_not_write_hash(self, tmp_db):
        """mark_claude_queue_failed does NOT touch last_enriched_content_hash."""
        db_conn, _ = tmp_db
        seg_id = insert_segment(db_conn, self._make_seg("hash-fail-s"))
        # Pre-set content_hash and an existing last_enriched_content_hash.
        db_conn.execute(
            "UPDATE claude_sessions SET content_hash = ?, last_enriched_content_hash = ? WHERE id = ?",
            ("abc123", "old456", seg_id),
        )
        db_conn.commit()

        mark_claude_queue_failed(db_conn, [seg_id], "LLM timeout")

        row = db_conn.execute(
            "SELECT last_enriched_content_hash FROM claude_sessions WHERE id = ?",
            (seg_id,),
        ).fetchone()
        assert row[0] == "old456", "failure path must not overwrite last_enriched_content_hash"

    def test_null_content_hash_skips_write(self, tmp_db):
        """When content_hash is NULL (legacy row), last_enriched_content_hash stays NULL."""
        db_conn, _ = tmp_db
        seg_id = insert_segment(db_conn, self._make_seg("hash-null-s"))
        # content_hash remains NULL (the daemon hasn't written one yet).

        write_claude_knowledge_node(
            db_conn,
            self._RESULT,
            [seg_id],
            "test-model",
            content_hashes=[None],
        )

        row = db_conn.execute(
            "SELECT last_enriched_content_hash FROM claude_sessions WHERE id = ?",
            (seg_id,),
        ).fetchone()
        assert row[0] is None, (
            "NULL content_hash must not write anything to last_enriched_content_hash"
        )

        # Enrichment must still have completed normally (queue = done).
        status = db_conn.execute(
            "SELECT status FROM claude_enrichment_queue WHERE claude_session_id = ?",
            (seg_id,),
        ).fetchone()
        assert status[0] == "done"
