"""Unit tests for capture_alarms → lessons graduation (SNUG-98 / F-15)."""

import sqlite3
from pathlib import Path

import pytest

from hippo_brain.capture_alarm_lessons import (
    cluster_key_for_alarm,
    sync_capture_alarms_to_lessons,
)

_CAPTURE_ALARMS_DDL = """
CREATE TABLE IF NOT EXISTS capture_alarms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invariant_id TEXT NOT NULL,
    raised_at INTEGER NOT NULL,
    details_json TEXT NOT NULL,
    acked_at INTEGER,
    resolved_at INTEGER,
    clean_ticks INTEGER NOT NULL DEFAULT 0
);
"""


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    db = tmp_path / "hippo.db"
    fixture = Path(__file__).parent.parent / "src/hippo_brain/_fixtures/schema_v5_min.sql"
    conn = sqlite3.connect(db)
    conn.executescript(fixture.read_text())
    conn.executescript(_CAPTURE_ALARMS_DDL)
    conn.commit()
    conn.close()
    return str(db)


def _insert_alarm(
    db_path: str,
    invariant_id: str,
    *,
    source: str = "shell",
    raised_at: int = 1_000,
) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json) VALUES (?, ?, ?)",
            (invariant_id, raised_at, f'{{"source":"{source}"}}'),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def test_cluster_key_uses_invariant_and_source() -> None:
    key = cluster_key_for_alarm(
        "stevencarpenter/hippo",
        "I-1",
        '{"source":"shell","since_ms":1}',
    )
    assert key.repo == "stevencarpenter/hippo"
    assert key.tool == "hippo-capture:shell"
    assert key.rule_id == "I-1"
    assert key.path_prefix == "docs/capture/"


def test_single_alarm_stays_pending(db_path: str) -> None:
    _insert_alarm(db_path, "I-1", raised_at=1_000)
    processed = sync_capture_alarms_to_lessons(db_path, now_ms=2_000)
    assert processed == 1

    conn = sqlite3.connect(db_path)
    try:
        lesson_count = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        pending_count = conn.execute("SELECT count FROM lesson_pending").fetchone()[0]
    finally:
        conn.close()

    assert lesson_count == 0
    assert pending_count == 1


def test_two_alarms_graduate_lesson(db_path: str) -> None:
    _insert_alarm(db_path, "I-1", raised_at=1_000)
    _insert_alarm(db_path, "I-1", raised_at=2_000)

    assert sync_capture_alarms_to_lessons(db_path) == 2

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT repo, tool, rule_id, occurrences FROM lessons WHERE rule_id = 'I-1'"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == "stevencarpenter/hippo"
    assert row[1] == "hippo-capture:shell"
    assert row[2] == "I-1"
    assert row[3] >= 2


def test_sync_is_idempotent_for_processed_alarms(db_path: str) -> None:
    _insert_alarm(db_path, "I-3", raised_at=1_000)
    _insert_alarm(db_path, "I-3", raised_at=2_000)
    assert sync_capture_alarms_to_lessons(db_path) == 2
    assert sync_capture_alarms_to_lessons(db_path) == 0

    conn = sqlite3.connect(db_path)
    try:
        occurrences = conn.execute(
            "SELECT occurrences FROM lessons WHERE rule_id = 'I-3'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert occurrences == 2
