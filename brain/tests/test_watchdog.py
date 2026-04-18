"""Tests for the enrichment queue watchdog (R-22).

Covers:
  - `reap_stale_locks` flips stale `processing` rows back to `pending`,
    increments `retry_count`, and promotes rows that hit `max_retries` to
    `failed`. Fresh locks are untouched.
  - `preflight_lm_studio` blocks when LM Studio is unreachable, when no
    chat models are loaded, and (when fallback is disabled) when the
    preferred model isn't loaded.
  - `claim_pending_events_by_session`, `claim_pending_claude_segments`, and
    `claim_pending_workflow_runs` each respect `max_claim_batch` so one
    cycle can't vacuum the whole backlog.
"""

import sqlite3
import time

import pytest

from hippo_brain.claude_sessions import (
    SessionSegment,
    claim_pending_claude_segments,
    insert_segment,
)
from hippo_brain.enrichment import claim_pending_events_by_session
from hippo_brain.watchdog import (
    DEFAULT_LOCK_TIMEOUT_MS,
    preflight_lm_studio,
    reap_stale_locks,
)
from hippo_brain.workflow_enrichment import claim_pending_workflow_runs


def _insert_event(conn, event_id: int, session_id: int = 1, ts: int | None = None):
    ts = ts if ts is not None else int(time.time() * 1000)
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (?, ?, 'zsh', 'laptop', 'user')",
        (session_id, ts),
    )
    conn.execute(
        """INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms,
                               cwd, hostname, shell)
           VALUES (?, ?, ?, 'cmd', 0, 10, '/p', 'laptop', 'zsh')""",
        (event_id, session_id, ts),
    )


