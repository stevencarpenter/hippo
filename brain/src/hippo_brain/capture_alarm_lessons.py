"""Graduate recurring capture alarms into queryable lessons (F-15 / SNUG-98).

The watchdog appends rows to ``capture_alarms`` when capture invariants fail.
This module watches for new alarm rows and feeds them into ``lessons.upsert_cluster``
so hippo's own capture failures surface via ``get_lessons`` after the usual
``min_occurrences`` threshold (default 2 distinct alarm firings for the same cluster).

Processed alarm ids are tracked in ``capture_alarm_lesson_cursor`` (brain-owned
auxiliary table, created idempotently on first sync).

Auto-resolved alarms (``capture_alarms.resolved_at`` set by the watchdog once an
invariant has stayed clean for 2 consecutive ticks) are not excluded from
graduation — a self-clearing flap still counts toward ``occurrences``. Instead,
the auto-resolved fraction and median resolution time are computed from the
full alarm history for each cluster and folded into the lesson summary text, so
a consumer of ``get_lessons`` can tell "flaps constantly but always clears
itself" apart from "fires and stays red" without inferring health from a raw
occurrence count alone (issue #264).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
import time
from dataclasses import dataclass

from hippo_brain.lessons import ClusterKey, upsert_cluster
from hippo_brain.source_filters import table_exists

logger = logging.getLogger("hippo_brain")

DEFAULT_HIPPO_REPO = "stevencarpenter/hippo"
DEFAULT_MIN_OCCURRENCES = 2

_CURSOR_DDL = """
CREATE TABLE IF NOT EXISTS capture_alarm_lesson_cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_alarm_id INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO capture_alarm_lesson_cursor (id, last_alarm_id) VALUES (1, 0);
"""


@dataclass(frozen=True)
class FlapStats:
    """Auto-resolution statistics for a capture-alarm cluster (invariant + source)."""

    total: int
    auto_resolved: int
    median_resolution_ms: int | None

    @property
    def auto_resolved_fraction(self) -> float:
        return self.auto_resolved / self.total if self.total else 0.0


def _extract_source(details_json: str) -> str:
    """Best-effort extraction of the ``source`` field from an alarm's details_json."""
    try:
        payload = json.loads(details_json)
    except json.JSONDecodeError:
        return ""
    if isinstance(payload, dict):
        raw_source = payload.get("source")
        if raw_source is not None:
            return str(raw_source)
    return ""


def cluster_key_for_alarm(
    repo: str,
    invariant_id: str,
    details_json: str,
) -> ClusterKey:
    """Derive a stable lesson cluster key from a watchdog alarm row."""
    try:
        json.loads(details_json)
    except json.JSONDecodeError:
        logger.debug("capture_alarm_lessons: unparseable details_json for %s", invariant_id)

    source = _extract_source(details_json)
    tool = f"hippo-capture:{source}" if source else "hippo-watchdog"
    return ClusterKey(
        repo=repo,
        tool=tool,
        rule_id=invariant_id,
        path_prefix="docs/capture/",
    )


def _source_for_key(key: ClusterKey) -> str:
    if key.tool.startswith("hippo-capture:"):
        return key.tool.removeprefix("hippo-capture:")
    return ""


def _format_duration_ms(duration_ms: int) -> str:
    """Render a millisecond duration as a short human-readable string."""
    if duration_ms < 1_000:
        return f"{duration_ms}ms"
    seconds = duration_ms / 1_000
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f}h"
    days = hours / 24
    return f"{days:.1f}d"


def _flap_stats_for_key(conn: sqlite3.Connection, invariant_id: str, source: str) -> FlapStats:
    """Aggregate auto-resolution stats across the full capture_alarms history for
    this (invariant_id, source) cluster — not just the alarms in the current sync
    batch, so the fraction stays accurate as older alarms keep resolving."""
    rows = conn.execute(
        "SELECT raised_at, resolved_at, details_json FROM capture_alarms WHERE invariant_id = ?",
        (invariant_id,),
    ).fetchall()

    total = 0
    auto_resolved = 0
    resolution_times: list[int] = []
    for raised_at, resolved_at, details_json in rows:
        if _extract_source(details_json or "{}") != source:
            continue
        total += 1
        if resolved_at is not None:
            auto_resolved += 1
            resolution_times.append(int(resolved_at) - int(raised_at))

    median_resolution_ms = int(statistics.median(resolution_times)) if resolution_times else None
    return FlapStats(
        total=total, auto_resolved=auto_resolved, median_resolution_ms=median_resolution_ms
    )


