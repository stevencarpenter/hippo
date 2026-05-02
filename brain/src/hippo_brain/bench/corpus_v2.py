"""Corpus v2: time-bucketed sampling + shadow SQLite snapshot + JSONL sidecar.

The v2 corpus targets the real hippo schema (events / claude_sessions /
browser_events / workflow_runs and their enrichment queues) so the bench
shadow brain can drain it the same way it would drain a live database. The
v1 corpus module remains untouched because v1 tests depend on its
simplified shapes.

Determinism is anchored in the seed: the same source data + seed yields the
same selected event IDs and per-row sha256s. The shadow SQLite file's bytes
are not stable across runs (sqlite default columns capture wall-clock time),
so verification is per-init: we hash the file we just wrote and compare on
re-read.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hippo_brain.bench.corpus import CorpusEntry
from hippo_brain.enrichment import is_enrichment_eligible
from hippo_brain.redaction import redact
from hippo_brain.schema_version import EXPECTED_SCHEMA_VERSION

CORPUS_VERSION_DEFAULT = "corpus-v2"


@dataclass
class _SourceSpec:
    """Internal description of a corpus source against the real hippo schema."""

    select: str
    ts_col: str
    has_probe_tag: bool
    shape: Callable[[sqlite3.Row], str]
    eligibility_dict: Callable[[sqlite3.Row], dict]
    id_col: str
    dest_table: str
    dest_columns: list[str]
    queue_table: str
    queue_event_col: str
    queue_event_value: Callable[[sqlite3.Row], Any] = field(default=lambda row: row["id"])


# Per-source select + payload + destination layout. Mirrors the live hippo
# schema (see crates/hippo-core/src/schema.sql); column names must stay in
# lockstep with EXPECTED_SCHEMA_VERSION.
_SOURCE_SPECS: dict[str, _SourceSpec] = {
    "shell": _SourceSpec(
        select=(
            "SELECT id, session_id, timestamp, command, stdout, stderr, "
            "duration_ms, exit_code, cwd, hostname, shell, git_repo, "
            "git_branch, git_commit, git_dirty, source_kind, probe_tag "
            "FROM events WHERE source_kind = 'shell'"
        ),
        ts_col="timestamp",
        has_probe_tag=True,
        shape=lambda row: json.dumps(
            {
                "command": row["command"],
                "stdout": row["stdout"],
                "stderr": row["stderr"],
                "duration_ms": row["duration_ms"],
                "exit_code": row["exit_code"],
                "cwd": row["cwd"],
            },
            sort_keys=True,
        ),
        eligibility_dict=lambda row: {
            "command": row["command"],
            "stdout": row["stdout"],
            "stderr": row["stderr"],
            "duration_ms": row["duration_ms"],
        },
        id_col="id",
        dest_table="events",
        dest_columns=[
            "id",
            "session_id",
            "timestamp",
            "command",
            "stdout",
            "stderr",
            "duration_ms",
            "exit_code",
            "cwd",
            "hostname",
            "shell",
            "git_repo",
            "git_branch",
            "git_commit",
            "git_dirty",
            "source_kind",
            "probe_tag",
        ],
        queue_table="enrichment_queue",
        queue_event_col="event_id",
    ),
    "claude": _SourceSpec(
        select=(
            "SELECT id, session_id, project_dir, cwd, git_branch, "
            "segment_index, start_time, end_time, summary_text, "
            "tool_calls_json, user_prompts_json, message_count, "
            "token_count, source_file, is_subagent, parent_session_id, "
            "probe_tag FROM claude_sessions"
        ),
        ts_col="start_time",
        has_probe_tag=True,
        shape=lambda row: json.dumps(
            {
                "session_id": row["session_id"],
                "summary_text": row["summary_text"],
                "tool_calls_json": row["tool_calls_json"],
                "user_prompts_json": row["user_prompts_json"],
                "message_count": row["message_count"],
            },
            sort_keys=True,
        ),
        eligibility_dict=lambda row: {
            "message_count": row["message_count"],
            "tool_calls_json": row["tool_calls_json"],
        },
        id_col="id",
        dest_table="claude_sessions",
        dest_columns=[
            "id",
            "session_id",
            "project_dir",
            "cwd",
            "git_branch",
            "segment_index",
            "start_time",
            "end_time",
            "summary_text",
            "tool_calls_json",
            "user_prompts_json",
            "message_count",
            "token_count",
            "source_file",
            "is_subagent",
            "parent_session_id",
            "probe_tag",
        ],
        queue_table="claude_enrichment_queue",
        queue_event_col="claude_session_id",
    ),
    "browser": _SourceSpec(
        select=(
            "SELECT id, timestamp, url, title, domain, dwell_ms, "
            "scroll_depth, extracted_text, search_query, referrer, "
            "content_hash, probe_tag FROM browser_events"
        ),
        ts_col="timestamp",
        has_probe_tag=True,
        shape=lambda row: json.dumps(
            {
                "url": row["url"],
                "title": row["title"],
                "dwell_ms": row["dwell_ms"],
                "scroll_depth": row["scroll_depth"],
            },
            sort_keys=True,
        ),
        eligibility_dict=lambda row: {"dwell_ms": row["dwell_ms"]},
        id_col="id",
        dest_table="browser_events",
        dest_columns=[
            "id",
            "timestamp",
            "url",
            "title",
            "domain",
            "dwell_ms",
            "scroll_depth",
            "extracted_text",
            "search_query",
            "referrer",
            "content_hash",
            "probe_tag",
        ],
        queue_table="browser_enrichment_queue",
        queue_event_col="browser_event_id",
    ),
    "workflow": _SourceSpec(
        select=(
            "SELECT id, repo, head_sha, head_branch, event, status, "
            "conclusion, started_at, completed_at, html_url, actor, "
            "raw_json, first_seen_at, last_seen_at "
            "FROM workflow_runs WHERE started_at IS NOT NULL"
        ),
        ts_col="started_at",
        has_probe_tag=False,
        shape=lambda row: json.dumps(
            {
                "repo": row["repo"],
                "head_sha": row["head_sha"],
                "event": row["event"],
                "status": row["status"],
                "conclusion": row["conclusion"],
                "raw_json": row["raw_json"],
            },
            sort_keys=True,
        ),
        eligibility_dict=lambda _row: {},
        id_col="id",
        dest_table="workflow_runs",
        dest_columns=[
            "id",
            "repo",
            "head_sha",
            "head_branch",
            "event",
            "status",
            "conclusion",
            "started_at",
            "completed_at",
            "html_url",
            "actor",
            "raw_json",
            "first_seen_at",
            "last_seen_at",
        ],
        queue_table="workflow_enrichment_queue",
        queue_event_col="run_id",
    ),
}


# Shadow DB schema. Mirrors the subset of crates/hippo-core/src/schema.sql
# that the brain enrichment touches. PRAGMA user_version is set separately
# from EXPECTED_SCHEMA_VERSION at write time. Keep in sync when the live
# schema changes the columns of these tables.
_SHADOW_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    start_time INTEGER NOT NULL DEFAULT 0,
    shell TEXT NOT NULL DEFAULT '',
    hostname TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    timestamp INTEGER NOT NULL,
    command TEXT NOT NULL,
    stdout TEXT,
    stderr TEXT,
    exit_code INTEGER,
    duration_ms INTEGER NOT NULL,
    cwd TEXT NOT NULL,
    hostname TEXT NOT NULL,
    shell TEXT NOT NULL,
    git_repo TEXT,
    git_branch TEXT,
    git_commit TEXT,
    git_dirty INTEGER,
    source_kind TEXT NOT NULL DEFAULT 'shell',
    tool_name TEXT,
    enriched INTEGER NOT NULL DEFAULT 0,
    redaction_count INTEGER NOT NULL DEFAULT 0,
    probe_tag TEXT,
    created_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS enrichment_queue (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL UNIQUE REFERENCES events(id),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','processing','done','failed','skipped')),
    priority INTEGER NOT NULL DEFAULT 5,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 5,
    error_message TEXT,
    locked_at INTEGER,
    locked_by TEXT,
    created_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS claude_sessions (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    project_dir TEXT NOT NULL,
    cwd TEXT NOT NULL,
    git_branch TEXT,
    segment_index INTEGER NOT NULL,
    start_time INTEGER NOT NULL,
    end_time INTEGER NOT NULL,
    summary_text TEXT NOT NULL,
    tool_calls_json TEXT,
    user_prompts_json TEXT,
    message_count INTEGER NOT NULL,
    token_count INTEGER,
    source_file TEXT NOT NULL,
    is_subagent INTEGER NOT NULL DEFAULT 0,
    parent_session_id TEXT,
    enriched INTEGER NOT NULL DEFAULT 0,
    probe_tag TEXT,
    content_hash TEXT,
    last_enriched_content_hash TEXT
);

CREATE TABLE IF NOT EXISTS claude_enrichment_queue (
    id INTEGER PRIMARY KEY,
    claude_session_id INTEGER NOT NULL UNIQUE REFERENCES claude_sessions(id),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','processing','done','failed','skipped')),
    priority INTEGER NOT NULL DEFAULT 5,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 5,
    error_message TEXT,
    locked_at INTEGER,
    locked_by TEXT,
    created_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS browser_events (
    id INTEGER PRIMARY KEY,
    timestamp INTEGER NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    domain TEXT NOT NULL,
    dwell_ms INTEGER NOT NULL,
    scroll_depth REAL,
    extracted_text TEXT,
    search_query TEXT,
    referrer TEXT,
    content_hash TEXT,
    enriched INTEGER NOT NULL DEFAULT 0,
    probe_tag TEXT
);

CREATE TABLE IF NOT EXISTS browser_enrichment_queue (
    id INTEGER PRIMARY KEY,
    browser_event_id INTEGER NOT NULL UNIQUE REFERENCES browser_events(id),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','processing','done','failed','skipped')),
    priority INTEGER NOT NULL DEFAULT 5,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 5,
    error_message TEXT,
    locked_at INTEGER,
    locked_by TEXT,
    created_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    head_branch TEXT,
    event TEXT NOT NULL,
    status TEXT NOT NULL,
    conclusion TEXT,
    started_at INTEGER,
    completed_at INTEGER,
    html_url TEXT NOT NULL,
    actor TEXT,
    raw_json TEXT NOT NULL,
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    enriched INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS workflow_enrichment_queue (
    run_id INTEGER PRIMARY KEY REFERENCES workflow_runs(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','processing','done','failed','skipped')),
    priority INTEGER NOT NULL DEFAULT 5,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 5,
    error_message TEXT,
    locked_at INTEGER,
    locked_by TEXT,
    enqueued_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS corpus_meta (
    schema_version INTEGER NOT NULL,
    corpus_version TEXT NOT NULL,
    generated_at_iso TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    seed INTEGER NOT NULL
);
"""


