import json
import time

import pytest

from hippo_brain.enrichment import (
    build_enrichment_prompt,
    claim_pending_events_by_session,
    mark_queue_failed,
    parse_enrichment_response,
    write_knowledge_node,
)
from hippo_brain.models import EnrichmentResult


def test_build_enrichment_prompt():
    events = [
        {
            "command": "cargo test -p hippo-core",
            "exit_code": 0,
            "duration_ms": 3500,
            "cwd": "/Users/dev/projects/hippo",
            "git_branch": "main",
            "git_commit": "abc1234",
        }
    ]
    prompt = build_enrichment_prompt(events)
    assert "cargo test -p hippo-core" in prompt
    assert "/Users/dev/projects/hippo" in prompt
    assert "main" in prompt
    assert "abc1234" in prompt


def test_parse_enrichment_response():
    raw = (
        '{"summary": "Ran tests", "intent": "testing", "outcome": "success", '
        '"entities": {"projects": ["hippo"], "tools": ["cargo"], "files": [], '
        '"services": [], "errors": []}, '
        '"tags": ["rust"], "embed_text": "cargo test hippo"}'
    )
    result = parse_enrichment_response(raw)
    assert isinstance(result, EnrichmentResult)
    assert result.summary == "Ran tests"
    assert result.outcome == "success"
    assert "hippo" in result.entities["projects"]


def test_parse_enrichment_response_with_code_fences():
    raw = """```json
{"summary": "Built project", "intent": "building", "outcome": "success",
 "entities": {"projects": [], "tools": [], "files": [], "services": [], "errors": []},
 "tags": [], "embed_text": "build project"}
```"""
    result = parse_enrichment_response(raw)
    assert result.summary == "Built project"
    assert result.intent == "building"


def _valid_enrichment_dict(**overrides) -> dict:
    """Return a minimal valid enrichment dict, with optional overrides."""
    base = {
        "summary": "Ran tests",
        "intent": "testing",
        "outcome": "success",
        "entities": {
            "projects": ["hippo"],
            "tools": ["cargo"],
            "files": [],
            "services": [],
            "errors": [],
        },
        "tags": ["rust"],
        "embed_text": "cargo test hippo",
    }
    base.update(overrides)
    return base


def test_parse_rejects_missing_required_field():
    data = _valid_enrichment_dict()
    del data["summary"]
    with pytest.raises(ValueError, match="summary"):
        parse_enrichment_response(json.dumps(data))


def test_parse_rejects_invalid_outcome():
    data = _valid_enrichment_dict(outcome="succeeded")
    with pytest.raises(ValueError, match="outcome"):
        parse_enrichment_response(json.dumps(data))


def test_parse_skips_non_string_entity_items():
    data = _valid_enrichment_dict(
        entities={
            "projects": [],
            "tools": ["cargo", 123],
            "files": [],
            "services": [],
            "errors": [],
        }
    )
    result = parse_enrichment_response(json.dumps(data))
    assert result.entities["tools"] == ["cargo"]


def test_parse_rejects_entities_not_dict():
    data = _valid_enrichment_dict(entities=["not", "a", "dict"])
    with pytest.raises(ValueError, match="entities must be a dict"):
        parse_enrichment_response(json.dumps(data))


def test_parse_rejects_invalid_json():
    with pytest.raises(json.JSONDecodeError):
        parse_enrichment_response("not json")


