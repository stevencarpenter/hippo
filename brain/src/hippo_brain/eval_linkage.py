"""Link open review findings to the later commit that changed their line (HIPO-66).

For each finding with no verdict, the job finds the first commit after the
review's ``head_sha`` whose version of the file no longer contains the flagged
line (matched on ``line_hash`` over raw lines), and records an automatic
``likely-addressed`` verdict for that commit. Git runs outside any transaction;
verdicts are written in short ``BEGIN IMMEDIATE`` batches. Design:
docs/superpowers/specs/2026-10-25-agent-eval-schema-design.md, sections 3.4 and 3.7.

This module is one of the few allowed to read the eval tables
(brain/tests/test_eval_isolation.py). It never feeds them to enrichment,
embedding, or any derived index (AP-14).

Run once: ``python -m hippo_brain.eval_linkage --once``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple, TypedDict

from hippo_brain.redaction import redact

logger = logging.getLogger(__name__)

INTERVAL_SECS = 3600
BATCH_ROWS = 100
PAYLOAD_RETENTION_MS = 7_776_000_000
VERDICT = "likely-addressed"
NOTES_FILE = "eval-linkage-notes.json"
# Marks a verdict whose commit is inferred, not observed removing the line.
INFERRED_NOTE = "inferred: line absent at walk start; first commit touching the path"
_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

# Side '-' lines are not in the reviewed tree, and a NULL hash cannot match (section 3.7).
_OPEN_FINDINGS = """
SELECT f.finding_id, f.repo, f.repo_identity, f.path, f.line_hash, r.head_sha, r.started_at
FROM eval_findings f
JOIN eval_review_runs r ON r.run_id = f.run_id
WHERE f.side = '+' AND f.line_hash IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM eval_verdicts v WHERE v.finding_id = f.finding_id)
ORDER BY f.repo, f.finding_id
"""

# Any verdict, human or automatic, closes the finding; the id check follows section 3.7.
_INSERT_VERDICT = """
INSERT INTO eval_verdicts (verdict_id, finding_id, verdict, commit_sha, note, auto,
                           decided_at, ingested_at, payload_expiry)
SELECT ?, ?, ?, ?, ?, 1, ?, ?, ?
WHERE NOT EXISTS (SELECT 1 FROM eval_verdicts WHERE finding_id = ? OR verdict_id = ?)
"""

# Steps 1 and 2 of the PR chain (section 3.7): a push run for the commit whose branch
# has a session in the same repository with recorded PR links.
_LOCAL_PR_KNOWN = """
SELECT 1
FROM workflow_runs w
JOIN agentic_sessions s
  ON s.git_branch = w.head_branch AND s.probe_tag IS NULL
 AND (s.cwd = ? OR substr(s.cwd, 1, length(?) + 1) = ? || '/')
