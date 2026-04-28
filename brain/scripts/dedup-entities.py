#!/usr/bin/env python3
"""One-shot entity deduplication migration.

Re-canonicalizes all entities using the current entity_resolver rules and
either merges duplicates or rebrands lone rows whose stored values are stale.

For each (type, new_canonical) group:

  * len > 1 — merge. Keep the oldest row (smallest created_at), re-point all
    knowledge_node_entities to the survivor, delete the duplicate rows, and
    update the survivor's name + canonical to the recomputed values.
  * len == 1 (lone row) — if the recomputed name or canonical differs from
    what's stored (e.g. an old polluted row with no clean dupe ever observed),
    UPDATE that single row in place. Without this pass, lone polluted rows
    persist indefinitely because nothing ever conflicts with them and #105's
    on-conflict repair never triggers.

This script handles two flavors of duplication / staleness transparently
because it delegates to canonicalize():

  1. Multiple project roots resolving to the same logical file (e.g.
     hippo vs. hippo-postgres) — the original v5→v6 use case.
  2. Ephemeral parallel-agent worktrees under .claude/worktrees/<X>/...
     (issue #98). Worktree subdirectory names vary (`agent-*`, `feat-*`,
     adjective-noun-hex), and the directories are removed once the agent's
     work is merged or discarded — leaving polluted entity rows. Re-running
     this script after canonicalize() learns the worktree-strip rule
     collapses N copies of every commonly-edited file to one canonical row,
     and rebrands any remaining lone polluted rows.

Usage:
    uv run --project brain python brain/scripts/dedup-entities.py [--data-dir PATH] [--dry-run]

Environment:
    HIPPO_PROJECT_ROOTS  Colon-separated list of absolute project root paths to
                         strip from path-type entity values (e.g.
                         /home/user/projects/hippo:/home/user/projects/hippo-postgres).
                         Required for project-root dedup to work correctly.

Exits 0 on success. Non-zero on error.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hippo_brain.entity_resolver import (  # noqa: E402
    canonicalize,
    is_path_type,
    strip_worktree_prefix,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("dedup-entities")


def _default_db_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "hippo" / "hippo.db"


def _recompute_name(etype: str, name: str) -> str:
    """Display-name form: strip worktree segments for path types, leave others alone.

    Mirrors `upsert_entities` in enrichment.py — the goal is for stored `name`
    values to match what the live write path produces today.
    """
    return strip_worktree_prefix(name) if is_path_type(etype) else name


def run(conn: sqlite3.Connection, dry_run: bool) -> dict[str, int]:
    """Run dedup against an open connection. Returns stats dict."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")

    rows = conn.execute(
        "SELECT id, type, name, canonical, created_at FROM entities ORDER BY created_at ASC"
    ).fetchall()

    # Group by (type, new_canonical); ORDER BY created_at ASC means first member = oldest.
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        new_canonical = canonicalize(row["type"], row["name"])
        key = (row["type"], new_canonical)
        groups.setdefault(key, []).append(row)

    merges: list[tuple[tuple[str, str], list[sqlite3.Row]]] = []
    rebrands: list[tuple[sqlite3.Row, str, str]] = []  # (row, new_name, new_canonical)
    for (etype, new_canonical), members in groups.items():
        if len(members) > 1:
            merges.append(((etype, new_canonical), members))
            continue
        # Lone group: rebrand if either name or canonical changed under the
        # current rules (e.g. an old worktree-polluted path with no clean dupe).
        row = members[0]
        new_name = _recompute_name(etype, row["name"])
        if new_canonical != row["canonical"] or new_name != row["name"]:
            rebrands.append((row, new_name, new_canonical))

    log.info("Total entities: %d", len(rows))
    log.info("Duplicate groups: %d", len(merges))
    log.info("Lone rebrand candidates: %d", len(rebrands))

    if dry_run:
        for (etype, new_canonical), members in merges:
            keep_id = members[0]["id"]
            dupe_ids = [m["id"] for m in members[1:]]
            print(
                f"MERGE type={etype!r} canonical={new_canonical!r}: "
                f"keep id={keep_id}, delete ids={dupe_ids}"
            )
        for row, new_name, new_canonical in rebrands:
            print(
                f"REBRAND id={row['id']} type={row['type']!r}: "
                f"name={row['name']!r} → {new_name!r}, "
                f"canonical={row['canonical']!r} → {new_canonical!r}"
            )
        log.info("Dry run — no changes made")
        return {
            "total": len(rows),
            "groups": len(merges),
            "deleted": 0,
            "rebranded": 0,
        }

    total_deleted = 0
    total_rebranded = 0
    # Final-value updates for every entity touched (merge survivors + lone
    # rebrands). We collect them first and apply in two phases below to avoid
    # transient UNIQUE collisions: a row's new_canonical may equal *another*
    # row's CURRENT canonical (where that other row's own new_canonical is
    # different and thus didn't get grouped with us). A naïve in-place UPDATE
    # then trips `UNIQUE (type, canonical)` even though the final state is
    # collision-free. Two-pass with unique placeholders sidesteps that.
    final_updates: list[tuple[int, str, str]] = []  # (id, new_name, new_canonical)
    conn.execute("BEGIN")
    try:
        for (etype, new_canonical), members in merges:
            keep = members[0]
            dupes = members[1:]

            for dupe in dupes:
                dupe_id = dupe["id"]
                # Remove links that would conflict with the survivor's existing links.
                conn.execute(
                    "DELETE FROM knowledge_node_entities "
                    "WHERE entity_id = ? AND knowledge_node_id IN ("
                    "  SELECT knowledge_node_id FROM knowledge_node_entities WHERE entity_id = ?"
                    ")",
                    [dupe_id, keep["id"]],
                )
                # Re-point remaining links to the survivor.
                conn.execute(
                    "UPDATE knowledge_node_entities SET entity_id = ? WHERE entity_id = ?",
                    [keep["id"], dupe_id],
                )
                # Delete the duplicate entity row.
                conn.execute("DELETE FROM entities WHERE id = ?", [dupe_id])
                total_deleted += 1

            # Survivor's new display name mirrors the enrichment write path
            # (PR #105 strips worktree from `name` for path types).
            new_name = _recompute_name(etype, keep["name"])
            final_updates.append((keep["id"], new_name, new_canonical))
            log.info(
                "Merged type=%r canonical=%r: kept id=%d, deleted %d duplicate(s)",
                etype,
                new_canonical,
                keep["id"],
                len(dupes),
            )

        for row, new_name, new_canonical in rebrands:
            final_updates.append((row["id"], new_name, new_canonical))
            total_rebranded += 1

        # Phase 1: park every touched row at a placeholder canonical so the
        # final UPDATEs can never collide on UNIQUE (type, canonical). The
        # placeholder embeds a per-run UUID4 (32 hex chars) plus the entity id;
        # collision with any real-world entity canonical or another in-flight
        # invocation's placeholder is astronomically unlikely. Earlier versions
        # used a deterministic `__dedup_pending_<id>__` token, which a
        # malicious or coincidental entity row could collide with directly.
        run_token = uuid.uuid4().hex
        for entity_id, _new_name, _new_canonical in final_updates:
            conn.execute(
                "UPDATE entities SET canonical = ? WHERE id = ?",
                [f"__dedup_pending_{run_token}_{entity_id}__", entity_id],
            )

        # Phase 2: apply the real new values. Now no other entity row holds
        # any of these canonicals (we cleared them above), so UNIQUE holds.
        for entity_id, new_name, new_canonical in final_updates:
            conn.execute(
                "UPDATE entities SET name = ?, canonical = ? WHERE id = ?",
                [new_name, new_canonical, entity_id],
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log.info(
        "Done. Deleted %d duplicate row(s), rebranded %d lone row(s).",
        total_deleted,
        total_rebranded,
    )
    return {
        "total": len(rows),
        "groups": len(merges),
        "deleted": total_deleted,
        "rebranded": total_rebranded,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Hippo data directory (default: XDG_DATA_HOME/hippo)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to the hippo SQLite DB (overrides --data-dir)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print proposed merges without making changes",
    )
    args = parser.parse_args()

    if args.db is not None:
        db_path = args.db
    elif args.data_dir is not None:
        db_path = args.data_dir / "hippo.db"
    else:
        db_path = _default_db_path()

    if not db_path.exists():
        log.error("Database not found: %s", db_path)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    try:
        run(conn, dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