def test_claim_and_write(tmp_db):
    conn, _ = tmp_db
    past_ms = int(time.time() * 1000) - 5000

    # Insert session
    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (past_ms,),
    )

    # Insert events
    conn.execute(
        """INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms,
                               cwd, hostname, shell)
           VALUES (1, 1, ?, 'cargo test', 0, 1000, '/project', 'laptop', 'zsh')""",
        (past_ms,),
    )
    conn.execute(
        """INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms,
                               cwd, hostname, shell)
           VALUES (2, 1, ?, 'cargo build', 0, 2000, '/project', 'laptop', 'zsh')""",
        (past_ms + 100,),
    )

    # Insert queue entries
    conn.execute("INSERT INTO enrichment_queue (event_id) VALUES (1)")
    conn.execute("INSERT INTO enrichment_queue (event_id) VALUES (2)")
    conn.commit()

    # Claim events (grouped by session)
    chunks = claim_pending_events_by_session(
        conn, max_per_chunk=10, worker_id="test-worker", stale_secs=1
    )
    assert len(chunks) == 1
    events = chunks[0]
    assert len(events) == 2
    event_ids = [e["id"] for e in events]

    # Write knowledge node
    result = EnrichmentResult(
        summary="Testing and building hippo",
        intent="testing",
        outcome="success",
        entities={
            "projects": ["hippo"],
            "tools": ["cargo"],
            "files": [],
            "services": [],
            "errors": [],
        },
        tags=["rust", "testing"],
        embed_text="cargo test and build hippo project",
    )
    node_id = write_knowledge_node(conn, result, event_ids, "test-model")
    assert node_id > 0

    # Verify knowledge node content
    row = conn.execute(
        "SELECT content, embed_text FROM knowledge_nodes WHERE id = ?", (node_id,)
    ).fetchone()
    assert "Testing and building hippo" in row[0]
    assert row[1] == "cargo test and build hippo project"

    # Verify entities created
    entities = conn.execute("SELECT type, name FROM entities").fetchall()
    entity_names = [e[1] for e in entities]
    assert "hippo" in entity_names
    assert "cargo" in entity_names

    # Verify events marked enriched
    enriched = conn.execute("SELECT enriched FROM events WHERE id = 1").fetchone()[0]
    assert enriched == 1

    # Verify queue marked done
    status = conn.execute("SELECT status FROM enrichment_queue WHERE event_id = 1").fetchone()[0]
    assert status == "done"


def test_mark_queue_failed(tmp_db):
    conn, _ = tmp_db
    now_ms = int(time.time() * 1000)

    # Insert session + event + queue
    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (now_ms,),
    )
    conn.execute(
        """INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms,
                               cwd, hostname, shell)
           VALUES (1, 1, ?, 'failing cmd', 1, 500, '/project', 'laptop', 'zsh')""",
        (now_ms,),
    )
    conn.execute("INSERT INTO enrichment_queue (event_id, max_retries) VALUES (1, 3)")
    conn.commit()

    # First failure — should stay pending
    mark_queue_failed(conn, [1], "timeout error")
    row = conn.execute(
        "SELECT status, retry_count FROM enrichment_queue WHERE event_id = 1"
    ).fetchone()
    assert row[0] == "pending"
    assert row[1] == 1

    # Second failure — still pending
    mark_queue_failed(conn, [1], "timeout error")
    row = conn.execute(
        "SELECT status, retry_count FROM enrichment_queue WHERE event_id = 1"
    ).fetchone()
    assert row[0] == "pending"
    assert row[1] == 2

    # Third failure — should be failed (retry_count >= max_retries)
    mark_queue_failed(conn, [1], "timeout error")
    row = conn.execute(
        "SELECT status, retry_count FROM enrichment_queue WHERE event_id = 1"
    ).fetchone()
    assert row[0] == "failed"
    assert row[1] == 3


def _seed_event_with_queue(conn, event_id=1, session_id=1):
    """Insert a session, event, and queue entry for write_knowledge_node tests."""
    now_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (?, ?, 'zsh', 'laptop', 'user')",
        (session_id, now_ms),
    )
    conn.execute(
        """INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms,
                               cwd, hostname, shell)
           VALUES (?, ?, ?, 'cargo test', 0, 1000, '/project', 'laptop', 'zsh')""",
        (event_id, session_id, now_ms),
    )
    conn.execute("INSERT INTO enrichment_queue (event_id) VALUES (?)", (event_id,))
    conn.commit()


def _make_result():
    """Return a valid EnrichmentResult for write tests."""
    return EnrichmentResult(
        summary="Ran tests",
        intent="testing",
        outcome="success",
        entities={
            "projects": ["hippo"],
            "tools": ["cargo"],
            "files": [],
            "services": [],
            "errors": [],
        },
        tags=["rust"],
        embed_text="cargo test hippo",
    )


