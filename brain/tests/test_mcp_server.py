"""Tests for the Hippo MCP server module."""

import json
import select
import sqlite3
import subprocess
import sys
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
