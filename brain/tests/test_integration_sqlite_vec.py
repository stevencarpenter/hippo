"""Integration tests for the sqlite-vec / FTS5 retrieval stack.

Covers the full pipeline end-to-end against an ephemeral SQLite database:

1. Build a minimal v6-shaped schema (knowledge_nodes + FTS5 triggers + vec0).
2. Insert a knowledge_node + matching fts5 / vec0 rows.
3. Call ``retrieval.search()`` in every mode and assert contract invariants.
4. Call ``mcp_queries.shape_semantic_results`` and assert uuid + linked events
   are present.
5. Call ``rag.ask`` with a mocked client that raises on chat and assert the
   degraded response preserves sources.

This is a reviewer-written test — it exercises the integration surface rather
than any single owner's module, so failures here map cleanly to cross-team
regressions.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hippo_brain import rag, retrieval, vector_store
from hippo_brain.mcp_queries import shape_semantic_results
from hippo_brain.retrieval import Filters

EMBED_DIM = vector_store.EMBED_DIM


_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "crates" / "hippo-core" / "src" / "schema.sql"


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Apply the production v6 schema to an empty connection.

    We replay the full schema.sql rather than run the Rust migration binding
    so this test stays Python-only. ``vector_store.open_conn`` has already
    created the vec0 virtual table.
    """
    sql = _SCHEMA_PATH.read_text()
    conn.executescript(sql)


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = vector_store.open_conn(tmp_path / "hippo.db")
    _apply_schema(conn)
    return conn


def _insert_node(
    conn: sqlite3.Connection,
    *,
    node_id: int,
    uuid: str,
    summary: str,
    embed_text: str,
    tags: list[str],
    vec: list[float],
    outcome: str = "success",
) -> None:
    content = json.dumps({"summary": summary})
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, outcome, tags, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (node_id, uuid, content, embed_text, outcome, json.dumps(tags), 1000, 1000),
    )
    vector_store.insert_vectors(conn, node_id, vec, vec)
    conn.commit()


def _unit_vec(index: int) -> list[float]:
    v = [0.0] * EMBED_DIM
    v[index % EMBED_DIM] = 1.0
    return v


# ---------------------------------------------------------------------------
# retrieval.search
# ---------------------------------------------------------------------------


def test_search_all_modes_return_shape_and_valid_scores(db: sqlite3.Connection) -> None:
    _insert_node(
        db,
        node_id=1,
        uuid="aaaa-1",
        summary="sqlite-vec hybrid retrieval",
        embed_text="hybrid retrieval combines vec0 knn and fts5 bm25",
        tags=["retrieval", "sqlite-vec"],
        vec=_unit_vec(0),
    )
    _insert_node(
        db,
        node_id=2,
        uuid="bbbb-2",
        summary="lancedb removal",
        embed_text="lancedb removed in favor of sqlite-vec",
        tags=["lancedb"],
        vec=_unit_vec(1),
    )

    for mode in ("semantic", "lexical", "hybrid", "recent"):
        results = retrieval.search(
            db,
            query="hybrid retrieval",
            query_vec=_unit_vec(0),
            mode=mode,
            limit=5,
        )
        assert isinstance(results, list), f"{mode}: expected list"
        uuids = [r.uuid for r in results]
        assert len(uuids) == len(set(uuids)), f"{mode}: duplicates leaked"
        for r in results:
            assert 0.0 <= r.score <= 1.0, f"{mode}: score {r.score} outside [0,1]"
            assert r.uuid, f"{mode}: missing uuid"