@dataclass
class _SampledRow:
    """One sampled row, paired with the bucket it landed in and its source row."""

    source: str
    bucket_index: int
    row: sqlite3.Row
    entry: CorpusEntry


def _bucket_bounds(
    corpus_days: int, corpus_buckets: int, now_ms: int | None = None
) -> list[tuple[int, int]]:
    """Equal-width time buckets across [now - corpus_days, now] in epoch ms."""
    if corpus_buckets <= 0:
        raise ValueError(f"corpus_buckets must be >= 1, got {corpus_buckets}")
    end_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    start_ms = end_ms - (corpus_days * 86_400_000)
    span = end_ms - start_ms
    width = span // corpus_buckets
    bounds: list[tuple[int, int]] = []
    for i in range(corpus_buckets):
        lo = start_ms + i * width
        hi = end_ms if i == corpus_buckets - 1 else start_ms + (i + 1) * width
        bounds.append((lo, hi))
    return bounds


def _build_entry(source: str, row: sqlite3.Row, spec: _SourceSpec) -> CorpusEntry:
    raw_payload = spec.shape(row)
    redacted = redact(raw_payload)
    return CorpusEntry(
        event_id=f"{source}-{row[spec.id_col]}",
        source=source,
        redacted_content=redacted,
        reference_enrichment=None,
    )


