"""Tests for capture freshness and coverage signals (SNUG-125)."""

from __future__ import annotations

import sqlite3
import time

import pytest

from hippo_brain.retrieval import Filters, search
from hippo_brain.source_freshness import (
    attach_freshness_to_packets,
    build_freshness_snapshot,
    classify_capture_status,
    CoverageSnapshot,
)
from hippo_brain.retrieval_eligibility import IN_FLIGHT_SETTLE_MS
from tests.retrieval_fixtures import FakeBackend, TRUST_EVAL_SCHEMA

_NOW = int(time.time() * 1000)
_SETTLED = _NOW - IN_FLIGHT_SETTLE_MS - 60_000


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(TRUST_EVAL_SCHEMA)
    c.execute(
        "INSERT INTO source_health "
        "(source, last_event_ts, consecutive_failures, events_last_24h, probe_ok, updated_at) "
        "VALUES ('shell', ?, 0, 5, 1, ?)",
        (_SETTLED, _NOW),
    )
    c.commit()
    return c


def test_fresh_shell_source(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO events (id, timestamp, command, cwd, source_kind) "
        "VALUES (1, ?, 'cargo test', '/p', 'shell')",
        (_SETTLED,),
    )
    conn.commit()
    snap = build_freshness_snapshot(conn, "shell", now_ms=_NOW)
    assert snap["status"] == "fresh"
    assert snap["coverage"]["row_count"] >= 0
    assert snap["capture_health"]["probe_ok"] == 1


def test_stale_shell_source(conn: sqlite3.Connection) -> None:
    stale_ts = _NOW - (8 * 24 * 3600 * 1000)
    conn.execute(
        "UPDATE source_health SET last_event_ts = ?, events_last_24h = 0 WHERE source = 'shell'",
        (stale_ts,),
    )
    conn.execute(
        "INSERT INTO events (id, timestamp, command, cwd, source_kind) "
        "VALUES (1, ?, 'old cmd', '/p', 'shell')",
        (stale_ts,),
    )
    conn.commit()

    snap = build_freshness_snapshot(conn, "shell", now_ms=_NOW)
    assert snap["status"] == "stale"
    assert snap["stale"] is True


def test_suppressed_idle_bursty_source(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO source_health "
        "(source, last_event_ts, consecutive_failures, events_last_24h, updated_at) "
        "VALUES ('agentic-session-codex', ?, 0, 0, ?)",
        (_NOW - 4 * 24 * 3600 * 1000, _NOW),
    )
    conn.execute(
        "INSERT INTO agentic_sessions "
        "(id, session_id, harness, segment_index, start_time, end_time, cwd, "
        " summary_text, message_count, source_file) "
        "VALUES (1, 's', 'codex', 0, ?, ?, '/p', 'old work', 3, '/f.jsonl')",
        (_NOW - 5 * 24 * 3600 * 1000, _NOW - 4 * 24 * 3600 * 1000),
    )
    conn.commit()

    snap = build_freshness_snapshot(conn, "agentic-session-codex", now_ms=_NOW)
    assert snap["status"] == "suppressed_idle"
    assert snap["coverage"]["row_count"] == 1


def test_expected_absent_known_gap(conn: sqlite3.Connection) -> None:
    snap = build_freshness_snapshot(conn, "workflow", now_ms=_NOW)
    assert snap["status"] == "expected_absent"
    assert snap["coverage"]["row_count"] == 0


def test_failing_from_active_alarm(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO capture_alarms (id, invariant_id, raised_at, details_json) "
        "VALUES (1, 'I-1', ?, '{\"source\":\"shell\"}')",
        (_NOW,),
    )
    conn.commit()

    status = classify_capture_status(
        source_key="shell",
        health={"consecutive_failures": 0, "events_last_24h": 1},
        coverage=CoverageSnapshot(10, _SETTLED),
        alarms=[{"invariant_id": "I-1"}],
        now_ms=_NOW,
    )
    assert status == "failing"


def test_evidence_packets_include_inline_freshness(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, created_at) "
        "VALUES (1, 'n', '{\"summary\":\"x\"}', 'embed', 'observation', ?)",
        (_SETTLED,),
    )
    conn.execute(
        "INSERT INTO events (id, timestamp, command, cwd, source_kind) "
        "VALUES (10, ?, 'cargo test', '/p', 'shell')",
        (_SETTLED,),
    )
    conn.execute("INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (1, 10)")
    conn.commit()

    backend = FakeBackend(knn=[(1, 0.9)], fts=[(1, 0.95)])
    results = search(conn, "cargo", [0.1] * 8, Filters(), mode="hybrid", limit=5, backend=backend)
    assert results[0].evidence[0]["freshness"]["status"] == "fresh"
    assert results[0].evidence[0]["freshness"]["source"] == "shell"


def test_attach_freshness_mutates_packets(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO events (id, timestamp, command, cwd, source_kind) "
        "VALUES (1, ?, 'ls', '/p', 'shell')",
        (_SETTLED,),
    )
    conn.commit()
    packets = [{"source_kind": "shell", "ref": "shell-1"}]
    attach_freshness_to_packets(conn, packets, now_ms=_NOW)
    assert packets[0]["freshness"]["status"] == "fresh"
