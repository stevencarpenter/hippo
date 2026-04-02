"""Tests for the Hippo MCP server module."""

import sqlite3
import tempfile
from pathlib import Path

from hippo_brain.mcp import _get_conn, mcp


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
