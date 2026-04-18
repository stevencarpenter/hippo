#!/usr/bin/env python3
"""Backfill events.git_repo for historical NULL rows (R-23 completion).

Reads all events where git_repo IS NULL and cwd IS NOT NULL, resolves the
owner/repo slug using the same logic as crates/hippo-daemon/src/git_repo.rs,
and batch-updates the rows in a single transaction.

Usage:
    uv run --project brain python brain/scripts/backfill-git-repo.py [options]

Options:
    --db PATH      Path to hippo.db (default: $XDG_DATA_HOME/hippo/hippo.db)
    --dry-run      Report counts without mutating the database
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("backfill-git-repo")


def _default_db() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "hippo" / "hippo.db"


# ---------------------------------------------------------------------------
# URL → owner/repo parsing (mirrors git_repo.rs parse_owner_repo exactly)
# ---------------------------------------------------------------------------

_REMOTE_SCHEMES = ("https://", "http://", "ssh://", "git://")


def parse_owner_repo(url: str) -> str | None:
    """Return 'owner/repo' slug for a git remote URL, or None if unresolvable.

    Mirrors crates/hippo-daemon/src/git_repo.rs::parse_owner_repo so that
    slug format is byte-identical between forward-path events and backfilled
    rows.
    """
    trimmed = url.strip()
    stripped = trimmed.removesuffix(".git")

    # scp-like: host:path (git@github.com:owner/repo)
    # Disambiguated from URL scheme by absence of '//' after the colon.
    colon_idx = stripped.find(":")
    if colon_idx != -1:
        pre = stripped[:colon_idx]
        post = stripped[colon_idx + 1 :]
        if not post.startswith("/") and "/" not in pre:
            slash_idx = post.find("/")
            if slash_idx != -1:
                owner = post[:slash_idx]
                repo = post[slash_idx + 1 :]
                return _join_slug(owner, repo)

    # URL-like: only accept known remote schemes.
    if not any(stripped.startswith(s) for s in _REMOTE_SCHEMES):
        return None

    # rsplit by '/', skip empty segments (trailing slash)
    segments = [s for s in stripped.rsplit("/") if s]
    if len(segments) < 2:
        return None
    repo = segments[-1]
    owner = segments[-2]
    # owner segment must be clean (no scheme artifact like 'github.com')
    if ":" in owner or "@" in owner:
        return None
    return _join_slug(owner, repo)


def _join_slug(owner: str, repo: str) -> str | None:
    if not owner or not repo:
        return None
    return f"{owner}/{repo}"


# ---------------------------------------------------------------------------
# Git resolution
# ---------------------------------------------------------------------------


def resolve_git_repo(cwd: str) -> str | None:
    """Resolve owner/repo for cwd using the same preference order as git_repo.rs.

    1. owner/repo from git config remote.origin.url
    2. basename of git rev-parse --show-toplevel (no-remote repo)
    3. None when cwd is not inside a git worktree
    """
    if not cwd:
        return None

    # Step 1: try remote origin
    url = _git(cwd, ["config", "--get", "remote.origin.url"])
    if url:
        slug = parse_owner_repo(url)
        if slug:
            return slug

    # Step 2: fall back to toplevel basename
    toplevel = _git(cwd, ["rev-parse", "--show-toplevel"])
    if toplevel:
        return Path(toplevel).name or None

    return None


def _git(cwd: str, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd] + args,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return None
        out = result.stdout.strip()
        return out if out else None
    except subprocess.TimeoutExpired, FileNotFoundError, OSError:
        return None


# ---------------------------------------------------------------------------
# Main backfill logic
# ---------------------------------------------------------------------------


def run(db_path: Path, dry_run: bool) -> int:
    import sqlite3

    if not db_path.exists():
        log.error("database not found: %s", db_path)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, cwd FROM events WHERE git_repo IS NULL AND cwd IS NOT NULL"
    ).fetchall()

    total_null = len(rows)
    log.info("found %d events with git_repo=NULL and cwd IS NOT NULL", total_null)

    if total_null == 0:
        log.info("nothing to do")
        conn.close()
        return 0

    # Group event IDs by cwd to avoid re-resolving the same directory
    cwd_to_ids: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        cwd_to_ids[row["cwd"]].append(row["id"])

    log.info("resolving %d unique working directories", len(cwd_to_ids))

    resolved: dict[str, str] = {}  # cwd → slug
    skipped_cwds: list[str] = []

    for cwd, ids in cwd_to_ids.items():
        slug = resolve_git_repo(cwd)
        if slug:
            resolved[cwd] = slug
            log.debug("resolved cwd=%s → %s (covers %d events)", cwd, slug, len(ids))
        else:
            skipped_cwds.append(cwd)
            log.debug("skipped cwd=%s (not a git repo or no resolvable remote)", cwd)

    would_update = sum(len(cwd_to_ids[c]) for c in resolved)
    would_skip = sum(len(cwd_to_ids[c]) for c in skipped_cwds)

    # Tally by slug for the report
    slug_counts: dict[str, int] = defaultdict(int)
    for cwd, slug in resolved.items():
        slug_counts[slug] += len(cwd_to_ids[cwd])

    log.info(
        "summary: total_null=%d would_update=%d would_skip=%d",
        total_null,
        would_update,
        would_skip,
    )
    for slug, count in sorted(slug_counts.items(), key=lambda x: -x[1]):
        log.info("  %-40s  %d events", slug, count)

    if skipped_cwds:
        log.info("skipped cwds (unresolvable):")
        for c in skipped_cwds[:20]:
            log.info("  %s", c)
        if len(skipped_cwds) > 20:
            log.info("  ... and %d more", len(skipped_cwds) - 20)

    if dry_run:
        log.info("--dry-run: no changes written")
        conn.close()
        return 0

    # Apply updates in a single transaction — use executemany with one row per
    # event (avoids dynamic IN-list SQL; ids come from our own SELECT so the
    # set is bounded, but executemany is cleaner and semgrep-safe).
    params = [(slug, event_id) for cwd, slug in resolved.items() for event_id in cwd_to_ids[cwd]]
    with conn:
        conn.executemany(
            "UPDATE events SET git_repo = ? WHERE id = ?",
            params,
        )

    log.info("updated %d events", would_update)
    conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=_default_db(), metavar="PATH")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report counts without mutating the database",
    )
    args = parser.parse_args()
    return run(args.db, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
