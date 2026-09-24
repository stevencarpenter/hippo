"""Tests for explainable confidence scoring (SNUG-126)."""

from __future__ import annotations

import sqlite3
import time

import pytest

from hippo_brain.confidence_scoring import assess_confidence
from hippo_brain.retrieval import Filters, search
from hippo_brain.retrieval_eligibility import IN_FLIGHT_SETTLE_MS
from tests.retrieval_fixtures import TRUST_EVAL_SCHEMA, FakeBackend

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
        score_semantics="recency_adjusted_cosine",
    )
    assert out["level"] == "high"
    assert out["score"] >= 0.72


def test_relative_rank_cannot_establish_high_confidence() -> None:
    evidence = [
        {"source_kind": kind, "freshness": _freshness("fresh")}
        for kind in ("shell", "claude", "browser")
    ]
    out = assess_confidence(
        retrieval_score=1.0,
        evidence=evidence,
        cwd="/hippo",
        git_branch="main",
        captured_at=_SETTLED,
        now_ms=_NOW,
    )
    assert out["level"] == "medium"
    assert out["relevance_calibrated"] is False
    assert out["score"] < 0.72
    assert "does not establish relevance" in out["explanation"]
    match = next(f for f in out["factors"] if f["name"] == "retrieval_match")
    assert match["contribution"] == 0.0


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


def test_opposite_vector_cannot_become_strong_match_through_rrf(conn: sqlite3.Connection) -> None:
    """An unrelated nearest neighbor still ranks first; rank is not relevance."""
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, created_at) "
        "VALUES (1, 'font', '{\"summary\":\"Installed a terminal font\"}', 'terminal font', ?)",
        (_SETTLED,),
    )
    conn.execute(
        "INSERT INTO events (id, timestamp, command, cwd, git_branch) "
        "VALUES (1, ?, 'brew install font', '/hippo', 'main')",
        (_SETTLED,),
    )
    conn.execute("INSERT INTO knowledge_node_events VALUES (1, 1)")
    results = search(
        conn,
        "What Postgres migration replaced sqlite-vec?",
        [1.0, 0.0],
        backend=FakeBackend(knn=[(1, 2.0)]),
        now_ms=_NOW,
    )
    assert results[0].score == 1.0  # retain backwards-compatible ranking
    assert results[0].score_semantics == "relative_rank"
    assert results[0].evidence[0]["score_semantics"] == "relative_rank"
    confidence = results[0].confidence
    assert confidence["level"] != "high"
    assert "strong lexical/semantic match" not in confidence["explanation"]
    match = next(f for f in confidence["factors"] if f["name"] == "retrieval_match")
    assert match["score"] == 0.0