JOIN agentic_session_facts f ON f.session_id = s.session_id AND f.harness = s.harness
WHERE w.repo = ? AND w.head_sha = ? AND f.pr_links_json <> '[]'
LIMIT 1
"""


class RunSummary(TypedDict):
    open_findings: int
    linked: int
    still_open: int


class Outcome(NamedTuple):
    commit: str | None
    reason: str | None
    note: str | None = None


def line_hash(line: bytes) -> str:
    """Hash one raw line: drop one trailing CR, strip ASCII spaces and tabs (section 3.7)."""
    return hashlib.sha256(line.removesuffix(b"\r").strip(b" \t")).hexdigest()


def verdict_id(finding_id: str, commit_sha: str) -> str:
    digest = hashlib.sha256(f"{finding_id}|{commit_sha}|{VERDICT}".encode()).hexdigest()
    return f"auto-{digest[:16]}"


@contextmanager
def connect(db_path: Path) -> Generator[sqlite3.Connection]:
    """Autocommit connection; every write opens its own BEGIN IMMEDIATE."""
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=rw", uri=True, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
    finally:
        conn.close()


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), *args],
        capture_output=True,
        check=check,
        timeout=60,
    )


def _blob(repo: Path, rev: str, path: str) -> bytes | None:
    """The file at ``rev``, or None when the path does not exist there."""
    found = _git(repo, "cat-file", "blob", f"{rev}:{path}", check=False)
    return found.stdout if found.returncode == 0 else None


def _contains(blob: bytes | None, wanted: str) -> bool:
    return blob is not None and any(line_hash(line) == wanted for line in blob.split(b"\n"))


class Walk(NamedTuple):
    start: str
    revs: list[str]
    blocked: str | None  # why inference is not allowed, or None


def _walk_start(repo: Path, head_sha: str, started_at_ms: int) -> Walk | str:
    """The commit the walk starts from and the rev-list range, or a skip reason.

    ``head_sha`` in HEAD's history: start there. ``head_sha`` on a branch HEAD
    does not contain: start at the merge-base, so mainline commits from the fork
    point onward are judged against the fork point's file; inference is allowed
    only once no other local or remote-tracking ref contains ``head_sha``
    (the branch was squash-merged and deleted). ``head_sha`` pruned: start at
    HEAD's last commit at or before the review's start second, excluded from the
    walk.
    """
    if _git(repo, "cat-file", "-e", f"{head_sha}^{{commit}}", check=False).returncode == 0:
        base = _git(repo, "merge-base", head_sha, "HEAD", check=False)
        if base.returncode == 1:
            return "HEAD shares no history with head_sha"
        if base.returncode != 0:
            return f"git merge-base failed (exit {base.returncode})"
        start = base.stdout.decode().strip()
        blocked = None
        if (
            start != head_sha
            and _git(
                repo,
                "for-each-ref",
                "--contains",
                head_sha,
                "--format=%(refname)",
                "refs/heads",
                "refs/remotes",
            ).stdout.strip()
        ):
            blocked = "reviewed branch still exists; not merged"
        return Walk(start, ["--ancestry-path", f"{start}..HEAD"], blocked)
    started = str(started_at_ms // 1000)  # git reads a 10-digit number as epoch seconds
    start = _git(repo, "rev-list", "-1", f"--before={started}", "HEAD").stdout.decode().strip()
    if not start:
        return "head_sha not in repository and no commit before the review"
    return Walk(start, ["--ancestry-path", f"{start}..HEAD"], None)


def find_change(
    repo: Path, head_sha: str, path: str, wanted: str, started_at_ms: int = 0
) -> Outcome:
    """First commit after the review whose file no longer holds the flagged line.

    The line counts as changed only once it is absent from HEAD, so a line that
    moved but stayed byte-identical stays open. Commits that descend from the
    start commit and touch ``path`` are walked oldest first; the line is tracked
    from the start commit or, for a line reviewed before it was committed, from
    the first later commit that contains it. A line that never appears in that
    history (fixed before its first commit, or fixed on a branch before a squash
    merge) is attributed to the first walked commit that touches the path, with
    ``INFERRED_NOTE``.
    """
    current = _blob(repo, "HEAD", path)
    if current is None:
        return Outcome(None, "file deleted at HEAD")
    if _contains(current, wanted):
        return Outcome(None, None)
    walk = _walk_start(repo, head_sha, started_at_ms)
    if isinstance(walk, str):
        return Outcome(None, walk)
    start, revs, blocked = walk
    commits = _git(repo, "rev-list", "--reverse", "--topo-order", *revs, "--", path).stdout.split()
    seen = _contains(_blob(repo, start, path), wanted)
    first_touch: str | None = None
    for commit in (c.decode() for c in commits):
        blob = _blob(repo, commit, path)
        if blob is None:
            continue  # deleted here; a later re-creation decides
        first_touch = first_touch or commit
        if _contains(blob, wanted):
            seen = True
        elif seen:
            return Outcome(commit, None)
    if seen:
        return Outcome(None, "no walked commit removed the line")
    # The line never reached the walked history (fixed before its first commit, or
    # on a branch before a squash merge). It is gone at HEAD, so the status holds;
    # the commit is a guess, and the note says so.
    if first_touch and blocked:
        return Outcome(None, blocked)
    if first_touch:
        return Outcome(first_touch, None, INFERRED_NOTE)
    return Outcome(None, "line absent at walk start and no later commit touches the path")


def gh_pr_urls(slug: str, sha: str) -> list[str]:
    """Outbound: ask GitHub which PRs contain ``sha``. Only runs when enabled."""
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{slug}/commits/{sha}/pulls", "--jq", ".[].html_url"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("eval linkage: gh PR lookup failed for %s: %s", sha, type(exc).__name__)
        return []
    return [url for url in result.stdout.split() if url.startswith("https://")]


def _pr_note(conn: sqlite3.Connection, repo: str, slug: str | None, sha: str) -> str | None:
    if not slug or not _SLUG.fullmatch(slug):
        return None
    if conn.execute(_LOCAL_PR_KNOWN, (repo, repo, repo, slug, sha)).fetchone():
        return None
    urls = gh_pr_urls(slug, sha)
    return redact("PR " + ", ".join(urls))[:4000] if urls else None


def write_verdicts(
    conn: sqlite3.Connection, links: list[tuple[str, str, str | None]], now_ms: int
) -> int:
    """Insert automatic verdicts in BEGIN IMMEDIATE batches of at most 100 rows."""
    inserted = 0
    for start in range(0, len(links), BATCH_ROWS):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for finding_id, commit, note in links[start : start + BATCH_ROWS]:
                vid = verdict_id(finding_id, commit)
                inserted += conn.execute(
                    _INSERT_VERDICT,
                    (
                        vid,
                        finding_id,
                        VERDICT,
                        commit,
                        note,
                        now_ms,
                        now_ms,
                        now_ms + PAYLOAD_RETENTION_MS,
                        finding_id,
                        vid,
                    ),
                ).rowcount
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return inserted


def _record_reasons(notes_path: Path, reasons: dict[str, str]) -> None:
    """Log each open-with-reason finding once; the file keeps the current set."""
    try:
        previous = json.loads(notes_path.read_text())
    except FileNotFoundError, ValueError:
        previous = {}
    for finding_id, reason in reasons.items():
        if previous.get(finding_id) != reason:
            logger.info("eval linkage: finding %s stays open: %s", finding_id, reason)
    if reasons != previous:
        tmp = notes_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(reasons, sort_keys=True))
        os.replace(tmp, notes_path)


def run_once(
    db_path: Path, notes_path: Path, *, gh_pr_lookup: bool = False, now_ms: int | None = None
) -> RunSummary:
    """One linkage pass over every open finding."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    with connect(db_path) as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'eval_findings'"
        ).fetchone():
            return RunSummary(open_findings=0, linked=0, still_open=0)
        findings = conn.execute(_OPEN_FINDINGS).fetchall()
        links: list[tuple[str, str, str | None]] = []
        reasons: dict[str, str] = {}
        for finding_id, repo, slug, path, wanted, head_sha, started_at in findings:
            root = Path(repo)
            if not (root / ".git").exists():
                reasons[finding_id] = "repository missing"
                continue
            try:
                outcome = find_change(root, head_sha, path, wanted, started_at)
            except (OSError, subprocess.SubprocessError) as exc:
                outcome = Outcome(None, f"git failed: {type(exc).__name__}")
            if outcome.reason:
                reasons[finding_id] = outcome.reason
            if outcome.commit:
                pr = None
                if gh_pr_lookup:
                    try:
                        pr = _pr_note(conn, repo, slug, outcome.commit)
                    except sqlite3.Error as exc:
                        logger.warning("eval linkage: PR chain read failed: %s", exc)
                note = "; ".join(part for part in (outcome.note, pr) if part)[:4000] or None
                links.append((finding_id, outcome.commit, note))
        linked = write_verdicts(conn, links, now_ms) if links else 0
    try:
        _record_reasons(notes_path, reasons)
    except OSError as exc:
        logger.warning("eval linkage: cannot update %s: %s", notes_path, exc)
    if linked:
        logger.info("eval linkage: recorded %d likely-addressed verdict(s)", linked)
    return RunSummary(
        open_findings=len(findings), linked=linked, still_open=len(findings) - len(links)
    )


def main(argv: list[str] | None = None) -> None:
    from hippo_brain import _load_runtime_settings

    parser = argparse.ArgumentParser(description="Link open review findings to later commits.")
    parser.add_argument("--once", action="store_true", required=True, help="run one pass and exit")
    parser.add_argument("--db", type=Path, help="database path (default: the configured one)")
    args = parser.parse_args(argv)
    settings = _load_runtime_settings()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    summary = run_once(
        args.db or Path(settings["db_path"]),
        Path(settings["data_dir"]) / NOTES_FILE,
        gh_pr_lookup=settings.get("eval_linkage", {}).get("gh_pr_lookup") is True,
    )
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
