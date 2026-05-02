"""Tests for hippo_brain.bench.corpus_v2 — time-bucketed sample, shadow SQLite,
JSONL sidecar, manifest verification."""

from __future__ import annotations

import json
import sqlite3

import pytest

from hippo_brain.bench.corpus_v2 import (
    init_corpus_v2,
    sample_from_hippo_db_v2,
    verify_corpus_v2,
)
from hippo_brain.schema_version import EXPECTED_SCHEMA_VERSION

# ── Fixtures ──────────────────────────────────────────────────────────────

NOW_MS = 1_800_000_000_000  # arbitrary fixed "now" so bucket math is deterministic
DAY_MS = 86_400_000


def _ensure_session(conn: sqlite3.Connection, sid: int = 1) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (?, ?, 'zsh', 'host', 'user')",
        (sid, NOW_MS - 90 * DAY_MS),
    )


def _insert_shell_event(
    conn: sqlite3.Connection,
    *,
    timestamp: int,
    command: str = "cargo build",
    stdout: str = "Compiling...",
    probe_tag: str | None = None,
) -> None:
    _ensure_session(conn)
    conn.execute(
        "INSERT INTO events "
        "(session_id, timestamp, command, stdout, stderr, exit_code, "
        " duration_ms, cwd, hostname, shell, source_kind, probe_tag) "
        "VALUES (?, ?, ?, ?, '', 0, 1234, '/repo', 'host', 'zsh', 'shell', ?)",
        (1, timestamp, command, stdout, probe_tag),
    )


def _insert_claude_session(
    conn: sqlite3.Connection,
    *,
    start_time: int,
    session_id: str | None = None,
    segment_index: int = 0,
    probe_tag: str | None = None,
) -> None:
    sid = session_id or f"sess-{start_time}-{segment_index}"
    conn.execute(
        "INSERT INTO claude_sessions "
        "(session_id, project_dir, cwd, segment_index, start_time, end_time, "
        " summary_text, tool_calls_json, user_prompts_json, message_count, "
        " source_file, probe_tag) "
        "VALUES (?, '/proj', '/proj', ?, ?, ?, 'summary', '[\"Bash\"]', "
        " '[]', 8, '/log.jsonl', ?)",
        (sid, segment_index, start_time, start_time + 1000, probe_tag),
    )


def _insert_browser_event(
    conn: sqlite3.Connection,
    *,
    timestamp: int,
    url: str | None = None,
    probe_tag: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO browser_events "
        "(timestamp, url, title, domain, dwell_ms, probe_tag) "
        "VALUES (?, ?, 'doc', 'docs.python.org', 60000, ?)",
        (timestamp, url or f"https://docs.python.org/{timestamp}", probe_tag),
    )


def _insert_workflow_run(
    conn: sqlite3.Connection,
    *,
    started_at: int,
    head_sha: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO workflow_runs "
        "(repo, head_sha, head_branch, event, status, conclusion, started_at, "
        " completed_at, html_url, raw_json, first_seen_at, last_seen_at) "
        "VALUES ('hippo', ?, 'main', 'push', 'completed', 'success', ?, ?, "
        " 'https://github.com/x', '{\"ok\":1}', ?, ?)",
        (
            head_sha or f"sha-{started_at}",
            started_at,
            started_at + 1000,
            started_at,
            started_at + 1000,
        ),
    )


