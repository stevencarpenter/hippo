#!/usr/bin/env python3
"""v5 → v6 phased migration for the hippo SQLite DB.

Upgrades from schema v5 (LanceDB-backed, no FTS5) to v6 (sqlite-vec + FTS5
hybrid retrieval).  The script is phase-gated: each destructive phase requires
an explicit flag so it cannot be triggered by accident.

Phases
------
1. preflight         — version check, services-stopped check, WAL-safe .backup
2. schema-forward    — apply v6 DDL (FTS5 virtual table + triggers + vec0),
                       bump user_version to 6
3. queue-cleanup     — release orphan processing locks; retry eligible failures
4. noise-cleanup     — delete knowledge_nodes whose source events are ineligible
                       (requires --yes-drop-noise; irreversible;
                        requires --yes-extreme-deletion if >50% of nodes would be deleted)
5. re-embed          — delegate to migrate-vectors.py subprocess
                       (requires --yes-reembed)
6. git-repo-backfill — delegate to backfill-git-repo.py subprocess
7. entity-dedup      — delegate to dedup-entities.py subprocess
8. verify            — row-count parity; synthetic schema round-trip (savepoint)

Usage
-----
    uv run --project brain python brain/scripts/migrate-v5-to-v6.py \\
        --yes-backup --yes-drop-noise --yes-reembed

    # dry-run (read-only; reports what would change):
    uv run --project brain python brain/scripts/migrate-v5-to-v6.py --dry-run

Safety invariants
-----------------
- Every mutating phase is transaction-wrapped.  On failure: stop, log, and exit
  non-zero.  Do NOT auto-rollback — restore from the .backup if needed.
- Subprocess phases (re-embed, backfill, dedup) commit internally; they are
  documented partial-state risks.  Verify row counts between phases if concerned.
- The .backup is taken via the SQLite backup API (WAL-safe, not cp).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import logging
import os
import socket as _socket
import sqlite3
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# sqlite-vec must be loaded before any connection touches knowledge_vectors.
import sqlite_vec  # type: ignore[import-untyped]  # noqa: E402

from hippo_brain.enrichment import is_enrichment_eligible  # type: ignore[import-untyped]  # noqa: E402
from hippo_brain.watchdog import QUEUES, reap_stale_locks  # type: ignore[import-untyped]  # noqa: E402

_SCRIPTS_DIR = Path(__file__).resolve().parent

PHASES = [
    "preflight",
    "schema-forward",
    "queue-cleanup",
    "noise-cleanup",
    "re-embed",
    "git-repo-backfill",
    "entity-dedup",
    "verify",
]

# Lock timeout used by queue-cleanup (matches watchdog default: 10 min).
_QUEUE_LOCK_TIMEOUT_MS = 10 * 60 * 1000
# Failures older than 24 h with retries remaining get reset to pending.
_FAILED_RETRY_WINDOW_MS = 24 * 60 * 60 * 1000
_EMBED_DIM = 768


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


_FMT = logging.Formatter()


class _JsonLineHandler(logging.FileHandler):
    """Emit one JSON object per log line to the structured log file."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
            entry: dict[str, object] = {
                "ts": ts,
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            if record.exc_info:
                entry["exc"] = _FMT.formatException(record.exc_info)
            self.stream.write(json.dumps(entry) + "\n")
            self.stream.flush()
        except Exception:
            self.handleError(record)


def _setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("migrate-v5-to-v6")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)
    logger.addHandler(ch)

    fh = _JsonLineHandler(str(log_path))
    fh.setLevel(logging.DEBUG)
    logger.addHandler(fh)

    return logger


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _open_conn(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with sqlite-vec loaded and standard hippo PRAGMAs."""
    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _vec_blob(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    # pragma_table_info(?) accepts a bind parameter — no string interpolation needed.
    row = conn.execute(
        "SELECT COUNT(*) FROM pragma_table_info(?) WHERE name = ?",
        (table, column),
    ).fetchone()
    return bool(row and row[0])


# ---------------------------------------------------------------------------
# Services-stopped check
# ---------------------------------------------------------------------------

_DEFAULT_SOCKET_NAME = "daemon.sock"


def _daemon_is_running(data_dir: Path) -> bool:
    """Return True if the hippo daemon socket is bound and accepting connections."""
    sock_path = data_dir / _DEFAULT_SOCKET_NAME
    if not sock_path.exists():
        return False
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(str(sock_path))
        return True
    except ConnectionRefusedError, OSError:
        return False


# ---------------------------------------------------------------------------
# Phase 1: preflight
# ---------------------------------------------------------------------------


def phase_preflight(
    conn: sqlite3.Connection,
    db_path: Path,
    dry_run: bool,
    yes_backup: bool,
    log: logging.Logger,
) -> None:
    log.info("[preflight] checking schema version")
    version = _schema_version(conn)
    if version != 5:
        hint = (
            " If schema was already bumped, resume with: "
            "--skip-phase preflight --skip-phase schema-forward"
            if version == 6
            else ""
        )
        raise RuntimeError(
            f"preflight: expected schema_version=5, got {version}. Already migrated, or wrong DB?{hint}"
        )
    log.info("[preflight] schema_version=5 ✓")

    data_dir = db_path.parent
    if _daemon_is_running(data_dir):
        raise RuntimeError(
            "[preflight] hippo daemon appears to be running "
            f"(socket {data_dir / _DEFAULT_SOCKET_NAME} is bound). "
            "Stop it with `mise run stop` before migrating."
        )
    log.info("[preflight] daemon socket not bound ✓")

    if dry_run:
        log.info("[preflight] --dry-run: skipping .backup snapshot")
        return

    if not yes_backup:
        raise RuntimeError(
            "[preflight] --yes-backup flag required to take the .backup snapshot "
            "and proceed past phase 1."
        )

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.parent / f"hippo.db.v5-backup-{ts}"
    log.info("[preflight] creating WAL-safe backup → %s", backup_path)
    backup_conn = sqlite3.connect(str(backup_path))
    try:
        conn.backup(backup_conn)
    finally:
        backup_conn.close()
    log.info(
        "[preflight] backup complete: %s (%.1f MB)", backup_path, backup_path.stat().st_size / 1e6
    )

    verify_conn = sqlite3.connect(str(backup_path))
    try:
        result = verify_conn.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise RuntimeError(
                f"[preflight] backup integrity check FAILED ({result}). "
                "Do not proceed. Check disk space and retry."
            )
        log.info("[preflight] backup integrity check passed ✓")
    finally:
        verify_conn.close()


# ---------------------------------------------------------------------------
# Phase 2: schema-forward
# ---------------------------------------------------------------------------

_V6_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    summary,
    embed_text,
    content,
    tokenize = 'porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS knowledge_nodes_fts_ai
AFTER INSERT ON knowledge_nodes
BEGIN
    INSERT INTO knowledge_fts (rowid, summary, embed_text, content)
    VALUES (
        NEW.id,
        COALESCE(CASE WHEN json_valid(NEW.content) THEN json_extract(NEW.content, '$.summary') END, ''),
        NEW.embed_text,
        NEW.content
    );
END;

CREATE TRIGGER IF NOT EXISTS knowledge_nodes_fts_ad
AFTER DELETE ON knowledge_nodes
BEGIN
    DELETE FROM knowledge_fts WHERE rowid = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS knowledge_nodes_fts_au
AFTER UPDATE ON knowledge_nodes
BEGIN
    DELETE FROM knowledge_fts WHERE rowid = OLD.id;
    INSERT INTO knowledge_fts (rowid, summary, embed_text, content)
    VALUES (
        NEW.id,
        COALESCE(CASE WHEN json_valid(NEW.content) THEN json_extract(NEW.content, '$.summary') END, ''),
        NEW.embed_text,
        NEW.content
    );
END;

CREATE TABLE IF NOT EXISTS embed_model_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    model TEXT NOT NULL
);
"""

_SQL_CREATE_VEC_TABLE = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_vectors USING vec0("
    "knowledge_node_id INTEGER PRIMARY KEY, "
    "vec_knowledge FLOAT[768] distance_metric=cosine, "
    "vec_command FLOAT[768] distance_metric=cosine)"
)

_SQL_FTS_BACKFILL = """
INSERT INTO knowledge_fts (rowid, summary, embed_text, content)
SELECT
    id,
    COALESCE(CASE WHEN json_valid(content) THEN json_extract(content, '$.summary') END, ''),
    embed_text,
    content
FROM knowledge_nodes
"""


def phase_schema_forward(
    conn: sqlite3.Connection,
    dry_run: bool,
    log: logging.Logger,
) -> None:
    log.info("[schema-forward] applying v6 DDL (FTS5 + triggers + vec0)")
    if dry_run:
        log.info(
            "[schema-forward] --dry-run: would apply v6 DDL, backfill FTS, "
            "and bump user_version to 6"
        )
        return

    with conn:
        conn.executescript(_V6_DDL)
        conn.execute(_SQL_CREATE_VEC_TABLE)
        conn.execute("PRAGMA user_version = 6")

    log.info("[schema-forward] schema_version bumped to 6 ✓")

    # AFTER INSERT triggers only fire for future inserts; backfill pre-existing rows.
    node_count = conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0]
    fts_existing = conn.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0]
    if fts_existing == 0 and node_count > 0:
        log.info("[schema-forward] backfilling FTS for %d existing knowledge_nodes", node_count)
        with conn:
            conn.execute(_SQL_FTS_BACKFILL)
        backfilled = conn.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0]
        log.info("[schema-forward] FTS backfill complete: %d rows", backfilled)
        if backfilled != node_count:
            raise RuntimeError(
                f"[schema-forward] FTS backfill incomplete: "
                f"expected {node_count} rows, got {backfilled}"
            )
    else:
        log.info(
            "[schema-forward] FTS already populated (%d rows), skipping backfill", fts_existing
        )


