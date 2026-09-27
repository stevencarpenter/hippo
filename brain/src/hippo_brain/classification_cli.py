"""Bounded operator commands for classification state and shared-topic discovery."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import TypedDict

from hippo_brain import _load_runtime_settings, classification
from hippo_brain.decision_capture import external_path
from hippo_brain.jev import digest
from hippo_brain.retrieval import Filters, _apply_filters
from hippo_brain.schema_version import ACCEPTED_READ_VERSIONS


class DatabaseIdentity(TypedDict):
    path: str
    device: int
    inode: int
    schema_version: int
    schema_hash: str


class BackfillCursor(TypedDict):
    format_version: int
    database: DatabaseIdentity
    recipe_hash: str
    high_watermark: int
    high_watermark_uuid: str | None
    after_id: int
    updated_at_ms: int
    complete: bool


def _connect(database: Path, *, write: bool = False) -> sqlite3.Connection:
    path = database.expanduser().resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        if write:
            _require_schema(conn)
            conn.execute("PRAGMA journal_mode=WAL")
        else:
            conn.execute("PRAGMA query_only=ON")
        return conn
    except BaseException:
        conn.close()
        raise


def _require_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in ACCEPTED_READ_VERSIONS or not classification.schema_ready(conn):
        raise ValueError(
            "classification schema is missing or incompatible; run the normal migration"
        )


def status(database: Path) -> dict:
    """Inspect an existing database without creating it or scheduling work."""
    with closing(_connect(database)) as conn, conn:
        conn.execute("BEGIN")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        available = version in ACCEPTED_READ_VERSIONS and classification.schema_ready(conn)
        counts = (
            dict(
                conn.execute(
                    "SELECT status,count(*) FROM knowledge_node_classifications GROUP BY status"
                )
            )
            if available
            else {}
        )
        oldest = (
            conn.execute(
                "SELECT min(enqueued_at) FROM knowledge_node_classifications "
                "WHERE status IN ('pending','processing')"
            ).fetchone()[0]
            if available
            else None
        )
        return {
            "database": str(database.expanduser().resolve()),
            "available": available,
            "schema_version": version,
            "recipe_hash": classification.default_recipe().recipe_hash,
            "counts": counts,
            "oldest_pending_age_ms": max(0, time.time_ns() // 1_000_000 - oldest)
            if oldest is not None
            else None,
        }


def _identity(conn: sqlite3.Connection, database: Path) -> DatabaseIdentity:
    stat = database.stat()
    schema = conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
    ).fetchall()
    return {
        "path": str(database),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "schema_hash": digest(schema),
    }


def _cursor_path(cursor: Path, database: Path) -> Path:
    path = external_path(cursor)
    protected = [
        database,
        *(Path(str(database) + suffix) for suffix in ("-wal", "-shm", "-journal")),
    ]
    if any(
        path == item or path.exists() and item.exists() and path.samefile(item)
        for item in protected
    ):
        raise ValueError("cursor must be separate from the database and SQLite sidecars")
    return path


def _read_cursor(
    conn: sqlite3.Connection, path: Path, database: DatabaseIdentity, recipe_hash: str
) -> BackfillCursor:
    if not path.exists():
        boundary = conn.execute(
            "SELECT id,uuid FROM knowledge_nodes ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "format_version": 1,
            "database": database,
            "recipe_hash": recipe_hash,
            "high_watermark": boundary[0] if boundary else 0,
            "high_watermark_uuid": boundary[1] if boundary else None,
            "after_id": 0,
            "updated_at_ms": 0,
            "complete": False,
        }
    if path.stat().st_size > 65_536:
        raise ValueError("cursor exceeds 64 KiB")
    value = json.loads(path.read_text())
    if (
        not isinstance(value, dict)
        or value.get("format_version") != 1
        or value.get("database") != database
        or value.get("recipe_hash") != recipe_hash
        or type(value.get("after_id")) is not int
        or type(value.get("high_watermark")) is not int
        or not 0 <= value["after_id"] <= value["high_watermark"]
    ):
        raise ValueError("cursor database, schema, recipe, or position does not match")
    boundary = conn.execute(
        "SELECT uuid FROM knowledge_nodes WHERE id=?", (value["high_watermark"],)
    ).fetchone()
    if (boundary[0] if boundary else None) != value.get("high_watermark_uuid"):
        raise ValueError("cursor node identity changed; start a new backfill cursor")
    return value


def _write_cursor(path: Path, value: BackfillCursor) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, prefix=".classification-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def backfill(database: Path, cursor: Path, *, limit: int = 100) -> dict:
    """Queue one page, then atomically advance an external, recipe-bound cursor."""
    if not 1 <= limit <= 1000:
        raise ValueError("backfill limit must be between one and 1000")
    database = database.expanduser().resolve(strict=True)
    cursor = _cursor_path(cursor, database)
    recipe = classification.default_recipe()
    with closing(_connect(database, write=True)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _require_schema(conn)
        state = _read_cursor(conn, cursor, _identity(conn, database), recipe.recipe_hash)
        previous = state["after_id"]
        # Freeze this job's population at its initial high watermark. New nodes
        # use the normal writer hook, or a new bounded backfill job.
        count = len(
            conn.execute(
                "SELECT id FROM knowledge_nodes WHERE id>? AND id<=? ORDER BY id LIMIT ?",
                (previous, state["high_watermark"], limit),
            ).fetchall()
        )
        result = (
            classification.backfill(conn, after_id=previous, limit=count, recipe=recipe)
            if count
            else {
                "after_id": previous,
                "scanned": 0,
                "queued": 0,
            }
        )
        state["after_id"] = result["after_id"]
        state["complete"] = (
            conn.execute(
                "SELECT 1 FROM knowledge_nodes WHERE id>? AND id<=? LIMIT 1",
                (state["after_id"], state["high_watermark"]),
            ).fetchone()
            is None
        )
        state["updated_at_ms"] = time.time_ns() // 1_000_000
    # Publication failure intentionally leaves the old cursor. Replaying an
    # already committed page is safe through enqueue_node's revision contract.
    _write_cursor(_cursor_path(cursor, database), state)
    return {
        "database": str(database),
        "cursor": str(cursor),
        "recipe_hash": recipe.recipe_hash,
        "previous_after_id": previous,
        **result,
        "complete": state["complete"],
    }


_EXPORT_COLUMNS = {
    "node_id": "BIGINT",
    "node_uuid": "VARCHAR",
    "node_type": "VARCHAR",
    "status": "VARCHAR",
    "error": "VARCHAR",
    "attempts": "INTEGER",
    "requested_revision": "INTEGER",
    "applied_revision": "INTEGER",
    "enqueued_at": "BIGINT",
    "started_at": "BIGINT",
    "applied_at": "BIGINT",
    "taxonomy_version": "VARCHAR",
    "model_id": "VARCHAR",
    "returned_model_id": "VARCHAR",
    "input_version": "VARCHAR",
    "recipe_hash": "VARCHAR",
    "prompt_hash": "VARCHAR",
    "threshold_hash": "VARCHAR",
    "desired_input_hash": "VARCHAR",
    "applied_input_hash": "VARCHAR",
    "probabilities_json": "VARCHAR",
    "accepted_topics_json": "VARCHAR",
}


def export(database: Path, out: Path) -> dict:
    """Write classification state to Parquet from one consistent read.

    ``classifications.parquet`` has one row per classification record; nodes never
    enqueued are absent. ``topic-probabilities.parquet`` has one row per ready node
    and topic. Both files publish together or not at all, and never overwrite.
    """
    try:
        import duckdb
    except ImportError as exc:
        raise ValueError(
            "classification export requires duckdb; run from a checkout synced with "
            "`uv sync --project brain`"
        ) from exc
    out = external_path(out)
    targets = [out / "classifications.parquet", out / "topic-probabilities.parquet"]
    if any(path.exists() for path in targets):
        raise ValueError("export files already exist; choose a new output directory")
    out.mkdir(parents=True, exist_ok=True, mode=0o700)

    def scratch(suffix: str) -> Path:
        descriptor, name = tempfile.mkstemp(
            dir=out, prefix=".classification-export-", suffix=suffix
        )
        os.close(descriptor)
        return Path(name)

    def literal(path: Path) -> str:
        return "'" + str(path).replace("'", "''") + "'"

    staging, *pending = scratch(".jsonl"), scratch(".parquet"), scratch(".parquet")
    published: list[Path] = []
    try:
        with closing(_connect(database)) as conn:
            _require_schema(conn)
            # One SELECT is one SQLite read snapshot, even against a live WAL database.
            rows = conn.execute(
                "SELECT "
                + ",".join(f"kn.{c}" if c == "node_type" else f"c.{c}" for c in _EXPORT_COLUMNS)
                + " FROM knowledge_node_classifications c"
                " JOIN knowledge_nodes kn ON kn.id=c.node_id ORDER BY c.node_id"
            )
            with staging.open("w") as stream:
                for row in rows:
                    stream.write(json.dumps(dict(zip(_EXPORT_COLUMNS, row, strict=True))) + "\n")
        columns = ",".join(f"'{name}':'{kind}'" for name, kind in _EXPORT_COLUMNS.items())
        try:
            with closing(duckdb.connect()) as db:
                db.execute(
                    "CREATE TABLE c AS SELECT *, applied_at - started_at AS latency_ms "
                    f"FROM read_json(?, format='newline_delimited', columns={{{columns}}})",
                    [str(staging)],
                )
                db.execute(
                    "CREATE TABLE t AS SELECT c.node_id, c.node_type, c.recipe_hash, e.key AS topic, "
                    "e.value::DOUBLE AS prob, "
                    "list_contains(json_extract_string(c.accepted_topics_json, '$[*]'), e.key) "
                    "AS accepted FROM c, json_each(c.probabilities_json) e WHERE c.status = 'ready'"
                )
                for table, path in zip(("c", "t"), pending, strict=True):
                    db.execute(
                        f"COPY {table} TO {literal(path)} (FORMAT parquet, COMPRESSION zstd)"
                    )
                counts = {
                    "classifications": db.execute("SELECT count(*) FROM c").fetchone()[0],
                    "topic_probabilities": db.execute("SELECT count(*) FROM t").fetchone()[0],
                }
        except duckdb.Error as exc:
            raise RuntimeError(f"Parquet export failed: {exc}") from exc
        # link() refuses an existing target, so a concurrent export cannot be overwritten.
        for path, target in zip(pending, targets, strict=True):
            os.link(path, target)
            published.append(target)
    except BaseException:
        for target in published:
            target.unlink(missing_ok=True)
        raise
    finally:
        for path in (staging, *pending):
            path.unlink(missing_ok=True)
    return {
        "database": str(database.expanduser().resolve()),
        "files": [str(path) for path in targets],
        "rows": counts,
    }


def _provenance(conn: sqlite3.Connection, node_id: int, probability: float) -> dict:
    row = conn.execute(
        "SELECT node_uuid,applied_revision,applied_input_hash,applied_recipe_hash,"
        "taxonomy_version,returned_model_id,applied_at FROM knowledge_node_classifications WHERE node_id=?",
        (node_id,),
    ).fetchone()
    return dict(
        zip(
            (
                "uuid",
                "revision",
                "input_hash",
                "recipe_hash",
                "taxonomy_version",
                "model",
                "applied_at_ms",
            ),
            row,
            strict=True,
        ),
        probability=probability,
    )


def connections(
    database: Path, node_uuid: str, *, limit: int = 20, filters: Filters | None = None
) -> dict:
    """Read bounded exact-topic connections, using normal retrieval eligibility."""
    if not 1 <= limit <= 100:
        raise ValueError("connection limit must be between one and 100")
    filters = filters or Filters()
    recipe = classification.default_recipe()
    with closing(_connect(database)) as conn, conn:
        conn.execute("BEGIN")
        _require_schema(conn)
        row = conn.execute("SELECT id FROM knowledge_nodes WHERE uuid=?", (node_uuid,)).fetchone()
        if row is None or not _apply_filters(conn, [row[0]], filters):
            raise ValueError("node is missing or not eligible under the requested filters")
        node_id = row[0]
        topics = classification.current_topics(conn, [node_id], recipe).get(node_id, {})
        output: dict = {
            "node_uuid": node_uuid,
            "recipe_hash": recipe.recipe_hash,
            "connections": [],
            "meaning": "shared topic membership, not factual support or causality",
        }
        if not topics:
            return output
        placeholders = ",".join("?" for _ in topics)
        candidates = conn.execute(
            f"SELECT DISTINCT kn.id,kn.uuid FROM knowledge_node_entities ne "
            f"JOIN entities e ON e.id=ne.entity_id JOIN knowledge_nodes kn ON kn.id=ne.knowledge_node_id "
            f"WHERE ne.knowledge_node_id!=? AND e.type='concept' AND e.canonical IN ({placeholders}) "
            "ORDER BY kn.uuid",
            (node_id, *(classification.NAMESPACE + topic for topic in topics)),
        )
        while batch := candidates.fetchmany(64):
            allowed = _apply_filters(conn, [nid for nid, _ in batch], filters)
            memberships = classification.current_topics(conn, sorted(allowed), recipe)
            for nid, uuid in batch:
                shared = sorted(topics.keys() & memberships.get(nid, {}).keys())
                if not shared:
                    continue
                output["connections"].append(
                    {
                        "node_uuid": uuid,
                        "topics": [
                            {
                                "topic_id": topic,
                                "from": _provenance(conn, node_id, topics[topic]),
                                "to": _provenance(conn, nid, memberships[nid][topic]),
                            }
                            for topic in shared
                        ],
                    }
                )
                if len(output["connections"]) == limit:
                    return output
        return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "backfill", "connections", "export"):
        child = commands.add_parser(command)
        child.add_argument("--database", type=Path, required=True)
        child.add_argument("--recipe", type=Path, help="Override classification.recipe_path")
        if command == "export":
            child.add_argument("--out", type=Path, required=True)
        if command == "backfill":
            child.add_argument("--cursor", type=Path, required=True)
            child.add_argument("--limit", type=int, default=100)
        elif command == "connections":
            child.add_argument("--node", required=True)
            child.add_argument("--limit", type=int, default=20)
            for name in ("project", "source", "branch", "entity", "memory-category"):
                child.add_argument("--" + name)
            child.add_argument("--since-ms", type=int)
    args = parser.parse_args(argv)
    try:
        recipe_path = args.recipe
        if recipe_path is None:
            settings = _load_runtime_settings().get("classification", {})
            if not isinstance(settings, dict):
                raise ValueError("classification configuration must be a table")
            recipe_path = settings.get("recipe_path")
        if recipe_path is not None and not isinstance(recipe_path, (str, Path)):
            raise ValueError("classification.recipe_path must be a path")
        classification.configure(False, classification.load_recipe(recipe_path))
        if args.command == "status":
            result = status(args.database)
        elif args.command == "backfill":
            result = backfill(args.database, args.cursor, limit=args.limit)
        elif args.command == "export":
            result = export(args.database, args.out)
        else:
            result = connections(
                args.database,
                args.node,
                limit=args.limit,
                filters=Filters(
                    project=args.project,
                    source=args.source,
                    branch=args.branch,
                    entity=args.entity,
                    memory_category=args.memory_category,
                    since_ms=args.since_ms,
                ),
            )
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        parser.exit(2, f"classification: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
