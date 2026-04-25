"""Tests for workflow_enrichment.enrich_one."""

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from hippo_brain.workflow_enrichment import (
    _path_prefix,
    enrich_one,
    enrich_one_async,
    mark_workflow_queue_failed,
)


@pytest.fixture
def enrichment_db(tmp_path: Path) -> str:
    """Seed a v5 DB with a completed workflow run + co-temporal shell event."""
    db = tmp_path / "hippo.db"
    fixture = Path(__file__).parent.parent / "src/hippo_brain/_fixtures/schema_v5_min.sql"
    conn = sqlite3.connect(db)
    conn.executescript(fixture.read_text())

    # Need the full events + claude_sessions tables.
    # The fixture may not have events/claude_sessions — add them if missing:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL,
            timestamp INTEGER NOT NULL,
            command TEXT NOT NULL,
            stdout TEXT,
            stderr TEXT,
            stdout_truncated INTEGER DEFAULT 0,
            stderr_truncated INTEGER DEFAULT 0,
            exit_code INTEGER,
            duration_ms INTEGER NOT NULL,
            cwd TEXT NOT NULL,
            hostname TEXT NOT NULL,
            shell TEXT NOT NULL,
            git_repo TEXT,
            git_branch TEXT,
            git_commit TEXT,
            git_dirty INTEGER,
            env_snapshot_id INTEGER,
            envelope_id TEXT,
            enriched INTEGER NOT NULL DEFAULT 0,
            redaction_count INTEGER NOT NULL DEFAULT 0,
            archived_at INTEGER,
            created_at INTEGER NOT NULL DEFAULT 0,
            probe_tag TEXT DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY,
            start_time INTEGER NOT NULL,
            end_time INTEGER,
            terminal TEXT,
            shell TEXT NOT NULL,
            hostname TEXT NOT NULL,
            username TEXT NOT NULL,
            summary TEXT,
            created_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS knowledge_node_events (
            knowledge_node_id INTEGER NOT NULL,
            event_id INTEGER NOT NULL,
            PRIMARY KEY (knowledge_node_id, event_id)
        );
        CREATE TABLE IF NOT EXISTS claude_sessions (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            project_dir TEXT NOT NULL DEFAULT '',
            cwd TEXT NOT NULL DEFAULT '',
            segment_index INTEGER NOT NULL DEFAULT 0,
            start_time INTEGER NOT NULL,
            end_time INTEGER NOT NULL,
            summary_text TEXT NOT NULL DEFAULT '',
            message_count INTEGER NOT NULL DEFAULT 0,
            source_file TEXT NOT NULL DEFAULT '',
            enriched INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT 0,
            probe_tag TEXT DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS knowledge_node_claude_sessions (
            knowledge_node_id INTEGER NOT NULL,
            claude_session_id INTEGER NOT NULL,
            PRIMARY KEY (knowledge_node_id, claude_session_id)
        );
        CREATE TABLE IF NOT EXISTS workflow_enrichment_queue (
            run_id          INTEGER PRIMARY KEY REFERENCES workflow_runs(id) ON DELETE CASCADE,
            status          TEXT NOT NULL DEFAULT 'pending',
            priority        INTEGER NOT NULL DEFAULT 5,
            retry_count     INTEGER NOT NULL DEFAULT 0,
            max_retries     INTEGER NOT NULL DEFAULT 5,
            error_message   TEXT,
            locked_at       INTEGER,
            locked_by       TEXT,
            enqueued_at     INTEGER NOT NULL,
            updated_at      INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS knowledge_node_workflow_runs (
            knowledge_node_id INTEGER NOT NULL REFERENCES knowledge_nodes(id),
            run_id            INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
            PRIMARY KEY (knowledge_node_id, run_id)
        );
    """)

    # Workflow run (completed, failure)
    conn.execute("""
        INSERT INTO workflow_runs
          (id, repo, head_sha, head_branch, event, status, conclusion,
           html_url, raw_json, first_seen_at, last_seen_at, started_at)
        VALUES (1, 'me/r', 'abc123', 'main', 'push', 'completed', 'failure',
                'https://x', '{}', 1000, 2000, 1000)
    """)
    conn.execute("""
        INSERT INTO workflow_jobs
          (id, run_id, name, status, conclusion, raw_json)
        VALUES (10, 1, 'lint', 'completed', 'failure', '{}')
    """)
    conn.execute("""
        INSERT INTO workflow_annotations
          (job_id, level, tool, rule_id, path, start_line, message)
        VALUES (10, 'failure', 'ruff', 'F401', 'brain/x.py', 3, 'F401 unused import')
    """)
    conn.execute("""
        INSERT INTO workflow_enrichment_queue (run_id, status, enqueued_at, updated_at)
        VALUES (1, 'pending', 1000, 1000)
    """)

    # Need a session row for the FK
    conn.execute("""
        INSERT INTO sessions (id, start_time, shell, hostname, username)
        VALUES (1, 1000, 'zsh', 'localhost', 'test')
    """)

    # Co-temporal shell event (matches the SHA via git_commit)
    conn.execute("""
        INSERT INTO events (id, session_id, timestamp, command, duration_ms, cwd,
                            hostname, shell, git_commit)
        VALUES (100, 1, 1000, 'git push', 100, '/hippo', 'localhost', 'zsh', 'abc123')
    """)

    conn.commit()
    conn.close()
    return str(db)


def test_enrich_one_creates_knowledge_node(enrichment_db):
    fake_lm = MagicMock()
    fake_lm.complete.return_value = "Push failed due to ruff F401 unused import in brain/x.py"

    enrich_one(enrichment_db, run_id=1, lm=fake_lm, query_model="test-model")

    conn = sqlite3.connect(enrichment_db)
    # Knowledge node created
    node = conn.execute(
        "SELECT node_type, embed_text, content, outcome FROM knowledge_nodes"
    ).fetchone()
    assert node is not None
    assert node[0] == "change_outcome"
    assert "abc123" in node[1]  # SHA in embed_text (title)
    assert "ruff" in node[2].lower() or "F401" in node[2]  # LLM summary in content

    # Linked to workflow run
    link = conn.execute("SELECT * FROM knowledge_node_workflow_runs").fetchone()
    assert link is not None

    # Linked to shell event
    event_link = conn.execute("SELECT * FROM knowledge_node_events").fetchone()
    assert event_link is not None

    # Queue marked done
    q = conn.execute("SELECT status FROM workflow_enrichment_queue WHERE run_id=1").fetchone()
    assert q[0] == "done"

    # Run marked enriched
    r = conn.execute("SELECT enriched FROM workflow_runs WHERE id=1").fetchone()
    assert r[0] == 1

    # Lesson pending created (first occurrence — not promoted yet)
    pending = conn.execute("SELECT count FROM lesson_pending WHERE tool='ruff'").fetchone()
    assert pending is not None and pending[0] == 1

    conn.close()


def test_enrich_one_skips_missing_run(enrichment_db):
    fake_lm = MagicMock()
    enrich_one(enrichment_db, run_id=999, lm=fake_lm, query_model="test-model")
    fake_lm.complete.assert_not_called()


def test_enrich_one_links_claude_sessions(enrichment_db):
    """enrich_one links co-temporal Claude sessions to the knowledge node (line 106)."""
    conn = sqlite3.connect(enrichment_db)
    # start_time=100 <= started+window, end_time=2000 >= started-window → within ±15min
    conn.execute("""
        INSERT INTO claude_sessions
          (id, session_id, project_dir, cwd, segment_index, start_time, end_time,
           summary_text, message_count, source_file, enriched, created_at)
        VALUES (200, 'sess-abc', '/hippo', '/hippo', 0, 100, 2000,
                'Worked on CI fix', 1, 'sess.jsonl', 0, 1000)
    """)
    conn.commit()
    conn.close()

    fake_lm = MagicMock()
    fake_lm.complete.return_value = "Summary with session context"

    enrich_one(enrichment_db, run_id=1, lm=fake_lm, query_model="test-model")

    conn = sqlite3.connect(enrichment_db)
    link = conn.execute("SELECT * FROM knowledge_node_claude_sessions").fetchone()
    assert link is not None
    conn.close()


# ---------------------------------------------------------------------------
# enrich_one_async tests
# ---------------------------------------------------------------------------


def test_enrich_one_async_creates_knowledge_node(enrichment_db):
    """enrich_one_async creates knowledge node, links run, marks queue done."""
    fake_lm = MagicMock()
    fake_lm.chat = AsyncMock(return_value="Async enrichment summary")

    asyncio.run(enrich_one_async(enrichment_db, run_id=1, lm=fake_lm, query_model="test-model"))

    conn = sqlite3.connect(enrichment_db)
    node = conn.execute("SELECT node_type, embed_text, content FROM knowledge_nodes").fetchone()
    assert node is not None
    assert node[0] == "change_outcome"
    assert "abc123" in node[1]  # SHA in embed_text

    link = conn.execute("SELECT * FROM knowledge_node_workflow_runs").fetchone()
    assert link is not None

    event_link = conn.execute("SELECT * FROM knowledge_node_events").fetchone()
    assert event_link is not None

    q = conn.execute("SELECT status FROM workflow_enrichment_queue WHERE run_id=1").fetchone()
    assert q[0] == "done"

    r = conn.execute("SELECT enriched FROM workflow_runs WHERE id=1").fetchone()
    assert r[0] == 1

    pending = conn.execute("SELECT count FROM lesson_pending WHERE tool='ruff'").fetchone()
    assert pending is not None and pending[0] == 1

    conn.close()


def test_enrich_one_async_links_claude_sessions(enrichment_db):
    """enrich_one_async links co-temporal Claude sessions."""
    conn = sqlite3.connect(enrichment_db)
    conn.execute("""
        INSERT INTO claude_sessions
          (id, session_id, project_dir, cwd, segment_index, start_time, end_time,
           summary_text, message_count, source_file, enriched, created_at)
        VALUES (200, 'sess-xyz', '/hippo', '/hippo', 0, 100, 2000,
                'CI debugging session', 1, 'sess.jsonl', 0, 1000)
    """)
    conn.commit()
    conn.close()

    fake_lm = MagicMock()
    fake_lm.chat = AsyncMock(return_value="Summary")

    asyncio.run(enrich_one_async(enrichment_db, run_id=1, lm=fake_lm, query_model="test-model"))

    conn = sqlite3.connect(enrichment_db)
    link = conn.execute("SELECT * FROM knowledge_node_claude_sessions").fetchone()
    assert link is not None
    conn.close()


def test_enrich_one_async_skips_missing_run(enrichment_db):
    """enrich_one_async is a no-op when run_id doesn't exist."""
    fake_lm = MagicMock()
    fake_lm.chat = AsyncMock()

    asyncio.run(enrich_one_async(enrichment_db, run_id=999, lm=fake_lm, query_model="test-model"))

    fake_lm.chat.assert_not_called()


# ---------------------------------------------------------------------------
# mark_workflow_queue_failed tests
# ---------------------------------------------------------------------------


def test_mark_workflow_queue_failed_resets_to_pending(enrichment_db):
    """mark_workflow_queue_failed increments retry_count and resets to pending."""
    conn = sqlite3.connect(enrichment_db)
    mark_workflow_queue_failed(conn, run_id=1, error="timeout")

    row = conn.execute(
        "SELECT status, retry_count, error_message FROM workflow_enrichment_queue WHERE run_id=1"
    ).fetchone()
    assert row[0] == "pending"
    assert row[1] == 1
    assert row[2] == "timeout"
    conn.close()


def test_mark_workflow_queue_failed_sets_failed_when_exhausted(enrichment_db):
    """mark_workflow_queue_failed sets status=failed when retry_count reaches max_retries."""
    conn = sqlite3.connect(enrichment_db)
    # max_retries default is 5; set retry_count to 4 so next call exhausts it
    conn.execute("UPDATE workflow_enrichment_queue SET retry_count=4 WHERE run_id=1")
    conn.commit()

    mark_workflow_queue_failed(conn, run_id=1, error="persistent failure")

    row = conn.execute(
        "SELECT status, retry_count FROM workflow_enrichment_queue WHERE run_id=1"
    ).fetchone()
    assert row[0] == "failed"
    assert row[1] == 5
    conn.close()


# ---------------------------------------------------------------------------
# _path_prefix edge case
# ---------------------------------------------------------------------------


def test_path_prefix_short_path():
    """_path_prefix returns the original path when it has fewer segments than requested."""
    # "x.py" has 1 part, segments=2 → shorter than requested, returns original path
    assert _path_prefix("x.py", 2) == "x.py"


def test_path_prefix_normal():
    """_path_prefix extracts first N directory segments."""
    assert _path_prefix("brain/src/x.py", 2) == "brain/src/"


def test_path_prefix_none():
    """_path_prefix returns empty string for None path."""
    assert _path_prefix(None, 2) == ""
