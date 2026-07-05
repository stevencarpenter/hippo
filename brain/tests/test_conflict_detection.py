"""Tests for conflict and staleness surfacing (SNUG-127)."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from hippo_brain.agent_query import AgentQueryRequest, run_agent_query
from hippo_brain.conflict_detection import analyze_conflicts, apply_conflict_confidence_caps
from hippo_brain.retrieval_eligibility import IN_FLIGHT_SETTLE_MS
from tests.retrieval_fixtures import FakeBackend, TRUST_EVAL_SCHEMA

_NOW = int(time.time() * 1000)
_SETTLED = _NOW - IN_FLIGHT_SETTLE_MS - 60_000


def test_no_conflicts_clean_report() -> None:
    hits = [
        {
            "uuid": "a",
            "outcome": "success",
            "captured_at": _SETTLED,
            "summary": "ok",
            "evidence": [{"ref": "shell-1", "freshness": {"status": "fresh"}}],
            "confidence": {"level": "medium", "explanation": "ok"},
        }
    ]
    report = analyze_conflicts(hits)
    assert report["has_unresolved_conflicts"] is False
    assert report["conflicts"] == []
    assert report["staleness"] is None


def test_stale_only_evidence_warning() -> None:
    hits = [
        {
            "uuid": "a",
            "evidence": [
                {"ref": "shell-1", "freshness": {"status": "stale"}},
                {"ref": "shell-2", "freshness": {"status": "suppressed_idle"}},
            ],
            "confidence": {"level": "high", "explanation": "x"},
        }
    ]
    report = analyze_conflicts(hits)
    assert report["staleness"]["kind"] == "stale_evidence"
    apply_conflict_confidence_caps(hits, report)
    assert hits[0]["confidence"]["level"] == "medium"


def test_outcome_disagreement_conflict() -> None:
    hits = [
        {
            "uuid": "old",
            "outcome": "failure",
            "captured_at": _SETTLED - 100_000,
            "summary": "build failed",
            "evidence": [{"ref": "shell-1"}],
        },
        {
            "uuid": "new",
            "outcome": "success",
            "captured_at": _SETTLED,
            "summary": "build fixed",
            "evidence": [{"ref": "shell-2"}],
        },
    ]
    report = analyze_conflicts(hits)
    assert report["has_unresolved_conflicts"] is True
    assert report["conflicts"][0]["kind"] == "outcome_disagreement"
    assert len(report["conflicts"][0]["sides"]) == 2


def test_decision_contradiction_newer_vs_older() -> None:
    hits = [
        {
            "uuid": "old",
            "captured_at": _SETTLED - 50_000,
            "design_decisions": [{"chosen": "LanceDB", "considered": "sqlite-vec"}],
            "evidence": [{"ref": "claude-1"}],
        },
        {
            "uuid": "new",
            "captured_at": _SETTLED,
            "design_decisions": [{"chosen": "sqlite-vec", "considered": "LanceDB"}],
            "evidence": [{"ref": "claude-2"}],
        },
    ]
    report = analyze_conflicts(hits)
    assert report["has_unresolved_conflicts"] is True
    assert report["conflicts"][0]["kind"] == "decision_contradiction"
    sides = report["conflicts"][0]["sides"]
    assert sides[0]["chosen"] == "LanceDB"
    assert sides[1]["chosen"] == "sqlite-vec"


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(TRUST_EVAL_SCHEMA)
    c.execute(
        "INSERT INTO source_health "
        "(source, last_event_ts, consecutive_failures, events_last_24h, updated_at) "
        "VALUES ('shell', ?, 0, 1, ?), ('agentic-session-claude', ?, 0, 1, ?)",
        (_SETTLED, _NOW, _SETTLED, _NOW),
    )
    c.commit()
    return c


def test_agent_query_surfaces_conflicts(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, outcome, node_type, created_at) "
        "VALUES (1, 'fail-node', ?, 'fail embed', 'failure', 'observation', ?), "
        "       (2, 'ok-node', ?, 'ok embed', 'success', 'observation', ?)",
        (
            json.dumps({"summary": "Tests failed"}),
            _SETTLED - 10_000,
            json.dumps({"summary": "Tests passed"}),
            _SETTLED,
        ),
    )
    for node_id, event_id in ((1, 10), (2, 11)):
        conn.execute(
            "INSERT INTO events (id, timestamp, command, cwd, source_kind) "
            "VALUES (?, ?, 'cargo test', '/p', 'shell')",
            (event_id, _SETTLED - (20_000 if node_id == 1 else 0)),
        )
        conn.execute(
            "INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (?, ?)",
            (node_id, event_id),
        )
    conn.commit()

    backend = FakeBackend(knn=[(1, 0.8), (2, 0.85)], fts=[(2, 0.9), (1, 0.7)])
    req = AgentQueryRequest(query="cargo test", mode="known", limit=5)
    out = run_agent_query(conn, req, [0.1] * 8, backend=backend)

    assert out["conflicts"]["has_unresolved_conflicts"] is True
    assert "Conflict/staleness" in out["answer"]
    assert any(h["confidence"]["level"] != "high" for h in out["hits"])
