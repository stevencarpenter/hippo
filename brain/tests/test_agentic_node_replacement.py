"""Tests for idempotent agentic-node enrichment (Fix B + Fix C).

`replace_prior_agentic_nodes` requires the vec0 `knowledge_vectors` table, so
these use a real sqlite-vec connection (vector_store.open_conn + schema.sql),
mirroring test_integration_sqlite_vec.py.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from hippo_brain import vector_store
from hippo_brain.claude_sessions import (
    _drop_already_enriched_claude_segments,
    replace_prior_agentic_nodes,
    write_claude_knowledge_node,
)
from hippo_brain.models import EnrichmentResult
from hippo_brain.opencode_sessions import write_opencode_knowledge_node

_SCHEMA = Path(__file__).resolve().parents[2] / "crates" / "hippo-core" / "src" / "schema.sql"
EMBED_DIM = vector_store.EMBED_DIM


@pytest.fixture
def vdb(tmp_path):
    """Full schema + vec0 connection."""
    conn = vector_store.open_conn(tmp_path / "hippo.db")
    conn.executescript(_SCHEMA.read_text())
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _session(conn, session_id, harness="claude-code", segment_index=0) -> int:
    cur = conn.execute(
        "INSERT INTO agentic_sessions "
        "(session_id, harness, segment_index, project_dir, cwd, summary_text, "
        " start_time, end_time, content_hash, last_enriched_content_hash) "
        "VALUES (?, ?, ?, '/p', '/p', 's', 0, 0, NULL, NULL)",
        (session_id, harness, segment_index),
    )
    conn.commit()
    return cur.lastrowid


def _node(conn, content="{}", embed_text="e", node_type="observation") -> int:
    cur = conn.execute(
        "INSERT INTO knowledge_nodes (uuid, content, embed_text, node_type) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), content, embed_text, node_type),
    )
    conn.commit()
    return cur.lastrowid


def _link(conn, node_id, session_id):
    conn.execute(
        "INSERT INTO knowledge_node_agentic_sessions (knowledge_node_id, agentic_session_id) "
        "VALUES (?, ?)",
        (node_id, session_id),
    )
    conn.commit()


def _has_vector(conn, node_id) -> bool:
    return (
        conn.execute(
            "SELECT COUNT(*) FROM knowledge_vectors WHERE knowledge_node_id = ?",
            (node_id,),
        ).fetchone()[0]
        > 0
    )


# ── replace_prior_agentic_nodes (Fix B core) ─────────────────────────────────


def test_replace_prior_deletes_node_links_and_vector(vdb):
    sess = _session(vdb, "s1")
    prior = _node(vdb, '{"summary":"old"}', "old embed")
    _link(vdb, prior, sess)
    vector_store.insert_vectors(vdb, prior, [0.1] * EMBED_DIM, [0.1] * EMBED_DIM)
    vdb.commit()
    assert _has_vector(vdb, prior)

    n = replace_prior_agentic_nodes(vdb, [sess])
    vdb.commit()

    assert n == 1
    assert (
        vdb.execute("SELECT COUNT(*) FROM knowledge_nodes WHERE id=?", (prior,)).fetchone()[0] == 0
    )
    assert not _has_vector(vdb, prior)
    assert (
        vdb.execute(
            "SELECT COUNT(*) FROM knowledge_node_agentic_sessions WHERE knowledge_node_id=?",
            (prior,),
        ).fetchone()[0]
        == 0
    )
    assert vdb.execute("PRAGMA foreign_key_check").fetchall() == []


def test_replace_prior_spares_node_covering_another_segment(vdb):
    s1 = _session(vdb, "s-a", segment_index=0)
    s2 = _session(vdb, "s-a", segment_index=1)
    shared = _node(vdb)
    _link(vdb, shared, s1)
    _link(vdb, shared, s2)  # also covers a segment we are NOT re-enriching

    vdb.execute("BEGIN")
    n = replace_prior_agentic_nodes(vdb, [s1])
    vdb.commit()

    assert n == 0
    assert (
        vdb.execute("SELECT COUNT(*) FROM knowledge_nodes WHERE id=?", (shared,)).fetchone()[0] == 1
    )


def test_replace_prior_no_prior_returns_zero(vdb):
    sess = _session(vdb, "empty")
    vdb.execute("BEGIN")
    assert replace_prior_agentic_nodes(vdb, [sess]) == 0
    vdb.commit()


def test_replace_prior_spares_change_outcome_workflow_nodes(vdb):
    """Workflow co-links its change_outcome (CI-outcome) nodes into
    knowledge_node_agentic_sessions. Replacement must delete ONLY the agentic
    writer's own observation node, never a co-linked change_outcome node — even
    when that change_outcome node is solely linked to the re-enriched segment.
    Regression guard for BLOCKER-1 (node_type-blind deletion of CI nodes)."""
    sess = _session(vdb, "s1")
    obs = _node(vdb, '{"summary":"obs"}', "e-obs", node_type="observation")
    co = _node(vdb, '{"summary":"ci run"}', "e-ci", node_type="change_outcome")
    _link(vdb, obs, sess)
    _link(vdb, co, sess)  # change_outcome solely linked to this one segment

    n = replace_prior_agentic_nodes(vdb, [sess])
    vdb.commit()

    assert n == 1  # only the observation node was replaced
    assert vdb.execute("SELECT COUNT(*) FROM knowledge_nodes WHERE id=?", (obs,)).fetchone()[0] == 0
    assert vdb.execute("SELECT COUNT(*) FROM knowledge_nodes WHERE id=?", (co,)).fetchone()[0] == 1


def test_replace_prior_raises_without_vec0(tmp_db):
    """On a connection without sqlite-vec, refuse rather than orphan a vector."""
    conn, _ = tmp_db
    sess = _session(conn, "s1")
    prior = _node(conn)
    _link(conn, prior, sess)
    conn.execute("BEGIN")
    with pytest.raises(RuntimeError, match="orphan vectors"):
        replace_prior_agentic_nodes(conn, [sess])
    conn.rollback()


def test_write_claude_replaces_on_reenrichment(vdb):
    """Re-enriching the same segment keeps exactly one node (the latest)."""
    sess = _session(vdb, "s1")
    result1 = EnrichmentResult(summary="first", intent="i", outcome="success", embed_text="e1")
    write_claude_knowledge_node(vdb, result1, [sess], "model-x")
    assert vdb.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0] == 1

    result2 = EnrichmentResult(summary="second", intent="i", outcome="success", embed_text="e2")
    n2 = write_claude_knowledge_node(vdb, result2, [sess], "model-x")

    # Exactly one node remains and it holds the SECOND enrichment's content.
    # (SQLite may reuse the deleted rowid, so the new id may equal the old —
    # identity is by content, not id.)
    remaining = vdb.execute("SELECT id, content FROM knowledge_nodes").fetchall()
    assert len(remaining) == 1
    assert remaining[0][0] == n2
    assert "second" in remaining[0][1]
    assert "first" not in remaining[0][1]
    # The segment still links to exactly the surviving node.
    links = vdb.execute(
        "SELECT knowledge_node_id FROM knowledge_node_agentic_sessions WHERE agentic_session_id=?",
        (sess,),
    ).fetchall()
    assert [r[0] for r in links] == [n2]
    assert vdb.execute("PRAGMA foreign_key_check").fetchall() == []


# ── _drop_already_enriched_claude_segments (Fix C) ───────────────────────────


def _queue(conn, session_id, status="processing"):
    conn.execute(
        "INSERT INTO agentic_enrichment_queue (session_id, status, enqueued_at, updated_at) "
        "VALUES (?, ?, 0, 0)",
        (session_id, status),
    )
    conn.commit()


def test_drop_already_enriched_skips_matching_hash(vdb):
    sess = _session(vdb, "s1")
    _queue(vdb, sess)
    segs = [{"id": sess, "content_hash": "abc", "last_enriched_content_hash": "abc"}]

    kept = _drop_already_enriched_claude_segments(vdb, segs)

    assert kept == []  # already enriched at this content ⇒ dropped
    assert (
        vdb.execute(
            "SELECT status FROM agentic_enrichment_queue WHERE session_id=?", (sess,)
        ).fetchone()[0]
        == "done"
    )
    assert (
        vdb.execute("SELECT enriched FROM agentic_sessions WHERE id=?", (sess,)).fetchone()[0] == 1
    )


def test_drop_already_enriched_keeps_changed_hash(vdb):
    sess = _session(vdb, "s1")
    _queue(vdb, sess)
    segs = [{"id": sess, "content_hash": "new", "last_enriched_content_hash": "old"}]

    kept = _drop_already_enriched_claude_segments(vdb, segs)

    assert len(kept) == 1
    assert (
        vdb.execute(
            "SELECT status FROM agentic_enrichment_queue WHERE session_id=?", (sess,)
        ).fetchone()[0]
        == "processing"
    )


def test_drop_already_enriched_keeps_null_hash(vdb):
    """A NULL content_hash (legacy row) must never be treated as 'unchanged'."""
    sess = _session(vdb, "s1")
    _queue(vdb, sess)
    segs = [{"id": sess, "content_hash": None, "last_enriched_content_hash": None}]

    kept = _drop_already_enriched_claude_segments(vdb, segs)

    assert len(kept) == 1


def test_write_opencode_advances_last_enriched_hash(vdb):
    """The opencode writer must advance last_enriched_content_hash so the
    daemon's re-enqueue gate can suppress unchanged re-polls."""
    sess = _session(vdb, "oc1", harness="opencode")
    _queue(vdb, sess)
    result = EnrichmentResult(summary="s", intent="i", outcome="success", embed_text="e")

    write_opencode_knowledge_node(vdb, result, [sess], "m", content_hashes=["hash123"])

    leh = vdb.execute(
        "SELECT last_enriched_content_hash FROM agentic_sessions WHERE id=?", (sess,)
    ).fetchone()[0]
    assert leh == "hash123"
