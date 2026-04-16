"""Tests for workflow_enrichment.enrich_one."""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hippo_brain.workflow_enrichment import enrich_one


@pytest.fixture
def enrichment_db(tmp_path: Path) -> str:
    """Seed a v5 DB with a completed workflow run + co-temporal shell event."""
    db = tmp_path / "hippo.db"
    fixture = Path(__file__).parent.parent / "src/hippo_brain/_fixtures/schema_v5_min.sql"
    conn = sqlite3.connect(db)
    conn.executescript(fixture.read_text())

    # Need the full events + claude_sessions + knowledge_nodes tables.
    # The fixture may not have events/claude_sessions — add them if missing:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            session_id INTEGER,
            timestamp_ms INTEGER NOT NULL,
            payload TEXT NOT NULL
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
            created_at INTEGER NOT NULL DEFAULT 0
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

    # Co-temporal shell event (contains the SHA)
    conn.execute("""
        INSERT INTO events (id, timestamp_ms, payload)
        VALUES (100, 1000, '{"command": "git push", "sha": "abc123"}')
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
    node = conn.execute("SELECT kind, title, body FROM knowledge_nodes").fetchone()
    assert node is not None
    assert node[0] == "change_outcome"
    assert "abc123" in node[1]  # SHA in title
    assert "ruff" in node[2].lower() or "F401" in node[2]  # LLM summary

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
