"""Tests for centralized retrieval eligibility policy (SNUG-121)."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

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
        (node_id, uuid, json.dumps({"summary": "s"}), "embed text", _SETTLED_END),
    )


def _link_agentic(
    conn: sqlite3.Connection,
    node_id: int,
    session_id: int,
    *,
    harness: str = "claude-code",
    source_file: str = "/proj/session.jsonl",
    message_count: int = 5,
    summary_text: str = "real work",
    end_time: int = _SETTLED_END,
    probe_tag: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO agentic_sessions "
        "(id, session_id, harness, project_dir, cwd, segment_index, start_time, end_time, "
        " summary_text, message_count, source_file, probe_tag) "
        "VALUES (?, ?, ?, '/p', '/p', 0, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            f"sess-{session_id}",
            harness,
            _SETTLED_END - 1000,
            end_time,
            summary_text,
            message_count,
            source_file,
            probe_tag,
        ),
    )
    conn.execute(
        "INSERT INTO knowledge_node_agentic_sessions (knowledge_node_id, agentic_session_id) "
        "VALUES (?, ?)",
        (node_id, session_id),
    )


def _link_workflow(
    conn: sqlite3.Connection,
    node_id: int,
    run_id: int,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
) -> None:
    conn.execute(
        "INSERT INTO workflow_runs (id, repo, head_sha, status, conclusion, started_at) "
        "VALUES (?, 'hippo', 'abc', ?, ?, ?)",
        (run_id, status, conclusion, _SETTLED_END),
    )
    conn.execute(
        "INSERT INTO knowledge_node_workflow_runs (knowledge_node_id, run_id) VALUES (?, ?)",
        (node_id, run_id),
    )


def test_workflow_journal_session_excluded(conn: sqlite3.Connection) -> None:
    _insert_node(conn, 1)
    _insert_node(conn, 2, uuid="good")
    journal = "/p/parent/subagents/workflows/wf/journal.jsonl"
    _link_agentic(conn, 1, 10, source_file=journal)
    _link_agentic(conn, 2, 11)
    conn.commit()

    backend = FakeBackend(knn=[(1, 0.9), (2, 0.85)], fts=[(2, 0.95), (1, 0.5)])
    results = search(conn, "work", [0.1] * 8, Filters(), mode="hybrid", limit=5, backend=backend)
    uuids = {r.uuid for r in results}
    assert "good" in uuids
    assert "n" not in uuids


def test_in_flight_agentic_session_excluded(conn: sqlite3.Connection) -> None:
    _insert_node(conn, 1)
    _insert_node(conn, 2, uuid="settled")
    _link_agentic(conn, 1, 20, end_time=_NOW - 1000)
    _link_agentic(conn, 2, 21, end_time=_SETTLED_END)
    conn.commit()

    backend = FakeBackend(knn=[(1, 0.9), (2, 0.88)], fts=[(2, 0.9), (1, 0.7)])
    results = search(conn, "work", [0.1] * 8, Filters(), mode="hybrid", limit=5, backend=backend)
    assert {r.uuid for r in results} == {"settled"}


def test_empty_stub_agentic_session_excluded(conn: sqlite3.Connection) -> None:
    _insert_node(conn, 1)
    _insert_node(conn, 2, uuid="real")
    _link_agentic(conn, 1, 30, message_count=0, summary_text="   ")
    _link_agentic(conn, 2, 31)
    conn.commit()

    backend = FakeBackend(knn=[(1, 0.92), (2, 0.9)], fts=[(2, 0.95), (1, 0.6)])
    results = search(conn, "work", [0.1] * 8, Filters(), mode="hybrid", limit=5, backend=backend)
    assert {r.uuid for r in results} == {"real"}


def test_in_progress_workflow_run_excluded(conn: sqlite3.Connection) -> None:
    _insert_node(conn, 1)
    _insert_node(conn, 2, uuid="done")
    _link_workflow(conn, 1, 40, status="in_progress", conclusion=None)
    _link_workflow(conn, 2, 41)
    conn.commit()

    backend = FakeBackend(knn=[(1, 0.9), (2, 0.88)], fts=[(2, 0.92), (1, 0.5)])
    results = search(conn, "ci", [0.1] * 8, Filters(), mode="hybrid", limit=5, backend=backend)
    assert {r.uuid for r in results} == {"done"}


def test_include_excluded_operator_mode(conn: sqlite3.Connection) -> None:
    _insert_node(conn, 1)
    journal = "/p/parent/subagents/workflows/wf/journal.jsonl"
    _link_agentic(conn, 1, 50, source_file=journal)
    conn.commit()

    backend = FakeBackend(knn=[(1, 0.95)], fts=[(1, 0.9)])
    results = search(
        conn,
        "work",
        [0.1] * 8,
        Filters(include_excluded=True),
        mode="hybrid",
        limit=5,
        backend=backend,
    )
    assert len(results) == 1
    assert results[0].uuid == "n"


def test_probe_tagged_session_excluded(conn: sqlite3.Connection) -> None:
    _insert_node(conn, 1)
    _insert_node(conn, 2, uuid="clean")
    _link_agentic(conn, 1, 60, probe_tag="canary")
    _link_agentic(conn, 2, 61)
    conn.commit()

    backend = FakeBackend(knn=[(1, 0.95), (2, 0.9)], fts=[(2, 0.92), (1, 0.7)])
    results = search(conn, "work", [0.1] * 8, Filters(), mode="semantic", limit=5, backend=backend)
    assert {r.uuid for r in results} == {"clean"}


def test_include_excluded_from_env(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    monkeypatch.setenv("HIPPO_RETRIEVAL_INCLUDE_EXCLUDED", "1")
    _insert_node(conn, 1)
    journal = "/p/parent/subagents/workflows/wf/journal.jsonl"
    _link_agentic(conn, 1, 70, source_file=journal)
    conn.commit()

    backend = FakeBackend(knn=[(1, 0.95)], fts=[(1, 0.9)])
    results = search(conn, "work", [0.1] * 8, Filters(), mode="hybrid", limit=5, backend=backend)
    assert len(results) == 1
    assert results[0].uuid == "n"
