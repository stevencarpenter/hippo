"""Automatic `likely-addressed` verdicts from later commits (HIPO-66).

Design: docs/superpowers/specs/2026-10-25-agent-eval-schema-design.md, sections
3.4 and 3.7. Each test builds a real git repository with fixed author and dates.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hippo_brain import eval_linkage

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "crates" / "hippo-core" / "src" / "schema"
NOW_MS = 1_793_000_000_000
FLAGGED = "    if user.is_admin or True:"
ORIGINAL = ["def check(user):", FLAGGED, "        return True", "    return False", ""]


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def db(tmp_db):
    conn, db_path = tmp_db
    # The Rust SCHEMA appends these two files to schema.sql (v28, v29).
    for name in ("agentic_eval.sql", "agent_eval.sql"):
        conn.executescript((SCHEMA_DIR / name).read_text())
    conn.commit()
    return conn, db_path


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.ticks = 0
        path.mkdir()
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        self.ticks += 1
        date = f"2026-10-01T00:{self.ticks // 60:02d}:{self.ticks % 60:02d}+00:00"
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
        }
        result = subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *args],
            cwd=self.path,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def commit(self, files: dict[str, list[str] | None], message: str) -> str:
        for name, lines in files.items():
            target = self.path / name
            if lines is None:
                self.git("rm", "-q", name)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("\n".join(lines))
                self.git("add", name)
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path / "work")


def add_finding(
    conn,
    repo_path: Path,
    head_sha: str,
    finding_id: str = "f1",
    line: int = 2,
    started_at: int = 1,
):
    conn.execute(
        "INSERT OR IGNORE INTO eval_review_runs (run_id, agent, mode, repo, base, base_sha, "
        "head_sha, input_sha256, started_at, finished_at, ingested_at) "
        "VALUES (?, 'blind-review', 'mechanical', ?, 'main', ?, ?, 'x', ?, 2, 3)",
        (f"run-{finding_id}", str(repo_path), head_sha, head_sha, started_at),
    )
    conn.execute(
        "INSERT INTO eval_findings (finding_id, run_id, repo, repo_identity, path, side, line, "
        "line_text, line_hash, severity, confidence, logged_at, ingested_at, payload_expiry) "
        "VALUES (?, ?, ?, 'owner/work', 'src/app.py', '+', ?, ?, ?, 'high', 'medium', 4, 5, 6)",
        (
            finding_id,
            f"run-{finding_id}",
            str(repo_path),
            line,
            FLAGGED,
            eval_linkage.line_hash(FLAGGED.encode()),
        ),
    )
    conn.commit()


def verdicts(conn) -> list[tuple]:
    return conn.execute(
        "SELECT verdict_id, finding_id, verdict, commit_sha, note, auto, decided_at, "
        "ingested_at, payload_expiry FROM eval_verdicts ORDER BY verdict_id"
    ).fetchall()


def run(db_path: Path, tmp_path: Path, **kwargs) -> eval_linkage.RunSummary:
    return eval_linkage.run_once(
        db_path, tmp_path / "eval-linkage-notes.json", now_ms=NOW_MS, **kwargs
    )


def test_line_hash_matches_the_shared_test_vector():
    assert eval_linkage.line_hash(b"    if user.is_admin or True:  \r") == (
        "d5474129f47594d84b580d2b1d5d10e2ddd523ce5b0668b5de867807203e676c"
    )


def test_line_fixed_in_a_later_commit_writes_an_auto_verdict(db, repo, tmp_path):
    conn, db_path = db
    head = repo.commit({"src/app.py": ORIGINAL}, "add check")
    repo.commit({"README": ["unrelated"]}, "docs")
    fixed = repo.commit(
        {"src/app.py": [ORIGINAL[0], "    if user.is_admin:", *ORIGINAL[2:]]}, "fix check"
    )
    repo.commit({"src/app.py": [ORIGINAL[0], "    if user.is_admin():", *ORIGINAL[2:]]}, "again")
    add_finding(conn, repo.path, head)

    summary = run(db_path, tmp_path)

    expected_id = "auto-" + hashlib.sha256(f"f1|{fixed}|likely-addressed".encode()).hexdigest()[:16]
    assert verdicts(conn) == [
        (
            expected_id,
            "f1",
            "likely-addressed",
            fixed,
            None,
            1,
            NOW_MS,
            NOW_MS,
            NOW_MS + 7_776_000_000,
        )
    ]
    assert summary == {"open_findings": 1, "linked": 1, "still_open": 0}


def test_line_added_after_head_is_tracked_from_its_first_commit(db, repo, tmp_path):
    """A line reviewed as an uncommitted change is committed later, then changed."""
    conn, db_path = db
    head = repo.commit({"src/app.py": [ORIGINAL[0], *ORIGINAL[2:]]}, "base")
    repo.commit({"src/app.py": ORIGINAL}, "commit the reviewed change")
    fixed = repo.commit(
        {"src/app.py": [ORIGINAL[0], "    if user.is_admin:", *ORIGINAL[2:]]}, "fix"
    )
    add_finding(conn, repo.path, head)

    run(db_path, tmp_path)

    assert [row[3] for row in verdicts(conn)] == [fixed]


def test_line_moved_but_unchanged_stays_open(db, repo, tmp_path):
    conn, db_path = db
    head = repo.commit({"src/app.py": ORIGINAL}, "add check")
    repo.commit(
        {"src/app.py": ["import os", "", "", ORIGINAL[0], "    x = 1", *ORIGINAL[1:]]}, "move"
    )
    add_finding(conn, repo.path, head)

    summary = run(db_path, tmp_path)

    assert verdicts(conn) == []
    assert summary == {"open_findings": 1, "linked": 0, "still_open": 1}


def test_deleted_file_stays_open_and_records_the_reason_once(db, repo, tmp_path, caplog):
    conn, db_path = db
    head = repo.commit({"src/app.py": ORIGINAL, "README": ["x"]}, "add check")
    repo.commit({"src/app.py": None}, "delete")
    add_finding(conn, repo.path, head)
    notes = tmp_path / "eval-linkage-notes.json"

    with caplog.at_level(logging.INFO, logger="hippo_brain.eval_linkage"):
        run(db_path, tmp_path)
        first = [r.getMessage() for r in caplog.records if "f1" in r.getMessage()]
        caplog.clear()
        run(db_path, tmp_path)
        second = [r.getMessage() for r in caplog.records if "f1" in r.getMessage()]

    assert verdicts(conn) == []
    assert len(first) == 1 and "file deleted" in first[0]
    assert second == []
    assert json.loads(notes.read_text()) == {"f1": "file deleted at HEAD"}


def test_manual_verdict_blocks_an_auto_verdict(db, repo, tmp_path):
    conn, db_path = db
    head = repo.commit({"src/app.py": ORIGINAL}, "add check")
    repo.commit({"src/app.py": [ORIGINAL[0], "    if user.is_admin:", *ORIGINAL[2:]]}, "fix")
    add_finding(conn, repo.path, head)
    conn.execute(
        "INSERT INTO eval_verdicts (verdict_id, finding_id, verdict, auto, decided_at, "
        "ingested_at, payload_expiry) VALUES ('human-1', 'f1', 'false-positive', 0, 1, 1, 1)"
    )
    conn.commit()

    summary = run(db_path, tmp_path)

    assert [row[0] for row in verdicts(conn)] == ["human-1"]
    assert summary["open_findings"] == 0


def test_write_skips_a_finding_that_gained_a_human_verdict_after_the_read(db, repo):
    """The insert itself re-checks, so a verdict ingested mid-run still wins."""
    conn, db_path = db
    head = repo.commit({"src/app.py": ORIGINAL}, "add check")
    add_finding(conn, repo.path, head)
    conn.execute(
        "INSERT INTO eval_verdicts (verdict_id, finding_id, verdict, auto, decided_at, "
        "ingested_at, payload_expiry) VALUES ('human-1', 'f1', 'accepted', 0, 1, 1, 1)"
    )
    conn.commit()

    with eval_linkage.connect(db_path) as write_conn:
        inserted = eval_linkage.write_verdicts(write_conn, [("f1", head, None)], NOW_MS)

    assert inserted == 0
    assert [row[0] for row in verdicts(conn)] == ["human-1"]


def test_second_run_inserts_nothing_new(db, repo, tmp_path):
    conn, db_path = db
    head = repo.commit({"src/app.py": ORIGINAL}, "add check")
    repo.commit({"src/app.py": [ORIGINAL[0], "    if user.is_admin:", *ORIGINAL[2:]]}, "fix")
    add_finding(conn, repo.path, head)

    run(db_path, tmp_path)
    first = verdicts(conn)
    summary = run(db_path, tmp_path)

    assert len(first) == 1
    assert verdicts(conn) == first
    assert summary == {"open_findings": 0, "linked": 0, "still_open": 0}


def test_missing_repository_never_writes(db, repo, tmp_path, monkeypatch):
    conn, db_path = db
    head = repo.commit({"src/app.py": ORIGINAL}, "add check")
    add_finding(conn, tmp_path / "gone", head)
    writes: list[object] = []
    monkeypatch.setattr(eval_linkage, "write_verdicts", lambda *a: writes.append(a) or 0)

    summary = run(db_path, tmp_path)

    assert writes == []
    assert verdicts(conn) == []
    assert summary == {"open_findings": 1, "linked": 0, "still_open": 1}
    notes = json.loads((tmp_path / "eval-linkage-notes.json").read_text())
    assert notes == {"f1": "repository missing"}


def test_database_without_eval_tables_is_a_noop(tmp_db, tmp_path):
    _, db_path = tmp_db
    assert run(db_path, tmp_path) == {"open_findings": 0, "linked": 0, "still_open": 0}


def test_gh_lookup_is_off_by_default(db, repo, tmp_path, monkeypatch):
    conn, db_path = db
    head = repo.commit({"src/app.py": ORIGINAL}, "add check")
    repo.commit({"src/app.py": [ORIGINAL[0], "    if user.is_admin:", *ORIGINAL[2:]]}, "fix")
    add_finding(conn, repo.path, head)

    def forbidden(*args):
        raise AssertionError("gh lookup ran while disabled")

    monkeypatch.setattr(eval_linkage, "gh_pr_urls", forbidden)

    run(db_path, tmp_path)

    assert [row[4] for row in verdicts(conn)] == [None]


def test_gh_lookup_when_enabled_stores_the_pr_in_the_note(db, repo, tmp_path, monkeypatch):
    conn, db_path = db
    head = repo.commit({"src/app.py": ORIGINAL}, "add check")
    fixed = repo.commit(
        {"src/app.py": [ORIGINAL[0], "    if user.is_admin:", *ORIGINAL[2:]]}, "fix"
    )
    add_finding(conn, repo.path, head)
    calls: list[tuple[str, str]] = []

    def fake(slug: str, sha: str) -> list[str]:
        calls.append((slug, sha))
        return ["https://github.com/owner/work/pull/7"]

    monkeypatch.setattr(eval_linkage, "gh_pr_urls", fake)

    run(db_path, tmp_path, gh_pr_lookup=True)

    assert calls == [("owner/work", fixed)]
    assert [row[4] for row in verdicts(conn)] == ["PR https://github.com/owner/work/pull/7"]


def test_gh_lookup_skips_a_commit_the_local_pr_chain_already_attributes(
    db, repo, tmp_path, monkeypatch
):
    conn, db_path = db
    head = repo.commit({"src/app.py": ORIGINAL}, "add check")
    fixed = repo.commit(
        {"src/app.py": [ORIGINAL[0], "    if user.is_admin:", *ORIGINAL[2:]]}, "fix"
    )
    add_finding(conn, repo.path, head)
    conn.execute(
        "INSERT INTO workflow_runs (repo, head_sha, head_branch, event, status, html_url, "
        "raw_json, first_seen_at, last_seen_at) "
        "VALUES ('owner/work', ?, 'feat/x', 'push', 'completed', 'u', '{}', 1, 1)",
        (fixed,),
    )
    conn.execute(
        "INSERT INTO agentic_sessions (session_id, harness, project_dir, cwd, git_branch, "
        "summary_text, start_time, end_time) VALUES ('s1', 'claude-code', ?, ?, 'feat/x', '', 1, 2)",
        (str(repo.path), str(repo.path / "src")),
    )
    conn.execute(
        "INSERT INTO agentic_session_facts (session_id, harness, pr_links_json, updated_at) "
        "VALUES ('s1', 'claude-code', '[\"https://github.com/owner/work/pull/7\"]', 1)"
    )
    conn.commit()

    def forbidden(*args):
        raise AssertionError("gh lookup ran although the local chain found a PR")

    monkeypatch.setattr(eval_linkage, "gh_pr_urls", forbidden)

    run(db_path, tmp_path, gh_pr_lookup=True)

    assert [row[4] for row in verdicts(conn)] == [None]


async def test_brain_server_runs_the_job_from_its_own_loop(tmp_db, tmp_path, monkeypatch):
    import asyncio

    from hippo_brain.server import BrainServer

    _, db_path = tmp_db
    server = BrainServer(db_path=str(db_path), data_dir=str(tmp_path))
    server.poll_interval_secs = 9999
    ran = asyncio.Event()
    calls: list[tuple] = []

    def fake_run_once(db, notes, *, gh_pr_lookup):
        calls.append((db, notes, gh_pr_lookup))
        ran.set()

    monkeypatch.setattr(eval_linkage, "INTERVAL_SECS", 0)
    monkeypatch.setattr(eval_linkage, "run_once", fake_run_once)

    server.start_enrichment()
    try:
        await asyncio.wait_for(ran.wait(), timeout=5)
    finally:
        await server.stop_enrichment()

    assert calls[0] == (Path(db_path), tmp_path / "eval-linkage-notes.json", False)
    assert server._eval_linkage_task is None


def test_cli_once_prints_the_summary(db, repo, tmp_path, monkeypatch, capsys):
    import hippo_brain

    conn, db_path = db
    head = repo.commit({"src/app.py": ORIGINAL}, "add check")
    repo.commit({"src/app.py": [ORIGINAL[0], "    if user.is_admin:", *ORIGINAL[2:]]}, "fix")
    add_finding(conn, repo.path, head)
    monkeypatch.setattr(
        hippo_brain,
        "_load_runtime_settings",
        lambda: {"db_path": str(db_path), "data_dir": str(tmp_path), "eval_linkage": {}},
    )

    eval_linkage.main(["--once"])

    assert json.loads(capsys.readouterr().out) == {
        "open_findings": 1,
        "linked": 1,
        "still_open": 0,
    }


FIXED = [ORIGINAL[0], "    if user.is_admin:", *ORIGINAL[2:]]
BASE = [ORIGINAL[0], *ORIGINAL[2:]]


def test_squash_merged_branch_links_the_later_mainline_fix(db, repo, tmp_path):
    conn, db_path = db
    repo.commit({"src/app.py": BASE}, "base")
    repo.git("checkout", "-q", "-b", "feat")
    head = repo.commit({"src/app.py": ORIGINAL}, "add check")
    repo.git("checkout", "-q", "main")
    repo.commit({"src/app.py": [*BASE, "y = 1"]}, "mainline touches the file")
    repo.git("merge", "-q", "--squash", "feat")
    (repo.path / "src/app.py").write_text("\n".join([*ORIGINAL, "y = 1"]))
    repo.git("commit", "-q", "-am", "squash feat")
    fixed = repo.commit({"src/app.py": [*FIXED, "y = 1"]}, "fix on main")
    repo.git("branch", "-q", "-D", "feat")
    add_finding(conn, repo.path, head)

    run(db_path, tmp_path)

    assert [row[3] for row in verdicts(conn)] == [fixed]


def test_pruned_head_sha_walks_from_the_review_start_time(db, repo, tmp_path):
    conn, db_path = db
    repo.commit({"src/app.py": BASE}, "base")
    repo.git("checkout", "-q", "-b", "feat")
    head = repo.commit({"src/app.py": ORIGINAL}, "add check")
    started_at = int(repo.git("log", "-1", "--format=%ct", head)) * 1000
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--squash", "feat")
    repo.git("commit", "-q", "-m", "squash feat")
    fixed = repo.commit({"src/app.py": FIXED}, "fix on main")
    repo.git("branch", "-q", "-D", "feat")
    repo.git("reflog", "expire", "--expire=now", "--all")
    repo.git("gc", "-q", "--prune=now")
    missing = subprocess.run(["git", "-C", str(repo.path), "cat-file", "-e", head])
    assert missing.returncode != 0
    add_finding(conn, repo.path, head, started_at=started_at)

    run(db_path, tmp_path)

    assert [row[3] for row in verdicts(conn)] == [fixed]


def test_mainline_commit_merged_into_the_branch_is_not_the_fix(db, repo, tmp_path):
    conn, db_path = db
    repo.commit({"src/app.py": BASE}, "base")
    repo.git("checkout", "-q", "-b", "feat")
    head = repo.commit({"src/app.py": ORIGINAL}, "add check")
    repo.git("checkout", "-q", "main")
    repo.commit({"src/app.py": [*BASE, "y = 1"]}, "mainline touches the file")
    repo.git("checkout", "-q", "feat")
    repo.git("merge", "-q", "--no-edit", "main")
    fixed = repo.commit({"src/app.py": [*FIXED, "y = 1"]}, "fix on feat")
    add_finding(conn, repo.path, head)

    run(db_path, tmp_path)

    assert [row[3] for row in verdicts(conn)] == [fixed]


INFERRED = "inferred: line absent at walk start; first commit touching the path"


def test_line_fixed_before_its_first_commit_on_a_merged_branch_is_inferred(db, repo, tmp_path):
    """Review the uncommitted tree, fix, commit, merge: the fixing commit, marked inferred."""
    conn, db_path = db
    repo.commit({"src/app.py": BASE}, "base")
    repo.git("checkout", "-q", "-b", "feat")
    head = repo.git("rev-parse", "HEAD")
    fixed = repo.commit({"src/app.py": FIXED}, "commit the already fixed version")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "--no-edit", "feat")
    add_finding(conn, repo.path, head)

    run(db_path, tmp_path)

    assert [(row[3], row[4], row[5]) for row in verdicts(conn)] == [(fixed, INFERRED, 1)]


def test_branch_review_then_squash_infers_the_squash_commit(db, repo, tmp_path):
    conn, db_path = db
    repo.commit({"src/app.py": BASE}, "base")
    repo.git("checkout", "-q", "-b", "feat")
    head = repo.commit({"src/app.py": ORIGINAL}, "add check")
    repo.commit({"src/app.py": FIXED}, "fix on feat")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--squash", "feat")
    repo.git("commit", "-q", "-m", "squash feat")
    squash = repo.git("rev-parse", "HEAD")
    repo.git("branch", "-q", "-D", "feat")
    add_finding(conn, repo.path, head)

    run(db_path, tmp_path)

    assert [(row[3], row[4], row[5]) for row in verdicts(conn)] == [(squash, INFERRED, 1)]


def test_observed_fix_keeps_an_empty_note(db, repo, tmp_path):
    conn, db_path = db
    head = repo.commit({"src/app.py": ORIGINAL}, "add check")
    repo.commit({"src/app.py": FIXED}, "fix")
    add_finding(conn, repo.path, head)

    run(db_path, tmp_path)

    assert [row[4] for row in verdicts(conn)] == [None]


def test_discarded_review_with_untouched_file_stays_open(db, repo, tmp_path):
    conn, db_path = db
    head = repo.commit({"src/app.py": BASE, "README": ["x"]}, "base")
    repo.commit({"README": ["y"]}, "unrelated")
    add_finding(conn, repo.path, head)

    summary = run(db_path, tmp_path)

    assert verdicts(conn) == []
    assert summary == {"open_findings": 1, "linked": 0, "still_open": 1}
    notes = json.loads((tmp_path / "eval-linkage-notes.json").read_text())
    assert notes == {"f1": "line absent at walk start and no later commit touches the path"}


def test_inferred_note_keeps_the_gh_pr(db, repo, tmp_path, monkeypatch):
    conn, db_path = db
    head = repo.commit({"src/app.py": BASE}, "base")
    repo.commit({"src/app.py": FIXED}, "commit the already fixed version")
    add_finding(conn, repo.path, head)
    monkeypatch.setattr(
        eval_linkage, "gh_pr_urls", lambda slug, sha: ["https://github.com/owner/work/pull/7"]
    )

    run(db_path, tmp_path, gh_pr_lookup=True)

    assert [row[4] for row in verdicts(conn)] == [
        f"{INFERRED}; PR https://github.com/owner/work/pull/7"
    ]


def test_unwritable_notes_file_does_not_block_verdicts(db, repo, tmp_path):
    conn, db_path = db
    head = repo.commit({"src/app.py": ORIGINAL, "README": ["x"]}, "add check")
    repo.commit({"src/app.py": FIXED}, "fix")
    add_finding(conn, repo.path, head)
    add_finding(conn, tmp_path / "gone", head, finding_id="f2")
    notes_dir = tmp_path / "notes-is-a-directory"
    notes_dir.mkdir()

    summary = eval_linkage.run_once(db_path, notes_dir, now_ms=NOW_MS)

    assert summary == {"open_findings": 2, "linked": 1, "still_open": 1}
    assert [row[1] for row in verdicts(conn)] == ["f1"]


async def test_brain_loop_survives_a_failing_pass(tmp_db, tmp_path, monkeypatch):
    import asyncio

    from hippo_brain.server import BrainServer

    _, db_path = tmp_db
    server = BrainServer(db_path=str(db_path), data_dir=str(tmp_path))
    server.poll_interval_secs = 9999
    second = asyncio.Event()
    calls: list[int] = []

    def flaky_run_once(db, notes, *, gh_pr_lookup):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        second.set()

    monkeypatch.setattr(eval_linkage, "INTERVAL_SECS", 0)
    monkeypatch.setattr(eval_linkage, "run_once", flaky_run_once)

    server.start_enrichment()
    try:
        await asyncio.wait_for(second.wait(), timeout=5)
    finally:
        await server.stop_enrichment()

    assert len(calls) >= 2


def test_unmerged_branch_stays_open_until_the_branch_is_deleted(db, repo, tmp_path):
    """A checkout without the reviewed branch must not infer a mainline commit."""
    conn, db_path = db
    repo.commit({"src/app.py": BASE}, "base")
    repo.git("checkout", "-q", "-b", "feat")
    head = repo.commit({"src/app.py": ORIGINAL}, "add check")
    repo.git("checkout", "-q", "main")
    touch = repo.commit({"src/app.py": [*BASE, "y = 1"]}, "mainline touches the file")
    add_finding(conn, repo.path, head)

    run(db_path, tmp_path)

    assert verdicts(conn) == []
    notes = json.loads((tmp_path / "eval-linkage-notes.json").read_text())
    assert notes == {"f1": "reviewed branch still exists; not merged"}

    repo.git("branch", "-q", "-D", "feat")
    run(db_path, tmp_path)

    assert [(row[3], row[4]) for row in verdicts(conn)] == [(touch, INFERRED)]


def test_pruned_head_sha_excludes_a_commit_in_the_review_start_second(db, repo, tmp_path):
    conn, db_path = db
    repo.commit({"src/app.py": ORIGINAL}, "early")
    same_second = repo.commit({"src/app.py": BASE}, "committed in the review's start second")
    started_at = int(repo.git("log", "-1", "--format=%ct", same_second)) * 1000 + 500
    add_finding(conn, repo.path, "ab" * 20, started_at=started_at)

    run(db_path, tmp_path)

    assert verdicts(conn) == []
    notes = json.loads((tmp_path / "eval-linkage-notes.json").read_text())
    assert notes == {"f1": "line absent at walk start and no later commit touches the path"}


def test_unrelated_history_stays_open_with_a_reason(db, repo, tmp_path):
    conn, db_path = db
    repo.commit({"src/app.py": BASE}, "base")
    repo.git("checkout", "-q", "--orphan", "other")
    head = repo.commit({"src/app.py": ORIGINAL}, "unrelated root")
    repo.git("checkout", "-q", "main")
    add_finding(conn, repo.path, head)

    run(db_path, tmp_path)

    assert verdicts(conn) == []
    notes = json.loads((tmp_path / "eval-linkage-notes.json").read_text())
    assert notes == {"f1": "HEAD shares no history with head_sha"}
