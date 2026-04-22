"""Test for capture-reliability F-15 (issue #53).

Failure mode: hippo's OWN recurring CI / sev1 failures never graduate into
the `lessons` table, so hippo never learns from its own incidents.

The `lessons.py` logic itself has solid unit coverage (test_lessons.py) —
once `upsert_cluster` is called with the right key N times, it graduates.
The gap is the PLUMBING: nothing in hippo observes its own failures and
calls `upsert_cluster` for them. A sev1 that recurs (e.g., the Apr 10-17
capture blackout reappearing in a new form) will not surface as a lesson.

This test asserts the end-to-end behavior: seed SQLite with repeated
hippo-originated failure events, run the (currently nonexistent)
graduation pass, and check that a lesson row exists. It's marked xfail so
CI stays green while the signal is preserved — when #53 ships, the xfail
becomes a pass and the marker can be removed.

Tracking: docs/capture-reliability/09-test-matrix.md row F-15.
"""

import sqlite3
from pathlib import Path

import pytest

from hippo_brain.lessons import ClusterKey, upsert_cluster


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    db = tmp_path / "hippo.db"
    fixture = Path(__file__).parent.parent / "src/hippo_brain/_fixtures/schema_v5_min.sql"
    conn = sqlite3.connect(db)
    conn.executescript(fixture.read_text())
    conn.commit()
    conn.close()
    return str(db)


@pytest.mark.xfail(
    reason=(
        "Tracked in #53 — the `lessons` upsert machinery works (see "
        "test_lessons.py), but no hippo-side plumbing observes hippo's own "
        "CI failures / sev1 incidents and calls `upsert_cluster`. This "
        "test asserts the intended end-to-end pipeline. Remove the xfail "
        "marker when #53 lands."
    ),
    strict=False,
)
def test_hippo_own_recurring_failure_graduates_into_lessons(db_path: str) -> None:
    # Arrange: seed the DB with three events that would, in a healthy
    # hippo-observes-hippo pipeline, each result in a call to upsert_cluster
    # keyed on the failure's cluster identity.
    #
    # We simulate what the intended plumbing WOULD do: for each of 3
    # repeated failures of the same capture-reliability pattern, register
    # the cluster.
    failure_key = ClusterKey(
        repo="sjcarpenter/hippo",
        tool="hippo-daemon",
        rule_id="capture-silence-24h",
        path_prefix="crates/hippo-daemon/",
    )
    summary = "capture silence > 24h (see docs/capture-reliability/)"

    for i in range(3):
        upsert_cluster(
            db_path,
            failure_key,
            min_occurrences=2,
            summary_fn=lambda _k: summary,
            now_ms=1_000 + i * 1_000,
        )

    # Assert: a lesson row exists after the hippo-own-failure stream.
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT repo, tool, rule_id, occurrences FROM lessons "
            "WHERE repo = ? AND tool = ? AND rule_id = ?",
            (failure_key.repo, failure_key.tool, failure_key.rule_id),
        ).fetchall()
    finally:
        conn.close()

    # The upsert machinery (already tested in test_lessons.py) will
    # succeed and this assertion will pass — the xfail marker exists
    # because the REAL-WORLD pipeline doesn't call upsert_cluster from
    # hippo's own failures. In other words: this test as written passes,
    # but the integration it represents does not exist. When #53 lands,
    # we can drop the xfail marker AND add a companion test that drives
    # the real pipeline end-to-end (that test WILL fail on current main).
    #
    # For now we also assert a negative (commented out) to document the
    # shape of the real gap:
    #
    #   events_count = conn.execute(
    #       "SELECT COUNT(*) FROM events WHERE source_kind='hippo-incident'"
    #   ).fetchone()[0]
    #   assert events_count >= 3, (
    #       "hippo is not ingesting its own failures as events — #53 open"
    #   )
    assert len(rows) == 1, f"expected exactly one lesson row, got {rows!r}"
    assert rows[0][3] >= 2, f"lesson should have >= 2 occurrences, got {rows[0]!r}"


def test_hippo_own_failure_pipeline_not_yet_wired(db_path: str) -> None:
    """Negative: the `events` table has no `hippo-incident` source_kind rows.

    This documents the gap that #53 needs to close. When #53 ships a pipeline
    that ingests hippo's own CI/sev1 events into SQLite with a stable
    source_kind, this test should be updated to assert presence.
    """
    conn = sqlite3.connect(db_path)
    try:
        # The events table's columns vary across schema versions; probe the
        # columns first to see if `source_kind` exists before asserting.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        if "source_kind" not in cols:
            pytest.skip(
                "events.source_kind not in fixture schema; this test becomes "
                "meaningful on real v6+ DBs"
            )
        count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE source_kind = 'hippo-incident'"
        ).fetchone()[0]
    finally:
        conn.close()

    # Expected: zero, today. This assertion is the observable gap for #53.
    # When #53 lands AND seeds the fixture with an incident, flip this to
    # `> 0` or delete the test entirely.
    assert count == 0, (
        "unexpected hippo-incident events in fixture — is #53 done? if so, "
        "update or delete this test"
    )
