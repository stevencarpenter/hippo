#!/usr/bin/env python3
"""One-shot knowledge-node deduplication.

Background
----------
Hippo enrichment historically minted a brand-new ``knowledge_nodes`` row on
every run with no dedup/replacement. When a session JSONL grew, the watcher
re-enqueued the (re-parsed) segment and the brain enriched it again, appending
an identical node instead of replacing the prior one. Long-lived sessions thus
accreted many byte-identical copies of the same observation. This script
collapses those copies.

What counts as a duplicate
--------------------------
Two nodes are the *same* node iff they share the full identity triple
``(content, embed_text, node_type)``. This is stricter than matching on the
``content`` JSON alone: ``embed_text`` is what gets embedded, so two
same-``content`` / different-``embed_text`` nodes produce *different vectors*
and are NOT retrieval-equivalent — collapsing them would silently change
semantic-search results. Matching on the summary alone is even less safe.

Algorithm (global, union-of-links)
-----------------------------------
For each identity group with >1 member:

  * survivor = ``MIN(id)`` (earliest; deterministic; verified never itself a
    loser of another group, so the map is a single pass and idempotent).
  * For every loser, UNION its edges onto the survivor in *every*
    ``knowledge_node_*`` link table (``INSERT OR IGNORE`` dedupes against the
    survivor's existing edges via the composite PK), then delete the loser's
    edges. This preserves the full set of ``(session/entity/event/..., node)``
    associations: every row that referenced any copy still references the
    survivor. Link tables are auto-discovered from ``sqlite_master`` (any
    two-column table with a ``knowledge_node_id`` column) so a future 8th link
    table is handled — and an unexpected table shape fails loudly.
  * Delete the loser's vector (vec0 ``knowledge_vectors`` — manual; vec0 does
    NOT auto-cascade) when the table is present, then delete the loser node
    (the ``knowledge_nodes_fts_ad`` trigger auto-cleans the FTS row).

All mutations run in a single ``BEGIN IMMEDIATE`` transaction; ``PRAGMA
foreign_key_check`` must be clean before COMMIT or the whole run rolls back.

Usage
-----
    # Dry run (DEFAULT — reports scope, changes nothing):
    uv run --project brain python brain/scripts/dedup-knowledge-nodes.py [--db PATH]
    # Apply (irreversible — run with writers stopped + a fresh backup in place):
    uv run --project brain python brain/scripts/dedup-knowledge-nodes.py --db PATH --apply

Exits 0 on success, non-zero on error.
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("dedup-knowledge-nodes")

# Identity columns that define a "same" knowledge node. embed_text is included
# because it deterministically produces the stored vector.
IDENTITY_COLS = ("content", "embed_text", "node_type")


def _default_db_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "hippo" / "hippo.db"


def _link_tables(conn: sqlite3.Connection) -> dict[str, str]:
    """Discover ``knowledge_node_*`` link tables → their non-node keycol.

    A link table is any table (other than ``knowledge_nodes``) that has a
    ``knowledge_node_id`` column. We additionally require exactly one *other*
    column (the foreign key into the linked entity) and fail loudly otherwise,
    so an unexpected schema shape can't silently corrupt the re-point.
    """
    # ESCAPE so the LIKE '_' is treated literally (otherwise it is a single-char
    # wildcard and would also match 'knowledge_nodes').
    names = [
        r[0]
        for r in conn.execute(
            r"SELECT name FROM sqlite_master WHERE type='table' "
            r"AND name LIKE 'knowledge\_node\_%' ESCAPE '\'"
        )
    ]
    tables: dict[str, str] = {}
    for name in names:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]
        if "knowledge_node_id" not in cols:
            continue
        others = [c for c in cols if c != "knowledge_node_id"]
        if len(others) != 1:
            raise RuntimeError(
                f"unexpected link-table shape for {name!r}: columns={cols!r} "
                "(expected exactly knowledge_node_id + one keycol)"
            )
        tables[name] = others[0]
    return tables


def _vec_table_state(conn: sqlite3.Connection) -> str:
    """Classify the vec0 ``knowledge_vectors`` table as one of:

    * ``"available"`` — exists and is queryable (sqlite-vec loaded). Delete vectors.
    * ``"absent"``     — no such table (test fixture / fresh install). Nothing to
                         orphan, so skipping vector deletes is safe.
    * ``"unreachable"``— the table EXISTS (recorded in sqlite_master) but querying
                         it fails because the vec0 module is not loaded on this
                         connection. Deleting nodes here would orphan their
                         vectors (vec0 has no FK cascade), so the caller MUST abort
                         rather than skip.

    The distinction matters: "absent" is benign, "unreachable" is dangerous, and
    a bare query-failure cannot tell them apart.
    """
    exists = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_vectors')"
    ).fetchone()[0]
    if not exists:
        return "absent"
    try:
        conn.execute("SELECT knowledge_node_id FROM knowledge_vectors LIMIT 0")
        return "available"
    except sqlite3.OperationalError:
        return "unreachable"


def _dedup_pairs(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """Return (loser_id, survivor_id) for every redundant node."""
    join = " AND ".join(f"g.{c} IS n.{c}" for c in IDENTITY_COLS)
    group_cols = ", ".join(IDENTITY_COLS)
    rows = conn.execute(
        f"""
        WITH g AS (
            SELECT {group_cols}, MIN(id) AS survivor_id
            FROM knowledge_nodes
            GROUP BY {group_cols}
            HAVING COUNT(*) > 1
        )
        SELECT n.id AS loser_id, g.survivor_id
        FROM knowledge_nodes n
        JOIN g ON {join}
        WHERE n.id <> g.survivor_id
        """
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def run(conn: sqlite3.Connection, *, dry_run: bool = True) -> dict[str, int]:
    """Deduplicate knowledge nodes on an open connection. Returns a stats dict.

    For production, pass a connection with sqlite-vec loaded (so the vec0 table
    is reachable); ``run`` tolerates its absence for tests.
    """
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")

    total = conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0]
    null_embed = conn.execute(
        "SELECT COUNT(*) FROM knowledge_nodes WHERE embed_text IS NULL"
    ).fetchone()[0]
    link_tables = _link_tables(conn)
    pairs = _dedup_pairs(conn)
    loser_ids = {loser for loser, _ in pairs}
    survivor_ids = {survivor for _, survivor in pairs}
    # Invariant: a survivor is never itself a loser (MIN(id) per group), so the
    # map is a single pass and re-running is a no-op once converged.
    chained = survivor_ids & loser_ids
    if chained:
        raise RuntimeError(
            f"survivor/loser chaining detected for ids {sorted(chained)[:20]} — "
            "dedup map is not single-pass; aborting"
        )

    stats = {
        "total_nodes": total,
        "dup_groups": len({s for _, s in pairs}),
        "losers": len(loser_ids),
        "predicted_after": total - len(loser_ids),
        "null_embed_text": null_embed,
        "link_tables": len(link_tables),
        "deleted": 0,
    }

    log.info("knowledge_nodes: %d", total)
    log.info("link tables discovered: %s", ", ".join(sorted(link_tables)) or "(none)")
    log.info(
        "duplicate groups: %d; redundant nodes to delete: %d; predicted after: %d",
        stats["dup_groups"],
        stats["losers"],
        stats["predicted_after"],
    )
    if null_embed:
        log.warning(
            "%d node(s) have NULL embed_text; these group together under the "
            "identity key (identical content+type with no vector ⇒ true dup). "
            "Verify this is intended before --apply.",
            null_embed,
        )

    if dry_run:
        log.info("dry run — no changes made (pass --apply to delete)")
        return stats

    if not pairs:
        log.info("nothing to dedup")
        return stats

    vec_state = _vec_table_state(conn)
    if vec_state == "unreachable":
        # The table exists but we can't delete from it — proceeding would orphan
        # every loser's vector. Refuse rather than corrupt the vector store.
        raise RuntimeError(
            "knowledge_vectors exists but is not queryable (sqlite-vec not loaded "
            "on this connection); aborting to avoid orphan vectors. Run via the "
            "brain's vector_store.open_conn (the CLI entry point already does)."
        )
    vec_ok = vec_state == "available"
    if not vec_ok:
        log.warning(
            "knowledge_vectors table absent (fresh install / test fixture) — "
            "skipping vector deletes; no vectors exist to orphan"
        )

    deleted = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for loser, survivor in pairs:
            for table, keycol in link_tables.items():
                # Union the loser's edges onto the survivor (PK dedupes), then
                # drop the loser's edges so the node delete won't FK-fail.
                conn.execute(
                    f"INSERT OR IGNORE INTO {table} (knowledge_node_id, {keycol}) "
                    f"SELECT ?, {keycol} FROM {table} WHERE knowledge_node_id = ?",
                    (survivor, loser),
                )
                conn.execute(f"DELETE FROM {table} WHERE knowledge_node_id = ?", (loser,))
            if vec_ok:
                conn.execute(
                    "DELETE FROM knowledge_vectors WHERE knowledge_node_id = ?",
                    (loser,),
                )
            conn.execute("DELETE FROM knowledge_nodes WHERE id = ?", (loser,))
            deleted += 1

        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_violations:
            raise RuntimeError(
                f"foreign_key_check failed ({len(fk_violations)} rows): {fk_violations[:20]}"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    stats["deleted"] = deleted
    log.info("done. deleted %d redundant node(s).", deleted)
    return stats


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
        "--apply",
        action="store_true",
        help="Apply deletions (default is a dry run that changes nothing)",
    )
    args = parser.parse_args()

    if args.db is not None:
        db_path = args.db
    elif args.data_dir is not None:
        db_path = args.data_dir / "hippo.db"
    else:
        db_path = _default_db_path()

    if not db_path.exists():
        log.error("database not found: %s", db_path)
        sys.exit(1)

    # Use the brain's vec0-aware connection so vector deletes work in production.
    from hippo_brain.vector_store import open_conn

    conn = open_conn(db_path)
    try:
        stats = run(conn, dry_run=not args.apply)
    finally:
        conn.close()

    log.info("stats: %s", stats)


if __name__ == "__main__":
    main()
