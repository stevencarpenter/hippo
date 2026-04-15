import sqlite3
from pathlib import Path

import pytest

from hippo_brain.mcp_queries import get_ci_status_impl
from hippo_brain.models import CIStatus


@pytest.fixture
def db_with_run(tmp_path: Path) -> Path:
    db = tmp_path / "hippo.db"
    fixture = Path(__file__).parent.parent / "src/hippo_brain/_fixtures/schema_v5_min.sql"
    conn = sqlite3.connect(db)
    conn.executescript(fixture.read_text())
    conn.execute("""
        INSERT INTO workflow_runs
          (id, repo, head_sha, event, status, conclusion, html_url,
           raw_json, first_seen_at, last_seen_at)
        VALUES (1, 'me/r', 'abc', 'push', 'completed', 'failure',
                'https://x', '{}', 1000, 2000)
    """)
    conn.execute("""
        INSERT INTO workflow_jobs
          (id, run_id, name, status, conclusion, raw_json)
        VALUES (10, 1, 'lint', 'completed', 'failure', '{}')
    """)
    conn.execute("""
        INSERT INTO workflow_annotations
          (job_id, level, tool, rule_id, path, start_line, message)
        VALUES (10, 'failure', 'ruff', 'F401', 'brain/x.py', 3,
                'F401 unused import')
    """)
    conn.commit()
    conn.close()
    return db


def test_get_ci_status_by_sha(db_with_run: Path):
    status = get_ci_status_impl(str(db_with_run), repo="me/r", sha="abc")
    assert isinstance(status, CIStatus)
    assert status.conclusion == "failure"
    assert len(status.jobs) == 1
    assert status.jobs[0].annotations[0].rule_id == "F401"


def test_get_ci_status_missing_returns_none(db_with_run: Path):
    status = get_ci_status_impl(str(db_with_run), repo="me/r", sha="zzz")
    assert status is None


def test_get_ci_status_by_branch(db_with_run: Path):
    # Add head_branch to the fixture run
    conn = sqlite3.connect(db_with_run)
    conn.execute("UPDATE workflow_runs SET head_branch = 'main' WHERE id = 1")
    conn.commit()
    conn.close()
    status = get_ci_status_impl(str(db_with_run), repo="me/r", branch="main")
    assert status is not None
    assert status.head_sha == "abc"


def test_get_ci_status_requires_sha_or_branch(db_with_run: Path):
    with pytest.raises(ValueError):
        get_ci_status_impl(str(db_with_run), repo="me/r")
