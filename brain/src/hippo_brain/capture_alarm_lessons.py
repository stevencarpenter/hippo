"""Graduate recurring capture alarms into queryable lessons (F-15 / SNUG-98).

The watchdog appends rows to ``capture_alarms`` when capture invariants fail.
This module watches for new alarm rows and feeds them into ``lessons.upsert_cluster``
so hippo's own capture failures surface via ``get_lessons`` after the usual
``min_occurrences`` threshold (default 2 distinct alarm firings for the same cluster).

Processed alarm ids are tracked in ``capture_alarm_lesson_cursor`` (brain-owned
auxiliary table, created idempotently on first sync).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time

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


def cluster_key_for_alarm(
    repo: str,
    invariant_id: str,
    details_json: str,
) -> ClusterKey:
    """Derive a stable lesson cluster key from a watchdog alarm row."""
    source = ""
    try:
        payload = json.loads(details_json)
        if isinstance(payload, dict):
            raw_source = payload.get("source")
            if raw_source is not None:
                source = str(raw_source)
    except json.JSONDecodeError:
        logger.debug("capture_alarm_lessons: unparseable details_json for %s", invariant_id)

    tool = f"hippo-capture:{source}" if source else "hippo-watchdog"
    return ClusterKey(
        repo=repo,
        tool=tool,
        rule_id=invariant_id,
        path_prefix="docs/capture/",
    )


def _summary_for_key(key: ClusterKey) -> str:
    if key.tool.startswith("hippo-capture:"):
        source = key.tool.removeprefix("hippo-capture:")
        return f"Capture invariant {key.rule_id} violated for source {source!r}"
    return f"Capture invariant {key.rule_id} violated (see docs/capture/)"


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

    return processed
