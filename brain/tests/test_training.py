import json
import tempfile
import time
from pathlib import Path

from hippo_brain.auto_memory import render_memory_enrichment_input
from hippo_brain.training import (
    BROWSER_SYSTEM_PROMPT,
    CLAUDE_SYSTEM_PROMPT,
    MEMORY_SYSTEM_PROMPT,
    SHELL_SYSTEM_PROMPT,
    WORKFLOW_SYSTEM_PROMPT,
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


def test_export_workflow_node(tmp_db):
    """Workflow (CI) nodes export with the live prompt — including failed runs."""
    conn, _ = tmp_db
    now_ms = int(time.time() * 1000)

    conn.execute(
        """INSERT INTO workflow_runs (id, repo, head_sha, head_branch, event, status,
             conclusion, started_at, completed_at, html_url, actor, raw_json,
             first_seen_at, last_seen_at, enriched)
           VALUES (1, 'owner/repo', 'abc123', 'main', 'push', 'completed', 'failure',
                   ?, ?, 'https://github.com/owner/repo/actions/runs/1', 'dev', '{}',
                   ?, ?, 1)""",
        (now_ms, now_ms + 60_000, now_ms, now_ms),
    )
    content = json.dumps(
        {
            "summary": "CI failed: clippy lint error in main.rs; fix by removing unused import",
            "intent": "ci",
            "outcome": "failure",
            "entities": {"projects": ["repo"], "tools": ["clippy"], "files": ["main.rs"]},
            "tags": ["ci", "failure"],
            "key_decisions": [],
            "problems_encountered": ["clippy lint error"],
            "design_decisions": [],
        }
    )
    # outcome column holds the raw GitHub conclusion ('failure') by design —
    # this test locks in that failed runs are still exported.
    conn.execute(
        """INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, outcome, tags,
             enrichment_model, created_at, updated_at)
           VALUES (1, 'uuid-wf-1', ?, 'ci failure clippy', 'change_outcome', 'failure', '[]',
                   'model', ?, ?)""",
        (content, now_ms, now_ms),
    )
    conn.execute(
        "INSERT INTO knowledge_node_workflow_runs (knowledge_node_id, run_id) VALUES (1, 1)"
    )
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        stats = export_training_data(conn, tmpdir)
        assert stats["total"] == 1
        assert stats["sources"]["workflow"] == 1

        data = json.loads((Path(tmpdir) / "train.jsonl").read_text().strip())
        messages = data["messages"]
        assert messages[0]["content"] == WORKFLOW_SYSTEM_PROMPT
        assert "owner/repo" in messages[1]["content"]
        assert "conclusion: failure" in messages[1]["content"]
        assistant = json.loads(messages[2]["content"])
        assert "clippy" in assistant["summary"]


def test_export_memory_node_matches_live_prompt(tmp_db):
    """Auto-memory export reproduces the live enrichment input byte-for-byte."""
    conn, _ = tmp_db
    now_ms = int(time.time() * 1000)

    conn.execute(
        """INSERT INTO memory_documents (id, uuid, repository, logical_path, source_path,
             observed_at, created_at, updated_at)
           VALUES (1, 'doc-uuid-1', 'hippo', 'MEMORY.md', '/home/user/.claude/MEMORY.md',
                   ?, ?, ?)""",
        (now_ms, now_ms, now_ms),
    )
    conn.execute(
        """INSERT INTO memory_revisions (id, document_id, revision_number, content_hash,
             source_hash, redacted_content, source_mtime_ms, source_size, chunker_name,
             created_at)
           VALUES (1, 1, 1, 'hash-1', 'src-hash-1', 'full doc', ?, 100, 'markdown', ?)""",
        (now_ms, now_ms),
    )
    chunk_texts = ["# Preferences\n\nUse Rust for hot paths.", "# Projects\n\nHippo is a daemon."]
    for i, text in enumerate(chunk_texts):
        conn.execute(
            """INSERT INTO memory_chunks (id, revision_id, ordinal, content, content_hash,
                 created_at)
               VALUES (?, 1, ?, ?, ?, ?)""",
            (i + 1, i, text, f"chunk-hash-{i}", now_ms),
        )
    content = json.dumps(
        {
            "summary": "User prefers Rust for performance-critical code",
            "intent": "memory",
            "outcome": "success",
            "entities": {"projects": ["hippo"], "tools": ["rust"]},
            "tags": ["memory", "preferences"],
            "key_decisions": [],
            "problems_encountered": [],
            "design_decisions": [],
        }
    )
    conn.execute(
        """INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, outcome, tags,
             enrichment_model, created_at, updated_at)
           VALUES (1, 'uuid-mem-1', ?, 'rust preference', 'observation', 'success', '[]',
                   'model', ?, ?)""",
        (content, now_ms, now_ms),
    )
    for chunk_id in (1, 2):
        conn.execute(
            "INSERT INTO knowledge_node_memory_chunks (knowledge_node_id, memory_chunk_id) VALUES (1, ?)",
            (chunk_id,),
        )
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        stats = export_training_data(conn, tmpdir)
        assert stats["total"] == 1
        assert stats["sources"]["auto-memory"] == 1

        data = json.loads((Path(tmpdir) / "train.jsonl").read_text().strip())
        messages = data["messages"]
        assert messages[0]["content"] == MEMORY_SYSTEM_PROMPT
        # The exported user message must be byte-identical to what the live
        # enrichment loop sends (render_memory_enrichment_input is the shared
        # single source of truth used by build_memory_enrichment_prompt).
        expected = render_memory_enrichment_input("hippo", "MEMORY.md", "hash-1", chunk_texts)
        assert messages[1]["content"] == expected
        assistant = json.loads(messages[2]["content"])
        assert "Rust" in assistant["summary"]