def sample_from_hippo_db_v2(
    db_path: Path,
    corpus_days: int = 90,
    corpus_buckets: int = 9,
    shell_min: int = 50,
    claude_min: int = 50,
    browser_min: int = 50,
    workflow_min: int = 50,
    seed: int = 42,
    *,
    now_ms: int | None = None,
) -> list[CorpusEntry]:
    """Stratified sample from a live hippo.db, time-bucketed across corpus_days.

    The window [now - corpus_days, now] is divided into corpus_buckets equal
    chunks; each (source, bucket) cell contributes proportionally to the
    per-source minimum floor. A source falling short of its floor (e.g.
    because its history doesn't cover the full window) is back-filled by
    additional random sampling from the union of all buckets.

    Probe rows (probe_tag IS NOT NULL on tables that have the column) are
    excluded. Trivial events that the production enrichment pipeline would
    skip via is_enrichment_eligible() are also excluded so the bench evaluates
    models on the same inputs they would see in production.

    Missing tables (e.g. older schema) are silently skipped.
    """
    rng = random.Random(seed)
    bounds = _bucket_bounds(corpus_days, corpus_buckets, now_ms=now_ms)
    minimums = {
        "shell": shell_min,
        "claude": claude_min,
        "browser": browser_min,
        "workflow": workflow_min,
    }

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sampled: list[_SampledRow] = []
    seen_ids: set[tuple[str, Any]] = set()

    try:
        for source, spec in _SOURCE_SPECS.items():
            target = minimums[source]
            if target <= 0:
                continue

            try:
                rows = conn.execute(spec.select).fetchall()
            except sqlite3.OperationalError:
                continue
            if not rows:
                continue

            if spec.has_probe_tag:
                rows = [r for r in rows if r["probe_tag"] is None]
            rows = [r for r in rows if is_enrichment_eligible(spec.eligibility_dict(r), source)[0]]
            if not rows:
                continue

            buckets: list[list[sqlite3.Row]] = [[] for _ in bounds]
            for row in rows:
                ts = row[spec.ts_col]
                if ts is None:
                    continue
                placed = False
                for i, (lo, hi) in enumerate(bounds):
                    upper_bound = hi if i == len(bounds) - 1 else hi
                    if lo <= ts < upper_bound or (i == len(bounds) - 1 and ts == upper_bound):
                        buckets[i].append(row)
                        placed = True
                        break
                if not placed and ts < bounds[0][0]:
                    buckets[0].append(row)

            per_bucket = max(1, target // corpus_buckets)
            for i, bucket_rows in enumerate(buckets):
                if not bucket_rows:
                    continue
                k = min(per_bucket, len(bucket_rows))
                picks = rng.sample(bucket_rows, k=k)
                for row in picks:
                    key = (source, row[spec.id_col])
                    if key in seen_ids:
                        continue
                    seen_ids.add(key)
                    sampled.append(
                        _SampledRow(
                            source=source,
                            bucket_index=i,
                            row=row,
                            entry=_build_entry(source, row, spec),
                        )
                    )

            already = sum(1 for s in sampled if s.source == source)
            if already < target:
                remaining_pool = [r for r in rows if (source, r[spec.id_col]) not in seen_ids]
                deficit = min(target - already, len(remaining_pool))
                if deficit > 0:
                    extras = rng.sample(remaining_pool, k=deficit)
                    for row in extras:
                        key = (source, row[spec.id_col])
                        seen_ids.add(key)
                        ts = row[spec.ts_col]
                        bidx = 0
                        if ts is not None:
                            for i, (lo, hi) in enumerate(bounds):
                                upper = hi if i == len(bounds) - 1 else hi
                                if lo <= ts < upper or (i == len(bounds) - 1 and ts == upper):
                                    bidx = i
                                    break
                        sampled.append(
                            _SampledRow(
                                source=source,
                                bucket_index=bidx,
                                row=row,
                                entry=_build_entry(source, row, spec),
                            )
                        )
    finally:
        conn.close()

    sampled.sort(key=lambda s: (s.source, s.row[_SOURCE_SPECS[s.source].id_col]))
    # Stash bucket_index on the entry so write_corpus_v2_jsonl can pick it up.
    for s in sampled:
        s.entry.__dict__["_v2_bucket_index"] = s.bucket_index
    return [s.entry for s in sampled]


def _write_source_rows(
    conn: sqlite3.Connection,
    source: str,
    sampled_rows: list[sqlite3.Row],
) -> None:
    spec = _SOURCE_SPECS[source]
    placeholders = ",".join("?" * len(spec.dest_columns))
    cols = ",".join(spec.dest_columns)
    sql = f"INSERT INTO {spec.dest_table} ({cols}) VALUES ({placeholders})"
    for row in sampled_rows:
        values = [row[c] if c in row.keys() else None for c in spec.dest_columns]
        conn.execute(sql, values)
    queue_sql = (
        f"INSERT INTO {spec.queue_table} ({spec.queue_event_col}, status) VALUES (?, 'pending')"
    )
    for row in sampled_rows:
        conn.execute(queue_sql, (spec.queue_event_value(row),))


def _ensure_session_row(conn: sqlite3.Connection, session_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (?, 0, '', '', '')",
        (session_id,),
    )


def write_corpus_v2_sqlite(
    entries: list[CorpusEntry],
    dest_db: Path,
    schema_version: int,
    *,
    source_rows: dict[str, list[sqlite3.Row]] | None = None,
    seed: int = 0,
    corpus_version: str = CORPUS_VERSION_DEFAULT,
) -> None:
    """Create a fresh shadow SQLite at dest_db with corpus rows + queue entries.

    `source_rows` carries the live sqlite3.Rows from sample_from_hippo_db_v2;
    when omitted, this function only writes corpus_meta and creates empty
    tables (useful for tests of schema shape).
    """
    if dest_db.exists():
        dest_db.unlink()
    dest_db.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(dest_db)
    try:
        conn.executescript(_SHADOW_SCHEMA_SQL)
        conn.execute(f"PRAGMA user_version = {int(schema_version)}")

        if source_rows:
            session_ids: set[int] = set()
            for row in source_rows.get("shell", []):
                if "session_id" in row.keys() and row["session_id"] is not None:
                    session_ids.add(int(row["session_id"]))
            for sid in session_ids:
                _ensure_session_row(conn, sid)

            for source in _SOURCE_SPECS:
                rows = source_rows.get(source) or []
                if rows:
                    _write_source_rows(conn, source, rows)

        conn.execute(
            "INSERT INTO corpus_meta "
            "(schema_version, corpus_version, generated_at_iso, event_count, seed) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                int(schema_version),
                corpus_version,
                _dt.datetime.now(tz=_dt.UTC).isoformat(),
                len(entries),
                int(seed),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def write_corpus_v2_jsonl(
    entries: list[CorpusEntry],
    dest_jsonl: Path,
    *,
    sampled_at_iso: str | None = None,
) -> None:
    """Write the JSONL sidecar, one record per event with bucket_index attached."""
    dest_jsonl.parent.mkdir(parents=True, exist_ok=True)
    iso = sampled_at_iso or _dt.datetime.now(tz=_dt.UTC).isoformat()
    with dest_jsonl.open("w", encoding="utf-8") as f:
        for e in entries:
            bidx = e.__dict__.get("_v2_bucket_index", 0)
            obj = {
                "event_id": e.event_id,
                "source": e.source,
                "redacted_content": e.redacted_content,
                "content_sha256": e.content_sha256,
                "bucket_index": int(bidx),
                "sampled_at_iso": iso,
            }
            f.write(json.dumps(obj, sort_keys=True))
            f.write("\n")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _sample_with_rows(
    db_path: Path,
    *,
    corpus_days: int,
    corpus_buckets: int,
    shell_min: int,
    claude_min: int,
    browser_min: int,
    workflow_min: int,
    seed: int,
    now_ms: int | None,
) -> tuple[list[CorpusEntry], dict[str, list[sqlite3.Row]]]:
    """Run sample_from_hippo_db_v2 and recover the underlying rows for sqlite write."""
    entries = sample_from_hippo_db_v2(
        db_path=db_path,
        corpus_days=corpus_days,
        corpus_buckets=corpus_buckets,
        shell_min=shell_min,
        claude_min=claude_min,
        browser_min=browser_min,
        workflow_min=workflow_min,
        seed=seed,
        now_ms=now_ms,
    )

    wanted_ids: dict[str, set[Any]] = {s: set() for s in _SOURCE_SPECS}
    for e in entries:
        try:
            _, raw_id = e.event_id.split("-", 1)
        except ValueError:
            continue
        try:
            wanted_ids[e.source].add(int(raw_id))
        except (ValueError, KeyError):
            continue

    source_rows: dict[str, list[sqlite3.Row]] = {s: [] for s in _SOURCE_SPECS}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for source, spec in _SOURCE_SPECS.items():
            ids = wanted_ids.get(source) or set()
            if not ids:
                continue
            try:
                rows = conn.execute(spec.select).fetchall()
            except sqlite3.OperationalError:
                continue
            source_rows[source] = [r for r in rows if r[spec.id_col] in ids]
    finally:
        conn.close()
    return entries, source_rows


def init_corpus_v2(
    db_path: Path,
    dest_sqlite: Path,
    dest_jsonl: Path,
    manifest_path: Path,
    corpus_version: str = CORPUS_VERSION_DEFAULT,
    *,
    schema_version: int = EXPECTED_SCHEMA_VERSION,
    corpus_days: int = 90,
    corpus_buckets: int = 9,
    shell_min: int = 50,
    claude_min: int = 50,
    browser_min: int = 50,
    workflow_min: int = 50,
    seed: int = 42,
    force: bool = False,
    now_ms: int | None = None,
) -> list[CorpusEntry]:
    """Sample, write the shadow SQLite + JSONL + manifest atomically.

    Atomicity is best-effort: if any step raises, partially-written files are
    removed before re-raising so a retry sees a clean slate.
    """
    if dest_sqlite.exists() and not force:
        raise FileExistsError(f"dest_sqlite already exists: {dest_sqlite}")
    if dest_jsonl.exists() and not force:
        raise FileExistsError(f"dest_jsonl already exists: {dest_jsonl}")

    cleanup_paths = [dest_sqlite, dest_jsonl, manifest_path]
    sampled_at = _dt.datetime.now(tz=_dt.UTC).isoformat()
    try:
        entries, source_rows = _sample_with_rows(
            db_path=db_path,
            corpus_days=corpus_days,
            corpus_buckets=corpus_buckets,
            shell_min=shell_min,
            claude_min=claude_min,
            browser_min=browser_min,
            workflow_min=workflow_min,
            seed=seed,
            now_ms=now_ms,
        )
        write_corpus_v2_sqlite(
            entries,
            dest_sqlite,
            schema_version=schema_version,
            source_rows=source_rows,
            seed=seed,
            corpus_version=corpus_version,
        )
        write_corpus_v2_jsonl(entries, dest_jsonl, sampled_at_iso=sampled_at)

        sqlite_event_ids = _read_sqlite_event_ids(dest_sqlite)
        jsonl_event_ids = sorted(e.event_id for e in entries)
        if sorted(sqlite_event_ids) != jsonl_event_ids:
            raise AssertionError("corpus_v2 mismatch: sqlite event IDs differ from jsonl event IDs")

        source_counts: dict[str, int] = {}
        for e in entries:
            source_counts[e.source] = source_counts.get(e.source, 0) + 1

        bounds = _bucket_bounds(corpus_days, corpus_buckets, now_ms=now_ms)
        manifest = {
            "corpus_version": corpus_version,
            "schema_version": int(schema_version),
            "generated_at_iso": sampled_at,
            "seed": int(seed),
            "source_counts": source_counts,
            "bucket_spec": {
                "days": corpus_days,
                "buckets": corpus_buckets,
                "window_start_ms": bounds[0][0],
                "window_end_ms": bounds[-1][1],
            },
            "corpus_content_hash": _sha256_file(dest_sqlite),
            "jsonl_content_hash": _sha256_file(dest_jsonl),
            "event_count": len(entries),
        }
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        return entries
    except BaseException:
        for p in cleanup_paths:
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        raise


def _read_sqlite_event_ids(dest_sqlite: Path) -> list[str]:
    """Reconstruct the corpus event_ids from the shadow SQLite contents."""
    conn = sqlite3.connect(f"file:{dest_sqlite}?mode=ro", uri=True)
    try:
        ids: list[str] = []
        for source, spec in _SOURCE_SPECS.items():
            try:
                rows = conn.execute(f"SELECT {spec.id_col} FROM {spec.dest_table}").fetchall()
            except sqlite3.OperationalError:
                continue
            ids.extend(f"{source}-{row[0]}" for row in rows)
        return ids
    finally:
        conn.close()


def verify_corpus_v2(sqlite_path: Path, jsonl_path: Path, manifest_path: Path) -> tuple[bool, str]:
    """Recompute SHA-256 of the SQLite + JSONL files; compare to manifest."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, f"manifest not found: {manifest_path}"
    except json.JSONDecodeError as e:
        return False, f"manifest unreadable: {e}"

    if not sqlite_path.exists():
        return False, f"sqlite not found: {sqlite_path}"
    if not jsonl_path.exists():
        return False, f"jsonl not found: {jsonl_path}"

    actual_sqlite = _sha256_file(sqlite_path)
    expected_sqlite = manifest.get("corpus_content_hash")
    if actual_sqlite != expected_sqlite:
        return (
            False,
            f"sqlite hash mismatch (manifest {expected_sqlite} vs recomputed {actual_sqlite})",
        )
    actual_jsonl = _sha256_file(jsonl_path)
    expected_jsonl = manifest.get("jsonl_content_hash")
    if actual_jsonl != expected_jsonl:
        return (
            False,
            f"jsonl hash mismatch (manifest {expected_jsonl} vs recomputed {actual_jsonl})",
        )
    return True, "ok"
