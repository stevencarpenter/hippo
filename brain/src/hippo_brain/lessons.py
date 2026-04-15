"""Lesson clustering: promote repeat-failure patterns into queryable lessons.

A "cluster" is identified by (repo, tool, rule_id, path_prefix). On each
occurrence, increment a pending counter; on the Nth occurrence (where N =
min_occurrences), synthesize a lesson row and clear the pending counter.
Subsequent occurrences just bump the lesson's occurrences count.

All cluster-key fields use empty string '' for "not specified" rather than
NULL. The schema v5 lessons + lesson_pending tables enforce NOT NULL DEFAULT ''
on tool/rule_id/path_prefix to preserve UNIQUE constraint semantics under
SQLite's null-distinct rules.
"""

import sqlite3
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ClusterKey:
    repo: str
    tool: str  # '' when not detected
    rule_id: str  # '' when no rule code
    path_prefix: str  # '' when no path


def upsert_cluster(
    db_path: str,
    key: ClusterKey,
    min_occurrences: int,
    summary_fn: Callable[[ClusterKey], str],
    now_ms: int,
    fix_hint_fn: Callable[[ClusterKey], str | None] | None = None,
) -> bool:
    """Register a cluster occurrence.

    Returns True if a lesson row exists after this call (newly promoted or
    already promoted); False if still pending below the threshold.
    """
    # Normalize None → '' (callers may pass either; the table requires '').
    repo = key.repo
    tool = key.tool or ""
    rule_id = key.rule_id or ""
    path_prefix = key.path_prefix or ""

    conn = sqlite3.connect(db_path)
    try:
        # If lesson already exists, just bump and return.
        row = conn.execute(
            """SELECT id FROM lessons
               WHERE repo = ? AND tool = ? AND rule_id = ? AND path_prefix = ?""",
            (repo, tool, rule_id, path_prefix),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE lessons SET occurrences = occurrences + 1, last_seen_at = ? WHERE id = ?",
                (now_ms, row[0]),
            )
            conn.commit()
            return True

        # Otherwise track pending count.
        conn.execute(
            """INSERT INTO lesson_pending (repo, tool, rule_id, path_prefix, count, first_seen_at)
               VALUES (?, ?, ?, ?, 1, ?)
               ON CONFLICT(repo, tool, rule_id, path_prefix) DO UPDATE SET count = count + 1""",
            (repo, tool, rule_id, path_prefix, now_ms),
        )
        count_row = conn.execute(
            """SELECT count, first_seen_at FROM lesson_pending
               WHERE repo = ? AND tool = ? AND rule_id = ? AND path_prefix = ?""",
            (repo, tool, rule_id, path_prefix),
        ).fetchone()
        count = count_row[0]
        first_seen_at = count_row[1]

        if count < min_occurrences:
            conn.commit()
            return False

        # Promote to lessons; clear pending.
        normalized_key = ClusterKey(repo=repo, tool=tool, rule_id=rule_id, path_prefix=path_prefix)
        summary = summary_fn(normalized_key)
        fix_hint = fix_hint_fn(normalized_key) if fix_hint_fn else None
        conn.execute(
            """INSERT INTO lessons
               (repo, tool, rule_id, path_prefix, summary, fix_hint,
                occurrences, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                repo,
                tool,
                rule_id,
                path_prefix,
                summary,
                fix_hint,
                count,
                first_seen_at,
                now_ms,
            ),
        )
        conn.execute(
            """DELETE FROM lesson_pending
               WHERE repo = ? AND tool = ? AND rule_id = ? AND path_prefix = ?""",
            (repo, tool, rule_id, path_prefix),
        )
        conn.commit()
        return True
    finally:
        conn.close()
