"""Tests for agent-trust eval corpus (SNUG-118)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

import pytest

from hippo_brain.retrieval import SearchResult
from hippo_brain.trust_eval import (
    TrustEvalCase,
    check_evidence,
    kinds_in_results,
    load_cases,
    run_case_search,
    validate_corpus,
)

from tests.retrieval_fixtures import TRUST_EVAL_SCHEMA, FakeBackend


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(TRUST_EVAL_SCHEMA)
    c.commit()
    try:
        yield c
    finally:
        c.close()


def test_default_corpus_validates():
    cases = load_cases()
    errors = validate_corpus(cases)
    assert errors == [], errors
    assert len(cases) >= 12
    assert any(c.pending for c in cases)


def test_check_evidence_min_distinct_kinds():
    case = TrustEvalCase.from_dict(
        {
            "id": "t",
            "question": "q",
            "evidence": {
                "source_kinds": ["shell", "browser"],
                "min_hits": 2,
                "min_distinct_source_kinds": 2,
            },
        }
    )
    results = [
        SearchResult(
            uuid="a",
            score=0.9,
            summary="s",
            embed_text="e",
            outcome=None,
            tags=[],
            cwd="/p",
            git_branch="main",
            captured_at=1,
            linked_source_ids=["shell-1"],
        ),
        SearchResult(
            uuid="b",
            score=0.8,
            summary="s",
            embed_text="e",
            outcome=None,
            tags=[],
            cwd="/p",
            git_branch="main",
            captured_at=1,
            linked_source_ids=["browser-2"],
        ),
    ]
    assert check_evidence(case, results).passed


def test_check_evidence_positive():
    case = TrustEvalCase.from_dict(
        {
            "id": "t",
            "question": "q",
            "evidence": {"source_kinds": ["shell"], "min_hits": 1, "require_captured_at": True},
        }
    )
    results = [
        SearchResult(
            uuid="u",
            score=0.9,
            summary="s",
            embed_text="e",
            outcome=None,
            tags=[],
            cwd="/p",
            git_branch="main",
            captured_at=1_700_000_000_000,
            linked_source_ids=["shell-1"],
        )
    ]
    out = check_evidence(case, results)
    assert out.passed, out.detail


def test_check_evidence_negative_gap():
    case = TrustEvalCase.from_dict(
        {
            "id": "t",
            "question": "q",
            "mode": "adversarial",
            "evidence": {
                "min_hits": 0,
                "max_hits": 0,
                "expect_coverage_gap": True,
                "gap_reason_contains": ["no evidence"],
            },
        }
    )
    out = check_evidence(case, [], gap_reason="no evidence in corpus for this claim")
    assert out.passed, out.detail


def _insert_node(
    conn: sqlite3.Connection,
    node_id: int,
    *,
    uuid: str = "uuid",
    created_at: int = 1_700_000_000_000,
) -> None:
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, created_at) "
        "VALUES (?, ?, ?, ?, 'observation', ?)",
        (node_id, uuid, json.dumps({"summary": "s"}), "embed", created_at),
    )


def _link_shell(conn: sqlite3.Connection, node_id: int, event_id: int, ts: int) -> None:
    conn.execute(
        "INSERT INTO events (id, timestamp, cwd, git_branch) VALUES (?, ?, '/hippo', 'main')",
        (event_id, ts),
    )
    conn.execute(
        "INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (?, ?)",
        (node_id, event_id),
    )


def _link_agentic(
    conn: sqlite3.Connection,
    node_id: int,
    session_id: int,
    harness: str,
    *,
    probe_tag: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO agentic_sessions (id, harness, start_time, end_time, cwd, probe_tag) "
        "VALUES (?, ?, ?, ?, '/hippo', ?)",
        (session_id, harness, 1_700_000_000_000, 1_700_000_060_000, probe_tag),
    )
    conn.execute(
        "INSERT INTO knowledge_node_agentic_sessions (knowledge_node_id, agentic_session_id) "
        "VALUES (?, ?)",
        (node_id, session_id),
    )


def _link_browser(conn: sqlite3.Connection, node_id: int, event_id: int) -> None:
    conn.execute(
        "INSERT INTO browser_events (id, timestamp, probe_tag) VALUES (?, ?, NULL)",
        (event_id, 1_700_000_000_000),
    )
    conn.execute(
        "INSERT INTO knowledge_node_browser_events (knowledge_node_id, browser_event_id) "
        "VALUES (?, ?)",
        (node_id, event_id),
    )


def _link_workflow(conn: sqlite3.Connection, node_id: int, run_id: int) -> None:
    conn.execute(
        "INSERT INTO workflow_runs (id, repo, head_sha) VALUES (?, 'hippo', 'abc')", (run_id,)
    )
    conn.execute(
        "INSERT INTO knowledge_node_workflow_runs (knowledge_node_id, run_id) VALUES (?, ?)",
        (node_id, run_id),
    )


@dataclass
class _Vec:
    v: list[float] = field(default_factory=lambda: [0.1] * 8)


@pytest.mark.parametrize(
    ("case_id", "setup"),
    [
        ("trust-shell-01", lambda c: _link_shell(c, 1, 10, 1_700_000_000_000)),
        ("trust-claude-01", lambda c: _link_agentic(c, 1, 20, "claude-code")),
        ("trust-codex-01", lambda c: _link_agentic(c, 1, 21, "codex")),
        ("trust-cursor-01", lambda c: _link_agentic(c, 1, 22, "cursor")),
        ("trust-opencode-01", lambda c: _link_agentic(c, 1, 23, "opencode")),
        ("trust-browser-01", lambda c: _link_browser(c, 1, 30)),
        ("trust-workflow-01", lambda c: _link_workflow(c, 1, 40)),
    ],
)
def test_trust_eval_retrieval_evidence(conn: sqlite3.Connection, case_id: str, setup) -> None:
    _insert_node(conn, 1)
    setup(conn)
    conn.commit()

    case = next(c for c in load_cases() if c.id == case_id)
    backend = FakeBackend(knn=[(1, 0.95)], fts=[(1, 0.9)])
    results = run_case_search(conn, case, _Vec().v, backend=backend, limit=5)
    check = check_evidence(case, results)
    assert check.passed, check.detail
    assert kinds_in_results(results)


def test_probe_sessions_excluded_from_claude_filter(conn: sqlite3.Connection) -> None:
    _insert_node(conn, 1)
    _insert_node(conn, 2, uuid="real")
    _link_agentic(conn, 1, 50, "claude-code", probe_tag="probe-canary")
    _link_agentic(conn, 2, 51, "claude-code")
    conn.commit()

    case = next(c for c in load_cases() if c.id == "trust-probe-exclude-01")
    backend = FakeBackend(knn=[(1, 0.9), (2, 0.85)], fts=[(2, 0.95), (1, 0.5)])
    results = run_case_search(conn, case, _Vec().v, backend=backend, limit=5)
    assert len(results) == 1
    assert results[0].uuid == "real"
    kinds = kinds_in_results(results)
    assert "claude" in kinds
    check = check_evidence(case, results)
    assert check.passed, check.detail


def test_mixed_source_case(conn: sqlite3.Connection) -> None:
    _insert_node(conn, 1)
    _insert_node(conn, 2, uuid="b")
    _link_shell(conn, 1, 60, 1_700_000_000_000)
    _link_browser(conn, 2, 61)
    conn.commit()

    case = next(c for c in load_cases() if c.id == "trust-mixed-01")
    backend = FakeBackend(knn=[(1, 0.9), (2, 0.88)], fts=[(1, 0.8), (2, 0.85)])
    results = run_case_search(conn, case, _Vec().v, backend=backend, limit=5)
    check = check_evidence(case, results)
    assert check.passed, check.detail
