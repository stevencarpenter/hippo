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
    resolved_at: int | None = None,
) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO capture_alarms (invariant_id, raised_at, details_json, resolved_at)
               VALUES (?, ?, ?, ?)""",
            (invariant_id, raised_at, f'{{"source":"{source}"}}', resolved_at),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _lesson_summary(db_path: str, rule_id: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT summary FROM lessons WHERE rule_id = ?", (rule_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


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


def test_summary_surfaces_auto_resolved_fraction_when_all_flaps_clear(
    db_path: str,
) -> None:
    """A cluster of alarms that always auto-resolve should read as a flap, not a
    stuck failure — the summary must carry the resolved fraction, not just a raw
    occurrence count (issue #264)."""
    _insert_alarm(db_path, "I-1", raised_at=1_000, resolved_at=1_000 + 1_320_000)
    _insert_alarm(db_path, "I-1", raised_at=2_000, resolved_at=2_000 + 1_320_000)

    assert sync_capture_alarms_to_lessons(db_path) == 2

    summary = _lesson_summary(db_path, "I-1")
    assert "100% auto-resolved" in summary
    assert "median resolution 22m" in summary


def test_summary_shows_zero_percent_for_alarms_that_never_resolve(db_path: str) -> None:
    """Alarms that stay open (resolved_at IS NULL) must not be conflated with
    auto-resolving flaps — a stuck failure should read as 0% auto-resolved."""
    _insert_alarm(db_path, "I-1", raised_at=1_000)
    _insert_alarm(db_path, "I-1", raised_at=2_000)

    assert sync_capture_alarms_to_lessons(db_path) == 2

    summary = _lesson_summary(db_path, "I-1")
    assert "0% auto-resolved" in summary
    assert "median resolution" not in summary


def test_summary_reflects_mixed_resolution_fraction(db_path: str) -> None:
    _insert_alarm(db_path, "I-1", raised_at=1_000, resolved_at=1_500)
    _insert_alarm(db_path, "I-1", raised_at=2_000, resolved_at=2_500)
    _insert_alarm(db_path, "I-1", raised_at=3_000, resolved_at=3_500)
    _insert_alarm(db_path, "I-1", raised_at=4_000)

    assert sync_capture_alarms_to_lessons(db_path) == 4

    summary = _lesson_summary(db_path, "I-1")
    assert "75% auto-resolved" in summary


def test_summary_stays_fresh_as_more_alarms_arrive_after_graduation(
    db_path: str,
) -> None:
    """The summary must reflect the CURRENT auto-resolved fraction, not the
    fraction as of the alarm that triggered graduation — otherwise a lesson
    frozen at "100% auto-resolved" after 2 occurrences would mislead once 1000
    more occurrences arrive with a different mix (issue #264)."""
    _insert_alarm(db_path, "I-1", raised_at=1_000, resolved_at=1_500)
    _insert_alarm(db_path, "I-1", raised_at=2_000, resolved_at=2_500)
    assert sync_capture_alarms_to_lessons(db_path) == 2
    assert "100% auto-resolved" in _lesson_summary(db_path, "I-1")

    # Two more alarms land, neither resolves — the fraction should drop.
    _insert_alarm(db_path, "I-1", raised_at=3_000)
    _insert_alarm(db_path, "I-1", raised_at=4_000)
    assert sync_capture_alarms_to_lessons(db_path) == 2

    summary = _lesson_summary(db_path, "I-1")
    assert "50% auto-resolved" in summary


def test_flap_stats_scoped_per_source_not_shared_across_invariant(db_path: str) -> None:
    """Two sources sharing an invariant_id must not blend their resolution
    stats — a firefox flap resolving shouldn't mask a stuck shell failure."""
    _insert_alarm(db_path, "I-1", source="shell", raised_at=1_000)
    _insert_alarm(db_path, "I-1", source="shell", raised_at=2_000)
    _insert_alarm(db_path, "I-1", source="browser", raised_at=1_000, resolved_at=1_500)
    _insert_alarm(db_path, "I-1", source="browser", raised_at=2_000, resolved_at=2_500)

    assert sync_capture_alarms_to_lessons(db_path) == 4

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT tool, summary FROM lessons WHERE rule_id = 'I-1'").fetchall()
    finally:
        conn.close()

    summaries = {tool: summary for tool, summary in rows}
    assert "0% auto-resolved" in summaries["hippo-capture:shell"]
    assert "100% auto-resolved" in summaries["hippo-capture:browser"]
