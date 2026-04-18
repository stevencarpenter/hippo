"""Tests for brain/scripts/migrate-v5-to-v6.py.

Synthesises a minimal v5-shaped SQLite DB in a tmpdir and exercises every
phase of the migration script.  All tests run entirely offline — no LM Studio,
no live DB.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load the migration module from its source file
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).resolve().parents[2] / "brain" / "scripts" / "migrate-v5-to-v6.py"


def _load_module():
    # Stub out hippo_brain imports before loading the module so the test
    # doesn't require the brain venv or LM Studio.
    watchdog_stub = MagicMock()
    watchdog_stub.QUEUES = ()  # no queues; individual tests override via patch
    watchdog_stub.reap_stale_locks = MagicMock(return_value={})
    watchdog_stub.DEFAULT_LOCK_TIMEOUT_MS = 10 * 60 * 1000

    enrichment_stub = MagicMock()

    with patch.dict(
        sys.modules,
        {
            "sqlite_vec": MagicMock(),
            "hippo_brain": MagicMock(),
            "hippo_brain.enrichment": enrichment_stub,
            "hippo_brain.watchdog": watchdog_stub,
        },
    ):
        spec = importlib.util.spec_from_file_location("migrate_v5_to_v6", _SCRIPT)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

    return mod, watchdog_stub, enrichment_stub


_mod, _watchdog_stub, _enrichment_stub = _load_module()


# ---------------------------------------------------------------------------
# Helpers to build a v5 fixture DB
# ---------------------------------------------------------------------------

_SCHEMA_SQL = Path(__file__).resolve().parents[2] / "crates" / "hippo-core" / "src" / "schema.sql"


def _v5_schema_sql() -> str:
    """Return the v5 schema: full schema.sql minus the v6 virtual tables."""
    full = _SCHEMA_SQL.read_text()
    # Strip everything from the v6 section comment onwards.
    cut = full.find("-- ─── v6:")
    if cut == -1:
        cut = full.find("PRAGMA user_version = 6")
    assert cut != -1, "Could not locate v6 section in schema.sql"
    v5 = full[:cut].strip()
    v5 += "\n\nPRAGMA user_version = 5;\n"
    return v5


def _make_v5_db(path: Path) -> sqlite3.Connection:
    """Create a minimal v5 DB at `path` and return an open connection."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_v5_schema_sql())

    # Minimal fixture: one session + event + knowledge_node + queue entries
    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username, created_at) "
        "VALUES (1, 1000, 'zsh', 'localhost', 'tester', 1000)"
    )
    # Eligible shell event (non-trivial)
    conn.execute(
        "INSERT INTO events (id, session_id, timestamp, command, duration_ms, "
        "cwd, hostname, shell, created_at) "
        "VALUES (1, 1, 1000, 'cargo test', 200, '/tmp', 'localhost', 'zsh', 1000)"
    )
    # Ineligible shell event (trivial clear with no output)
    conn.execute(
        "INSERT INTO events (id, session_id, timestamp, command, duration_ms, "
        "cwd, hostname, shell, created_at) "
        "VALUES (2, 1, 2000, 'clear', 10, '/tmp', 'localhost', 'zsh', 2000)"
    )
    # knowledge_node linked to the eligible event
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, "
        "created_at, updated_at) "
        "VALUES (1, 'uuid-keep', '{\"summary\":\"cargo test run\"}', 'test run', "
        "'observation', 1000, 1000)"
    )
    conn.execute("INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (1, 1)")
    # knowledge_node linked only to the ineligible event (should be deleted by noise-cleanup)
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, "
        "created_at, updated_at) "
        "VALUES (2, 'uuid-noise', '{\"summary\":\"clear\"}', 'clear', "
        "'observation', 2000, 2000)"
    )
    conn.execute("INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (2, 2)")
    # Orphan processing lock in enrichment_queue
    conn.execute(
        "INSERT INTO enrichment_queue (event_id, status, locked_at, locked_by, "
        "created_at, updated_at) "
        "VALUES (1, 'processing', 0, 'old-worker', 1000, 1000)"
    )
    # Failed row with retries remaining, recent failure (< 24h ago)
    conn.execute(
        "INSERT INTO enrichment_queue (event_id, status, retry_count, max_retries, "
        "error_message, created_at, updated_at) "
        "VALUES (2, 'failed', 1, 5, 'llm error', 1000, ?)",
        (int(__import__("time").time() * 1000),),
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path: Path):
    db_path = tmp_path / "hippo.db"
    conn = _make_v5_db(db_path)
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# is_enrichment_eligible stub helpers
# ---------------------------------------------------------------------------


def _stub_eligibility_simple() -> None:
    """Configure enrichment stub: 'clear' with no output/duration is ineligible."""

    def _check(event_dict: dict, source: str) -> tuple[bool, str]:
        if source == "shell":
            cmd = (event_dict.get("command") or "").strip()
            dur = event_dict.get("duration_ms") or 0
            stdout = event_dict.get("stdout") or ""
            stderr = event_dict.get("stderr") or ""
            if cmd == "clear" and not stdout and not stderr and dur < 100:
                return False, "trivial clear"
        return True, "eligible"

    # _mod.is_enrichment_eligible is bound to _enrichment_stub.is_enrichment_eligible
    # at module-load time — setting side_effect propagates into the script.
    _enrichment_stub.is_enrichment_eligible.side_effect = _check