def _summary_for_key(key: ClusterKey, stats: FlapStats | None = None) -> str:
    if key.tool.startswith("hippo-capture:"):
        source = key.tool.removeprefix("hippo-capture:")
        base = f"Capture invariant {key.rule_id} violated for source {source!r}"
    else:
        base = f"Capture invariant {key.rule_id} violated (see docs/capture/)"

    if stats is None or stats.total == 0:
        return base

    pct = round(stats.auto_resolved_fraction * 100)
    if stats.median_resolution_ms is not None:
        duration = _format_duration_ms(stats.median_resolution_ms)
        return f"{base} ({pct}% auto-resolved, median resolution {duration})"
    return f"{base} ({pct}% auto-resolved)"


def _fix_hint_for_key(_key: ClusterKey) -> str:
    return "Run `hippo doctor --explain` and see docs/capture/operator-runbook.md"


def _ensure_cursor_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_CURSOR_DDL)


def sync_capture_alarms_to_lessons(
    db_path: str,
    *,
    repo: str = DEFAULT_HIPPO_REPO,
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
    now_ms: int | None = None,
) -> int:
    """Process unseen ``capture_alarms`` rows into lesson clusters.

    Returns the number of alarm rows successfully registered. Best-effort: a
    failure on one row leaves the cursor unchanged for that row so the next
    enrichment-loop tick retries it.
    """
    if min_occurrences < 1:
        raise ValueError("min_occurrences must be >= 1")

    conn = sqlite3.connect(db_path)
    try:
        if not table_exists(conn, "capture_alarms"):
            return 0
        _ensure_cursor_schema(conn)
        cursor_row = conn.execute(
            "SELECT last_alarm_id FROM capture_alarm_lesson_cursor WHERE id = 1"
        ).fetchone()
        last_alarm_id = int(cursor_row[0]) if cursor_row else 0
        alarm_rows = conn.execute(
            """
            SELECT id, invariant_id, raised_at, details_json
            FROM capture_alarms
            WHERE id > ?
            ORDER BY id ASC
            """,
            (last_alarm_id,),
        ).fetchall()
    finally:
        conn.close()

    if not alarm_rows:
        return 0

    processed = 0
    fallback_now = now_ms if now_ms is not None else int(time.time() * 1000)
    # Clusters touched this sync, in first-seen order — their lesson summary
    # gets refreshed once at the end from the full alarm history, not per row.
    touched_keys: dict[ClusterKey, None] = {}

    for alarm_id, invariant_id, raised_at, details_json in alarm_rows:
        key = cluster_key_for_alarm(repo, invariant_id, details_json or "{}")
        event_ms = int(raised_at) if raised_at is not None else fallback_now
        try:
            upsert_cluster(
                db_path,
                key,
                min_occurrences=min_occurrences,
                summary_fn=_summary_for_key,
                fix_hint_fn=_fix_hint_for_key,
                now_ms=event_ms,
            )
        except Exception:
            logger.warning(
                "capture alarm lesson sync failed for alarm id=%s invariant=%s",
                alarm_id,
                invariant_id,
                exc_info=True,
            )
            break

        touched_keys[key] = None

        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE capture_alarm_lesson_cursor SET last_alarm_id = ? WHERE id = 1",
                (alarm_id,),
            )
            conn.commit()
        finally:
            conn.close()
        processed += 1

    if touched_keys:
        _refresh_flap_summaries(db_path, touched_keys)

    return processed


def _refresh_flap_summaries(db_path: str, keys: dict[ClusterKey, None]) -> None:
    """Recompute and persist the auto-resolved-fraction summary for each
    touched cluster's lesson row (a no-op for clusters still pending below
    ``min_occurrences``, since no lesson row exists for them yet).

    ``upsert_cluster`` only invokes ``summary_fn`` on the alarm that triggers
    graduation, so without this the summary would freeze at whatever fraction
    was true at graduation time (often just 2 occurrences) even as hundreds
    more alarms accumulate — this keeps it current on every sync.
    """
    conn = sqlite3.connect(db_path)
    try:
        for key in keys:
            source = _source_for_key(key)
            stats = _flap_stats_for_key(conn, key.rule_id, source)
            conn.execute(
                """UPDATE lessons SET summary = ?
                   WHERE repo = ? AND tool = ? AND rule_id = ? AND path_prefix = ?""",
                (
                    _summary_for_key(key, stats),
                    key.repo,
                    key.tool,
                    key.rule_id,
                    key.path_prefix,
                ),
            )
        conn.commit()
    finally:
        conn.close()
