import json
import tempfile
import time
from pathlib import Path

from hippo_brain.training import (
    BROWSER_SYSTEM_PROMPT,
    CLAUDE_SYSTEM_PROMPT,
    SHELL_SYSTEM_PROMPT,
    export_training_data,
)


def _seed_db(conn):
    """Insert sessions, events, knowledge nodes with linked events."""
    now_ms = int(time.time() * 1000)

    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (now_ms,),
    )

    for i in range(1, 4):
        conn.execute(
            """INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms,
                                   cwd, hostname, shell, git_branch)
               VALUES (?, 1, ?, ?, 0, ?, '/project', 'laptop', 'zsh', 'main')""",
            (i, now_ms + i, f"command-{i}", 1000 + i),
        )

    content = json.dumps(
        {
            "summary": "Ran project commands successfully",
            "intent": "development",
            "outcome": "success",
            "entities": {
                "projects": ["hippo"],
                "tools": ["cargo"],
                "files": ["src/main.rs"],
                "services": [],
                "errors": [],
                "env_vars": [],
            },
            "tags": ["dev", "rust"],
            "key_decisions": ["Used cargo build"],
            "problems_encountered": [],
            "design_decisions": [],
        }
    )
    conn.execute(
        """INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, outcome, tags,
                                        enrichment_model, created_at, updated_at)
           VALUES (1, 'uuid-1', ?, 'ran project commands', 'observation', 'success', '["dev"]', 'model', ?, ?)""",
        (content, now_ms, now_ms),
    )

    for i in range(1, 4):
        conn.execute(
            "INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (1, ?)",
            (i,),
        )

    conn.commit()


def test_export_training_data(tmp_db):
    conn, _ = tmp_db
    _seed_db(conn)

    with tempfile.TemporaryDirectory() as tmpdir:
        stats = export_training_data(conn, tmpdir)

        assert stats["total"] == 1
        assert stats["train"] >= 1
        assert stats["sources"]["shell"] == 1

        train_path = Path(tmpdir) / "train.jsonl"
        assert train_path.exists()

        with open(train_path) as f:
            for line in f:
                data = json.loads(line)
                assert "messages" in data
                messages = data["messages"]
                assert len(messages) == 3
                assert messages[0]["role"] == "system"
                assert messages[1]["role"] == "user"
                assert messages[2]["role"] == "assistant"

                # System prompt should be exactly the live shell enrichment prompt
                assert messages[0]["content"] == SHELL_SYSTEM_PROMPT

                # User message should contain command text
                assert "command-" in messages[1]["content"]
                assert "developer (human)" in messages[1]["content"]

                # Assistant message should be the full JSON enrichment result
                assistant = json.loads(messages[2]["content"])
                assert assistant["summary"] == "Ran project commands successfully"
                assert "entities" in assistant
                assert "tags" in assistant
                assert "key_decisions" in assistant


def test_export_empty_db(tmp_db):
    conn, _ = tmp_db

    with tempfile.TemporaryDirectory() as tmpdir:
        stats = export_training_data(conn, tmpdir)
        assert stats["total"] == 0
        assert stats["train"] == 0
        assert stats["valid"] == 0
        assert stats["test"] == 0
        assert stats["sources"] == {}


def test_export_agentic_session(tmp_db):
    """Agentic session (Claude/OpenCode) nodes should export with correct prompt."""
    conn, _ = tmp_db
    now_ms = int(time.time() * 1000)

    conn.execute(
        """INSERT INTO agentic_sessions (id, session_id, harness, segment_index, cwd,
             project_dir, summary_text, start_time, end_time, model)
           VALUES (1, 'sess-1', 'claude-code', 0, '/project', '/project',
                   'Fixed a bug in the auth module', ?, ?, 'claude-sonnet-4-20250514')""",
        (now_ms, now_ms + 5000),
    )
    content = json.dumps(
        {
            "summary": "Fixed authentication bug by updating token validation",
            "intent": "bug fix",
            "outcome": "success",
            "entities": {
                "projects": ["api"],
                "tools": [],
                "files": ["auth.py"],
                "services": [],
                "errors": [],
            },
            "tags": ["auth", "bugfix"],
            "key_decisions": [],
            "problems_encountered": ["Token expired mid-debug"],
            "design_decisions": [],
        }
    )
    conn.execute(
        """INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, outcome, tags,
             enrichment_model, created_at, updated_at)
           VALUES (1, 'uuid-agentic-1', ?, 'fixed auth bug', 'observation', 'success', '[]', 'model', ?, ?)""",
        (content, now_ms, now_ms),
    )
    conn.execute(
        "INSERT INTO knowledge_node_agentic_sessions (knowledge_node_id, agentic_session_id) VALUES (1, 1)"
    )
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        stats = export_training_data(conn, tmpdir)
        assert stats["total"] == 1
        assert stats["sources"]["agentic"] == 1

        train_path = Path(tmpdir) / "train.jsonl"
        data = json.loads(train_path.read_text().strip())
        messages = data["messages"]
        assert messages[0]["content"] == CLAUDE_SYSTEM_PROMPT
        assert "Fixed a bug" in messages[1]["content"]
        assistant = json.loads(messages[2]["content"])
        assert "Fixed authentication bug" in assistant["summary"]


def test_export_browser_node(tmp_db):
    """Browser enrichment nodes should export with correct prompt."""
    conn, _ = tmp_db
    now_ms = int(time.time() * 1000)

    conn.execute(
        """INSERT INTO browser_events (id, timestamp, url, title, domain, dwell_ms, scroll_depth)
           VALUES (1, ?, 'https://docs.rs/tokio', 'Tokio docs', 'docs.rs', 30000, 0.5)""",
        (now_ms,),
    )
    content = json.dumps(
        {
            "summary": "Researched Tokio async runtime documentation",
            "intent": "research",
            "outcome": "success",
            "entities": {
                "topics": ["async-rust"],
                "urls": ["https://docs.rs/tokio"],
                "projects": ["hippo"],
            },
            "tags": ["rust", "async", "research"],
            "key_decisions": [],
            "problems_encountered": [],
            "design_decisions": [],
        }
    )
    conn.execute(
        """INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, outcome, tags,
             enrichment_model, created_at, updated_at)
           VALUES (1, 'uuid-browser-1', ?, 'tokio docs research', 'observation', 'success', '[]', 'model', ?, ?)""",
        (content, now_ms, now_ms),
    )
    conn.execute(
        "INSERT INTO knowledge_node_browser_events (knowledge_node_id, browser_event_id) VALUES (1, 1)"
    )
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        stats = export_training_data(conn, tmpdir)
        assert stats["total"] == 1
        assert stats["sources"]["browser"] == 1

        train_path = Path(tmpdir) / "train.jsonl"
        data = json.loads(train_path.read_text().strip())
        messages = data["messages"]
        assert messages[0]["content"] == BROWSER_SYSTEM_PROMPT
        assert "docs.rs" in messages[1]["content"]
        assistant = json.loads(messages[2]["content"])
        assert "Tokio" in assistant["summary"]