def _seed_processing_queue_row(
    conn,
    event_id: int,
    locked_at_ms: int,
    retry_count: int = 0,
    max_retries: int = 5,
    worker_id: str = "wedged-worker",
):
    """Seed a queue row in 'processing' state with a controlled locked_at."""
    _insert_event(conn, event_id=event_id)
    conn.execute(
        """INSERT INTO enrichment_queue
           (event_id, status, locked_at, locked_by, retry_count, max_retries,
            created_at, updated_at)
           VALUES (?, 'processing', ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            locked_at_ms,
            worker_id,
            retry_count,
            max_retries,
            locked_at_ms,
            locked_at_ms,
        ),
    )
    conn.commit()


def test_reaper_flips_stale_processing_to_pending(tmp_db):
    conn, _ = tmp_db
    now_ms = int(time.time() * 1000)
    stale_at = now_ms - (DEFAULT_LOCK_TIMEOUT_MS + 60_000)
    _seed_processing_queue_row(conn, event_id=1, locked_at_ms=stale_at)

    result = reap_stale_locks(conn, lock_timeout_ms=DEFAULT_LOCK_TIMEOUT_MS, now_ms=now_ms)

    assert result["shell"] == 1
    row = conn.execute(
        "SELECT status, locked_at, locked_by, retry_count FROM enrichment_queue WHERE event_id = 1"
    ).fetchone()
    assert row[0] == "pending"
    assert row[1] is None
    assert row[2] is None
    assert row[3] == 1


def test_reaper_ignores_fresh_locks(tmp_db):
    conn, _ = tmp_db
    now_ms = int(time.time() * 1000)
    fresh_at = now_ms - 30_000  # only 30s old
    _seed_processing_queue_row(conn, event_id=1, locked_at_ms=fresh_at)

    result = reap_stale_locks(conn, lock_timeout_ms=DEFAULT_LOCK_TIMEOUT_MS, now_ms=now_ms)

    assert result["shell"] == 0
    row = conn.execute(
        "SELECT status, retry_count FROM enrichment_queue WHERE event_id = 1"
    ).fetchone()
    assert row[0] == "processing"
    assert row[1] == 0


def test_reaper_marks_row_failed_after_max_retries(tmp_db):
    conn, _ = tmp_db
    now_ms = int(time.time() * 1000)
    stale_at = now_ms - (DEFAULT_LOCK_TIMEOUT_MS + 1000)
    # retry_count=2 with max_retries=3: reap bumps to 3 and transitions to 'failed'.
    _seed_processing_queue_row(
        conn, event_id=1, locked_at_ms=stale_at, retry_count=2, max_retries=3
    )

    reap_stale_locks(conn, lock_timeout_ms=DEFAULT_LOCK_TIMEOUT_MS, now_ms=now_ms)

    row = conn.execute(
        "SELECT status, retry_count FROM enrichment_queue WHERE event_id = 1"
    ).fetchone()
    assert row[0] == "failed"
    assert row[1] == 3


def test_reaper_attaches_error_message(tmp_db):
    conn, _ = tmp_db
    now_ms = int(time.time() * 1000)
    stale_at = now_ms - (DEFAULT_LOCK_TIMEOUT_MS + 1000)
    _seed_processing_queue_row(conn, event_id=1, locked_at_ms=stale_at)

    reap_stale_locks(conn, lock_timeout_ms=DEFAULT_LOCK_TIMEOUT_MS, now_ms=now_ms)

    err = conn.execute("SELECT error_message FROM enrichment_queue WHERE event_id = 1").fetchone()[
        0
    ]
    assert "stale lock" in err


def test_reaper_respects_pending_rows(tmp_db):
    """A row already in 'pending' with a stale locked_at is not transitioned."""
    conn, _ = tmp_db
    now_ms = int(time.time() * 1000)
    _insert_event(conn, event_id=1)
    conn.execute(
        """INSERT INTO enrichment_queue
           (event_id, status, locked_at, created_at, updated_at)
           VALUES (?, 'pending', ?, ?, ?)""",
        (1, now_ms - (DEFAULT_LOCK_TIMEOUT_MS + 1000), now_ms, now_ms),
    )
    conn.commit()

    result = reap_stale_locks(conn, lock_timeout_ms=DEFAULT_LOCK_TIMEOUT_MS, now_ms=now_ms)
    assert result["shell"] == 0


def test_reaper_swallows_missing_table_error(tmp_db):
    """'no such table' OperationalError → debug-logged, count=0, no raise."""
    conn, _ = tmp_db
    # Drop one of the queue tables to simulate an old schema.
    conn.execute("DROP TABLE browser_enrichment_queue")
    conn.commit()

    now_ms = int(time.time() * 1000)
    result = reap_stale_locks(conn, lock_timeout_ms=DEFAULT_LOCK_TIMEOUT_MS, now_ms=now_ms)

    assert result["browser"] == 0
    # Other queues that exist should still run.
    assert "shell" in result


def test_reaper_propagates_non_missing_table_operational_error():
    """OperationalErrors other than 'no such table' must propagate."""

    class _LockedConn:
        def execute(self, *_a, **_kw):
            raise sqlite3.OperationalError("database is locked")

        def commit(self):
            pass

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        reap_stale_locks(_LockedConn(), lock_timeout_ms=DEFAULT_LOCK_TIMEOUT_MS)


class _FakeClient:
    """Stand-in for LMStudioClient that only needs list_models()."""

    def __init__(self, models=None, error=None):
        self._models = models or []
        self._error = error

    async def list_models(self):
        if self._error:
            raise self._error
        return list(self._models)


@pytest.mark.asyncio
async def test_preflight_blocks_when_unreachable():
    client = _FakeClient(error=ConnectionError("connection refused"))
    decision = await preflight_lm_studio(client, preferred_model="qwen-test")
    assert decision.proceed is False
    assert decision.reason == "unreachable"
    assert "connection refused" in (decision.error or "")


@pytest.mark.asyncio
async def test_preflight_blocks_when_no_models_loaded():
    client = _FakeClient(models=[])
    decision = await preflight_lm_studio(client, preferred_model="qwen-test")
    assert decision.proceed is False
    assert decision.reason == "no_models"


@pytest.mark.asyncio
async def test_preflight_blocks_when_only_embedding_models_loaded():
    # The embedding-hint filter should strip these out and leave no chat models.
    client = _FakeClient(models=["text-embedding-nomic-embed-text-v2", "modernbert-base"])
    decision = await preflight_lm_studio(client, preferred_model="qwen-test")
    assert decision.proceed is False
    assert decision.reason == "no_models"


@pytest.mark.asyncio
async def test_preflight_ok_when_preferred_model_loaded():
    client = _FakeClient(models=["qwen-test", "text-embedding-nomic"])
    decision = await preflight_lm_studio(client, preferred_model="qwen-test")
    assert decision.proceed is True
    assert decision.reason == "ok"
    assert "qwen-test" in decision.loaded_models


@pytest.mark.asyncio
async def test_preflight_falls_back_when_preferred_missing():
    client = _FakeClient(models=["some-other-chat-model"])
    decision = await preflight_lm_studio(client, preferred_model="qwen-test", allow_fallback=True)
    assert decision.proceed is True
    assert decision.reason == "fallback"


@pytest.mark.asyncio
async def test_preflight_blocks_when_preferred_missing_and_fallback_disabled():
    client = _FakeClient(models=["some-other-chat-model"])
    decision = await preflight_lm_studio(client, preferred_model="qwen-test", allow_fallback=False)
    assert decision.proceed is False
    assert decision.reason == "model_missing"


def test_claim_respects_max_claim_batch(tmp_db):
    conn, _ = tmp_db
    past_ms = int(time.time() * 1000) - 10_000

    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (past_ms,),
    )
    # Seed 20 events in one session; cap claim at 5.
    for i in range(1, 21):
        conn.execute(
            """INSERT INTO events (id, session_id, timestamp, command, exit_code,
                                   duration_ms, cwd, hostname, shell)
               VALUES (?, 1, ?, 'cmd', 0, 100, '/p', 'laptop', 'zsh')""",
            (i, past_ms + i),
        )
        conn.execute("INSERT INTO enrichment_queue (event_id) VALUES (?)", (i,))
    conn.commit()

    chunks = claim_pending_events_by_session(
        conn,
        max_per_chunk=50,
        worker_id="test",
        stale_secs=1,
        max_claim_batch=5,
    )

    claimed_ids = [e["id"] for chunk in chunks for e in chunk]
    assert len(claimed_ids) == 5

    # Remaining 15 rows should still be pending for the next cycle.
    pending = conn.execute(
        "SELECT COUNT(*) FROM enrichment_queue WHERE status = 'pending'"
    ).fetchone()[0]
    assert pending == 15

    processing = conn.execute(
        "SELECT COUNT(*) FROM enrichment_queue WHERE status = 'processing'"
    ).fetchone()[0]
    assert processing == 5


def test_claim_unbounded_when_max_claim_batch_none(tmp_db):
    conn, _ = tmp_db
    past_ms = int(time.time() * 1000) - 10_000

    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (past_ms,),
    )
    for i in range(1, 8):
        conn.execute(
            """INSERT INTO events (id, session_id, timestamp, command, exit_code,
                                   duration_ms, cwd, hostname, shell)
               VALUES (?, 1, ?, 'cmd', 0, 100, '/p', 'laptop', 'zsh')""",
            (i, past_ms + i),
        )
        conn.execute("INSERT INTO enrichment_queue (event_id) VALUES (?)", (i,))
    conn.commit()

    chunks = claim_pending_events_by_session(
        conn, max_per_chunk=50, worker_id="test", stale_secs=1, max_claim_batch=None
    )
    claimed_ids = [e["id"] for chunk in chunks for e in chunk]
    assert len(claimed_ids) == 7


def _insert_claude_segment(conn, session_id: str, cwd: str, idx: int = 0) -> int | None:
    seg = SessionSegment(
        session_id=session_id,
        project_dir="proj",
        cwd=cwd,
        git_branch=None,
        segment_index=idx,
        start_time=1000 + idx * 1000,
        end_time=2000 + idx * 1000,
        user_prompts=[f"prompt {idx}"],
        message_count=1,
        source_file="/tmp/test.jsonl",
    )
    return insert_segment(conn, seg)


def test_claim_claude_respects_max_claim_batch(tmp_db):
    """claim_pending_claude_segments caps across all cwd groups."""
    conn, _ = tmp_db
    for i in range(8):
        _insert_claude_segment(conn, f"s{i}", f"/proj-{i % 3}", idx=i)

    batches = claim_pending_claude_segments(conn, "test", max_claim_batch=3)
    assert len(batches) == 3

    processing = conn.execute(
        "SELECT COUNT(*) FROM claude_enrichment_queue WHERE status = 'processing'"
    ).fetchone()[0]
    assert processing == 3

    pending = conn.execute(
        "SELECT COUNT(*) FROM claude_enrichment_queue WHERE status = 'pending'"
    ).fetchone()[0]
    assert pending == 5


def test_claim_claude_unbounded_when_max_claim_batch_none(tmp_db):
    """claim_pending_claude_segments claims everything when cap is None."""
    conn, _ = tmp_db
    for i in range(5):
        _insert_claude_segment(conn, f"s{i}", f"/proj-{i}", idx=i)

    batches = claim_pending_claude_segments(conn, "test", max_claim_batch=None)
    assert len(batches) == 5


def _insert_workflow_run(conn, run_id: int) -> None:
    now = int(time.time() * 1000)
    conn.execute(
        """INSERT INTO workflow_runs
           (id, repo, head_sha, head_branch, event, status, conclusion,
            html_url, raw_json, first_seen_at, last_seen_at, started_at)
           VALUES (?, 'me/r', 'abc', 'main', 'push', 'completed', 'failure',
                   'https://x', '{}', ?, ?, ?)""",
        (run_id, now, now, now),
    )
    conn.execute(
        "INSERT INTO workflow_enrichment_queue (run_id, status, enqueued_at, updated_at) "
        "VALUES (?, 'pending', ?, ?)",
        (run_id, now, now),
    )


def test_claim_workflow_respects_max_claim_batch(tmp_db):
    """claim_pending_workflow_runs caps claimed rows per cycle."""
    conn, _ = tmp_db
    for i in range(1, 11):
        _insert_workflow_run(conn, i)
    conn.commit()

    claimed = claim_pending_workflow_runs(conn, "test", max_claim_batch=4)
    assert len(claimed) == 4

    processing = conn.execute(
        "SELECT COUNT(*) FROM workflow_enrichment_queue WHERE status = 'processing'"
    ).fetchone()[0]
    assert processing == 4

    pending = conn.execute(
        "SELECT COUNT(*) FROM workflow_enrichment_queue WHERE status = 'pending'"
    ).fetchone()[0]
    assert pending == 6


def test_claim_workflow_unbounded_when_max_claim_batch_none(tmp_db):
    """claim_pending_workflow_runs claims everything when cap is None."""
    conn, _ = tmp_db
    for i in range(1, 7):
        _insert_workflow_run(conn, i)
    conn.commit()

    claimed = claim_pending_workflow_runs(conn, "test", max_claim_batch=None)
    assert len(claimed) == 6
