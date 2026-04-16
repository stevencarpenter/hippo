"""Tests for GitHub Actions MCP tools (get_ci_status, get_lessons)."""

import asyncio
import time

import pytest

from hippo_brain.mcp import _state, get_ci_status, get_lessons, mcp


class TestToolRegistration:
    def test_get_ci_status_registered(self):
        assert "get_ci_status" in mcp._tool_manager._tools

    def test_get_lessons_registered(self):
        assert "get_lessons" in mcp._tool_manager._tools


@pytest.fixture()
def workflow_db(tmp_db):
    """Insert a workflow run with job and annotation into the shared tmp_db fixture."""
    conn, db_path = tmp_db
    conn.execute("""
        INSERT INTO workflow_runs
          (id, repo, head_sha, head_branch, event, status, conclusion,
           html_url, raw_json, first_seen_at, last_seen_at)
        VALUES (1, 'me/r', 'abc123', 'main', 'push', 'completed', 'failure',
                'https://github.com/me/r/actions/runs/1', '{}', 1000, 2000)
    """)
    conn.execute("""
        INSERT INTO workflow_jobs (id, run_id, name, status, conclusion, raw_json)
        VALUES (10, 1, 'lint', 'completed', 'failure', '{}')
    """)
    conn.execute("""
        INSERT INTO workflow_annotations
          (job_id, level, tool, rule_id, path, start_line, message)
        VALUES (10, 'failure', 'ruff', 'F401', 'src/main.py', 3, 'F401 unused import')
    """)
    conn.commit()
    return conn, db_path


@pytest.fixture()
def lessons_db(tmp_db):
    """Insert a lesson into the shared tmp_db fixture."""
    conn, db_path = tmp_db
    now_ms = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO lessons
          (repo, tool, rule_id, path_prefix, summary, fix_hint,
           occurrences, first_seen_at, last_seen_at)
        VALUES ('me/r', 'ruff', 'F401', 'src/', 'unused imports in src/',
                'remove unused imports', 3, ?, ?)
        """,
        (now_ms - 10000, now_ms),
    )
    conn.commit()
    return conn, db_path


@pytest.fixture(autouse=True)
def _reset_state():
    """Save and restore _state between tests."""
    old_db = _state.db_path
    yield
    _state.db_path = old_db


class TestGetCIStatusTool:
    def test_returns_status_for_known_sha(self, workflow_db):
        _, db_path = workflow_db
        _state.db_path = str(db_path)

        result = asyncio.run(get_ci_status(repo="me/r", sha="abc123"))
        assert result["conclusion"] == "failure"
        assert len(result["jobs"]) == 1
        assert result["jobs"][0]["annotations"][0]["rule_id"] == "F401"

    def test_returns_empty_dict_for_missing_sha(self, workflow_db):
        _, db_path = workflow_db
        _state.db_path = str(db_path)

        result = asyncio.run(get_ci_status(repo="me/r", sha="notfound"))
        assert result == {}

    def test_returns_status_by_branch(self, workflow_db):
        _, db_path = workflow_db
        _state.db_path = str(db_path)

        result = asyncio.run(get_ci_status(repo="me/r", branch="main"))
        assert result["head_sha"] == "abc123"

    def test_raises_on_missing_sha_and_branch(self, workflow_db):
        _, db_path = workflow_db
        _state.db_path = str(db_path)

        with pytest.raises(ValueError):
            asyncio.run(get_ci_status(repo="me/r"))


class TestGetLessonsTool:
    def test_returns_lessons_for_repo(self, lessons_db):
        _, db_path = lessons_db
        _state.db_path = str(db_path)

        results = asyncio.run(get_lessons(repo="me/r"))
        assert len(results) == 1
        assert results[0]["summary"] == "unused imports in src/"
        assert results[0]["occurrences"] == 3

    def test_returns_empty_list_for_unknown_repo(self, lessons_db):
        _, db_path = lessons_db
        _state.db_path = str(db_path)

        results = asyncio.run(get_lessons(repo="other/repo"))
        assert results == []

    def test_filter_by_tool(self, lessons_db):
        _, db_path = lessons_db
        _state.db_path = str(db_path)

        results = asyncio.run(get_lessons(repo="me/r", tool="ruff"))
        assert len(results) == 1

        results = asyncio.run(get_lessons(repo="me/r", tool="clippy"))
        assert results == []

    def test_returns_list_of_dicts(self, lessons_db):
        _, db_path = lessons_db
        _state.db_path = str(db_path)

        results = asyncio.run(get_lessons())
        assert isinstance(results, list)
        assert all(isinstance(r, dict) for r in results)