class _FailingConn:
    """Thin wrapper around a sqlite3.Connection that injects a failure
    when a specific SQL fragment is executed."""

    def __init__(self, real_conn, fail_on: str):
        self._conn = real_conn
        self._fail_on = fail_on

    def execute(self, sql, *args, **kwargs):
        if self._fail_on in str(sql):
            raise RuntimeError("injected mid-write failure")
        return self._conn.execute(sql, *args, **kwargs)

    def executemany(self, sql, *args, **kwargs):
        if self._fail_on in str(sql):
            raise RuntimeError("injected mid-write failure")
        return self._conn.executemany(sql, *args, **kwargs)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()


def test_write_knowledge_node_failure_leaves_no_partial_state(tmp_db):
    conn, _ = tmp_db
    _seed_event_with_queue(conn, event_id=1)

    result = _make_result()

    wrapper = _FailingConn(conn, "INSERT INTO knowledge_node_events")
    with pytest.raises(RuntimeError, match="injected mid-write failure"):
        write_knowledge_node(wrapper, result, [1], "test-model")

    # Verify no partial state persisted
    assert conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM knowledge_node_events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM knowledge_node_entities").fetchone()[0] == 0
    assert conn.execute("SELECT enriched FROM events WHERE id = 1").fetchone()[0] == 0


def test_retry_after_rollback_writes_clean_node(tmp_db):
    conn, _ = tmp_db
    _seed_event_with_queue(conn, event_id=1)

    result = _make_result()

    # First call: injected failure at knowledge_node_events INSERT
    wrapper = _FailingConn(conn, "INSERT INTO knowledge_node_events")
    with pytest.raises(RuntimeError):
        write_knowledge_node(wrapper, result, [1], "test-model")

    # Second call: should succeed cleanly on the real connection
    node_id = write_knowledge_node(conn, result, [1], "test-model")
    assert node_id > 0

    # Exactly one node
    assert conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0] == 1
    # Linked to event
    assert conn.execute("SELECT COUNT(*) FROM knowledge_node_events").fetchone()[0] == 1


def test_batch_events_returned_in_timestamp_order(tmp_db):
    conn, _ = tmp_db
    past_ms = int(time.time() * 1000) - 10_000

    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (past_ms,),
    )

    # Insert events with out-of-order timestamps
    timestamps = [past_ms + 300, past_ms + 100, past_ms + 200]
    for i, ts in enumerate(timestamps, 1):
        conn.execute(
            """INSERT INTO events (id, session_id, timestamp, command, exit_code,
                                   duration_ms, cwd, hostname, shell)
               VALUES (?, 1, ?, 'cmd', 0, 100, '/p', 'laptop', 'zsh')""",
            (i, ts),
        )
        conn.execute("INSERT INTO enrichment_queue (event_id) VALUES (?)", (i,))
    conn.commit()

    chunks = claim_pending_events_by_session(conn, max_per_chunk=10, worker_id="test", stale_secs=1)
    assert len(chunks) == 1
    events = chunks[0]
    returned_timestamps = [e["timestamp"] for e in events]
    assert returned_timestamps == sorted(returned_timestamps)
    assert returned_timestamps == [past_ms + 100, past_ms + 200, past_ms + 300]


def test_multiple_sessions_produce_separate_chunks(tmp_db):
    conn, _ = tmp_db
    past_ms = int(time.time() * 1000) - 10_000

    # Two sessions
    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (past_ms,),
    )
    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (2, ?, 'zsh', 'laptop', 'user')",
        (past_ms,),
    )

    # Events from session 1
    conn.execute(
        """INSERT INTO events (id, session_id, timestamp, command, exit_code,
                               duration_ms, cwd, hostname, shell)
           VALUES (1, 1, ?, 'cmd1', 0, 100, '/p', 'laptop', 'zsh')""",
        (past_ms,),
    )
    conn.execute("INSERT INTO enrichment_queue (event_id) VALUES (1)")

    # Events from session 2
    conn.execute(
        """INSERT INTO events (id, session_id, timestamp, command, exit_code,
                               duration_ms, cwd, hostname, shell)
           VALUES (2, 2, ?, 'cmd2', 0, 100, '/p', 'laptop', 'zsh')""",
        (past_ms + 100,),
    )
    conn.execute("INSERT INTO enrichment_queue (event_id) VALUES (2)")
    conn.commit()

    chunks = claim_pending_events_by_session(conn, max_per_chunk=10, worker_id="test", stale_secs=1)
    # Two sessions produce two separate chunks
    assert len(chunks) == 2
    all_ids = {e["id"] for chunk in chunks for e in chunk}
    assert all_ids == {1, 2}
    # Each chunk has events from a single session
    for chunk in chunks:
        session_ids = {e["session_id"] for e in chunk}
        assert len(session_ids) == 1


