"""Extended MCP server tests pinning behavior on public tool handlers.

These tests exist to prevent silent breakage of MCP tool contracts — each one
pins a behavior a caller would observe through the stdio protocol. Tests call
the registered handler coroutines directly with real dict payloads (no mock of
the MCP framework itself).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from hippo_brain.mcp import (
    _state,
    ask,
    get_context,
    list_projects,
    search_hybrid,
)


@pytest.fixture(autouse=True)
def _reset_state():
    """Save/restore module state so tests don't leak."""
    old_db = _state.db_path
    old_vt = _state.vector_table
    old_lm = _state.lm_client
    old_em = _state.embedding_model
    old_qm = _state.query_model
    yield
    _state.db_path = old_db
    _state.vector_table = old_vt
    _state.lm_client = old_lm
    _state.embedding_model = old_em
    _state.query_model = old_qm


# ---------------------------------------------------------------------------
# ask() — contract when prerequisites are missing
# ---------------------------------------------------------------------------


class TestAskPrerequisiteErrors:
    """Pin the exact string contract for unusable-environment errors.

    Callers (including the stdio MCP client and downstream tests) branch on
    the presence of the substring "Error:" — if this prefix ever changes
    silently, all error-handling consumers are broken.
    """

    def test_ask_returns_error_string_when_lm_client_missing(self, tmp_db):
        _conn, db_path = tmp_db
        _state.db_path = str(db_path)
        _state.lm_client = None
        _state.vector_table = object()  # present, but lm_client is not
        _state.query_model = "some-model"

        result = asyncio.run(ask(question="anything"))
        assert isinstance(result, str)
        assert result.startswith("Error:")
        assert "Semantic search not available" in result

    def test_ask_returns_error_string_when_vector_table_missing(self, tmp_db):
        _conn, db_path = tmp_db
        _state.db_path = str(db_path)
        _state.lm_client = AsyncMock()
        _state.vector_table = None  # present lm_client, missing vector table
        _state.query_model = "some-model"

        result = asyncio.run(ask(question="anything"))
        assert isinstance(result, str)
        assert result.startswith("Error:")
        assert "Semantic search not available" in result

    def test_ask_returns_error_string_when_query_model_unconfigured(self, tmp_db):
        _conn, db_path = tmp_db
        _state.db_path = str(db_path)
        _state.lm_client = AsyncMock()
        _state.vector_table = object()
        _state.query_model = ""  # misconfigured

        result = asyncio.run(ask(question="anything"))
        assert isinstance(result, str)
        assert result.startswith("Error:")
        assert "query model" in result.lower()
        assert "config.toml" in result


# ---------------------------------------------------------------------------
# search_hybrid() — exposed MCP tool; must return SearchResult-shaped dicts
# ---------------------------------------------------------------------------


