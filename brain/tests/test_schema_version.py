"""Tests for schema_version.require_accepted_schema."""

import sqlite3

import pytest

from hippo_brain.schema_version import EXPECTED_SCHEMA_VERSION, require_accepted_schema


def test_require_accepted_schema_allows_expected_version(tmp_path):
    db_path = tmp_path / "ok.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(f"PRAGMA user_version = {EXPECTED_SCHEMA_VERSION}")
    conn.close()

    conn = sqlite3.connect(str(db_path))
    require_accepted_schema(conn)
    assert conn.execute("SELECT 1").fetchone() == (1,)
    conn.close()


def test_require_accepted_schema_rejects_stale_version(tmp_path):
    db_path = tmp_path / "stale.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 11")
    conn.close()

    conn = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(RuntimeError, match="schema version mismatch"):
            require_accepted_schema(conn)
    finally:
        conn.close()
