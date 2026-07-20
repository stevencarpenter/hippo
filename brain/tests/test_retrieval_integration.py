"""End-to-end integration tests: retrieval.search against the REAL backend.

Unlike test_retrieval.py (fake backend), these run the full pipeline —
sqlite-vec vec0 KNN, FTS5 BM25, RRF fusion, entity expansion, recency, MMR,
detail hydration — against a real on-disk database built with the production
``vector_store`` module. Skipped automatically if the sqlite-vec extension
cannot load in this environment.
"""

from __future__ import annotations

import time

import pytest

from hippo_brain import retrieval, vector_store
from hippo_brain.retrieval import Filters, Tuning
from hippo_brain.vector_store import EMBED_DIM

from tests.retrieval_fixtures import TRUST_EVAL_SCHEMA

pytest.importorskip("sqlite_vec")

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    summary, embed_text, content,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS knowledge_nodes_fts_ai
AFTER INSERT ON knowledge_nodes BEGIN
    INSERT INTO knowledge_fts (rowid, summary, embed_text, content)
    VALUES (
        NEW.id,
        COALESCE(CASE WHEN json_valid(NEW.content) THEN json_extract(NEW.content, '$.summary') END, ''),
        NEW.embed_text,
        NEW.content
    );
END;
"""

NO_RECENCY = Tuning(recency_half_life_days=0)


def _basis(axis: int, dim: int = EMBED_DIM) -> list[float]:
    """Unit vector along one axis — exact cosine distances for assertions."""
    v = [0.0] * dim
    v[axis] = 1.0
    return v


def _blend(a: list[float], b: list[float], wa: float, wb: float) -> list[float]:
    return [wa * x + wb * y for x, y in zip(a, b)]


@pytest.fixture
def conn(tmp_path):
    c = vector_store.open_conn(tmp_path / "hippo.db")
    c.executescript(TRUST_EVAL_SCHEMA)
    c.executescript(_FTS_SCHEMA)
    yield c
    c.close()


def _insert_node(
    conn,
    node_id: int,
    *,
    summary: str,
    embed_text: str,
    vec_knowledge: list[float],
    vec_command: list[float] | None = None,
    created_at: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, created_at)"
        " VALUES (?, ?, json_object('summary', ?), ?, ?)",
        (
            node_id,
            f"uuid-{node_id}",
            summary,
            embed_text,
            created_at if created_at is not None else int(time.time() * 1000),
        ),
    )
    vector_store.insert_vectors(
        conn, node_id, vec_knowledge, vec_command if vec_command is not None else vec_knowledge
    )
    conn.commit()


def test_hybrid_fuses_real_knn_and_fts_arms(conn):
    # Node 1: vector-near the query, no keyword overlap.
    _insert_node(
        conn,
        1,
        summary="daemon socket work",
        embed_text="unix socket length prefixed frames",
        vec_knowledge=_basis(0),
    )
    # Node 2: keyword match for the query, vector-far.
    _insert_node(
        conn,
        2,
        summary="watchdog invariants",
        embed_text="watchdog capture_alarms invariant sweep",
        vec_knowledge=_basis(5),
    )

    results = retrieval.search(
        conn,
        "watchdog invariant sweep",
        _basis(0),
        Filters(),
        mode="hybrid",
        limit=5,
        tuning=NO_RECENCY,
    )

    uuids = [r.uuid for r in results]
    assert set(uuids) == {"uuid-1", "uuid-2"}
    # Top score is exactly 1.0 (normalization invariant) and all in [0, 1].
    assert results[0].score == pytest.approx(1.0)
    assert all(0.0 <= r.score <= 1.0 for r in results)


def test_lexical_entity_expansion_end_to_end(conn):
    # The node only ever mentions the canonical hyphenated spelling.
    _insert_node(
        conn,
        1,
        summary="vector store migration",
        embed_text="loaded sqlite-vec extension for the knowledge_vectors table",
        vec_knowledge=_basis(0),
    )
    conn.execute(
        "INSERT INTO entities (id, type, name, canonical) VALUES (1, 'tool', 'sqlite-vec', 'sqlitevec')"
    )
    conn.commit()

    # Query uses the alias spelling; FTS5 alone can't bridge "sqlitevec" to
    # the indexed tokens [sqlite, vec] — only entity expansion can. (No other
    # query token may share a porter stem with the document, or the miss-case
    # would match without expansion.)
    hit = retrieval.search(conn, "sqlitevec setup", None, Filters(), mode="lexical", limit=5)
    assert [r.uuid for r in hit] == ["uuid-1"]

    miss = retrieval.search(
        conn,
        "sqlitevec setup",
        None,
        Filters(),
        mode="lexical",
        limit=5,
        tuning=Tuning(entity_expansion=False),
    )
    assert miss == []


def test_command_arm_lifts_command_only_matches(conn):
    query_vec = _basis(0)
    # Node 1: prose vector mildly related to the query; command vector far.
    _insert_node(
        conn,
        1,
        summary="prose node",
        embed_text="general notes",
        vec_knowledge=_blend(_basis(0), _basis(3), 0.5, 0.5),
        vec_command=_basis(7),
    )
    # Node 2: prose vector far, but its command embedding matches the query.
    _insert_node(
        conn,
        2,
        summary="command node",
        embed_text="other notes",
        vec_knowledge=_basis(5),
        vec_command=_basis(0),
    )

    with_arm = retrieval.search(
        conn,
        "",
        query_vec,
        Filters(),
        mode="hybrid",
        limit=5,
        tuning=Tuning(command_weight=5.0, recency_half_life_days=0),
    )
    without_arm = retrieval.search(
        conn,
        "",
        query_vec,
        Filters(),
        mode="hybrid",
        limit=5,
        tuning=Tuning(command_weight=0.0, recency_half_life_days=0),
    )

    assert [r.uuid for r in without_arm][0] == "uuid-1"
    assert [r.uuid for r in with_arm][0] == "uuid-2"


def test_project_filter_pushdown_with_real_backend(conn):
    _insert_node(conn, 1, summary="keep", embed_text="alpha work", vec_knowledge=_basis(0))
    _insert_node(conn, 2, summary="drop", embed_text="alpha work", vec_knowledge=_basis(1))
    conn.execute(
        "INSERT INTO events (id, timestamp, cwd, git_repo, git_branch)"
        " VALUES (10, 1000, '/work/hippo', 'o/hippo', 'main')"
    )
    conn.execute("INSERT INTO knowledge_node_events VALUES (1, 10)")
    conn.execute(
        "INSERT INTO events (id, timestamp, cwd, git_repo, git_branch)"
        " VALUES (11, 1000, '/other/repo', 'o/other', 'main')"
    )
    conn.execute("INSERT INTO knowledge_node_events VALUES (2, 11)")
    conn.commit()

    results = retrieval.search(
        conn,
        "alpha work",
        _basis(0),
        Filters(project="hippo"),
        mode="hybrid",
        limit=5,
        tuning=NO_RECENCY,
    )
    assert [r.uuid for r in results] == ["uuid-1"]
    # Detail hydration ran against the real link tables.
    assert results[0].cwd == "/work/hippo"
    assert results[0].linked_source_ids == ["shell-10"]


def test_recency_prior_reorders_on_real_backend(conn):
    now = int(time.time() * 1000)
    _insert_node(
        conn,
        1,
        summary="stale",
        embed_text="beta topic",
        vec_knowledge=_basis(0),
        created_at=now - 400 * 86_400_000,
    )
    _insert_node(
        conn,
        2,
        summary="fresh",
        embed_text="beta topic",
        vec_knowledge=_basis(0),
        created_at=now - 3_600_000,
    )

    results = retrieval.search(conn, "beta topic", _basis(0), Filters(), mode="hybrid", limit=2)
    assert [r.uuid for r in results] == ["uuid-2", "uuid-1"]
    assert results[0].score == pytest.approx(1.0)
