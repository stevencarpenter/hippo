import sqlite3
import time

import pytest

from hippo_brain.mcp_queries import (
    get_entities_impl,
    parse_since,
    search_events_impl,
    search_knowledge_lexical,
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
