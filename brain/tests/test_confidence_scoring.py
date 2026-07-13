"""Tests for explainable confidence scoring (SNUG-126)."""

from __future__ import annotations

import sqlite3
import time

import pytest

from hippo_brain.confidence_scoring import assess_confidence
from hippo_brain.retrieval import Filters, search
from hippo_brain.retrieval_eligibility import IN_FLIGHT_SETTLE_MS
from tests.retrieval_fixtures import FakeBackend, TRUST_EVAL_SCHEMA

_NOW = int(time.time() * 1000)
_SETTLED = _NOW - IN_FLIGHT_SETTLE_MS - 60_000


def _freshness(status: str = "fresh") -> dict:
    return {"source": "shell", "status": status, "stale": status in {"stale", "failing"}}


def test_insufficient_without_evidence() -> None:
    out = assess_confidence(retrieval_score=0.95, evidence=[], captured_at=_SETTLED, now_ms=_NOW)
    assert out["level"] == "insufficient"
    assert out["withheld"] is True
    assert out["score"] <= 0.2
    assert "withheld" in out["explanation"].lower()


def test_low_stale_capture_health() -> None:
    evidence = [
        {"source_kind": "shell", "ref": "shell-1", "freshness": _freshness("stale")},
    ]
    out = assess_confidence(
        retrieval_score=0.4,
        evidence=evidence,
        captured_at=_NOW - 40 * 24 * 3600 * 1000,
        now_ms=_NOW,
    )
    assert out["level"] == "low"
    assert out["withheld"] is False


def test_medium_single_fresh_evidence() -> None:
    evidence = [{"source_kind": "shell", "ref": "shell-2", "freshness": _freshness("fresh")}]
    out = assess_confidence(
        retrieval_score=0.62,
        evidence=evidence,
        cwd="/projects/hippo",
        git_branch="main",
        captured_at=_SETTLED,
        now_ms=_NOW,
    )
    assert out["level"] == "medium"
    assert len(out["factors"]) >= 5
    assert out["explanation"]


def test_high_multi_source_fresh_evidence() -> None:
    evidence = [
        {"source_kind": "shell", "ref": "shell-1", "freshness": _freshness("fresh")},
        {"source_kind": "claude", "ref": "claude-2", "freshness": _freshness("fresh")},
        {"source_kind": "browser", "ref": "browser-3", "freshness": _freshness("fresh")},
    ]
    out = assess_confidence(
        retrieval_score=0.88,
        evidence=evidence,
        cwd="/projects/hippo",
        git_branch="main",
        captured_at=_SETTLED,
        now_ms=_NOW,
    )
    assert out["level"] == "high"
    assert out["score"] >= 0.72


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(TRUST_EVAL_SCHEMA)
    c.execute(
        "INSERT INTO source_health "
        "(source, last_event_ts, consecutive_failures, events_last_24h, updated_at) "
        "VALUES ('shell', ?, 0, 2, ?)",
        (_SETTLED, _NOW),
    )
    c.commit()
    try:
        yield c
    finally:
        c.close()


def test_search_attaches_confidence(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, created_at) "
        "VALUES (1, 'n', '{\"summary\":\"cargo\"}', 'cargo embed', 'observation', ?)",
        (_SETTLED,),
    )
    conn.execute(
        "INSERT INTO events (id, timestamp, command, cwd, git_branch, source_kind) "
        "VALUES (10, ?, 'cargo test', '/p', 'main', 'shell')",
        (_SETTLED,),
    )
    conn.execute("INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (1, 10)")
    conn.commit()

    backend = FakeBackend(knn=[(1, 0.9)], fts=[(1, 0.95)])
    results = search(conn, "cargo", [0.1] * 8, Filters(), mode="hybrid", limit=5, backend=backend)
    assert results[0].confidence
    assert results[0].confidence["level"] in {"medium", "high", "low"}
    assert results[0].confidence["explanation"]
