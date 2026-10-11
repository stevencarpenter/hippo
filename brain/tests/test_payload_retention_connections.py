"""Connection settings are observable on synthetic stores, not inferred from source."""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from hippo_brain.auto_memory import _open_db
from hippo_brain.classification_cli import _connect
from hippo_brain.mcp import _get_conn
from hippo_brain.schema_version import require_accepted_schema
from hippo_brain.server import BrainServer
from hippo_brain.vector_store import open_conn


@pytest.mark.parametrize("owner", ["vector", "mcp", "server", "memory", "classification"])
def test_runtime_connections_enable_secure_delete(tmp_db, owner, monkeypatch):
    original_connect = sqlite3.connect

    def insecure_default(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        conn.execute("PRAGMA secure_delete=OFF")
        return conn

    monkeypatch.setattr(sqlite3, "connect", insecure_default)
    _, path = tmp_db
    if owner == "vector":
        conn = open_conn(path)
    elif owner == "mcp":
        conn = _get_conn(str(path))
    elif owner == "server":
        server = BrainServer.__new__(BrainServer)
        server.db_path = str(path)
        conn = server._get_conn()
    elif owner == "memory":
        conn = _open_db(path)
    else:
        conn = _connect(path, write=True)
    try:
        assert conn.execute("PRAGMA secure_delete").fetchone() == (1,)
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    finally:
        conn.close()


def test_exporter_connection_enables_secure_delete_and_remains_read_only(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[2] / "scripts" / "hippo-metrics-exporter.py"
    spec = importlib.util.spec_from_file_location("hippo_metrics_exporter", script)
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    path = tmp_path / "metrics.db"
    original_connect = sqlite3.connect
    with original_connect(path) as setup:
        setup.execute("CREATE TABLE sample (value TEXT)")
        setup.execute("INSERT INTO sample VALUES ('retained')")
    setup.close()

    def insecure_default(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        conn.execute("PRAGMA secure_delete=OFF")
        return conn

    monkeypatch.setattr(sqlite3, "connect", insecure_default)
    conn = exporter._open_ro(path)
    try:
        assert conn.execute("PRAGMA secure_delete").fetchone() == (1,)
        assert conn.execute("PRAGMA query_only").fetchone() == (1,)
        assert conn.execute("SELECT value FROM sample").fetchall() == [("retained",)]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM sample")
        conn.execute("PRAGMA query_only=OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM sample")
        assert conn.execute("SELECT value FROM sample").fetchall() == [("retained",)]
    finally:
        conn.close()


@pytest.mark.parametrize("version", [24, 25, 26])
def test_additive_payload_migration_preserves_brain_read_compatibility(tmp_db, version):
    conn, _ = tmp_db
    conn.execute(f"PRAGMA user_version={version}")
    require_accepted_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)
