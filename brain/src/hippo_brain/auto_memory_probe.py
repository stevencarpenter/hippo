"""Synthetic auto-memory probe — dedicated fixture outside Claude's datastore (SNUG-138)."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import uuid
from pathlib import Path

from hippo_brain.auto_memory_constants import PROBE_LOGICAL_PATH, PROBE_REPOSITORY
from hippo_brain.auto_memory_ingest import ingest_memory_file
from hippo_brain.schema_version import EXPECTED_SCHEMA_VERSION


def probe_fixture_dir(data_dir: Path) -> Path:
    return data_dir / "probe-auto-memory"


def run_probe(
    conn: sqlite3.Connection,
    *,
    fixture_root: Path,
    now_ms: int | None = None,
) -> dict[str, object]:
    """Create/mutate probe fixture, ingest twice, return ok + lag_ms."""
    now_ms = now_ms or int(time.time() * 1000)
    fixture_root.mkdir(parents=True, exist_ok=True)
    fixture = fixture_root / "probe-memory.md"
    tag = uuid.uuid4().hex
    fixture.write_text(f"# Probe {tag}\n\nSynthetic auto-memory probe content.\n")
    probe_start = int(time.time() * 1000)

    first = ingest_memory_file(
        conn,
        fixture,
        repository=PROBE_REPOSITORY,
        logical_path=PROBE_LOGICAL_PATH,
        now_ms=now_ms,
    )
    if not first.changed:
        return {"ok": False, "lag_ms": None, "error": "initial ingest was no-op"}

    row = conn.execute(
        """
        SELECT md.updated_at, mr.revision_number
        FROM memory_documents md
        JOIN memory_revisions mr ON mr.id = md.current_revision_id
        WHERE md.repository = ? AND md.logical_path = ?
        """,
        (PROBE_REPOSITORY, PROBE_LOGICAL_PATH),
    ).fetchone()
    if row is None:
        return {"ok": False, "lag_ms": None, "error": "probe document missing after ingest"}

    fixture.write_text(
        fixture.read_text() + f"\n## Mutation\n\nUpdated at {tag}.\n",
        encoding="utf-8",
    )
    second = ingest_memory_file(
        conn,
        fixture,
        repository=PROBE_REPOSITORY,
        logical_path=PROBE_LOGICAL_PATH,
        now_ms=now_ms + 1,
    )
    if not second.changed or second.revision_number < 2:
        return {"ok": False, "lag_ms": None, "error": "mutation ingest did not advance revision"}

    updated = conn.execute(
        "SELECT updated_at FROM memory_documents WHERE repository = ? AND logical_path = ?",
        (PROBE_REPOSITORY, PROBE_LOGICAL_PATH),
    ).fetchone()
    if updated is None:
        return {"ok": False, "lag_ms": None, "error": "probe document missing after mutation"}

    lag_ms = max(0, int(updated[0]) - probe_start)
    return {"ok": True, "lag_ms": lag_ms, "revision_number": second.revision_number}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run synthetic Claude auto-memory probe.")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path.home() / ".local" / "share" / "hippo" / "hippo.db",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Hippo data directory for probe fixture (defaults to parent of --db)",
    )
    args = parser.parse_args(argv)
    data_dir = args.data_dir or args.db.parent
    conn = sqlite3.connect(args.db)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != EXPECTED_SCHEMA_VERSION:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "lag_ms": None,
                        "error": f"schema version {version} != {EXPECTED_SCHEMA_VERSION}",
                    }
                )
            )
            return 1
        result = run_probe(conn, fixture_root=probe_fixture_dir(data_dir))
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("ok") else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
