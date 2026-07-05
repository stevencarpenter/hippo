"""Tests for inspectable evidence packets on retrieval (SNUG-123)."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from hippo_brain.evidence_packets import inspect_evidence, parse_ref
from hippo_brain.retrieval import Filters, search
from hippo_brain.retrieval_eligibility import IN_FLIGHT_SETTLE_MS
from tests.retrieval_fixtures import FakeBackend, TRUST_EVAL_SCHEMA

_NOW = int(time.time() * 1000)
_SETTLED_END = _NOW - IN_FLIGHT_SETTLE_MS - 60_000


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(TRUST_EVAL_SCHEMA)
    c.commit()
    return c


def _insert_node(conn: sqlite3.Connection, node_id: int, *, uuid: str = "n") -> None:
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, created_at) "
        "VALUES (?, ?, ?, ?, 'observation', ?)",
        (node_id, uuid, json.dumps({"summary": "work summary"}), "embed text", _SETTLED_END),
    )


def test_parse_ref_roundtrip() -> None:
    kind, row_id = parse_ref("claude-42")
    assert kind == "claude"
    assert row_id == 42


def test_shell_knowledge_includes_evidence_packet(conn: sqlite3.Connection) -> None:
    _insert_node(conn, 1, uuid="shell-node")
    conn.execute(
        "INSERT INTO events (id, timestamp, command, cwd, git_branch, source_kind) "
        "VALUES (10, ?, 'cargo test -p hippo-core', '/p', 'main', 'shell')",
        (_SETTLED_END,),
    )
    conn.execute("INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (1, 10)")
    conn.commit()

    backend = FakeBackend(knn=[(1, 0.9)], fts=[(1, 0.95)])
    results = search(conn, "cargo", [0.1] * 8, Filters(), mode="hybrid", limit=5, backend=backend)
    assert len(results) == 1
    assert len(results[0].evidence) == 1
    pkt = results[0].evidence[0]
    assert pkt["ref"] == "shell-10"
    assert pkt["source_kind"] == "shell"
    assert pkt["table"] == "events"
    assert pkt["row_id"] == 10
    assert "cargo test" in pkt["excerpt"]
    assert pkt["rank"] == 0
    assert pkt["retrieval_score"] == results[0].score


def test_agentic_knowledge_includes_evidence_packet(conn: sqlite3.Connection) -> None:
    _insert_node(conn, 2, uuid="agentic-node")
    conn.execute(
        "INSERT INTO agentic_sessions "
        "(id, session_id, harness, segment_index, start_time, end_time, cwd, git_branch, "
        " summary_text, message_count, source_file) "
        "VALUES (20, 'sess-abc', 'claude-code', 1, ?, ?, '/p', 'feat/x', "
        " 'Refactored retrieval eligibility', 8, '/proj/session.jsonl')",
        (_SETTLED_END - 1000, _SETTLED_END),
    )
    conn.execute(
        "INSERT INTO knowledge_node_agentic_sessions (knowledge_node_id, agentic_session_id) "
        "VALUES (2, 20)"
    )
    conn.commit()

    backend = FakeBackend(knn=[(2, 0.88)], fts=[(2, 0.92)])
    results = search(
        conn, "eligibility", [0.1] * 8, Filters(), mode="hybrid", limit=5, backend=backend
    )
    assert len(results) == 1
    assert len(results[0].evidence) == 1
    pkt = results[0].evidence[0]
    assert pkt["ref"] == "claude-20"
    assert pkt["source_kind"] == "claude"
    assert pkt["table"] == "agentic_sessions"
    assert pkt["session_id"] == "sess-abc"
    assert pkt["harness"] == "claude-code"
    assert pkt["segment_index"] == 1
    assert "Refactored retrieval" in pkt["excerpt"]


def test_probe_shell_row_excluded_from_evidence(conn: sqlite3.Connection) -> None:
    _insert_node(conn, 3)
    conn.execute(
        "INSERT INTO events (id, timestamp, command, cwd, probe_tag) "
        "VALUES (30, ?, 'hippo probe canary', '/p', 'probe-uuid')",
        (_SETTLED_END,),
    )
    conn.execute("INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (3, 30)")
    conn.commit()

    backend = FakeBackend(knn=[(3, 0.99)], fts=[(3, 0.99)])
    results = search(conn, "probe", [0.1] * 8, Filters(), mode="hybrid", limit=5, backend=backend)
    assert results == []


def test_inspect_evidence_returns_raw_row(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO events (id, timestamp, command, cwd, source_kind) "
        "VALUES (40, ?, 'git status', '/p', 'shell')",
        (_SETTLED_END,),
    )
    conn.commit()

    payload = inspect_evidence(conn, "shell-40")
    assert payload["table"] == "events"
    assert payload["row"]["command"] == "git status"


def test_inspect_evidence_rejects_probe_without_operator_mode(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO events (id, timestamp, command, cwd, probe_tag) "
        "VALUES (50, ?, 'probe cmd', '/p', 'tag')",
        (_SETTLED_END,),
    )
    conn.commit()

    with pytest.raises(LookupError):
        inspect_evidence(conn, "shell-50", include_excluded=False)

    payload = inspect_evidence(conn, "shell-50", include_excluded=True)
    assert payload["row"]["probe_tag"] == "tag"


def test_browser_knowledge_includes_evidence_packet(conn: sqlite3.Connection) -> None:
    _insert_node(conn, 4, uuid="browser-node")
    conn.execute(
        "INSERT INTO browser_events (id, timestamp, title, url, domain) "
        "VALUES (40, ?, 'SQLite WAL', 'https://sqlite.org/wal', 'sqlite.org')",
        (_SETTLED_END,),
    )
    conn.execute(
        "INSERT INTO knowledge_node_browser_events (knowledge_node_id, browser_event_id) "
        "VALUES (4, 40)"
    )
    conn.execute(
        "INSERT INTO source_health "
        "(source, last_event_ts, consecutive_failures, events_last_24h, updated_at) "
        "VALUES ('browser', ?, 0, 1, ?)",
        (_SETTLED_END, _NOW),
    )
    conn.commit()

    backend = FakeBackend(knn=[(4, 0.9)], fts=[(4, 0.9)])
    results = search(conn, "sqlite", [0.1] * 8, Filters(), mode="hybrid", limit=5, backend=backend)
    pkt = results[0].evidence[0]
    assert pkt["ref"] == "browser-40"
    assert pkt["source_kind"] == "browser"
    assert pkt["freshness"]["source"] == "browser"


def test_workflow_knowledge_includes_evidence_packet(conn: sqlite3.Connection) -> None:
    _insert_node(conn, 5, uuid="workflow-node")
    conn.execute(
        "INSERT INTO workflow_runs (id, name, repo, conclusion, started_at) "
        "VALUES (50, 'CI', 'stevencarpenter/hippo', 'success', ?)",
        (_SETTLED_END,),
    )
    conn.execute(
        "INSERT INTO knowledge_node_workflow_runs (knowledge_node_id, run_id) VALUES (5, 50)"
    )
    conn.execute(
        "INSERT INTO source_health "
        "(source, last_event_ts, consecutive_failures, events_last_24h, updated_at) "
        "VALUES ('workflow', ?, 0, 1, ?)",
        (_SETTLED_END, _NOW),
    )
    conn.commit()

    backend = FakeBackend(knn=[(5, 0.85)], fts=[(5, 0.88)])
    results = search(conn, "CI", [0.1] * 8, Filters(), mode="hybrid", limit=5, backend=backend)
    pkt = results[0].evidence[0]
    assert pkt["ref"] == "workflow-50"
    assert pkt["source_kind"] == "workflow"
    assert "hippo" in pkt["excerpt"]