# ---------------------------------------------------------------------------
# Phase 3: queue-cleanup
# Pre-built SQL strings keyed by table name — no runtime string interpolation.
# All table names come from the frozen QUEUES tuple in watchdog.py.
# ---------------------------------------------------------------------------

# Hardcoded per-table SQL — table names are literals, never user input.
_SQL_COUNT_STALE: dict[str, str] = {
    "enrichment_queue": "SELECT COUNT(*) FROM enrichment_queue WHERE status = 'processing' AND COALESCE(locked_at, 0) <= ?",  # noqa: E501
    "claude_enrichment_queue": "SELECT COUNT(*) FROM claude_enrichment_queue WHERE status = 'processing' AND COALESCE(locked_at, 0) <= ?",  # noqa: E501
    "browser_enrichment_queue": "SELECT COUNT(*) FROM browser_enrichment_queue WHERE status = 'processing' AND COALESCE(locked_at, 0) <= ?",  # noqa: E501
    "workflow_enrichment_queue": "SELECT COUNT(*) FROM workflow_enrichment_queue WHERE status = 'processing' AND COALESCE(locked_at, 0) <= ?",  # noqa: E501
}
_SQL_COUNT_FAILED_ELIGIBLE: dict[str, str] = {
    "enrichment_queue": "SELECT COUNT(*) FROM enrichment_queue WHERE status = 'failed' AND retry_count < max_retries AND COALESCE(updated_at, 0) > ?",  # noqa: E501
    "claude_enrichment_queue": "SELECT COUNT(*) FROM claude_enrichment_queue WHERE status = 'failed' AND retry_count < max_retries AND COALESCE(updated_at, 0) > ?",  # noqa: E501
    "browser_enrichment_queue": "SELECT COUNT(*) FROM browser_enrichment_queue WHERE status = 'failed' AND retry_count < max_retries AND COALESCE(updated_at, 0) > ?",  # noqa: E501
    "workflow_enrichment_queue": "SELECT COUNT(*) FROM workflow_enrichment_queue WHERE status = 'failed' AND retry_count < max_retries AND COALESCE(updated_at, 0) > ?",  # noqa: E501
}
_SQL_RESET_FAILED: dict[str, str] = {
    "enrichment_queue": "UPDATE enrichment_queue SET status = 'pending', error_message = 'reset by v5→v6 migration', locked_at = NULL, locked_by = NULL, updated_at = ? WHERE status = 'failed' AND retry_count < max_retries AND COALESCE(updated_at, 0) > ?",  # noqa: E501
    "claude_enrichment_queue": "UPDATE claude_enrichment_queue SET status = 'pending', error_message = 'reset by v5→v6 migration', locked_at = NULL, locked_by = NULL, updated_at = ? WHERE status = 'failed' AND retry_count < max_retries AND COALESCE(updated_at, 0) > ?",  # noqa: E501
    "browser_enrichment_queue": "UPDATE browser_enrichment_queue SET status = 'pending', error_message = 'reset by v5→v6 migration', locked_at = NULL, locked_by = NULL, updated_at = ? WHERE status = 'failed' AND retry_count < max_retries AND COALESCE(updated_at, 0) > ?",  # noqa: E501
    "workflow_enrichment_queue": "UPDATE workflow_enrichment_queue SET status = 'pending', error_message = 'reset by v5→v6 migration', locked_at = NULL, locked_by = NULL, updated_at = ? WHERE status = 'failed' AND retry_count < max_retries AND COALESCE(updated_at, 0) > ?",  # noqa: E501
}
_SQL_GIVEUP: dict[str, str] = {
    "enrichment_queue": "UPDATE enrichment_queue SET giveup = 1 WHERE status = 'failed' AND COALESCE(updated_at, 0) <= ?",  # noqa: E501
    "claude_enrichment_queue": "UPDATE claude_enrichment_queue SET giveup = 1 WHERE status = 'failed' AND COALESCE(updated_at, 0) <= ?",  # noqa: E501
    "browser_enrichment_queue": "UPDATE browser_enrichment_queue SET giveup = 1 WHERE status = 'failed' AND COALESCE(updated_at, 0) <= ?",  # noqa: E501
    "workflow_enrichment_queue": "UPDATE workflow_enrichment_queue SET giveup = 1 WHERE status = 'failed' AND COALESCE(updated_at, 0) <= ?",  # noqa: E501
}


