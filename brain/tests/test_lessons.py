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


def test_first_occurrence_does_not_create_lesson(db_path):
    """Single failure does not graduate to a lesson (min_occurrences=2)."""
    key = ClusterKey(repo="me/r", tool="ruff", rule_id="F401", path_prefix="brain/")
    promoted = upsert_cluster(
        db_path,
        key,
        min_occurrences=2,
        summary_fn=lambda k: "unused imports",
        now_ms=1000,
    )
    assert promoted is False
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT count(*) FROM lessons").fetchone()[0] == 0
    pending = conn.execute("SELECT count FROM lesson_pending").fetchone()
    assert pending == (1,)  # one pending occurrence
    conn.close()


def test_second_occurrence_promotes_and_clears_pending(db_path):
    key = ClusterKey(repo="me/r", tool="ruff", rule_id="F401", path_prefix="brain/")
    upsert_cluster(
        db_path,
        key,
        min_occurrences=2,
        summary_fn=lambda k: "unused imports",
        now_ms=1000,
    )
    promoted = upsert_cluster(
        db_path,
        key,
        min_occurrences=2,
        summary_fn=lambda k: "unused imports",
        now_ms=2000,
    )
    assert promoted is True
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT occurrences, summary, last_seen_at FROM lessons").fetchone()
    assert row == (2, "unused imports", 2000)
    pending = conn.execute("SELECT count(*) FROM lesson_pending").fetchone()[0]
    assert pending == 0  # cleared after promotion
    conn.close()


def test_third_occurrence_increments_existing_lesson(db_path):
    """Once promoted, subsequent occurrences just bump count + last_seen_at."""
    key = ClusterKey(repo="me/r", tool="ruff", rule_id="F401", path_prefix="brain/")
    upsert_cluster(db_path, key, min_occurrences=2, summary_fn=lambda k: "unused", now_ms=1000)
    upsert_cluster(db_path, key, min_occurrences=2, summary_fn=lambda k: "unused", now_ms=2000)
    promoted = upsert_cluster(
        db_path, key, min_occurrences=2, summary_fn=lambda k: "unused", now_ms=3000
    )
    assert promoted is True
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT occurrences, last_seen_at FROM lessons").fetchone()
    assert row == (3, 3000)
    conn.close()


def test_min_occurrences_3_requires_three_calls(db_path):
    key = ClusterKey(repo="me/r", tool="ruff", rule_id="F401", path_prefix="brain/")
    for i in range(2):
        promoted = upsert_cluster(
            db_path,
            key,
            min_occurrences=3,
            summary_fn=lambda k: "unused",
            now_ms=1000 + i,
        )
        assert promoted is False
    promoted = upsert_cluster(
        db_path, key, min_occurrences=3, summary_fn=lambda k: "unused", now_ms=3000
    )
    assert promoted is True