def test_build_enrichment_prompt_with_browser_context():
    events = [
        {
            "command": "cargo build",
            "exit_code": 0,
            "duration_ms": 1000,
            "cwd": "/tmp",
            "shell": "zsh",
        }
    ]
    context = '\nBrowser Activity (concurrent):\n  stackoverflow.com - "Rust help" (read 5.0s, 80% scroll)'
    prompt = build_enrichment_prompt(events, browser_context=context)
    assert "cargo build" in prompt
    assert "Browser Activity (concurrent):" in prompt
    assert "stackoverflow.com" in prompt


def test_build_enrichment_prompt_no_browser_context():
    events = [
        {
            "command": "ls",
            "exit_code": 0,
            "duration_ms": 10,
            "cwd": "/tmp",
            "shell": "zsh",
        }
    ]
    prompt = build_enrichment_prompt(events, browser_context="")
    assert "Browser Activity" not in prompt


def test_write_knowledge_node_stores_key_decisions(tmp_db):
    conn, _ = tmp_db
    _seed_event_with_queue(conn, event_id=1)

    result = EnrichmentResult(
        summary="Built and tested hippo",
        intent="testing",
        outcome="success",
        entities={
            "projects": ["hippo"],
            "tools": ["cargo"],
            "files": [],
            "services": [],
            "errors": [],
        },
        tags=["rust"],
        embed_text="cargo build and test hippo",
        key_decisions=["Chose build.rs over vergen for zero deps"],
        problems_encountered=["clippy warning on unused import"],
    )

    node_id = write_knowledge_node(conn, result, [1], "test-model")
    assert node_id > 0

    row = conn.execute("SELECT content FROM knowledge_nodes WHERE id = ?", (node_id,)).fetchone()
    import json

    content = json.loads(row[0])
    assert content["key_decisions"] == ["Chose build.rs over vergen for zero deps"]
    assert content["problems_encountered"] == ["clippy warning on unused import"]


# ---------------------------------------------------------------------------
# Enrichment eligibility
# ---------------------------------------------------------------------------
from hippo_brain.enrichment import is_enrichment_eligible  # noqa: E402


class TestEligibilityShell:
    def test_exec_zsh_no_output_short_is_ineligible(self):
        ok, reason = is_enrichment_eligible(
            {"command": "exec zsh", "stdout": "", "stderr": "", "duration_ms": 5},
            "shell",
        )
        assert ok is False
        assert "exec zsh" in reason

    def test_clear_no_output_short_is_ineligible(self):
        ok, _ = is_enrichment_eligible(
            {"command": "clear", "stdout": "", "stderr": "", "duration_ms": 10},
            "shell",
        )
        assert ok is False

    def test_empty_command_no_output_is_ineligible(self):
        ok, _ = is_enrichment_eligible(
            {"command": "", "stdout": "", "stderr": "", "duration_ms": 0},
            "shell",
        )
        assert ok is False

    def test_exec_zsh_with_stderr_is_eligible(self):
        ok, _ = is_enrichment_eligible(
            {
                "command": "exec zsh",
                "stdout": "",
                "stderr": "not found",
                "duration_ms": 5,
            },
            "shell",
        )
        assert ok is True

    def test_exec_zsh_long_duration_is_eligible(self):
        ok, _ = is_enrichment_eligible(
            {"command": "exec zsh", "stdout": "", "stderr": "", "duration_ms": 500},
            "shell",
        )
        assert ok is True

    def test_real_command_is_eligible(self):
        ok, _ = is_enrichment_eligible(
            {
                "command": "cargo test",
                "stdout": "ok",
                "stderr": "",
                "duration_ms": 3000,
            },
            "shell",
        )
        assert ok is True