def phase_queue_cleanup(
    conn: sqlite3.Connection,
    dry_run: bool,
    log: logging.Logger,
) -> None:
    log.info("[queue-cleanup] releasing orphan processing locks")
    now_ms = int(time.time() * 1000)

    if dry_run:
        for spec in QUEUES:
            threshold_ms = now_ms - _QUEUE_LOCK_TIMEOUT_MS
            try:
                count = conn.execute(_SQL_COUNT_STALE[spec.table], (threshold_ms,)).fetchone()[0]
                if count:
                    log.info(
                        "[queue-cleanup] --dry-run: would reap %d stale locks in %s",
                        count,
                        spec.table,
                    )
            except sqlite3.OperationalError:
                pass

        retry_threshold_ms = now_ms - _FAILED_RETRY_WINDOW_MS
        for spec in QUEUES:
            try:
                count = conn.execute(
                    _SQL_COUNT_FAILED_ELIGIBLE[spec.table], (retry_threshold_ms,)
                ).fetchone()[0]
                if count:
                    log.info(
                        "[queue-cleanup] --dry-run: would reset %d failed→pending in %s",
                        count,
                        spec.table,
                    )
            except sqlite3.OperationalError:
                pass
        return

    reaped = reap_stale_locks(conn, lock_timeout_ms=_QUEUE_LOCK_TIMEOUT_MS, now_ms=now_ms)
    for queue_name, count in reaped.items():
        if count:
            log.info("[queue-cleanup] reaped %d stale locks in %s queue", count, queue_name)

    retry_threshold_ms = now_ms - _FAILED_RETRY_WINDOW_MS
    total_reset = 0
    for spec in QUEUES:
        try:
            with conn:
                cursor = conn.execute(_SQL_RESET_FAILED[spec.table], (now_ms, retry_threshold_ms))
            count = cursor.rowcount
            if count:
                log.info("[queue-cleanup] reset %d failed→pending in %s", count, spec.table)
                total_reset += count
        except sqlite3.OperationalError as e:
            if "no such table" in str(e).lower():
                continue
            raise

    for spec in QUEUES:
        if _column_exists(conn, spec.table, "giveup"):
            with conn:
                conn.execute(_SQL_GIVEUP[spec.table], (retry_threshold_ms,))
            log.info("[queue-cleanup] marked old failures giveup=1 in %s", spec.table)

    log.info("[queue-cleanup] done; total_reset=%d", total_reset)