# ---------------------------------------------------------------------------
# Phase-level unit tests
# ---------------------------------------------------------------------------


def _open_v5(path: Path) -> sqlite3.Connection:
    """Open a v5 DB with row_factory but without loading sqlite-vec extension."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class TestPreflight:
    def test_rejects_wrong_version(self, tmp_db: Path) -> None:
        conn = _open_v5(tmp_db)
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        conn.close()

        conn = _open_v5(tmp_db)
        with pytest.raises(RuntimeError, match="expected schema_version=5"):
            _mod.phase_preflight(conn, tmp_db, dry_run=False, yes_backup=False, log=_dummy_log())
        conn.close()

    def test_dry_run_skips_backup(self, tmp_db: Path) -> None:
        conn = _open_v5(tmp_db)
        _mod.phase_preflight(conn, tmp_db, dry_run=True, yes_backup=False, log=_dummy_log())
        # No backup file should exist
        assert not list(tmp_db.parent.glob("hippo.db.v5-backup-*"))
        conn.close()

    def test_requires_yes_backup_flag(self, tmp_db: Path) -> None:
        conn = _open_v5(tmp_db)
        with pytest.raises(RuntimeError, match="--yes-backup"):
            _mod.phase_preflight(conn, tmp_db, dry_run=False, yes_backup=False, log=_dummy_log())
        conn.close()

    def test_creates_backup(self, tmp_db: Path) -> None:
        conn = _open_v5(tmp_db)
        # Patch _daemon_is_running to return False (daemon stopped)
        with patch.object(_mod, "_daemon_is_running", return_value=False):
            _mod.phase_preflight(conn, tmp_db, dry_run=False, yes_backup=True, log=_dummy_log())
        backups = list(tmp_db.parent.glob("hippo.db.v5-backup-*"))
        assert len(backups) == 1
        assert backups[0].stat().st_size > 0
        conn.close()

    def test_rejects_running_daemon(self, tmp_db: Path) -> None:
        conn = _open_v5(tmp_db)
        with patch.object(_mod, "_daemon_is_running", return_value=True):
            with pytest.raises(RuntimeError, match="daemon appears to be running"):
                _mod.phase_preflight(conn, tmp_db, dry_run=False, yes_backup=True, log=_dummy_log())
        conn.close()


class TestSchemaForward:
    def test_dry_run_no_changes(self, tmp_db: Path) -> None:
        conn = _open_v5(tmp_db)
        _mod.phase_schema_forward(conn, dry_run=True, log=_dummy_log())
        assert _mod._schema_version(conn) == 5
        conn.close()

    def test_applies_v6_ddl(self, tmp_db: Path) -> None:
        conn = _open_v5(tmp_db)
        # Stub sqlite-vec's load to be a no-op; we can't run vec0 without extension
        with patch.object(_mod, "_SQL_CREATE_VEC_TABLE", "SELECT 1"):
            _mod.phase_schema_forward(conn, dry_run=False, log=_dummy_log())
        assert _mod._schema_version(conn) == 6
        # FTS table should now exist
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_fts'"
        ).fetchone()
        assert row is not None
        conn.close()


class TestQueueCleanup:
    def test_dry_run_no_mutation(self, tmp_db: Path) -> None:
        conn = _open_v5(tmp_db)
        _mod.phase_queue_cleanup(conn, dry_run=True, log=_dummy_log())
        # Orphan lock should still be 'processing'
        row = conn.execute("SELECT status FROM enrichment_queue WHERE event_id = 1").fetchone()
        assert row["status"] == "processing"
        conn.close()

    def test_releases_orphan_locks(self, tmp_db: Path) -> None:
        fake_reaped = {"shell": 1, "claude": 0, "browser": 0, "workflow": 0}
        _watchdog_stub.reap_stale_locks.return_value = fake_reaped

        conn = _open_v5(tmp_db)
        _mod.phase_queue_cleanup(conn, dry_run=False, log=_dummy_log())
        _watchdog_stub.reap_stale_locks.assert_called_once()
        conn.close()

    def test_resets_recent_failed_rows(self, tmp_db: Path) -> None:
        _watchdog_stub.reap_stale_locks.return_value = {}
        _watchdog_stub.QUEUES = ()  # skip the loop via empty QUEUES in the stub

        # Directly test the SQL reset by calling it against the real DB
        conn = _open_v5(tmp_db)
        import time

        now_ms = int(time.time() * 1000)
        retry_threshold_ms = now_ms - _mod._FAILED_RETRY_WINDOW_MS
        cursor = conn.execute(
            _mod._SQL_RESET_FAILED["enrichment_queue"],
            (now_ms, retry_threshold_ms),
        )
        conn.commit()
        assert cursor.rowcount == 1
        row = conn.execute("SELECT status FROM enrichment_queue WHERE event_id = 2").fetchone()
        assert row["status"] == "pending"
        conn.close()


class TestNoiseCleanup:
    def test_dry_run_no_deletion(self, tmp_db: Path) -> None:
        _stub_eligibility_simple()
        conn = _open_v5(tmp_db)
        _mod.phase_noise_cleanup(conn, dry_run=True, yes_drop_noise=False, log=_dummy_log())
        count = conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0]
        assert count == 2
        conn.close()

    def test_requires_yes_drop_noise(self, tmp_db: Path) -> None:
        _stub_eligibility_simple()
        conn = _open_v5(tmp_db)
        with pytest.raises(RuntimeError, match="--yes-drop-noise"):
            _mod.phase_noise_cleanup(conn, dry_run=False, yes_drop_noise=False, log=_dummy_log())
        conn.close()

    def test_deletes_noise_nodes(self, tmp_db: Path) -> None:
        _stub_eligibility_simple()
        conn = _open_v5(tmp_db)
        _mod.phase_noise_cleanup(conn, dry_run=False, yes_drop_noise=True, log=_dummy_log())
        rows = conn.execute("SELECT uuid FROM knowledge_nodes ORDER BY id").fetchall()
        uuids = [r["uuid"] for r in rows]
        assert "uuid-keep" in uuids
        assert "uuid-noise" not in uuids
        conn.close()

    def test_keeps_eligible_nodes(self, tmp_db: Path) -> None:
        _enrichment_stub.is_enrichment_eligible.side_effect = lambda *_: (True, "eligible")
        conn = _open_v5(tmp_db)
        _mod.phase_noise_cleanup(conn, dry_run=False, yes_drop_noise=True, log=_dummy_log())
        count = conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0]
        assert count == 2
        conn.close()


class TestSkipPhase:
    def test_skip_noise_cleanup(self, tmp_db: Path, tmp_path: Path) -> None:
        log_path = tmp_path / "logs" / "test.log"
        result = _mod.main(
            [
                "--db",
                str(tmp_db),
                "--dry-run",
                "--skip-phase",
                "noise-cleanup",
                "--log",
                str(log_path),
            ]
        )
        # dry-run skips backup flag requirement, but preflight will succeed
        # (version check passes on v5 DB)
        assert result == 0


class TestDryRunEndToEnd:
    def test_dry_run_full_pipeline(self, tmp_db: Path, tmp_path: Path) -> None:
        """Full dry-run must complete without error and leave DB unchanged."""
        _stub_eligibility_simple()
        log_path = tmp_path / "logs" / "migration.log"

        with patch.object(_mod, "_daemon_is_running", return_value=False):
            result = _mod.main(["--db", str(tmp_db), "--dry-run", "--log", str(log_path)])

        assert result == 0
        # DB schema version must still be 5
        conn = sqlite3.connect(str(tmp_db))
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == 5
        # Log file must exist and contain JSON lines
        assert log_path.exists()
        lines = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
        assert len(lines) > 0
        for line in lines:
            obj = json.loads(line)
            assert "ts" in obj and "level" in obj and "msg" in obj


class TestAbortPaths:
    def test_wrong_version_aborts(self, tmp_db: Path, tmp_path: Path) -> None:
        conn = sqlite3.connect(str(tmp_db))
        conn.execute("PRAGMA user_version = 6")
        conn.commit()
        conn.close()

        result = _mod.main(["--db", str(tmp_db), "--dry-run", "--log", str(tmp_path / "m.log")])
        assert result == 1

    def test_missing_db_aborts(self, tmp_path: Path) -> None:
        result = _mod.main(
            ["--db", str(tmp_path / "nonexistent.db"), "--log", str(tmp_path / "m.log")]
        )
        assert result == 1

    def test_unknown_skip_phase_aborts(self, tmp_db: Path, tmp_path: Path) -> None:
        result = _mod.main(
            [
                "--db",
                str(tmp_db),
                "--dry-run",
                "--skip-phase",
                "does-not-exist",
                "--log",
                str(tmp_path / "m.log"),
            ]
        )
        assert result == 1


class TestVerifySyntheticRoundTrip:
    def test_synthetic_roundtrip_rolls_back(self, tmp_db: Path) -> None:
        """_synthetic_round_trip must leave no rows behind after rollback.

        We apply schema-forward (with vec0 patched out) and call
        _synthetic_round_trip directly so the FTS trigger path is exercised
        without needing the sqlite-vec extension.
        """
        conn = _open_v5(tmp_db)
        with patch.object(_mod, "_SQL_CREATE_VEC_TABLE", "SELECT 1"):
            _mod.phase_schema_forward(conn, dry_run=False, log=_dummy_log())

        before = conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0]

        # _synthetic_round_trip handles missing knowledge_vectors gracefully
        # (OperationalError → warning, not exception).
        _mod._synthetic_round_trip(conn, _dummy_log())

        after = conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0]
        assert after == before  # savepoint was fully rolled back
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dummy_log() -> object:
    import logging

    logger = logging.getLogger("test-migrate")
    logger.addHandler(logging.NullHandler())
    return logger
