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
_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

# Side '-' lines are not in the reviewed tree, and a NULL hash cannot match (section 3.7).
_OPEN_FINDINGS = """
SELECT f.finding_id, f.repo, f.repo_identity, f.path, f.line_hash, r.head_sha
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
    found = _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}:{path}", check=False)
    if found.returncode != 0:
        return None
    return _git(repo, "cat-file", "blob", found.stdout.decode().strip()).stdout


def _contains(blob: bytes | None, wanted: str) -> bool:
    return blob is not None and any(line_hash(line) == wanted for line in blob.split(b"\n"))


def find_change(repo: Path, head_sha: str, path: str, wanted: str) -> Outcome:
    """First commit after ``head_sha`` whose file no longer holds the flagged line.

    The line counts as changed only once it is absent from HEAD, so a line that
    moved but stayed byte-identical stays open. Commits are walked oldest first;
    the line is tracked from ``head_sha`` or, for a line reviewed before it was
    committed, from the first later commit that contains it.
    """
    if _git(repo, "cat-file", "-e", f"{head_sha}^{{commit}}", check=False).returncode != 0:
        return Outcome(None, "head_sha not in repository")
    ancestor = _git(repo, "merge-base", "--is-ancestor", head_sha, "HEAD", check=False)
    if ancestor.returncode == 1:
        return Outcome(None, "HEAD does not contain head_sha")
    if ancestor.returncode != 0:
        raise subprocess.CalledProcessError(
            ancestor.returncode, ancestor.args, stderr=ancestor.stderr
        )
    current = _blob(repo, "HEAD", path)
    if current is None:
        return Outcome(None, "file deleted at HEAD")
    if _contains(current, wanted):
        return Outcome(None, None)
    commits = _git(
        repo, "rev-list", "--reverse", "--topo-order", f"{head_sha}..HEAD", "--", path
    ).stdout.split()
    seen = _contains(_blob(repo, head_sha, path), wanted)
    for commit in (c.decode() for c in commits):
        blob = _blob(repo, commit, path)
        if blob is None:
            continue  # deleted here; a later re-creation decides
        if _contains(blob, wanted):
            seen = True
        elif seen:
            return Outcome(commit, None)
    return Outcome(None, "line not found in any commit after head_sha")


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
        for finding_id, repo, slug, path, wanted, head_sha in findings:
            root = Path(repo)
            if not (root / ".git").exists():
                reasons[finding_id] = "repository missing"
                continue
            try:
                outcome = find_change(root, head_sha, path, wanted)
            except (OSError, subprocess.SubprocessError) as exc:
                outcome = Outcome(None, f"git failed: {type(exc).__name__}")
            if outcome.reason:
                reasons[finding_id] = outcome.reason
            if outcome.commit:
                note = _pr_note(conn, repo, slug, outcome.commit) if gh_pr_lookup else None
                links.append((finding_id, outcome.commit, note))
        _record_reasons(notes_path, reasons)
        linked = write_verdicts(conn, links, now_ms) if links else 0
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