# ---------------------------------------------------------------------------
# Phase 4: noise-cleanup
# ---------------------------------------------------------------------------


def _load_shell_events_for_node(conn: sqlite3.Connection, node_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT e.id, e.command, e.stdout, e.stderr, e.duration_ms "
        "FROM knowledge_node_events kne "
        "JOIN events e ON e.id = kne.event_id "
        "WHERE kne.knowledge_node_id = ?",
        (node_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_claude_sessions_for_node(conn: sqlite3.Connection, node_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT cs.id, cs.message_count, cs.tool_calls_json "
        "FROM knowledge_node_claude_sessions kncs "
        "JOIN claude_sessions cs ON cs.id = kncs.claude_session_id "
        "WHERE kncs.knowledge_node_id = ?",
        (node_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_browser_events_for_node(conn: sqlite3.Connection, node_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT be.id, be.dwell_ms "
        "FROM knowledge_node_browser_events knbe "
        "JOIN browser_events be ON be.id = knbe.browser_event_id "
        "WHERE knbe.knowledge_node_id = ?",
        (node_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _node_is_noise(conn: sqlite3.Connection, node_id: int) -> bool:
    """Return True if all source events for this node fail eligibility."""
    shell_events = _load_shell_events_for_node(conn, node_id)
    claude_sessions = _load_claude_sessions_for_node(conn, node_id)
    browser_events = _load_browser_events_for_node(conn, node_id)

    # A node with no linked sources is kept (may be workflow or future source).
    if not shell_events and not claude_sessions and not browser_events:
        return False

    # If ANY source is eligible, keep the node.
    for ev in shell_events:
        ok, _ = is_enrichment_eligible(ev, "shell")
        if ok:
            return False

    for sess in claude_sessions:
        ok, _ = is_enrichment_eligible(sess, "claude")
        if ok:
            return False

    for ev in browser_events:
        ok, _ = is_enrichment_eligible(ev, "browser")
        if ok:
            return False

    return True


def phase_noise_cleanup(
    conn: sqlite3.Connection,
    dry_run: bool,
    yes_drop_noise: bool,
    yes_extreme_deletion: bool,
    log: logging.Logger,
) -> None:
    log.info("[noise-cleanup] scanning knowledge_nodes for ineligible source events")

    node_ids = [
        row[0] for row in conn.execute("SELECT id FROM knowledge_nodes ORDER BY id").fetchall()
    ]
    log.info("[noise-cleanup] total knowledge_nodes: %d", len(node_ids))

    noise_ids = [nid for nid in node_ids if _node_is_noise(conn, nid)]
    log.info("[noise-cleanup] noise nodes to delete: %d / %d", len(noise_ids), len(node_ids))

    if not noise_ids:
        log.info("[noise-cleanup] nothing to delete")
        return

    if dry_run:
        log.info("[noise-cleanup] --dry-run: would delete %d noise nodes", len(noise_ids))
        return

    if not yes_drop_noise:
        raise RuntimeError(
            "[noise-cleanup] --yes-drop-noise flag required. "
            "This phase hard-deletes knowledge_nodes; restore from .backup if needed."
        )

    if len(noise_ids) > 0.5 * len(node_ids):
        if not yes_extreme_deletion:
            raise RuntimeError(
                f"[noise-cleanup] ABORTED: would delete {len(noise_ids)}/{len(node_ids)} nodes "
                f"({100 * len(noise_ids) // len(node_ids)}% of corpus). "
                "This exceeds the 50% safety threshold. "
                "Inspect the noise list via --dry-run, then rerun with "
                "--yes-drop-noise --yes-extreme-deletion to override."
            )
        log.warning(
            "[noise-cleanup] WARNING: deleting >50%% of nodes (%d/%d) — "
            "--yes-extreme-deletion passed, proceeding.",
            len(noise_ids),
            len(node_ids),
        )

    with conn:
        for nid in noise_ids:
            # Delete join-table rows first (schema has no ON DELETE CASCADE).
            conn.execute("DELETE FROM knowledge_node_events WHERE knowledge_node_id = ?", (nid,))
            conn.execute("DELETE FROM knowledge_node_entities WHERE knowledge_node_id = ?", (nid,))
            conn.execute(
                "DELETE FROM knowledge_node_claude_sessions WHERE knowledge_node_id = ?", (nid,)
            )
            conn.execute(
                "DELETE FROM knowledge_node_browser_events WHERE knowledge_node_id = ?", (nid,)
            )
            conn.execute(
                "DELETE FROM knowledge_node_workflow_runs WHERE knowledge_node_id = ?", (nid,)
            )
            conn.execute("DELETE FROM knowledge_node_lessons WHERE knowledge_node_id = ?", (nid,))
            # FTS row cascades via trigger; vec0 row cascades via trigger if present.
            conn.execute("DELETE FROM knowledge_nodes WHERE id = ?", (nid,))

    log.info(
        "[noise-cleanup] deleted %d noise nodes (vec + FTS cascade via triggers)", len(noise_ids)
    )


# ---------------------------------------------------------------------------
# Phase 5–7: subprocess phases
# ---------------------------------------------------------------------------


def _run_subprocess(
    cmd: list[str],
    phase_label: str,
    log: logging.Logger,
    dry_run: bool,
) -> None:
    """Run a child script, streaming its stderr into our log. Raise on non-zero exit."""
    if dry_run:
        log.info("[%s] --dry-run: would run: %s", phase_label, " ".join(cmd))
        return

    log.info("[%s] running: %s", phase_label, " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    stdout_data, stderr_data = proc.communicate()

    for line in stderr_data.splitlines():
        line = line.rstrip("\n")
        if line:
            log.info("[%s] %s", phase_label, line)

    for line in stdout_data.splitlines():
        line = line.rstrip("\n")
        if line:
            log.debug("[%s] stdout: %s", phase_label, line)
    if proc.returncode != 0:
        raise RuntimeError(
            f"[{phase_label}] subprocess exited with code {proc.returncode}: {' '.join(cmd)}"
        )
    log.info("[%s] subprocess completed successfully", phase_label)


def phase_re_embed(
    db_path: Path,
    dry_run: bool,
    yes_reembed: bool,
    log: logging.Logger,
) -> None:
    if not dry_run and not yes_reembed:
        raise RuntimeError(
            "[re-embed] --yes-reembed flag required. "
            "Re-embedding ~1.9K nodes takes ~6 min and chews LM Studio capacity."
        )

    script = _SCRIPTS_DIR / "migrate-vectors.py"
    cmd = [
        sys.executable,
        str(script),
        "--db",
        str(db_path),
    ]
    _run_subprocess(cmd, "re-embed", log, dry_run)


def phase_git_repo_backfill(
    db_path: Path,
    dry_run: bool,
    log: logging.Logger,
) -> None:
    script = _SCRIPTS_DIR / "backfill-git-repo.py"
    cmd = [sys.executable, str(script), "--db", str(db_path)]
    if dry_run:
        cmd.append("--dry-run")
    _run_subprocess(
        cmd, "git-repo-backfill", log, dry_run=False
    )  # always run; handles --dry-run itself


def phase_entity_dedup(
    db_path: Path,
    dry_run: bool,
    log: logging.Logger,
) -> None:
    script = _SCRIPTS_DIR / "dedup-entities.py"
    cmd = [sys.executable, str(script), "--db", str(db_path)]
    if dry_run:
        cmd.append("--dry-run")
    _run_subprocess(cmd, "entity-dedup", log, dry_run=False)  # always run; handles --dry-run itself


# ---------------------------------------------------------------------------
# Phase 8: verify
# ---------------------------------------------------------------------------


def phase_verify(
    conn: sqlite3.Connection,
    dry_run: bool,
    log: logging.Logger,
) -> None:
    log.info("[verify] checking row-count parity")

    kn_count = conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0]

    # knowledge_vectors (vec0) is created by schema-forward; skip count if the
    # table doesn't exist yet (e.g. when running verify after a dry-run migration).
    try:
        kv_count: int | None = conn.execute("SELECT COUNT(*) FROM knowledge_vectors").fetchone()[0]
    except sqlite3.OperationalError:
        kv_count = None
        log.warning("[verify] knowledge_vectors table not found — was schema-forward skipped?")

    # knowledge_fts (FTS5) is likewise created by schema-forward.
    try:
        fts_count: int | None = conn.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0]
    except sqlite3.OperationalError:
        fts_count = None
        log.warning("[verify] knowledge_fts table not found — was schema-forward skipped?")

    log.info(
        "[verify] knowledge_nodes=%d  knowledge_vectors=%s  knowledge_fts=%s",
        kn_count,
        kv_count if kv_count is not None else "N/A",
        fts_count if fts_count is not None else "N/A",
    )

    if not dry_run:
        if kv_count is None:
            raise RuntimeError("[verify] knowledge_vectors table missing after schema-forward")
        if fts_count is None:
            raise RuntimeError("[verify] knowledge_fts table missing after schema-forward")
        if kn_count != kv_count:
            raise RuntimeError(
                f"[verify] row-count mismatch: knowledge_nodes={kn_count} "
                f"!= knowledge_vectors={kv_count}"
            )
        if kn_count != fts_count:
            raise RuntimeError(
                f"[verify] row-count mismatch: knowledge_nodes={kn_count} "
                f"!= knowledge_fts={fts_count}"
            )

    log.info("[verify] running synthetic round-trip (savepoint)")
    _synthetic_round_trip(conn, log)
    log.info("[verify] all checks passed ✓")


def _synthetic_round_trip(conn: sqlite3.Connection, log: logging.Logger) -> None:
    """Insert fake rows in a savepoint, verify triggers + vec0, then rollback."""
    SYNTH_UUID = "00000000-0000-0000-0000-000000000000"
    SYNTH_CONTENT = json.dumps({"summary": "synthetic migration verify node"})
    SYNTH_EMBED_TEXT = "synthetic migration verify"
    ZERO_VEC = _vec_blob([0.0] * _EMBED_DIM)

    conn.execute("SAVEPOINT synth_verify")
    try:
        # Temporarily disable FK enforcement so we can insert without a real session/event.
        conn.execute("PRAGMA foreign_keys=OFF")

        # Insert a synthetic session and event (needed for FK chain if enforced).
        conn.execute(
            "INSERT INTO sessions (id, start_time, terminal, shell, hostname, username, created_at) "
            "VALUES (999999, 1, NULL, 'zsh', 'localhost', 'verify', 1)"
        )
        conn.execute(
            "INSERT INTO events (id, session_id, timestamp, command, duration_ms, "
            "cwd, hostname, shell, created_at) "
            "VALUES (999999, 999999, 1, 'echo verify', 1, '/', 'localhost', 'zsh', 1)"
        )

        cursor = conn.execute(
            "INSERT INTO knowledge_nodes (uuid, content, embed_text, node_type, "
            "created_at, updated_at) VALUES (?, ?, ?, 'observation', 1, 1)",
            (SYNTH_UUID, SYNTH_CONTENT, SYNTH_EMBED_TEXT),
        )
        node_id = cursor.lastrowid

        # Verify FTS trigger fired (table exists after schema-forward).
        try:
            fts_row = conn.execute(
                "SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH 'synthetic'",
            ).fetchone()
            if fts_row is None:
                raise RuntimeError("[verify] FTS trigger did not fire for synthetic node")
            log.debug("[verify] FTS trigger fired ✓ (rowid=%s)", fts_row[0])
        except sqlite3.OperationalError as e:
            if "no such table" in str(e).lower():
                log.warning(
                    "[verify] knowledge_fts not found — schema-forward may have been skipped"
                )
            else:
                raise

        # Insert vec0 row (only if table exists — requires sqlite-vec extension).
        try:
            conn.execute(
                "INSERT OR REPLACE INTO knowledge_vectors "
                "(knowledge_node_id, vec_knowledge, vec_command) VALUES (?, ?, ?)",
                (node_id, ZERO_VEC, ZERO_VEC),
            )
            vec_row = conn.execute(
                "SELECT knowledge_node_id FROM knowledge_vectors WHERE knowledge_node_id = ?",
                (node_id,),
            ).fetchone()
            if vec_row is None:
                raise RuntimeError("[verify] vec0 insert did not land")
            log.debug("[verify] vec0 insert ✓ (node_id=%s)", node_id)
        except sqlite3.OperationalError as e:
            if "no such table" in str(e).lower():
                log.warning(
                    "[verify] knowledge_vectors not found — extension or schema-forward skipped"
                )
            else:
                raise

    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("ROLLBACK TO SAVEPOINT synth_verify")
        conn.execute("RELEASE SAVEPOINT synth_verify")
        log.debug("[verify] synthetic rows rolled back")


# ---------------------------------------------------------------------------
# Argument parsing + main
# ---------------------------------------------------------------------------


def _default_db() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "hippo" / "hippo.db"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=_default_db(),
        metavar="PATH",
        help="Path to hippo.db (default: $XDG_DATA_HOME/hippo/hippo.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read-only mode: report what would change, make no mutations.",
    )
    parser.add_argument(
        "--yes-backup",
        action="store_true",
        help="Acknowledge backup strategy; required to proceed past phase 1.",
    )
    parser.add_argument(
        "--yes-drop-noise",
        action="store_true",
        help="Acknowledge irreversible noise-cleanup deletion; required for phase 4.",
    )
    parser.add_argument(
        "--yes-reembed",
        action="store_true",
        help="Acknowledge re-embed duration and LM Studio cost; required for phase 5.",
    )
    parser.add_argument(
        "--yes-extreme-deletion",
        action="store_true",
        help=(
            "Required when noise-cleanup would delete >50%% of the corpus. "
            "Pass alongside --yes-drop-noise to override the 50%% safety gate."
        ),
    )
    parser.add_argument(
        "--skip-phase",
        action="append",
        default=[],
        metavar="PHASE",
        dest="skip_phases",
        help=f"Skip a phase by name (repeatable). Valid: {', '.join(PHASES)}",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        metavar="PATH",
        help="Structured log path (default: logs/migration-<timestamp>.log)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = args.log or Path("logs") / f"migration-{ts}.log"
    log = _setup_logging(log_path)

    for phase in args.skip_phases:
        if phase not in PHASES:
            log.error("Unknown --skip-phase value: %r. Valid phases: %s", phase, PHASES)
            return 1

    db_path: Path = args.db
    if not db_path.exists():
        log.error("DB not found: %s", db_path)
        return 1

    log.info(
        "Starting v5→v6 migration  db=%s  dry_run=%s  skip_phases=%s",
        db_path,
        args.dry_run,
        args.skip_phases or "none",
    )

    try:
        conn = _open_conn(db_path)
    except Exception as e:
        log.error("Failed to open DB: %s", e)
        return 1

    def _skip(phase: str) -> bool:
        if phase in args.skip_phases:
            log.info("[%s] skipped via --skip-phase", phase)
            return True
        return False

    try:
        # Phase 1: preflight
        if not _skip("preflight"):
            phase_preflight(conn, db_path, args.dry_run, args.yes_backup, log)

        # Phase 2: schema-forward
        if not _skip("schema-forward"):
            phase_schema_forward(conn, args.dry_run, log)

        # Phase 3: queue-cleanup
        if not _skip("queue-cleanup"):
            phase_queue_cleanup(conn, args.dry_run, log)

        # Phase 4: noise-cleanup (requires --yes-drop-noise)
        if not _skip("noise-cleanup"):
            phase_noise_cleanup(
                conn, args.dry_run, args.yes_drop_noise, args.yes_extreme_deletion, log
            )

        # Phase 5: re-embed (requires --yes-reembed)
        if not _skip("re-embed"):
            phase_re_embed(db_path, args.dry_run, args.yes_reembed, log)

        # Phase 6: git-repo-backfill
        if not _skip("git-repo-backfill"):
            phase_git_repo_backfill(db_path, args.dry_run, log)

        # Phase 7: entity-dedup
        if not _skip("entity-dedup"):
            phase_entity_dedup(db_path, args.dry_run, log)

        # Phase 8: verify
        if not _skip("verify"):
            phase_verify(conn, args.dry_run, log)

        log.info("Migration complete ✓  log=%s", log_path)
        return 0

    except RuntimeError as e:
        log.error("Migration FAILED: %s", e)
        log.error("DO NOT attempt auto-rollback. Restore from .backup if needed.")
        return 1
    except Exception as e:
        log.exception("Unexpected error during migration: %s", e)
        log.error("DO NOT attempt auto-rollback. Restore from .backup if needed.")
        return 1
    finally:
        with contextlib.suppress(Exception):
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