class TestEligibilityClaude:
    def test_short_segment_no_tools_is_ineligible(self):
        ok, reason = is_enrichment_eligible(
            {"message_count": 2, "tool_calls_json": "[]"},
            "claude",
        )
        assert ok is False
        assert "message_count=2" in reason

    def test_short_segment_with_tools_is_eligible(self):
        ok, _ = is_enrichment_eligible(
            {
                "message_count": 2,
                "tool_calls_json": '[{"name": "Read", "summary": "foo"}]',
            },
            "claude",
        )
        assert ok is True

    def test_long_segment_no_tools_is_eligible(self):
        ok, _ = is_enrichment_eligible(
            {"message_count": 10, "tool_calls_json": None},
            "claude",
        )
        assert ok is True

    def test_malformed_tool_calls_json_treated_as_empty(self):
        ok, _ = is_enrichment_eligible(
            {"message_count": 1, "tool_calls_json": "not json"},
            "claude",
        )
        assert ok is False


class TestEligibilityBrowser:
    def test_short_dwell_is_ineligible(self):
        ok, reason = is_enrichment_eligible({"dwell_ms": 500}, "browser")
        assert ok is False
        assert "dwell_ms=500" in reason

    def test_long_dwell_is_eligible(self):
        ok, _ = is_enrichment_eligible({"dwell_ms": 5000}, "browser")
        assert ok is True

    def test_missing_dwell_treated_as_zero(self):
        ok, _ = is_enrichment_eligible({}, "browser")
        assert ok is False


class TestEligibilityWorkflow:
    def test_workflow_always_eligible(self):
        ok, _ = is_enrichment_eligible({}, "workflow")
        assert ok is True


def test_shell_claim_marks_noise_skipped(tmp_db):
    """Ineligible shell events are marked skipped and excluded from chunks."""
    conn, _ = tmp_db
    past_ms = int(time.time() * 1000) - 5000

    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (past_ms,),
    )
    # Event 1: noise (exec zsh, no output, 5ms)
    conn.execute(
        """INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms,
                               cwd, hostname, shell)
           VALUES (1, 1, ?, 'exec zsh', 0, 5, '/project', 'laptop', 'zsh')""",
        (past_ms,),
    )
    # Event 2: real work
    conn.execute(
        """INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms,
                               cwd, hostname, shell, stdout)
           VALUES (2, 1, ?, 'cargo build', 0, 3000, '/project', 'laptop', 'zsh', 'compiling')""",
        (past_ms + 100,),
    )
    conn.execute("INSERT INTO enrichment_queue (event_id) VALUES (1)")
    conn.execute("INSERT INTO enrichment_queue (event_id) VALUES (2)")
    conn.commit()

    chunks = claim_pending_events_by_session(conn, max_per_chunk=10, worker_id="test", stale_secs=1)
    # Only the real event survives
    assert len(chunks) == 1
    assert [e["id"] for e in chunks[0]] == [2]

    # Noise event marked skipped with reason recorded
    row = conn.execute(
        "SELECT status, error_message FROM enrichment_queue WHERE event_id = 1"
    ).fetchone()
    assert row[0] == "skipped"
    assert "exec zsh" in row[1]

    # Noise event also flagged enriched so it isn't rescanned
    assert conn.execute("SELECT enriched FROM events WHERE id = 1").fetchone()[0] == 1


def test_shell_claim_whole_session_of_noise(tmp_db):
    """A session containing only noise yields no chunks but skips all rows."""
    conn, _ = tmp_db
    past_ms = int(time.time() * 1000) - 5000

    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (past_ms,),
    )
    conn.execute(
        """INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms,
                               cwd, hostname, shell)
           VALUES (1, 1, ?, 'exec zsh', 0, 5, '/p', 'l', 'zsh')""",
        (past_ms,),
    )
    conn.execute("INSERT INTO enrichment_queue (event_id) VALUES (1)")
    conn.commit()

    chunks = claim_pending_events_by_session(conn, max_per_chunk=10, worker_id="test", stale_secs=1)
    assert chunks == []
    status = conn.execute("SELECT status FROM enrichment_queue WHERE event_id = 1").fetchone()[0]
    assert status == "skipped"