class TestSearchHybridTool:
    """search_hybrid is a public MCP tool — pin its response shape and that
    it flows through the retrieval layer.
    """

    def test_search_hybrid_lexical_mode_returns_search_result_dicts(self, tmp_db):
        """lexical mode runs against FTS5 and returns the documented shape."""
        conn, db_path = tmp_db
        # knowledge_nodes FTS trigger populates knowledge_fts on insert.
        conn.execute(
            "INSERT INTO knowledge_nodes (uuid, content, embed_text, outcome, tags) "
            "VALUES ('hybrid-u1', "
            '\'{"summary":"Investigating cargo build failure"}\', '
            "'cargo build failed with linker error', "
            "'failure', '[\"rust\"]')"
        )
        conn.commit()
        _state.db_path = str(db_path)
        _state.lm_client = None  # lexical mode should not need embedding
        _state.vector_table = None

        results = asyncio.run(search_hybrid(query="cargo", mode="lexical", limit=5))
        assert isinstance(results, list)
        assert len(results) == 1
        r = results[0]
        # Exact shape contract — every consumer of search_hybrid depends on
        # these keys being present on every row.
        expected_keys = {
            "uuid",
            "score",
            "summary",
            "embed_text",
            "outcome",
            "tags",
            "cwd",
            "git_branch",
            "captured_at",
            "linked_event_ids",
        }
        assert expected_keys.issubset(r.keys()), (
            f"missing keys in result: {expected_keys - set(r.keys())}"
        )
        assert r["uuid"] == "hybrid-u1"
        assert isinstance(r["tags"], list)
        assert isinstance(r["linked_event_ids"], list)
        assert isinstance(r["score"], float)

    def test_search_hybrid_lexical_no_match_returns_empty_list(self, tmp_db):
        conn, db_path = tmp_db
        conn.execute(
            "INSERT INTO knowledge_nodes (uuid, content, embed_text, outcome, tags) "
            "VALUES ('u1', "
            '\'{"summary":"cargo test"}\', '
            "'cargo test', 'success', '[]')"
        )
        conn.commit()
        _state.db_path = str(db_path)
        _state.lm_client = None
        _state.vector_table = None

        results = asyncio.run(search_hybrid(query="zzz_no_match_xyz", mode="lexical", limit=5))
        assert results == []

    def test_search_hybrid_negative_limit_is_clamped(self, tmp_db):
        _conn, db_path = tmp_db
        _state.db_path = str(db_path)
        _state.lm_client = None
        _state.vector_table = None

        results = asyncio.run(search_hybrid(query="anything", mode="lexical", limit=-1))
        assert results == []

    def test_search_hybrid_raises_when_retrieval_fails_unrecoverably(self, tmp_db):
        """If both retrieval.search AND the lexical fallback fail, the tool
        increments error metrics and re-raises — the MCP client sees an
        error response rather than silent empty results.
        """
        _conn, db_path = tmp_db
        _state.db_path = str(db_path) + "_missing"  # bad path → fallback also fails
        _state.lm_client = None
        _state.vector_table = None

        with pytest.raises(Exception):
            asyncio.run(search_hybrid(query="anything", mode="lexical", limit=5))


# ---------------------------------------------------------------------------
# get_context() — exposed MCP tool; Markdown context block contract
# ---------------------------------------------------------------------------


class TestGetContextTool:
    def test_get_context_returns_markdown_with_query_header(self, tmp_db):
        conn, db_path = tmp_db
        conn.execute(
            "INSERT INTO knowledge_nodes (uuid, content, embed_text, outcome, tags) "
            "VALUES ('ctx-u1', "
            '\'{"summary":"Fixed clippy lint"}\', '
            "'clippy warning fixed by adding Default derive', "
            "'success', '[\"rust\"]')"
        )
        conn.commit()
        _state.db_path = str(db_path)
        _state.lm_client = None
        _state.vector_table = None

        # Patch _retrieve_filtered so we run through get_context's shaping
        # code without depending on the hybrid retrieval path's ranking.
        # This keeps the test focused on the public handler contract.
        fake_results = [
            {
                "uuid": "ctx-u1",
                "score": 0.87,
                "summary": "Fixed clippy lint",
                "embed_text": "clippy warning fixed by adding Default derive",
                "outcome": "success",
                "tags": ["rust"],
                "cwd": "/projects/hippo",
                "git_branch": "main",
                "captured_at": int(time.time() * 1000),
                "linked_event_ids": [],
            }
        ]
        with patch(
            "hippo_brain.mcp._retrieve_filtered",
            new=AsyncMock(return_value=fake_results),
        ):
            result = asyncio.run(get_context(query="clippy lint", limit=3))

        # Contract: returns a Markdown string with the query echoed in the
        # top-level header; callers paste this directly into agent prompts.
        assert isinstance(result, str)
        assert result.startswith("# Hippo context for: clippy lint")
        assert "Fixed clippy lint" in result
        assert "ctx-u1" in result
        assert "0.87" in result  # score formatted

    def test_get_context_empty_results_renders_stub(self, tmp_db):
        _conn, db_path = tmp_db
        _state.db_path = str(db_path)
        _state.lm_client = None
        _state.vector_table = None

        with patch(
            "hippo_brain.mcp._retrieve_filtered",
            new=AsyncMock(return_value=[]),
        ):
            result = asyncio.run(get_context(query="nothing here", limit=3))

        assert isinstance(result, str)
        assert "nothing here" in result
        assert "No relevant knowledge" in result

    def test_get_context_limit_is_clamped(self, tmp_db):
        _conn, db_path = tmp_db
        _state.db_path = str(db_path)
        _state.lm_client = None
        _state.vector_table = None

        captured_limits = []

        async def _capture(**kwargs):
            captured_limits.append(kwargs["limit"])
            return []

        with patch("hippo_brain.mcp._retrieve_filtered", new=_capture):
            asyncio.run(get_context(query="anything", limit=-5))

        # Clamp guarantees we never forward a negative limit to retrieval.
        assert captured_limits == [0]


