"""Tests for workflow_enrichment.enrich_one_async."""

import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hippo_brain.enrichment import CURRENT_ENRICHMENT_VERSION
from hippo_brain.workflow_enrichment import (
    _path_prefix,
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


# ---------------------------------------------------------------------------
# enrich_one_async tests
# ---------------------------------------------------------------------------


def test_enrich_one_async_creates_knowledge_node(enrichment_db):
    """enrich_one_async creates knowledge node, links run, marks queue done."""
    fake_lm = MagicMock()
    fake_lm.chat = AsyncMock(return_value=_VALID_WORKFLOW_JSON)

    asyncio.run(
        enrich_one_async(enrichment_db, run_id=1, inference=fake_lm, query_model="test-model")
    )

    conn = sqlite3.connect(enrichment_db)
    node = conn.execute("SELECT node_type, embed_text, content FROM knowledge_nodes").fetchone()
    assert node is not None
    assert node[0] == "change_outcome"
    assert "ruff" in node[1]  # LLM embed_text (identifier-dense), not the title

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


_VALID_WORKFLOW_JSON = json.dumps(
    {
        "summary": "CI failed: ruff F401 unused import in brain/x.py",
        "intent": "ci debugging",
        "outcome": "failure",
        "entities": {
            "projects": ["hippo"],
            "tools": ["ruff"],
            "files": ["brain/x.py"],
            "services": [],
            "errors": ["F401"],
        },
        "tags": ["ci", "ruff"],
        "embed_text": "ruff F401 brain/x.py unused import workflow failure",
    }
)


def test_enrich_one_async_stores_valid_json_and_model(enrichment_db):
    """Workflow nodes must persist validated JSON content and record the model.

    Regression: the path stored the raw LLM string (often a reasoning model's
    chain-of-thought) directly into content with a blank enrichment_model,
    producing thousands of invalid-JSON nodes.
    """
    fake_lm = MagicMock()
    fake_lm.chat = AsyncMock(return_value=_VALID_WORKFLOW_JSON)

    asyncio.run(
        enrich_one_async(enrichment_db, run_id=1, inference=fake_lm, query_model="test-model")
    )

    conn = sqlite3.connect(enrichment_db)
    row = conn.execute(
        "SELECT content, enrichment_model, json_valid(content), embed_text, tags, "
        "enrichment_version FROM knowledge_nodes"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[2] == 1, "workflow node content must be valid JSON"
    assert json.loads(row[0])["summary"].startswith("CI failed")
    assert row[1] == "test-model", "enrichment_model must be recorded on the node"
    # embed_text is the LLM's identifier-dense field, not the weak repo@sha title.
    assert row[3] == "ruff F401 brain/x.py unused import workflow failure"
    # tags column + enrichment_version mirror the claude path (not left NULL/stale).
    assert json.loads(row[4]) == ["ci", "ruff"]
    assert row[5] == CURRENT_ENRICHMENT_VERSION
    # A full EnrichmentResult JSON can't fit in a few hundred tokens — no tiny cap.
    max_tokens = fake_lm.chat.call_args.kwargs.get("max_tokens")
    assert max_tokens is None or max_tokens >= 2048


def test_enrich_one_async_rejects_non_json_output(enrichment_db):
    """A reasoning model emitting chain-of-thought must NOT create a garbage node.

    Better to fail the enrichment (so it can retry / go terminal) than to persist
    an invalid-JSON node that breaks RAG/search.
    """
    fake_lm = MagicMock()
    fake_lm.chat = AsyncMock(
        return_value="Here's a thinking process:\n1. Analyze the run. The branch is main..."
    )

    with pytest.raises(json.JSONDecodeError):
        asyncio.run(
            enrich_one_async(enrichment_db, run_id=1, inference=fake_lm, query_model="test-model")
        )

    conn = sqlite3.connect(enrichment_db)
    n = conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0]
    conn.close()
    assert n == 0, "no node should be written when model output isn't valid JSON"


def test_enrich_one_async_rejects_invalid_field(enrichment_db):
    """Valid JSON but a bad field (out-of-vocab outcome) must also fail the run via
    validate_enrichment_data — not write a node. Covers the ValueError branch, the
    most common local-LLM failure after raw chain-of-thought.
    """
    fake_lm = MagicMock()
    fake_lm.chat = AsyncMock(
        return_value=(
            '{"summary": "ran", "intent": "ci", "outcome": "flaky", '
            '"entities": {"projects": [], "tools": [], "files": [], '
            '"services": [], "errors": []}, "tags": [], "embed_text": "x"}'
        )
    )

    with pytest.raises(ValueError):
        asyncio.run(
            enrich_one_async(enrichment_db, run_id=1, inference=fake_lm, query_model="test-model")
        )

    conn = sqlite3.connect(enrichment_db)
    n = conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0]
    conn.close()
    assert n == 0, "no node should be written when validation fails"


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
    fake_lm.chat = AsyncMock(return_value=_VALID_WORKFLOW_JSON)

    asyncio.run(
        enrich_one_async(enrichment_db, run_id=1, inference=fake_lm, query_model="test-model")
    )

    conn = sqlite3.connect(enrichment_db)
    link = conn.execute("SELECT * FROM knowledge_node_claude_sessions").fetchone()
    assert link is not None
    conn.close()


def test_enrich_one_async_skips_missing_run(enrichment_db):
    """enrich_one_async is a no-op when run_id doesn't exist."""
    fake_lm = MagicMock()
    fake_lm.chat = AsyncMock()

    asyncio.run(
        enrich_one_async(enrichment_db, run_id=999, inference=fake_lm, query_model="test-model")
    )

    fake_lm.chat.assert_not_called()


def test_enrich_one_async_survives_clustering_failure(enrichment_db):
    """A lesson-clustering failure must not fail the run.

    The knowledge node and queue 'done' status are committed before lesson
    clustering runs; if clustering raises, the run must NOT propagate the
    error (which would mark it failed, re-enrich, and duplicate the node).
    """
    fake_lm = MagicMock()
    fake_lm.chat = AsyncMock(return_value=_VALID_WORKFLOW_JSON)

    with patch(
        "hippo_brain.workflow_enrichment.upsert_cluster",
        side_effect=RuntimeError("clustering boom"),
    ):
        result = asyncio.run(
            enrich_one_async(enrichment_db, run_id=1, inference=fake_lm, query_model="test-model")
        )

    assert result is not None  # completed despite the clustering failure

    conn = sqlite3.connect(enrichment_db)
    node = conn.execute("SELECT id FROM knowledge_nodes").fetchone()
    assert node is not None
    status = conn.execute("SELECT status FROM workflow_enrichment_queue WHERE run_id=1").fetchone()[
        0
    ]
    assert status == "done"
    conn.close()


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
