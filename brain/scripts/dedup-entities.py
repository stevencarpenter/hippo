#!/usr/bin/env python3
"""One-shot entity deduplication migration (R-16).

Re-canonicalizes all entities using the new entity_resolver rules and merges
rows where (type, new_canonical) would collide. Keeps the oldest row (smallest
created_at), re-points all knowledge_node_entities to the survivor, then
deletes the duplicate entity rows.

Usage:
    uv run --project brain python brain/scripts/dedup-entities.py [--data-dir PATH] [--dry-run]

Environment:
    HIPPO_PROJECT_ROOTS  Colon-separated list of absolute project root paths to
                         strip from path-type entity values (e.g.
                         /home/user/projects/hippo:/home/user/projects/hippo-postgres).
                         Required for worktree-prefix dedup to work correctly.

Exits 0 on success. Non-zero on error.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hippo_brain.entity_resolver import canonicalize  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("dedup-entities")


def _default_db_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "hippo" / "hippo.db"


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

    merges = [(key, members) for key, members in groups.items() if len(members) > 1]

    log.info("Total entities: %d", len(rows))
    log.info("Duplicate groups: %d", len(merges))

    if dry_run:
        for (etype, new_canonical), members in merges:
            keep_id = members[0]["id"]
            dupe_ids = [m["id"] for m in members[1:]]
            print(
                f"MERGE type={etype!r} canonical={new_canonical!r}: "
                f"keep id={keep_id}, delete ids={dupe_ids}"
            )
        log.info("Dry run — no changes made")
        return {"total": len(rows), "groups": len(merges), "deleted": 0}

    total_deleted = 0
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

            # Update the survivor's canonical to the new form.
            conn.execute(
                "UPDATE entities SET canonical = ? WHERE id = ?",
                [new_canonical, keep["id"]],
            )
            log.info(
                "Merged type=%r canonical=%r: kept id=%d, deleted %d duplicate(s)",
                etype,
                new_canonical,
                keep["id"],
                len(dupes),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log.info("Done. Deleted %d duplicate entity row(s).", total_deleted)
    return {"total": len(rows), "groups": len(merges), "deleted": total_deleted}


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
