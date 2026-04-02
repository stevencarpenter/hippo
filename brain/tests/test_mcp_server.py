"""Tests for the Hippo MCP server module."""

import asyncio
import json
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hippo_brain.embeddings import EMBED_DIM
from hippo_brain.mcp import (
    _get_conn,
    _load_config,
    _state,
    get_entities,
    mcp,
    metrics,
    search_events,
    search_knowledge,
)


class TestToolRegistration:
    def test_search_knowledge_registered(self):
        assert "search_knowledge" in mcp._tool_manager._tools

    def test_search_events_registered(self):
        assert "search_events" in mcp._tool_manager._tools

    def test_get_entities_registered(self):
        assert "get_entities" in mcp._tool_manager._tools

    def test_exactly_three_tools(self):
        assert len(mcp._tool_manager._tools) == 3


class TestGetConn:
    def test_returns_working_connection(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            conn = _get_conn(db_path=db_path)
            assert isinstance(conn, sqlite3.Connection)
            # Verify we can execute a query
            result = conn.execute("SELECT 1").fetchone()
            assert result == (1,)
            conn.close()
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_wal_mode_enabled(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            conn = _get_conn(db_path=db_path)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode == "wal"
            conn.close()
        finally:
            Path(db_path).unlink(missing_ok=True)
            Path(db_path + "-wal").unlink(missing_ok=True)
            Path(db_path + "-shm").unlink(missing_ok=True)


class TestMCPStdioProtocol:
    def test_server_starts_and_responds_to_initialize(self):
        """Start hippo-mcp as subprocess, send MCP initialize, verify response.

        MCP SDK >=1.x uses newline-delimited JSON for stdio transport (not
        Content-Length framing).  Each message is a single JSON line.
        """
        proc = subprocess.Popen(
            [sys.executable, "-m", "hippo_brain.mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            init_msg = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0.1"},
                    },
                }
            )

            proc.stdin.write((init_msg + "\n").encode())
            proc.stdin.flush()

            # Read response line with timeout
            ready, _, _ = select.select([proc.stdout], [], [], 10)
            assert ready, "MCP server did not respond within 10 seconds"

            response_line = proc.stdout.readline()
            assert response_line, "MCP server returned empty response"

            response = json.loads(response_line)

            assert response.get("id") == 1
            assert "result" in response
            assert "serverInfo" in response["result"]
        finally:
            proc.terminate()
            proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_missing_config_returns_defaults(self, tmp_path, monkeypatch):
        """When config.toml doesn't exist, defaults are returned."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        config = _load_config()
        assert "hippo.db" in config["db_path"]
        assert config["lmstudio_base_url"] == "http://localhost:1234/v1"
        assert config["embedding_model"] == ""
        # data_dir should derive from home
        assert config["data_dir"] == str(tmp_path / ".local" / "share" / "hippo")

    def test_valid_config_parsed(self, tmp_path, monkeypatch):
        """Config values are read from TOML correctly."""
        config_dir = tmp_path / ".config" / "hippo"
        config_dir.mkdir(parents=True)
        (config_dir / "config.toml").write_text(
            '[storage]\ndata_dir = "/custom/data"\n\n'
            '[lmstudio]\nbase_url = "http://custom:5678/v1"\n\n'
            '[models]\nembedding = "nomic-embed"\n'
        )
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        config = _load_config()
        assert config["db_path"] == "/custom/data/hippo.db"
        assert config["data_dir"] == "/custom/data"
        assert config["lmstudio_base_url"] == "http://custom:5678/v1"
        assert config["embedding_model"] == "nomic-embed"

    def test_partial_config_fills_defaults(self, tmp_path, monkeypatch):
        """Missing sections fall back to defaults."""
        config_dir = tmp_path / ".config" / "hippo"
        config_dir.mkdir(parents=True)
        (config_dir / "config.toml").write_text('[lmstudio]\nbase_url = "http://other:9999/v1"\n')
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        config = _load_config()
        # storage section missing → defaults
        assert config["data_dir"] == str(tmp_path / ".local" / "share" / "hippo")
        assert config["lmstudio_base_url"] == "http://other:9999/v1"
        assert config["embedding_model"] == ""


# ---------------------------------------------------------------------------
# Helpers for tool-level tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def knowledge_db(tmp_db):
    """Insert a knowledge node into the shared tmp_db fixture."""
    conn, db_path = tmp_db
    conn.execute(
        "INSERT INTO knowledge_nodes (uuid, content, embed_text, outcome, tags) "
        "VALUES ('u1', "
        '\'{"summary":"Fixed cargo build error","intent":"debugging"}\', '
        "'Fixed cargo build error by adding missing dependency', "
        "'success', '[\"rust\",\"cargo\"]')"
    )
    conn.commit()
    return conn, db_path


@pytest.fixture()
def events_db(tmp_db):
    """Insert a shell event into the shared tmp_db fixture."""
    conn, db_path = tmp_db
    # events requires a session row due to FK constraint
    conn.execute(
        "INSERT INTO sessions (start_time, shell, hostname, username) "
        "VALUES (?, 'zsh', 'test-host', 'testuser')",
        (int(time.time() * 1000),),
    )
    session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    now_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO events (session_id, timestamp, command, exit_code, duration_ms, "
        "cwd, hostname, shell) "
        "VALUES (?, ?, 'cargo test', 0, 1000, '/projects/hippo', 'test-host', 'zsh')",
        (session_id, now_ms),
    )
    conn.commit()
    return conn, db_path


@pytest.fixture()
def entities_db(tmp_db):
    """Insert an entity into the shared tmp_db fixture."""
    conn, db_path = tmp_db
    conn.execute(
        "INSERT INTO entities (type, name, canonical, first_seen, last_seen) "
        "VALUES ('tool', 'cargo', 'cargo', 1000, 2000)"
    )
    conn.commit()
    return conn, db_path


@pytest.fixture(autouse=True)
def _reset_state():
    """Save and restore _state and metrics between tests."""
    old_db = _state.db_path
    old_vt = _state.vector_table
    old_lm = _state.lm_client
    old_em = _state.embedding_model
    snapshot = metrics.snapshot()
    yield
    _state.db_path = old_db
    _state.vector_table = old_vt
    _state.lm_client = old_lm
    _state.embedding_model = old_em
    # Restore metric counters
    for k, v in snapshot.items():
        setattr(metrics, k, v)


# ---------------------------------------------------------------------------
# search_knowledge tool
# ---------------------------------------------------------------------------


class TestSearchKnowledgeTool:
    def test_lexical_search_finds_matching_node(self, knowledge_db):
        conn, db_path = knowledge_db
        _state.db_path = str(db_path)
        _state.vector_table = None
        _state.lm_client = None

        old_calls = metrics.tool_calls
        old_lexical = metrics.lexical_searches

        results = asyncio.run(search_knowledge("cargo build", mode="lexical", limit=10))
        assert len(results) == 1
        assert "cargo build" in results[0]["embed_text"].lower()
        assert metrics.tool_calls == old_calls + 1
        assert metrics.lexical_searches == old_lexical + 1

    def test_lexical_search_no_results(self, knowledge_db):
        conn, db_path = knowledge_db
        _state.db_path = str(db_path)
        _state.vector_table = None
        _state.lm_client = None

        results = asyncio.run(
            search_knowledge("nonexistent_gibberish_xyz", mode="lexical", limit=10)
        )
        assert results == []

    def test_semantic_fallback_when_no_vector_table(self, knowledge_db):
        """When mode=semantic but no vector table exists, falls back to lexical."""
        conn, db_path = knowledge_db
        _state.db_path = str(db_path)
        _state.vector_table = None
        _state.lm_client = None

        old_lexical = metrics.lexical_searches

        results = asyncio.run(search_knowledge("cargo", mode="semantic", limit=10))
        assert len(results) == 1
        # Should have gone through lexical path (no vector_table, no lm_client)
        assert metrics.lexical_searches == old_lexical + 1

    def test_semantic_fallback_when_no_lm_client(self, knowledge_db):
        """When mode=semantic but lm_client is None, falls back to lexical."""
        conn, db_path = knowledge_db
        _state.db_path = str(db_path)
        _state.vector_table = "fake_table"
        _state.lm_client = None

        results = asyncio.run(search_knowledge("cargo", mode="semantic", limit=10))
        assert len(results) == 1

    def test_semantic_fallback_on_embed_error(self, knowledge_db):
        """When embedding call fails, falls back to lexical and increments error counters."""
        conn, db_path = knowledge_db
        _state.db_path = str(db_path)
        _state.embedding_model = "test-model"

        mock_client = AsyncMock()
        mock_client.embed.side_effect = RuntimeError("LM Studio unreachable")
        _state.lm_client = mock_client
        _state.vector_table = "fake_table"

        old_fallbacks = metrics.lexical_fallbacks
        old_lm_errors = metrics.lmstudio_errors

        results = asyncio.run(search_knowledge("cargo", mode="semantic", limit=10))
        assert len(results) == 1  # Fell back to lexical successfully
        assert metrics.lexical_fallbacks == old_fallbacks + 1
        assert metrics.lmstudio_errors == old_lm_errors + 1

    def test_semantic_search_pads_query_vector_to_embed_dim(self, knowledge_db, monkeypatch):
        conn, db_path = knowledge_db
        _state.db_path = str(db_path)
        _state.embedding_model = "test-model"
        _state.vector_table = object()

        mock_client = AsyncMock()
        mock_client.embed.return_value = [[0.25] * 384]
        _state.lm_client = mock_client

        def fake_search_similar(table, query_vec, limit=10):
            assert table is _state.vector_table
            assert len(query_vec) == EMBED_DIM
            return [
                {
                    "_distance": 0.1,
                    "summary": "semantic result",
                    "outcome": "success",
                    "tags": "[]",
                    "embed_text": "semantic result",
                    "cwd": "/projects/hippo",
                    "git_branch": "main",
                }
            ]

        monkeypatch.setattr("hippo_brain.mcp.search_similar", fake_search_similar)

        results = asyncio.run(search_knowledge("cargo", mode="semantic", limit=10))
        assert len(results) == 1
        assert results[0]["score"] == 0.9

    def test_empty_query_returns_all(self, knowledge_db):
        conn, db_path = knowledge_db
        _state.db_path = str(db_path)
        _state.vector_table = None
        _state.lm_client = None

        results = asyncio.run(search_knowledge("", mode="lexical", limit=10))
        assert len(results) == 1

    def test_search_knowledge_negative_limit_is_clamped(self, knowledge_db):
        conn, db_path = knowledge_db
        _state.db_path = str(db_path)
        _state.vector_table = None
        _state.lm_client = None

        results = asyncio.run(search_knowledge("cargo", mode="lexical", limit=-1))
        assert results == []

    def test_limit_respected(self, knowledge_db):
        conn, db_path = knowledge_db
        # Add a second node
        conn.execute(
            "INSERT INTO knowledge_nodes (uuid, content, embed_text, outcome, tags) "
            "VALUES ('u2', "
            '\'{"summary":"cargo clippy clean","intent":"linting"}\', '
            "'cargo clippy all clean', 'success', '[\"rust\"]')"
        )
        conn.commit()

        _state.db_path = str(db_path)
        _state.vector_table = None
        _state.lm_client = None

        results = asyncio.run(search_knowledge("cargo", mode="lexical", limit=1))
        assert len(results) == 1


# ---------------------------------------------------------------------------
# search_events tool
# ---------------------------------------------------------------------------


class TestSearchEventsTool:
    def test_search_events_shell(self, events_db):
        conn, db_path = events_db
        _state.db_path = str(db_path)

        old_calls = metrics.tool_calls
        old_events = metrics.events_searched

        results = asyncio.run(search_events(query="cargo", source="shell", limit=10))
        assert len(results) == 1
        assert results[0]["source"] == "shell"
        assert "cargo test" in results[0]["summary"]
        assert metrics.tool_calls == old_calls + 1
        assert metrics.events_searched == old_events + 1

    def test_search_events_no_match(self, events_db):
        conn, db_path = events_db
        _state.db_path = str(db_path)

        results = asyncio.run(search_events(query="nonexistent_xyz", source="shell", limit=10))
        assert results == []

    def test_search_events_claude_source(self, events_db):
        """Claude source returns empty when no claude_sessions rows exist."""
        conn, db_path = events_db
        _state.db_path = str(db_path)

        results = asyncio.run(search_events(query="", source="claude", limit=10))
        assert results == []

    def test_search_events_browser_source(self, events_db):
        """Browser source returns empty when no browser_events rows exist."""
        conn, db_path = events_db
        _state.db_path = str(db_path)

        results = asyncio.run(search_events(query="", source="browser", limit=10))
        assert results == []

    def test_search_events_all_sources(self, events_db):
        """Source 'all' includes shell events."""
        conn, db_path = events_db
        _state.db_path = str(db_path)

        results = asyncio.run(search_events(query="cargo", source="all", limit=10))
        assert len(results) == 1
        assert results[0]["source"] == "shell"

    def test_search_events_with_project_filter(self, events_db):
        conn, db_path = events_db
        _state.db_path = str(db_path)

        results = asyncio.run(search_events(query="", source="shell", project="hippo", limit=10))
        assert len(results) == 1

        results = asyncio.run(
            search_events(query="", source="shell", project="nonexistent", limit=10)
        )
        assert results == []

    def test_search_events_with_browser_data(self, events_db):
        """Browser events are returned when data exists."""
        conn, db_path = events_db
        now_ms = int(time.time() * 1000)
        conn.execute(
            "INSERT INTO browser_events (timestamp, url, title, domain, dwell_ms, scroll_depth) "
            "VALUES (?, 'https://docs.rs/anyhow', 'anyhow docs', 'docs.rs', 5000, 0.75)",
            (now_ms,),
        )
        conn.commit()
        _state.db_path = str(db_path)

        results = asyncio.run(search_events(query="anyhow", source="browser", limit=10))
        assert len(results) == 1
        assert results[0]["source"] == "browser"
        assert "docs.rs" in results[0]["summary"]

    def test_search_events_negative_limit_is_clamped(self, events_db):
        conn, db_path = events_db
        _state.db_path = str(db_path)

        results = asyncio.run(search_events(query="cargo", source="shell", limit=-1))
        assert results == []


# ---------------------------------------------------------------------------
# get_entities tool
# ---------------------------------------------------------------------------


class TestGetEntitiesTool:
    def test_get_entities_by_type(self, entities_db):
        conn, db_path = entities_db
        _state.db_path = str(db_path)

        old_calls = metrics.tool_calls
        old_entities = metrics.entities_returned

        results = asyncio.run(get_entities(type="tool", limit=10))
        assert len(results) == 1
        assert results[0]["name"] == "cargo"
        assert results[0]["type"] == "tool"
        assert metrics.tool_calls == old_calls + 1
        assert metrics.entities_returned == old_entities + 1

    def test_get_entities_no_filter(self, entities_db):
        conn, db_path = entities_db
        _state.db_path = str(db_path)

        results = asyncio.run(get_entities(limit=50))
        assert len(results) == 1

    def test_get_entities_query_filter(self, entities_db):
        conn, db_path = entities_db
        _state.db_path = str(db_path)

        results = asyncio.run(get_entities(query="car", limit=10))
        assert len(results) == 1

        results = asyncio.run(get_entities(query="nonexistent", limit=10))
        assert results == []

    def test_get_entities_type_mismatch(self, entities_db):
        conn, db_path = entities_db
        _state.db_path = str(db_path)

        results = asyncio.run(get_entities(type="project", limit=10))
        assert results == []

    def test_get_entities_multiple(self, entities_db):
        conn, db_path = entities_db
        conn.execute(
            "INSERT INTO entities (type, name, canonical, first_seen, last_seen) "
            "VALUES ('tool', 'rustc', 'rustc', 1000, 3000)"
        )
        conn.commit()
        _state.db_path = str(db_path)

        results = asyncio.run(get_entities(type="tool", limit=50))
        assert len(results) == 2
        # Sorted by last_seen DESC
        assert results[0]["name"] == "rustc"
        assert results[1]["name"] == "cargo"

    def test_get_entities_negative_limit_is_clamped(self, entities_db):
        conn, db_path = entities_db
        _state.db_path = str(db_path)

        results = asyncio.run(get_entities(type="tool", limit=-1))
        assert results == []


# ---------------------------------------------------------------------------
# Error handling and metrics
# ---------------------------------------------------------------------------


class TestMetricsOnError:
    def test_search_knowledge_increments_errors_on_db_failure(self, tmp_path):
        """When DB doesn't exist, search_knowledge raises and increments tool_errors."""
        _state.db_path = str(tmp_path / "nonexistent.db")
        _state.vector_table = None
        _state.lm_client = None

        old_errors = metrics.tool_errors
        with pytest.raises(Exception):
            asyncio.run(search_knowledge("test", mode="lexical"))
        assert metrics.tool_errors == old_errors + 1

    def test_search_events_increments_errors_on_db_failure(self, tmp_path):
        """When DB doesn't exist, search_events raises and increments tool_errors."""
        _state.db_path = str(tmp_path / "nonexistent.db")

        old_errors = metrics.tool_errors
        with pytest.raises(Exception):
            asyncio.run(search_events(query="test", source="shell"))
        assert metrics.tool_errors == old_errors + 1

    def test_get_entities_increments_errors_on_db_failure(self, tmp_path):
        """When DB doesn't exist, get_entities raises and increments tool_errors."""
        _state.db_path = str(tmp_path / "nonexistent.db")

        old_errors = metrics.tool_errors
        with pytest.raises(Exception):
            asyncio.run(get_entities(type="tool"))
        assert metrics.tool_errors == old_errors + 1

    def test_tool_calls_always_incremented_even_on_failure(self, tmp_path):
        """tool_calls increments even when the tool errors."""
        _state.db_path = str(tmp_path / "nonexistent.db")
        _state.vector_table = None
        _state.lm_client = None

        old_calls = metrics.tool_calls
        with pytest.raises(Exception):
            asyncio.run(search_knowledge("test", mode="lexical"))
        assert metrics.tool_calls == old_calls + 1