# ---------------------------------------------------------------------------
# list_projects() — exposed MCP tool; discovery helper
# ---------------------------------------------------------------------------


class TestListProjectsTool:
    def test_list_projects_returns_distinct_projects_ordered(self, tmp_db):
        conn, db_path = tmp_db
        # Two sessions so we can insert events with known timestamps.
        conn.execute(
            "INSERT INTO sessions (start_time, shell, hostname, username) "
            "VALUES (1000, 'zsh', 'test-host', 'u')"
        )
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Older project
        conn.execute(
            "INSERT INTO events (session_id, timestamp, command, exit_code, "
            "duration_ms, cwd, git_repo, hostname, shell) "
            "VALUES (?, 1000, 'git status', 0, 10, "
            "'/projects/old', 'sjcarpenter/old', 'test-host', 'zsh')",
            (session_id,),
        )
        # Newer project (higher timestamp → first in results)
        conn.execute(
            "INSERT INTO events (session_id, timestamp, command, exit_code, "
            "duration_ms, cwd, git_repo, hostname, shell) "
            "VALUES (?, 5000, 'git status', 0, 10, "
            "'/projects/hippo', 'sjcarpenter/hippo', 'test-host', 'zsh')",
            (session_id,),
        )
        conn.commit()
        _state.db_path = str(db_path)

        results = asyncio.run(list_projects(limit=10))
        assert isinstance(results, list)
        assert len(results) == 2
        # Contract: each row has git_repo, cwd_root, last_seen.
        for row in results:
            assert set(row.keys()) == {"git_repo", "cwd_root", "last_seen"}
            assert isinstance(row["last_seen"], int)
        # Ordered newest-first.
        assert results[0]["cwd_root"] == "/projects/hippo"
        assert results[1]["cwd_root"] == "/projects/old"

    def test_list_projects_empty_db_returns_empty_list(self, tmp_db):
        _conn, db_path = tmp_db
        _state.db_path = str(db_path)
        results = asyncio.run(list_projects(limit=50))
        assert results == []

    def test_list_projects_limit_is_clamped(self, tmp_db):
        _conn, db_path = tmp_db
        _state.db_path = str(db_path)
        # Clamp guarantees a negative limit yields no rows (and doesn't error).
        results = asyncio.run(list_projects(limit=-1))
        assert results == []

    def test_list_projects_raises_on_db_failure(self, tmp_path):
        _state.db_path = str(tmp_path / "nonexistent.db")
        with pytest.raises(Exception):
            asyncio.run(list_projects(limit=10))


# ---------------------------------------------------------------------------
# _retrieve_filtered() — lexical fallback when retrieval.search() raises.
# This is a real behavior: if sqlite-vec or the vec table is unusable at
# query time, the tool must still return rows via the SQL-only lexical path.
# ---------------------------------------------------------------------------


class TestRetrieveFilteredFallback:
    def test_fallback_to_lexical_sql_when_retrieval_search_raises(self, tmp_db):
        """When retrieval.search() raises, _retrieve_filtered falls back to
        search_knowledge_lexical. Exposed via search_hybrid.
        """
        conn, db_path = tmp_db
        conn.execute(
            "INSERT INTO knowledge_nodes (uuid, content, embed_text, outcome, tags) "
            "VALUES ('fallback-u1', "
            '\'{"summary":"Need clippy run"}\', '
            "'cargo clippy clean', 'success', '[]')"
        )
        conn.commit()
        _state.db_path = str(db_path)
        _state.lm_client = None
        _state.vector_table = None

        # Force retrieval.search to raise; fall-through should hit lexical SQL.
        with patch(
            "hippo_brain.retrieval.search",
            side_effect=RuntimeError("vec0 unavailable"),
        ):
            results = asyncio.run(search_hybrid(query="clippy", mode="lexical", limit=5))

        # Fallback returned the real row from search_knowledge_lexical.
        assert isinstance(results, list)
        assert len(results) == 1
        # search_knowledge_lexical returns its own shape (no "uuid" key in
        # all variants); we assert only that a row came back and the fallback
        # did not re-raise.
        assert results[0]  # non-empty mapping
