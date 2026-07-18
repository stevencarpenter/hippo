"""Tests for compact agent query API (SNUG-124)."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from hippo_brain.agent_query import AgentQueryRequest, run_agent_query
from hippo_brain.retrieval_eligibility import IN_FLIGHT_SETTLE_MS
from tests.retrieval_fixtures import FakeBackend, TRUST_EVAL_SCHEMA

_NOW = int(time.time() * 1000)
_SETTLED_END = _NOW - IN_FLIGHT_SETTLE_MS - 60_000

_SOURCE_HEALTH_ROW = (
    "INSERT INTO source_health "
    "(source, last_event_ts, consecutive_failures, events_last_24h, probe_ok, updated_at) "
    "VALUES ('shell', ?, 0, 3, 1, ?)"
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(TRUST_EVAL_SCHEMA)
    c.execute(_SOURCE_HEALTH_ROW, (_SETTLED_END, _NOW))
    c.commit()
    try:
        yield c
    finally:
        c.close()


def _insert_shell_node(conn: sqlite3.Connection, node_id: int = 1) -> None:
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, created_at) "
        "VALUES (?, 'shell-node', ?, 'cargo test embed', 'observation', ?)",
        (node_id, json.dumps({"summary": "Ran cargo tests"}), _SETTLED_END),
    )
    conn.execute(
        "INSERT INTO events (id, timestamp, command, cwd, git_branch, source_kind) "
        "VALUES (10, ?, 'cargo test -p hippo-core', '/p', 'main', 'shell')",
        (_SETTLED_END,),
    )
    conn.execute(
        "INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (?, 10)",
        (node_id,),
    )


def _insert_agentic_node(conn: sqlite3.Connection, node_id: int = 2) -> None:
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, created_at) "
        "VALUES (?, 'agent-node', ?, 'session embed', 'observation', ?)",
        (
            node_id,
            json.dumps(
                {
                    "summary": "Chose sqlite-vec over LanceDB",
                    "design_decisions": [
                        {
                            "considered": "LanceDB",
                            "chosen": "sqlite-vec",
                            "reason": "single DB",
                        }
                    ],
                }
            ),
            _SETTLED_END,
        ),
    )
    conn.execute(
        "INSERT INTO agentic_sessions "
        "(id, session_id, harness, segment_index, start_time, end_time, cwd, git_branch, "
        " summary_text, message_count, source_file) "
        "VALUES (20, 'sess-1', 'claude-code', 0, ?, ?, '/p', 'main', "
        " 'Discussed vector store', 5, '/proj/session.jsonl')",
        (_SETTLED_END - 1000, _SETTLED_END),
    )
    conn.execute(
        "INSERT INTO knowledge_node_agentic_sessions (knowledge_node_id, agentic_session_id) "
        "VALUES (?, 20)",
        (node_id,),
    )


def test_known_mode_includes_evidence_and_freshness(conn: sqlite3.Connection) -> None:
    _insert_shell_node(conn)
    conn.commit()

    backend = FakeBackend(knn=[(1, 0.9)], fts=[(1, 0.95)])
    req = AgentQueryRequest(query="cargo", mode="known", limit=5)
    out = run_agent_query(conn, req, [0.1] * 8, backend=backend)

    assert out["mode"] == "known"
    assert out["hits"]
    assert out["hits"][0]["evidence"]
    assert out["hits"][0]["evidence"][0]["ref"] == "shell-10"
    assert "shell" in out["freshness"]
    assert out["freshness"]["shell"]["status"] == "fresh"
    assert out["freshness"]["shell"]["capture_health"]["probe_ok"] == 1
    assert out["hits"][0]["evidence"][0]["freshness"]["status"] == "fresh"


def test_source_filter_shell_excludes_agentic(conn: sqlite3.Connection) -> None:
    _insert_shell_node(conn, 1)
    _insert_agentic_node(conn, 2)
    conn.commit()

    backend = FakeBackend(knn=[(1, 0.7), (2, 0.95)], fts=[(2, 0.99), (1, 0.5)])
    req = AgentQueryRequest(query="store", mode="known", source="shell", limit=5)
    out = run_agent_query(conn, req, [0.1] * 8, backend=backend)

    assert len(out["hits"]) == 1
    assert out["hits"][0]["uuid"] == "shell-node"


def test_empty_results(conn: sqlite3.Connection) -> None:
    backend = FakeBackend(knn=[], fts=[])
    req = AgentQueryRequest(query="missing topic", mode="known")
    out = run_agent_query(conn, req, [0.1] * 8, backend=backend)
    assert out["hits"] == []
    assert "No matching knowledge" in out["answer"]


def test_decisions_mode_filters_design_decisions(conn: sqlite3.Connection) -> None:
    _insert_shell_node(conn, 1)
    _insert_agentic_node(conn, 2)
    conn.commit()

    backend = FakeBackend(knn=[(1, 0.9), (2, 0.88)], fts=[(2, 0.92), (1, 0.7)])
    req = AgentQueryRequest(query="vector", mode="decisions", limit=5)
    out = run_agent_query(conn, req, [0.1] * 8, backend=backend)

    assert len(out["hits"]) == 1
    assert out["hits"][0]["uuid"] == "agent-node"
    assert out["hits"][0]["design_decisions"]


def test_evidence_mode_answer_describes_packets(conn: sqlite3.Connection) -> None:
    _insert_agentic_node(conn)
    conn.commit()

    backend = FakeBackend(knn=[(2, 0.9)], fts=[(2, 0.9)])
    req = AgentQueryRequest(query="sqlite", mode="evidence", limit=5)
    out = run_agent_query(conn, req, [0.1] * 8, backend=backend)

    assert "evidence packet" in out["answer"].lower()
    assert out["hits"][0]["evidence"][0]["source_kind"] == "claude"


def test_recent_mode_uses_recent_retrieval(conn: sqlite3.Connection, monkeypatch) -> None:
    _insert_shell_node(conn)
    conn.commit()

    captured: list[str] = []

    def _spy_search(*_args, mode: str = "hybrid", **_kwargs):
        captured.append(mode)
        return []

    monkeypatch.setattr("hippo_brain.agent_query.search", _spy_search)
    req = AgentQueryRequest(query="cargo", mode="recent", limit=5)
    run_agent_query(conn, req, [0.1] * 8, backend=FakeBackend())

    assert captured == ["recent"]


def test_freshness_marks_stale_source(conn: sqlite3.Connection) -> None:
    _insert_shell_node(conn)
    stale_ts = int(time.time() * 1000) - (25 * 3600 * 1000)
    conn.execute("UPDATE source_health SET last_event_ts = ?", (stale_ts,))
    conn.commit()

    backend = FakeBackend(knn=[(1, 0.9)], fts=[(1, 0.95)])
    req = AgentQueryRequest(query="cargo", mode="known", limit=5)
    out = run_agent_query(conn, req, [0.1] * 8, backend=backend)

    assert out["freshness"]["shell"]["status"] == "stale"
    assert out["freshness"]["shell"]["stale"] is True
