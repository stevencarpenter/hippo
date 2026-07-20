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
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(TRUST_EVAL_SCHEMA)
    c.commit()
    try:
        yield c
    finally:
        c.close()


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
    assert pkt["freshness"]["source"] == "workflow"


def test_memory_knowledge_includes_evidence_packet(conn: sqlite3.Connection) -> None:
    _insert_node(conn, 6, uuid="memory-node")
    conn.execute(
        "INSERT INTO memory_documents (id, uuid, repository, source_path, state, updated_at) "
        "VALUES (1, 'doc-1', 'hippo', 'MEMORY.md', 'active', ?)",
        (_SETTLED_END,),
    )
    conn.execute(
        "INSERT INTO memory_revisions (id, document_id, revision_number, created_at) "
        "VALUES (10, 1, 1, ?)",
        (_SETTLED_END,),
    )
    conn.execute("UPDATE memory_documents SET active_revision_id = 10 WHERE id = 1")
    conn.execute(
        "INSERT INTO memory_chunks (id, revision_id, ordinal, heading_path, content, created_at) "
        "VALUES (100, 10, 0, 'Vector store', 'sqlite-vec consolidation notes', ?)",
        (_SETTLED_END,),
    )
    conn.execute(
        "INSERT INTO knowledge_node_memory_chunks (knowledge_node_id, memory_chunk_id) "
        "VALUES (6, 100)"
    )
    conn.execute(
        "INSERT INTO source_health "
        "(source, last_event_ts, consecutive_failures, events_last_24h, updated_at) "
        "VALUES ('claude-auto-memory', ?, 0, 1, ?)",
        (_SETTLED_END, _NOW),
    )
    conn.commit()

    backend = FakeBackend(knn=[(6, 0.87)], fts=[(6, 0.9)])
    results = search(
        conn, "sqlite-vec", [0.1] * 8, Filters(), mode="hybrid", limit=5, backend=backend
    )
    pkt = results[0].evidence[0]
    assert pkt["ref"] == "memory-100"
    assert pkt["source_kind"] == "claude-auto-memory"
    assert "Vector store" in pkt["excerpt"]


# ---------------------------------------------------------------------------
# inspect_evidence per-kind branches + the hippo-evidence-inspect CLI
# ---------------------------------------------------------------------------


def _insert_settled_agentic(conn: sqlite3.Connection, row_id: int, harness: str) -> None:
    conn.execute(
        "INSERT INTO agentic_sessions "
        "(id, session_id, harness, segment_index, start_time, end_time, cwd, git_branch, "
        " summary_text, message_count, source_file) "
        "VALUES (?, 'sess-x', ?, 0, ?, ?, '/p', 'main', 'work', 3, '/proj/s.jsonl')",
        (row_id, harness, _SETTLED_END - 1000, _SETTLED_END),
    )


def test_inspect_evidence_agentic_claude_and_codex(conn: sqlite3.Connection) -> None:
    _insert_settled_agentic(conn, 21, "claude-code")
    _insert_settled_agentic(conn, 22, "codex")
    conn.commit()

    claude = inspect_evidence(conn, "claude-21")
    assert claude["table"] == "agentic_sessions"
    assert claude["row"]["harness"] == "claude-code"

    codex = inspect_evidence(conn, "codex-22")
    assert codex["row"]["harness"] == "codex"

    # A claude ref must not resolve a codex row (harness mismatch).
    with pytest.raises(LookupError):
        inspect_evidence(conn, "claude-22")


def test_inspect_evidence_browser_workflow_memory(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO browser_events (id, timestamp, title, url, domain) "
        "VALUES (41, ?, 'WAL docs', 'https://sqlite.org/wal', 'sqlite.org')",
        (_SETTLED_END,),
    )
    conn.execute(
        "INSERT INTO workflow_runs (id, name, repo, conclusion, started_at) "
        "VALUES (51, 'CI', 'o/hippo', 'success', ?)",
        (_SETTLED_END,),
    )
    conn.execute(
        "INSERT INTO memory_documents (id, uuid, repository, source_path, state, updated_at) "
        "VALUES (2, 'doc-2', 'hippo', 'MEMORY.md', 'active', ?)",
        (_SETTLED_END,),
    )
    conn.execute(
        "INSERT INTO memory_revisions (id, document_id, revision_number, created_at) "
        "VALUES (11, 2, 1, ?)",
        (_SETTLED_END,),
    )
    conn.execute("UPDATE memory_documents SET active_revision_id = 11 WHERE id = 2")
    conn.execute(
        "INSERT INTO memory_chunks (id, revision_id, ordinal, heading_path, content, created_at) "
        "VALUES (101, 11, 0, 'Notes', 'chunk body', ?)",
        (_SETTLED_END,),
    )
    conn.commit()

    assert inspect_evidence(conn, "browser-41")["row"]["domain"] == "sqlite.org"
    assert inspect_evidence(conn, "workflow-51")["row"]["conclusion"] == "success"
    memory = inspect_evidence(conn, "memory-101")
    assert memory["row"]["content"] == "chunk body"
    assert memory["row"]["repository"] == "hippo"

    with pytest.raises(LookupError):
        inspect_evidence(conn, "browser-999")
    with pytest.raises(ValueError):
        inspect_evidence(conn, "not-a-ref-at-all !")


def test_inspect_evidence_memory_requires_active_revision(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO memory_documents (id, uuid, repository, source_path, state, updated_at) "
        "VALUES (3, 'doc-3', 'hippo', 'MEMORY.md', 'active', ?)",
        (_SETTLED_END,),
    )
    conn.execute(
        "INSERT INTO memory_revisions (id, document_id, revision_number, created_at) "
        "VALUES (12, 3, 1, ?)",
        (_SETTLED_END,),
    )
    # active_revision_id deliberately left NULL — stale chunk must not resolve.
    conn.execute(
        "INSERT INTO memory_chunks (id, revision_id, ordinal, heading_path, content, created_at) "
        "VALUES (102, 12, 0, 'Stale', 'orphan chunk', ?)",
        (_SETTLED_END,),
    )
    conn.commit()

    with pytest.raises(LookupError):
        inspect_evidence(conn, "memory-102")


class TestEvidenceInspectCli:
    def _make_db(self, tmp_path) -> str:
        db = tmp_path / "hippo.db"
        c = sqlite3.connect(db)
        c.executescript(TRUST_EVAL_SCHEMA)
        c.execute(
            "INSERT INTO events (id, timestamp, command, cwd, git_branch, source_kind) "
            "VALUES (7, ?, 'git status', '/p', 'main', 'shell')",
            (_SETTLED_END,),
        )
        c.commit()
        c.close()
        return str(db)

    def test_main_prints_row_json(self, tmp_path, capsys) -> None:
        from hippo_brain.evidence_packets import main

        rc = main(["shell-7", "--db", self._make_db(tmp_path)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["table"] == "events"
        assert payload["row"]["command"] == "git status"

    def test_main_missing_db_and_bad_ref(self, tmp_path, capsys) -> None:
        from hippo_brain.evidence_packets import main

        assert main(["shell-7", "--db", str(tmp_path / "nope.db")]) == 1
        assert "database not found" in capsys.readouterr().err

        db = self._make_db(tmp_path)
        assert main(["shell-999", "--db", db]) == 1
        assert "no eligible events row" in capsys.readouterr().err