def _seed_full_corpus(conn: sqlite3.Connection, *, per_source: int = 12) -> None:
    """Spread per_source events of each source across the 9-week window."""
    # Place at -60, -53, -46, -39, -32, -25, -18, -11, -4 days, then +shifts.
    days = [-60, -53, -46, -39, -32, -25, -18, -11, -4]
    for i in range(per_source):
        d = days[i % len(days)]
        # Add an hour offset per repeat so identical-day rows have unique ts.
        ts = NOW_MS + d * DAY_MS + (i // len(days)) * 3_600_000
        _insert_shell_event(conn, timestamp=ts, command=f"cargo build {i}")
        _insert_claude_session(
            conn,
            start_time=ts,
            session_id=f"sess-{i}",
            segment_index=i,
        )
        _insert_browser_event(conn, timestamp=ts, url=f"https://docs.python.org/{i}")
        _insert_workflow_run(conn, started_at=ts, head_sha=f"sha-{i}")
    conn.commit()


@pytest.fixture
def seeded_db(tmp_db):
    conn, db_path = tmp_db
    _seed_full_corpus(conn, per_source=12)
    yield conn, db_path


# ── 1. Determinism ────────────────────────────────────────────────────────


def test_sample_determinism(seeded_db):
    _, db_path = seeded_db
    a = sample_from_hippo_db_v2(
        db_path=db_path,
        corpus_days=63,
        corpus_buckets=9,
        shell_min=20,
        claude_min=20,
        browser_min=20,
        workflow_min=20,
        seed=42,
        now_ms=NOW_MS,
    )
    b = sample_from_hippo_db_v2(
        db_path=db_path,
        corpus_days=63,
        corpus_buckets=9,
        shell_min=20,
        claude_min=20,
        browser_min=20,
        workflow_min=20,
        seed=42,
        now_ms=NOW_MS,
    )
    assert [e.event_id for e in a] == [e.event_id for e in b]
    assert len(a) > 0


# ── 2. Time-bucket coverage ───────────────────────────────────────────────


def test_time_bucket_coverage(tmp_db):
    """With 9 events spread across 9 weeks, sampling spans at least 7 buckets."""
    conn, db_path = tmp_db
    days = [-60, -53, -46, -39, -32, -25, -18, -11, -4]
    for i, d in enumerate(days):
        ts = NOW_MS + d * DAY_MS
        _insert_shell_event(conn, timestamp=ts, command=f"cargo build {i}")
    conn.commit()

    entries = sample_from_hippo_db_v2(
        db_path=db_path,
        corpus_days=63,
        corpus_buckets=9,
        shell_min=20,
        claude_min=0,
        browser_min=0,
        workflow_min=0,
        seed=7,
        now_ms=NOW_MS,
    )
    buckets_hit = {e.__dict__.get("_v2_bucket_index", -1) for e in entries}
    assert len(entries) == 9
    assert len(buckets_hit) >= 7, f"only {len(buckets_hit)} buckets covered: {buckets_hit}"


# ── 3. Source-minimum floor (cannot exceed available) ─────────────────────


def test_source_minimum_floor(tmp_db):
    conn, db_path = tmp_db
    # 10 shell events spread across recent buckets.
    days = [-60, -53, -46, -39, -32, -25, -18, -11, -4, -2]
    for i, d in enumerate(days):
        _insert_shell_event(conn, timestamp=NOW_MS + d * DAY_MS, command=f"cargo build {i}")
    conn.commit()

    entries = sample_from_hippo_db_v2(
        db_path=db_path,
        corpus_days=63,
        corpus_buckets=9,
        shell_min=50,
        claude_min=0,
        browser_min=0,
        workflow_min=0,
        seed=42,
        now_ms=NOW_MS,
    )
    shell = [e for e in entries if e.source == "shell"]
    assert len(shell) == 10


# ── 4. SQLite/JSONL equivalence ───────────────────────────────────────────


def test_sqlite_jsonl_equivalence(seeded_db, tmp_path):
    _, db_path = seeded_db
    dest_sqlite = tmp_path / "corpus-v2.sqlite"
    dest_jsonl = tmp_path / "corpus-v2.jsonl"
    manifest = tmp_path / "corpus-v2.manifest.json"

    init_corpus_v2(
        db_path=db_path,
        dest_sqlite=dest_sqlite,
        dest_jsonl=dest_jsonl,
        manifest_path=manifest,
        corpus_days=63,
        corpus_buckets=9,
        shell_min=10,
        claude_min=10,
        browser_min=10,
        workflow_min=10,
        seed=42,
        now_ms=NOW_MS,
    )

    jsonl_ids = []
    for line in dest_jsonl.read_text().splitlines():
        if line.strip():
            jsonl_ids.append(json.loads(line)["event_id"])

    sqlite_ids: list[str] = []
    table_map = [
        ("events", "shell", "id"),
        ("claude_sessions", "claude", "id"),
        ("browser_events", "browser", "id"),
        ("workflow_runs", "workflow", "id"),
    ]
    conn = sqlite3.connect(f"file:{dest_sqlite}?mode=ro", uri=True)
    try:
        for table, source, idcol in table_map:
            for row in conn.execute(f"SELECT {idcol} FROM {table}"):
                sqlite_ids.append(f"{source}-{row[0]}")
    finally:
        conn.close()

    assert sorted(sqlite_ids) == sorted(jsonl_ids)
    assert len(sqlite_ids) > 0


# ── 5. Schema version recorded in corpus_meta ─────────────────────────────


def test_schema_version_in_corpus_meta(seeded_db, tmp_path):
    _, db_path = seeded_db
    dest_sqlite = tmp_path / "corpus-v2.sqlite"
    dest_jsonl = tmp_path / "corpus-v2.jsonl"
    manifest = tmp_path / "corpus-v2.manifest.json"

    init_corpus_v2(
        db_path=db_path,
        dest_sqlite=dest_sqlite,
        dest_jsonl=dest_jsonl,
        manifest_path=manifest,
        shell_min=4,
        claude_min=4,
        browser_min=4,
        workflow_min=4,
        seed=42,
        now_ms=NOW_MS,
    )

    conn = sqlite3.connect(f"file:{dest_sqlite}?mode=ro", uri=True)
    try:
        (sv,) = conn.execute("SELECT schema_version FROM corpus_meta").fetchone()
    finally:
        conn.close()
    assert sv == EXPECTED_SCHEMA_VERSION


# ── 6. verify_corpus_v2 passes on fresh corpus ────────────────────────────


def test_verify_corpus_v2_passes(seeded_db, tmp_path):
    _, db_path = seeded_db
    dest_sqlite = tmp_path / "corpus-v2.sqlite"
    dest_jsonl = tmp_path / "corpus-v2.jsonl"
    manifest = tmp_path / "corpus-v2.manifest.json"

    init_corpus_v2(
        db_path=db_path,
        dest_sqlite=dest_sqlite,
        dest_jsonl=dest_jsonl,
        manifest_path=manifest,
        shell_min=4,
        claude_min=4,
        browser_min=4,
        workflow_min=4,
        seed=42,
        now_ms=NOW_MS,
    )

    ok, detail = verify_corpus_v2(dest_sqlite, dest_jsonl, manifest)
    assert ok, detail
    assert detail == "ok"


# ── 7. verify_corpus_v2 detects sqlite tampering ──────────────────────────


def test_verify_corpus_v2_fails_on_tamper(seeded_db, tmp_path):
    _, db_path = seeded_db
    dest_sqlite = tmp_path / "corpus-v2.sqlite"
    dest_jsonl = tmp_path / "corpus-v2.jsonl"
    manifest = tmp_path / "corpus-v2.manifest.json"

    init_corpus_v2(
        db_path=db_path,
        dest_sqlite=dest_sqlite,
        dest_jsonl=dest_jsonl,
        manifest_path=manifest,
        shell_min=4,
        claude_min=4,
        browser_min=4,
        workflow_min=4,
        seed=42,
        now_ms=NOW_MS,
    )

    # Flip one byte in the SQLite file (last byte is safe — usually unused).
    data = bytearray(dest_sqlite.read_bytes())
    data[-1] ^= 0xFF
    dest_sqlite.write_bytes(bytes(data))

    ok, detail = verify_corpus_v2(dest_sqlite, dest_jsonl, manifest)
    assert not ok
    assert "hash" in detail.lower()


# ── 8. Overwrite protection ───────────────────────────────────────────────


def test_overwrite_protection(seeded_db, tmp_path):
    _, db_path = seeded_db
    dest_sqlite = tmp_path / "corpus-v2.sqlite"
    dest_jsonl = tmp_path / "corpus-v2.jsonl"
    manifest = tmp_path / "corpus-v2.manifest.json"

    common_kwargs: dict = dict(
        db_path=db_path,
        dest_sqlite=dest_sqlite,
        dest_jsonl=dest_jsonl,
        manifest_path=manifest,
        shell_min=4,
        claude_min=4,
        browser_min=4,
        workflow_min=4,
        seed=42,
        now_ms=NOW_MS,
    )
    init_corpus_v2(**common_kwargs)
    with pytest.raises(FileExistsError):
        init_corpus_v2(**common_kwargs)


# ── 9. Probe-tag exclusion ────────────────────────────────────────────────


def test_probe_tag_excluded(tmp_db):
    conn, db_path = tmp_db
    ts = NOW_MS - 5 * DAY_MS

    _insert_shell_event(conn, timestamp=ts, command="cargo build real")
    _insert_shell_event(conn, timestamp=ts + 1000, command="cargo build probe", probe_tag="probe-1")
    _insert_claude_session(conn, start_time=ts, session_id="real-claude")
    _insert_claude_session(
        conn, start_time=ts + 1000, session_id="probe-claude", probe_tag="probe-1"
    )
    _insert_browser_event(conn, timestamp=ts, url="https://real.example/")
    _insert_browser_event(
        conn, timestamp=ts + 1000, url="https://probe.example/", probe_tag="probe-1"
    )
    conn.commit()

    entries = sample_from_hippo_db_v2(
        db_path=db_path,
        corpus_days=63,
        corpus_buckets=9,
        shell_min=10,
        claude_min=10,
        browser_min=10,
        workflow_min=0,
        seed=42,
        now_ms=NOW_MS,
    )
    contents = " ".join(e.redacted_content for e in entries)
    assert "probe" not in contents
    assert "real" in contents
    assert len(entries) == 3  # one of each non-probe source


# ── 10. Enrichment queues seeded for every event ──────────────────────────


def test_enrichment_queue_seeded(seeded_db, tmp_path):
    _, db_path = seeded_db
    dest_sqlite = tmp_path / "corpus-v2.sqlite"
    dest_jsonl = tmp_path / "corpus-v2.jsonl"
    manifest = tmp_path / "corpus-v2.manifest.json"

    init_corpus_v2(
        db_path=db_path,
        dest_sqlite=dest_sqlite,
        dest_jsonl=dest_jsonl,
        manifest_path=manifest,
        shell_min=6,
        claude_min=6,
        browser_min=6,
        workflow_min=6,
        seed=42,
        now_ms=NOW_MS,
    )

    pairs = [
        ("events", "enrichment_queue", "event_id"),
        ("claude_sessions", "claude_enrichment_queue", "claude_session_id"),
        ("browser_events", "browser_enrichment_queue", "browser_event_id"),
        ("workflow_runs", "workflow_enrichment_queue", "run_id"),
    ]
    conn = sqlite3.connect(f"file:{dest_sqlite}?mode=ro", uri=True)
    try:
        for src_table, queue_table, fk in pairs:
            (src_count,) = conn.execute(f"SELECT COUNT(*) FROM {src_table}").fetchone()
            (q_count,) = conn.execute(
                f"SELECT COUNT(*) FROM {queue_table} WHERE status='pending'"
            ).fetchone()
            assert src_count > 0, f"{src_table} should have rows"
            assert q_count == src_count, (
                f"{queue_table} pending rows ({q_count}) != {src_table} rows ({src_count})"
            )
            (matched,) = conn.execute(
                f"SELECT COUNT(*) FROM {queue_table} q JOIN {src_table} s ON q.{fk} = s.id"
            ).fetchone()
            assert matched == src_count, f"{queue_table}.{fk} did not 1:1 match {src_table}.id"
    finally:
        conn.close()
