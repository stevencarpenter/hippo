import json
import sqlite3
import time

import pytest

from hippo_brain.mcp_queries import (
    format_context_block,
    get_entities_impl,
    list_projects_impl,
    parse_since,
    search_events_impl,
    search_knowledge_lexical,
    shape_semantic_results,
)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE knowledge_nodes (
            id INTEGER PRIMARY KEY,
            uuid TEXT NOT NULL UNIQUE,
            content TEXT NOT NULL,
            embed_text TEXT NOT NULL,
            node_type TEXT NOT NULL DEFAULT 'observation',
            outcome TEXT,
            tags TEXT,
            enrichment_model TEXT,
            enrichment_version INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            session_id INTEGER,
            timestamp INTEGER NOT NULL,
            command TEXT,
            exit_code INTEGER,
            duration_ms INTEGER,
            cwd TEXT,
            hostname TEXT,
            shell TEXT,
            git_repo TEXT,
            git_branch TEXT,
            git_commit TEXT,
            git_dirty INTEGER,
            stdout TEXT,
            stderr TEXT,
            enriched INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE claude_sessions (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            project_dir TEXT NOT NULL,
            cwd TEXT NOT NULL,
            git_branch TEXT,
            segment_index INTEGER NOT NULL,
            start_time INTEGER NOT NULL,
            end_time INTEGER NOT NULL,
            summary_text TEXT NOT NULL,
            tool_calls_json TEXT,
            user_prompts_json TEXT,
            message_count INTEGER NOT NULL,
            token_count INTEGER,
            source_file TEXT NOT NULL,
            is_subagent INTEGER NOT NULL DEFAULT 0,
            parent_session_id TEXT,
            enriched INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT 0,
            UNIQUE (session_id, segment_index)
        );
        CREATE TABLE browser_events (
            id INTEGER PRIMARY KEY,
            timestamp INTEGER NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            domain TEXT NOT NULL,
            dwell_ms INTEGER NOT NULL,
            scroll_depth REAL,
            extracted_text TEXT,
            search_query TEXT,
            referrer TEXT,
            content_hash TEXT,
            envelope_id TEXT,
            enriched INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            canonical TEXT,
            metadata TEXT,
            first_seen INTEGER NOT NULL DEFAULT 0,
            last_seen INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT 0,
            UNIQUE (type, canonical)
        );
        CREATE TABLE knowledge_node_events (
            knowledge_node_id INTEGER NOT NULL,
            event_id INTEGER NOT NULL,
            PRIMARY KEY (knowledge_node_id, event_id)
        );
        CREATE TABLE knowledge_node_claude_sessions (
            knowledge_node_id INTEGER NOT NULL,
            claude_session_id INTEGER NOT NULL,
            PRIMARY KEY (knowledge_node_id, claude_session_id)
        );
        CREATE TABLE knowledge_node_browser_events (
            knowledge_node_id INTEGER NOT NULL,
            browser_event_id INTEGER NOT NULL,
            PRIMARY KEY (knowledge_node_id, browser_event_id)
        );
        CREATE TABLE knowledge_node_workflow_runs (
            knowledge_node_id INTEGER NOT NULL,
            run_id INTEGER NOT NULL,
            PRIMARY KEY (knowledge_node_id, run_id)
        );
        CREATE TABLE knowledge_node_entities (
            knowledge_node_id INTEGER NOT NULL,
            entity_id INTEGER NOT NULL,
            PRIMARY KEY (knowledge_node_id, entity_id)
        );
    """)
    return conn


class TestParseSince:
    def test_hours(self):
        now_ms = int(time.time() * 1000)
        result = parse_since("24h")
        assert abs(result - (now_ms - 24 * 3600 * 1000)) < 2000

    def test_days(self):
        now_ms = int(time.time() * 1000)
        result = parse_since("7d")
        assert abs(result - (now_ms - 7 * 24 * 3600 * 1000)) < 2000

    def test_minutes(self):
        now_ms = int(time.time() * 1000)
        result = parse_since("30m")
        assert abs(result - (now_ms - 30 * 60 * 1000)) < 2000

    def test_invalid_returns_zero(self):
        assert parse_since("") == 0
        assert parse_since("abc") == 0
        assert parse_since("24x") == 0


class TestSearchKnowledgeLexical:
    def test_finds_matching_nodes(self, db):
        db.execute(
            "INSERT INTO knowledge_nodes (uuid, content, embed_text, outcome, tags) "
            "VALUES ('u1', '{\"summary\":\"cargo build fix\"}', 'Fixed cargo build error in hippo', 'success', '[]')"
        )
        db.execute(
            "INSERT INTO knowledge_nodes (uuid, content, embed_text, outcome) "
            "VALUES ('u2', '{\"summary\":\"unrelated\"}', 'Nothing relevant here', 'success')"
        )
        db.commit()

        results = search_knowledge_lexical(db, "cargo build", limit=10)
        assert len(results) == 1
        assert "cargo build" in results[0]["embed_text"].lower()

    def test_empty_query_returns_recent(self, db):
        db.execute(
            "INSERT INTO knowledge_nodes (uuid, content, embed_text, outcome, created_at) "
            "VALUES ('u1', '{\"summary\":\"recent\"}', 'recent work', 'success', 9999)"
        )
        db.commit()

        results = search_knowledge_lexical(db, "", limit=10)
        assert len(results) == 1

    def test_limit_respected(self, db):
        for i in range(5):
            db.execute(
                "INSERT INTO knowledge_nodes (uuid, content, embed_text, outcome) "
                f"VALUES ('u{i}', '{{\"summary\":\"cargo {i}\"}}', 'cargo test {i}', 'success')"
            )
        db.commit()

        results = search_knowledge_lexical(db, "cargo", limit=3)
        assert len(results) == 3


class TestSearchEvents:
    def test_shell_events(self, db):
        now_ms = int(time.time() * 1000)
        db.execute(
            "INSERT INTO events (timestamp, command, exit_code, duration_ms, cwd, shell, git_branch) "
            "VALUES (?, 'cargo test', 0, 1000, '/projects/hippo', 'zsh', 'main')",
            (now_ms,),
        )
        db.commit()

        results = search_events_impl(db, query="cargo", source="shell", limit=10)
        assert len(results) == 1
        assert results[0]["source"] == "shell"
        assert results[0]["summary"] == "cargo test"

    def test_browser_events(self, db):
        now_ms = int(time.time() * 1000)
        db.execute(
            "INSERT INTO browser_events (timestamp, url, title, domain, dwell_ms, scroll_depth) "
            "VALUES (?, 'https://docs.rs/serde', 'serde docs', 'docs.rs', 5000, 0.8)",
            (now_ms,),
        )
        db.commit()

        results = search_events_impl(db, query="serde", source="browser", limit=10)
        assert len(results) == 1
        assert results[0]["source"] == "browser"
        assert "docs.rs" in results[0]["summary"]

    def test_claude_events(self, db):
        now_ms = int(time.time() * 1000)
        db.execute(
            "INSERT INTO claude_sessions "
            "(session_id, project_dir, cwd, segment_index, start_time, end_time, "
            "summary_text, message_count, source_file) "
            "VALUES ('s1', '/proj', '/proj', 0, ?, ?, 'Implemented MCP server', 10, 'f.jsonl')",
            (now_ms, now_ms + 60000),
        )
        db.commit()

        results = search_events_impl(db, query="MCP", source="claude", limit=10)
        assert len(results) == 1
        assert results[0]["source"] == "claude"

    def test_source_all(self, db):
        now_ms = int(time.time() * 1000)
        db.execute(
            "INSERT INTO events (timestamp, command, exit_code, duration_ms, cwd, shell) "
            "VALUES (?, 'echo all', 0, 10, '/tmp', 'zsh')",
            (now_ms,),
        )
        db.execute(
            "INSERT INTO browser_events (timestamp, url, title, domain, dwell_ms) "
            "VALUES (?, 'https://all.com', 'all page', 'all.com', 3000)",
            (now_ms,),
        )
        db.commit()

        results = search_events_impl(db, source="all", limit=10)
        sources = {r["source"] for r in results}
        assert "shell" in sources
        assert "browser" in sources

    def test_since_filter(self, db):
        old_ms = int(time.time() * 1000) - 48 * 3600 * 1000  # 48h ago
        now_ms = int(time.time() * 1000)
        db.execute(
            "INSERT INTO events (timestamp, command, exit_code, duration_ms, cwd, shell) "
            "VALUES (?, 'old cmd', 0, 10, '/tmp', 'zsh')",
            (old_ms,),
        )
        db.execute(
            "INSERT INTO events (timestamp, command, exit_code, duration_ms, cwd, shell) "
            "VALUES (?, 'new cmd', 0, 10, '/tmp', 'zsh')",
            (now_ms,),
        )
        db.commit()

        results = search_events_impl(db, since="24h", source="shell", limit=10)
        assert len(results) == 1
        assert results[0]["summary"] == "new cmd"

    def test_project_filter(self, db):
        now_ms = int(time.time() * 1000)
        db.execute(
            "INSERT INTO events (timestamp, command, exit_code, duration_ms, cwd, shell) "
            "VALUES (?, 'cmd1', 0, 10, '/projects/hippo', 'zsh')",
            (now_ms,),
        )
        db.execute(
            "INSERT INTO events (timestamp, command, exit_code, duration_ms, cwd, shell) "
            "VALUES (?, 'cmd2', 0, 10, '/projects/other', 'zsh')",
            (now_ms,),
        )
        db.commit()

        results = search_events_impl(db, project="hippo", source="shell", limit=10)
        assert len(results) == 1
        assert results[0]["summary"] == "cmd1"


class TestGetEntities:
    def test_basic(self, db):
        db.execute(
            "INSERT INTO entities (type, name, canonical, first_seen, last_seen) "
            "VALUES ('tool', 'cargo', 'cargo', 1000, 2000)"
        )
        db.commit()

        results = get_entities_impl(db, limit=50)
        assert len(results) == 1
        assert results[0]["type"] == "tool"
        assert results[0]["name"] == "cargo"

    def test_type_filter(self, db):
        db.execute(
            "INSERT INTO entities (type, name, canonical, first_seen, last_seen) "
            "VALUES ('tool', 'cargo', 'cargo', 1000, 2000)"
        )
        db.execute(
            "INSERT INTO entities (type, name, canonical, first_seen, last_seen) "
            "VALUES ('project', 'hippo', 'hippo', 1000, 2000)"
        )
        db.commit()

        results = get_entities_impl(db, entity_type="project", limit=50)
        assert len(results) == 1
        assert results[0]["name"] == "hippo"

    def test_query_filter(self, db):
        db.execute(
            "INSERT INTO entities (type, name, canonical, first_seen, last_seen) "
            "VALUES ('tool', 'cargo', 'cargo', 1000, 2000)"
        )
        db.execute(
            "INSERT INTO entities (type, name, canonical, first_seen, last_seen) "
            "VALUES ('tool', 'ruff', 'ruff', 1000, 2000)"
        )
        db.commit()

        results = get_entities_impl(db, query="carg", limit=50)
        assert len(results) == 1
        assert results[0]["name"] == "cargo"

    def test_limit(self, db):
        for i in range(10):
            db.execute(
                "INSERT INTO entities (type, name, canonical, first_seen, last_seen) "
                f"VALUES ('tool', 'tool{i}', 'tool{i}', 1000, 2000)"
            )
        db.commit()

        results = get_entities_impl(db, limit=5)
        assert len(results) == 5


def _insert_kn(db, uuid, summary="s", embed_text="e", outcome="success", created_at=0):
    """Insert a knowledge_nodes row, returning the autogenerated id."""
    cur = db.execute(
        "INSERT INTO knowledge_nodes (uuid, content, embed_text, outcome, tags, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (uuid, json.dumps({"summary": summary}), embed_text, outcome, "[]", created_at),
    )
    return cur.lastrowid


class TestKnowledgeFilters:
    def test_uuid_and_links_exposed(self, db):
        kid = _insert_kn(db, "u-1", embed_text="cargo build broke")
        db.execute(
            "INSERT INTO events (id, timestamp, command, exit_code, duration_ms, cwd, shell) "
            "VALUES (101, 1000, 'cargo build', 1, 200, '/proj/hippo', 'zsh')"
        )
        db.execute(
            "INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (?, 101)",
            (kid,),
        )
        db.commit()

        results = search_knowledge_lexical(db, "cargo", limit=10)
        assert len(results) == 1
        assert results[0]["uuid"] == "u-1"
        assert results[0]["linked_event_ids"] == [101]
        assert results[0]["linked_claude_session_ids"] == []
        assert results[0]["linked_browser_event_ids"] == []

    def test_project_filter_via_event_cwd(self, db):
        k1 = _insert_kn(db, "u-hippo", embed_text="cargo test")
        k2 = _insert_kn(db, "u-other", embed_text="cargo test")
        db.execute(
            "INSERT INTO events (id, timestamp, command, exit_code, duration_ms, cwd, shell) "
            "VALUES (1, 1000, 'cargo test', 0, 100, '/projects/hippo', 'zsh')"
        )
        db.execute(
            "INSERT INTO events (id, timestamp, command, exit_code, duration_ms, cwd, shell) "
            "VALUES (2, 1000, 'cargo test', 0, 100, '/projects/other', 'zsh')"
        )
        db.execute("INSERT INTO knowledge_node_events VALUES (?, 1)", (k1,))
        db.execute("INSERT INTO knowledge_node_events VALUES (?, 2)", (k2,))
        db.commit()

        results = search_knowledge_lexical(db, "cargo", project="hippo", limit=10)
        assert {r["uuid"] for r in results} == {"u-hippo"}

    def test_since_filter(self, db):
        old = _insert_kn(db, "u-old", embed_text="cargo old", created_at=1000)
        now_ms = int(time.time() * 1000)
        new = _insert_kn(db, "u-new", embed_text="cargo new", created_at=now_ms)
        db.commit()
        assert old != new

        results = search_knowledge_lexical(db, "cargo", since="1h", limit=10)
        assert {r["uuid"] for r in results} == {"u-new"}

    def test_source_filter_shell_only(self, db):
        k_shell = _insert_kn(db, "u-shell", embed_text="match")
        k_browser = _insert_kn(db, "u-browser", embed_text="match")
        db.execute(
            "INSERT INTO events (id, timestamp, command, exit_code, duration_ms, cwd, shell) "
            "VALUES (10, 1000, 'cmd', 0, 1, '/x', 'zsh')"
        )
        db.execute(
            "INSERT INTO browser_events (id, timestamp, url, title, domain, dwell_ms) "
            "VALUES (20, 1000, 'https://x', 't', 'x.com', 1)"
        )
        db.execute("INSERT INTO knowledge_node_events VALUES (?, 10)", (k_shell,))
        db.execute("INSERT INTO knowledge_node_browser_events VALUES (?, 20)", (k_browser,))
        db.commit()

        results = search_knowledge_lexical(db, "match", source="shell", limit=10)
        assert {r["uuid"] for r in results} == {"u-shell"}

    def test_branch_filter(self, db):
        k_main = _insert_kn(db, "u-main", embed_text="match")
        k_feat = _insert_kn(db, "u-feat", embed_text="match")
        db.execute(
            "INSERT INTO events (id, timestamp, command, exit_code, duration_ms, cwd, shell, "
            "git_branch) VALUES (1, 1000, 'cmd', 0, 1, '/x', 'zsh', 'main')"
        )
        db.execute(
            "INSERT INTO events (id, timestamp, command, exit_code, duration_ms, cwd, shell, "
            "git_branch) VALUES (2, 1000, 'cmd', 0, 1, '/x', 'zsh', 'feature/x')"
        )
        db.execute("INSERT INTO knowledge_node_events VALUES (?, 1)", (k_main,))
        db.execute("INSERT INTO knowledge_node_events VALUES (?, 2)", (k_feat,))
        db.commit()

        results = search_knowledge_lexical(db, "match", branch="feature/x", limit=10)
        assert {r["uuid"] for r in results} == {"u-feat"}


class TestSearchEventsBranch:
    def test_branch_filter_shell(self, db):
        now_ms = int(time.time() * 1000)
        db.execute(
            "INSERT INTO events (timestamp, command, exit_code, duration_ms, cwd, shell, "
            "git_branch) VALUES (?, 'cmd', 0, 1, '/x', 'zsh', 'main')",
            (now_ms,),
        )
        db.execute(
            "INSERT INTO events (timestamp, command, exit_code, duration_ms, cwd, shell, "
            "git_branch) VALUES (?, 'cmd', 0, 1, '/x', 'zsh', 'postgres')",
            (now_ms,),
        )
        db.commit()

        results = search_events_impl(db, source="shell", branch="postgres", limit=10)
        assert len(results) == 1
        assert results[0]["git_branch"] == "postgres"

    def test_event_id_exposed(self, db):
        now_ms = int(time.time() * 1000)
        db.execute(
            "INSERT INTO events (id, timestamp, command, exit_code, duration_ms, cwd, shell) "
            "VALUES (42, ?, 'cmd', 0, 1, '/x', 'zsh')",
            (now_ms,),
        )
        db.commit()

        results = search_events_impl(db, source="shell", limit=10)
        assert results[0]["id"] == 42


class TestGetEntitiesFilters:
    def test_since_filter(self, db):
        db.execute(
            "INSERT INTO entities (type, name, canonical, first_seen, last_seen) "
            "VALUES ('tool', 'old', 'old', 1, 1)"
        )
        now_ms = int(time.time() * 1000)
        db.execute(
            "INSERT INTO entities (type, name, canonical, first_seen, last_seen) "
            "VALUES ('tool', 'new', 'new', ?, ?)",
            (now_ms, now_ms),
        )
        db.commit()

        results = get_entities_impl(db, since="1h", limit=50)
        assert {r["name"] for r in results} == {"new"}


class TestListProjects:
    def test_distinct_repo_cwd_pairs(self, db):
        db.execute(
            "INSERT INTO events (timestamp, command, exit_code, duration_ms, cwd, shell, git_repo) "
            "VALUES (1000, 'a', 0, 1, '/p/hippo', 'zsh', 'hippo')"
        )
        db.execute(
            "INSERT INTO events (timestamp, command, exit_code, duration_ms, cwd, shell, git_repo) "
            "VALUES (2000, 'b', 0, 1, '/p/hippo', 'zsh', 'hippo')"
        )
        db.execute(
            "INSERT INTO events (timestamp, command, exit_code, duration_ms, cwd, shell, git_repo) "
            "VALUES (1500, 'c', 0, 1, '/p/dotfiles', 'zsh', 'dotfiles')"
        )
        db.commit()

        results = list_projects_impl(db, limit=10)
        # Most recent first
        assert results[0]["cwd_root"] == "/p/hippo"
        assert results[0]["last_seen"] == 2000
        assert {r["cwd_root"] for r in results} == {"/p/hippo", "/p/dotfiles"}

    def test_includes_claude_sessions(self, db):
        db.execute(
            "INSERT INTO claude_sessions "
            "(session_id, project_dir, cwd, segment_index, start_time, end_time, "
            "summary_text, message_count, source_file) "
            "VALUES ('s1', '/p/claude-only', '/p/claude-only', 0, 5000, 6000, 's', 1, 'f.jsonl')"
        )
        db.commit()

        results = list_projects_impl(db, limit=10)
        assert any(r["cwd_root"] == "/p/claude-only" for r in results)


class TestShapeSemanticResults:
    def test_includes_uuid_and_links_when_conn(self, db):
        kid = _insert_kn(db, "u-sem", embed_text="hello")
        db.execute(
            "INSERT INTO events (id, timestamp, command, exit_code, duration_ms, cwd, shell) "
            "VALUES (77, 1000, 'cmd', 0, 1, '/x', 'zsh')"
        )
        db.execute("INSERT INTO knowledge_node_events VALUES (?, 77)", (kid,))
        db.commit()

        hits = [
            {
                "id": kid,
                "_distance": 0.1,
                "summary": "s",
                "outcome": "success",
                "tags": "[]",
                "embed_text": "hello",
                "cwd": "/x",
                "git_branch": "main",
                "captured_at": 1000,
            }
        ]
        results = shape_semantic_results(hits, conn=db)
        assert results[0]["uuid"] == "u-sem"
        assert results[0]["linked_event_ids"] == [77]
        assert 0.0 <= results[0]["score"] <= 1.0

    def test_no_conn_leaves_uuid_empty(self):
        hits = [{"id": 1, "_distance": 0.0, "summary": "s", "embed_text": "e"}]
        results = shape_semantic_results(hits, conn=None)
        assert results[0]["uuid"] == ""
        assert results[0]["linked_event_ids"] == []


class TestFormatContextBlock:
    def test_renders_numbered_sources(self):
        results = [
            {
                "uuid": "u-1",
                "score": 0.85,
                "summary": "Fixed cargo build",
                "outcome": "success",
                "cwd": "/p/hippo",
                "git_branch": "main",
                "captured_at": 1700000000000,
                "embed_text": "Removed stale dep",
            },
            {
                "uuid": "u-2",
                "score": 0.55,
                "summary": "Added pytest fixture",
                "outcome": "success",
                "cwd": "/p/hippo",
                "git_branch": "main",
                "captured_at": 1700000100000,
                "embed_text": "Use tmp_path",
            },
        ]
        block = format_context_block("how did I fix the build?", results)
        assert "# Hippo context for: how did I fix the build?" in block
        assert "## [1] Fixed cargo build" in block
        assert "## [2] Added pytest fixture" in block
        assert "u-1" in block and "u-2" in block

    def test_empty_results_returns_no_relevant(self):
        block = format_context_block("nothing", [])
        assert "_No relevant knowledge found._" in block
