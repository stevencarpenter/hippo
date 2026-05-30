"""Tests for write-time content dedup in the workflow + browser enrichers.

These writers' duplication is *cross-source* — distinct runs/visits producing
byte-identical enrichment. `find_identical_node` lets the writer link the new
source key onto the existing node instead of minting a duplicate (returns None
so the server skips embedding). See AP-13.
"""

from __future__ import annotations

import json
from pathlib import Path

from hippo_brain import vector_store
from hippo_brain.browser_enrichment import write_browser_knowledge_node
from hippo_brain.claude_sessions import find_identical_node
from hippo_brain.workflow_enrichment import write_workflow_knowledge_node

_SCHEMA = Path(__file__).resolve().parents[2] / "crates" / "hippo-core" / "src" / "schema.sql"


def _db(tmp_path):
    conn = vector_store.open_conn(tmp_path / "hippo.db")
    conn.executescript(_SCHEMA.read_text())
    conn.commit()
    return conn


def _run(conn, run_id) -> int:
    """Insert a minimal workflow_runs row; return run_id."""
    conn.execute(
        "INSERT INTO workflow_runs (id, repo, run_id, status, conclusion, created_at, updated_at) "
        "VALUES (?, 'o/r', ?, 'completed', 'success', 0, 0)",
        (run_id, run_id),
    )
    conn.commit()
    return run_id


def _node_count(conn, node_type) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM knowledge_nodes WHERE node_type = ?", (node_type,)
    ).fetchone()[0]


# ── find_identical_node ──────────────────────────────────────────────────────


def test_find_identical_node_matches_full_triple(tmp_path):
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO knowledge_nodes (uuid, content, embed_text, node_type) "
        "VALUES ('u1', '{\"s\":1}', 'e', 'change_outcome')"
    )
    conn.commit()
    assert find_identical_node(conn, '{"s":1}', "e", "change_outcome") is not None
    # different embed_text / node_type / content => no match
    assert find_identical_node(conn, '{"s":1}', "OTHER", "change_outcome") is None
    assert find_identical_node(conn, '{"s":1}', "e", "observation") is None
    assert find_identical_node(conn, '{"s":2}', "e", "change_outcome") is None


# ── workflow ─────────────────────────────────────────────────────────────────

_WF_RESULT = {
    "summary": "Created a GitHub release for hippo v0.20.4",
    "intent": "release",
    "outcome": "success",
    "entities": {},
    "tags": [],
    "embed_text": "github release hippo v0.20.4",
}


def test_workflow_distinct_runs_same_content_collapse(tmp_path):
    conn = _db(tmp_path)
    r1, r2 = _run(conn, 1), _run(conn, 2)

    n1 = write_workflow_knowledge_node(conn, r1, _WF_RESULT, "m")
    n2 = write_workflow_knowledge_node(conn, r2, dict(_WF_RESULT), "m")

    assert n1 is not None
    assert n2 is None  # second run linked onto the existing node, no new mint
    assert _node_count(conn, "change_outcome") == 1
    # BOTH runs link to the surviving node.
    runs = {
        r[0]
        for r in conn.execute(
            "SELECT run_id FROM knowledge_node_workflow_runs WHERE knowledge_node_id = ?",
            (n1,),
        )
    }
    assert runs == {1, 2}


def test_workflow_different_content_mints_new_node(tmp_path):
    conn = _db(tmp_path)
    r1, r2 = _run(conn, 1), _run(conn, 2)
    write_workflow_knowledge_node(conn, r1, _WF_RESULT, "m")
    other = dict(_WF_RESULT, summary="Tests failed on macos", embed_text="ci failure macos")
    n2 = write_workflow_knowledge_node(conn, r2, other, "m")
    assert n2 is not None
    assert _node_count(conn, "change_outcome") == 2


def test_workflow_same_run_still_idempotent(tmp_path):
    conn = _db(tmp_path)
    r1 = _run(conn, 1)
    write_workflow_knowledge_node(conn, r1, _WF_RESULT, "m")
    # Re-enriching the SAME run is skipped by the deterministic-uuid guard.
    assert write_workflow_knowledge_node(conn, r1, _WF_RESULT, "m") is None
    assert _node_count(conn, "change_outcome") == 1


# ── browser ──────────────────────────────────────────────────────────────────


def _browser_event(conn, eid) -> int:
    conn.execute(
        "INSERT INTO browser_events (id, url, title, domain, visited_at, captured_at) "
        "VALUES (?, 'https://x', 't', 'x', 0, 0)",
        (eid,),
    )
    conn.commit()
    return eid


_BR_RESULT = {
    "summary": "Read the sqlite-vec docs",
    "intent": "research",
    "outcome": "success",
    "entities": {},
    "tags": [],
    "embed_text": "sqlite-vec docs vec0",
}


def test_browser_identical_content_collapses(tmp_path):
    conn = _db(tmp_path)
    e1, e2 = _browser_event(conn, 1), _browser_event(conn, 2)

    n1 = write_browser_knowledge_node(conn, _BR_RESULT, [e1], "m")
    n2 = write_browser_knowledge_node(conn, dict(_BR_RESULT), [e2], "m")

    assert n1 is not None
    assert n2 is None
    assert _node_count(conn, "observation") == 1
    events = {
        r[0]
        for r in conn.execute(
            "SELECT browser_event_id FROM knowledge_node_browser_events WHERE knowledge_node_id = ?",
            (n1,),
        )
    }
    assert events == {1, 2}
