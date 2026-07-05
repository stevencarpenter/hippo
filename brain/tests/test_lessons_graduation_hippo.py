"""End-to-end test for capture-reliability F-15 (issue #53 / SNUG-98).

Hippo's own recurring capture invariant violations graduate into the ``lessons``
table via ``capture_alarm_lessons.sync_capture_alarms_to_lessons``.

Tracking: docs/capture/test-matrix.md row F-15.
"""

import sqlite3
from pathlib import Path

import pytest

from hippo_brain.capture_alarm_lessons import sync_capture_alarms_to_lessons

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


def _insert_alarm(db_path: str, invariant_id: str, raised_at: int, source: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json) VALUES (?, ?, ?)",
            (invariant_id, raised_at, f'{{"source":"{source}"}}'),
        )
        conn.commit()
    finally:
        conn.close()


def test_hippo_own_recurring_capture_alarm_graduates_into_lessons(db_path: str) -> None:
    """Two watchdog alarm firings for the same invariant promote a lesson."""
    _insert_alarm(db_path, "I-1", 1_000, "shell")
    _insert_alarm(db_path, "I-1", 2_000, "shell")

    processed = sync_capture_alarms_to_lessons(db_path)
    assert processed == 2

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT repo, tool, rule_id, occurrences FROM lessons "
            "WHERE repo = 'stevencarpenter/hippo' AND rule_id = 'I-1'"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1, f"expected exactly one lesson row, got {rows!r}"
    assert rows[0][3] >= 2, f"lesson should have >= 2 occurrences, got {rows[0]!r}"