def test_search_hybrid_filter_project_requires_link(db: sqlite3.Connection) -> None:
    # node 1: linked to event under /repo/hippo
    _insert_node(
        db,
        node_id=1,
        uuid="aaaa",
        summary="linked node",
        embed_text="relevant",
        tags=[],
        vec=_unit_vec(0),
    )
    db.execute(
        "INSERT OR IGNORE INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, 0, 'zsh', 'h', 'u')"
    )
    # retrieval._apply_filters uses ``f"{project}%"`` (prefix LIKE) and does
    # NOT check ``git_repo`` — inconsistent with mcp_queries which uses
    # ``%project%`` against both cwd and git_repo. See scorecard.
    # We use a cwd starting with "hippo" so this test passes today.
    db.execute(
        "INSERT INTO events (id, session_id, command, duration_ms, "
        "hostname, shell, cwd, git_repo, timestamp) "
        "VALUES (1, 1, 'ls', 0, 'h', 'zsh', 'hippo/repo', 'hippo', 1000)"
    )
    db.execute("INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (1, 1)")
    # node 2: no link — should be filtered out
    _insert_node(
        db,
        node_id=2,
        uuid="bbbb",
        summary="orphan node",
        embed_text="relevant",
        tags=[],
        vec=_unit_vec(0),
    )
    db.commit()

    hits = retrieval.search(
        db,
        query="relevant",
        query_vec=_unit_vec(0),
        filters=Filters(project="hippo"),
        mode="hybrid",
        limit=5,
    )
    assert [h.uuid for h in hits] == ["aaaa"], "project filter should keep only linked node"


# ---------------------------------------------------------------------------
# mcp_queries shaping
# ---------------------------------------------------------------------------


def test_shape_semantic_results_exposes_uuid_and_linked_events(db: sqlite3.Connection) -> None:
    _insert_node(
        db,
        node_id=7,
        uuid="node-7-uuid",
        summary="s",
        embed_text="e",
        tags=["t"],
        vec=_unit_vec(0),
    )
    db.execute(
        "INSERT OR IGNORE INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, 0, 'zsh', 'h', 'u')"
    )
    db.execute(
        "INSERT INTO events (id, session_id, command, duration_ms, "
        "hostname, shell, cwd, timestamp) "
        "VALUES (101, 1, 'ls', 0, 'h', 'zsh', '/tmp', 1000)"
    )
    db.execute("INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (7, 101)")
    db.commit()

    shaped = shape_semantic_results(
        [{"id": 7, "summary": "s", "_distance": 0.2, "tags": ["t"]}],
        conn=db,
    )
    assert len(shaped) == 1
    r = shaped[0]
    assert r["uuid"] == "node-7-uuid"
    assert r["linked_event_ids"] == [101]
    assert 0.0 <= r["score"] <= 1.0


# ---------------------------------------------------------------------------
# rag.ask degraded path
# ---------------------------------------------------------------------------


class _TimeoutClient:
    base_url = "http://localhost:1234"

    async def health_check(self, model: str):
        return {"ok": True}

    async def embed(self, texts, model):
        # Return a usable embedding so retrieval actually runs.
        return [_unit_vec(0) for _ in texts]

    async def chat(self, *a, **kw):
        raise TimeoutError("chat timed out")


@pytest.mark.asyncio
async def test_rag_ask_degraded_mode_returns_sources_on_chat_timeout(
    db: sqlite3.Connection,
) -> None:
    _insert_node(
        db,
        node_id=1,
        uuid="rag-source",
        summary="hippo retrieval stack",
        embed_text="hippo retrieval stack over sqlite-vec",
        tags=["rag"],
        vec=_unit_vec(0),
    )

    result = await rag.ask(
        # Avoid FTS5 special chars like '?' — retrieval does not sanitize
        # free-text queries before MATCH (known gap; flagged in scorecard).
        question="retrieval stack",
        lm_client=_TimeoutClient(),
        vector_table=db,
        query_model="qwen",
        embedding_model="embed",
        limit=5,
        filters=Filters(since_ms=0),  # forces the filtered retrieval.search path
        conn=db,
    )
    assert result["degraded"] is True
    assert result["answer"] is None
    assert result["sources"], "degraded mode must still return retrieved sources"
    assert any("rag-source" == s.get("uuid") for s in result["sources"])
