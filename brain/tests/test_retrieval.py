"""Unit tests for hippo_brain.retrieval.

These tests drive retrieval.search() with a fake vec0/FTS5 backend and a tiny
in-memory SQLite database that mirrors the shape of the real schema's
knowledge_nodes + link tables. That lets us verify filter pushdown, RRF
merging, MMR diversification, and score normalization without depending on
storage agent's sqlite-vec work.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

import pytest

from hippo_brain.retrieval import (
    MMR_LAMBDA,
    RRF_K,
    Filters,
    SearchResult,
    search,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


SCHEMA = """
CREATE TABLE knowledge_nodes (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL,
    content TEXT NOT NULL,
    embed_text TEXT NOT NULL,
    node_type TEXT NOT NULL DEFAULT 'observation',
    outcome TEXT,
    tags TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    timestamp INTEGER NOT NULL,
    cwd TEXT NOT NULL,
    git_branch TEXT
);
CREATE TABLE knowledge_node_events (
    knowledge_node_id INTEGER NOT NULL,
    event_id INTEGER NOT NULL,
    PRIMARY KEY (knowledge_node_id, event_id)
);
CREATE TABLE claude_sessions (
    id INTEGER PRIMARY KEY,
    start_time INTEGER NOT NULL,
    cwd TEXT NOT NULL,
    git_branch TEXT
);
CREATE TABLE knowledge_node_claude_sessions (
    knowledge_node_id INTEGER NOT NULL,
    claude_session_id INTEGER NOT NULL,
    PRIMARY KEY (knowledge_node_id, claude_session_id)
);
CREATE TABLE browser_events (
    id INTEGER PRIMARY KEY,
    timestamp INTEGER NOT NULL
);
CREATE TABLE knowledge_node_browser_events (
    knowledge_node_id INTEGER NOT NULL,
    browser_event_id INTEGER NOT NULL,
    PRIMARY KEY (knowledge_node_id, browser_event_id)
);
CREATE TABLE entities (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    canonical TEXT
);
CREATE TABLE knowledge_node_entities (
    knowledge_node_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,
    PRIMARY KEY (knowledge_node_id, entity_id)
);
CREATE TABLE knowledge_node_workflow_runs (
    knowledge_node_id INTEGER NOT NULL,
    workflow_run_id INTEGER NOT NULL,
    PRIMARY KEY (knowledge_node_id, workflow_run_id)
);
"""


@dataclass
class FakeBackend:
    """Injectable backend matching :mod:`hippo_brain.vector_store` shape.

    Both primitives return dicts with ``knowledge_node_id`` + ``score``
    (and ``distance``/``bm25`` for provenance) — the real backend does the
    same.
    """

    knn: list[tuple[int, float]] = field(default_factory=list)
    fts: list[tuple[int, float]] = field(default_factory=list)

    def knn_search(self, _conn, _query_vec, column="vec_knowledge", limit=10):
        assert column in {"vec_knowledge", "vec_command"}
        return [
            {
                "knowledge_node_id": nid,
                "distance": dist,
                "score": max(0.0, 1.0 - dist / 2.0),
            }
            for nid, dist in self.knn[:limit]
        ]

    def fts_search(self, _conn, _query, limit=10):
        return [
            {
                "knowledge_node_id": nid,
                "bm25": bm25,
                "score": 1.0 / (1.0 + abs(bm25)),
            }
            for nid, bm25 in self.fts[:limit]
        ]


def _install_vec_fixture(conn: sqlite3.Connection, vectors: dict[int, list[float]]) -> None:
    """Stand up a minimal ``knowledge_vectors`` table so MMR can fetch vecs.

    Emulates :func:`sqlite_vec.vec_to_json` with a pure-Python wrapper — the
    real extension isn't loaded in unit tests.
    """
    import json as _json

    conn.execute(
        "CREATE TABLE knowledge_vectors (knowledge_node_id INTEGER PRIMARY KEY, vec_knowledge TEXT)"
    )

    def vec_to_json(blob):
        return blob

    conn.create_function("vec_to_json", 1, vec_to_json)
    for nid, vec in vectors.items():
        conn.execute(
            "INSERT INTO knowledge_vectors (knowledge_node_id, vec_knowledge) VALUES (?, ?)",
            (nid, _json.dumps(vec)),
        )


def _insert_node(
    conn: sqlite3.Connection,
    node_id: int,
    *,
    summary: str = "",
    embed_text: str = "",
    tags: list[str] | None = None,
    outcome: str | None = None,
    created_at: int = 1_700_000_000_000,
    node_type: str = "observation",
) -> None:
    conn.execute(
        "INSERT INTO knowledge_nodes"
        " (id, uuid, content, embed_text, node_type, outcome, tags, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            node_id,
            f"uuid-{node_id}",
            json.dumps({"summary": summary}),
            embed_text,
            node_type,
            outcome,
            json.dumps(tags or []),
            created_at,
        ),
    )


def _link_event(
    conn: sqlite3.Connection,
    node_id: int,
    event_id: int,
    *,
    timestamp: int = 1_700_000_000_000,
    cwd: str = "/tmp",
    branch: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events (id, timestamp, cwd, git_branch) VALUES (?, ?, ?, ?)",
        (event_id, timestamp, cwd, branch),
    )
    conn.execute(
        "INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (?, ?)",
        (node_id, event_id),
    )


def _link_claude(
    conn: sqlite3.Connection,
    node_id: int,
    session_id: int,
    *,
    start_time: int = 1_700_000_000_000,
    cwd: str = "/tmp",
    branch: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO claude_sessions (id, start_time, cwd, git_branch) VALUES (?, ?, ?, ?)",
        (session_id, start_time, cwd, branch),
    )
    conn.execute(
        "INSERT INTO knowledge_node_claude_sessions"
        " (knowledge_node_id, claude_session_id) VALUES (?, ?)",
        (node_id, session_id),
    )


def _link_workflow(conn: sqlite3.Connection, node_id: int, run_id: int) -> None:
    conn.execute(
        "INSERT INTO knowledge_node_workflow_runs"
        " (knowledge_node_id, workflow_run_id) VALUES (?, ?)",
        (node_id, run_id),
    )


def _link_entity(conn: sqlite3.Connection, node_id: int, entity_id: int, canonical: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO entities (id, type, name, canonical) VALUES (?, ?, ?, ?)",
        (entity_id, "concept", canonical, canonical),
    )
    conn.execute(
        "INSERT INTO knowledge_node_entities (knowledge_node_id, entity_id) VALUES (?, ?)",
        (node_id, entity_id),
    )


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    return c


# ---------------------------------------------------------------------------
# Mode behavior
# ---------------------------------------------------------------------------


def test_semantic_mode_normalizes_scores(conn):
    _insert_node(conn, 1, summary="node one", embed_text="alpha")
    _insert_node(conn, 2, summary="node two", embed_text="beta")
    _install_vec_fixture(conn, {1: [1.0, 0.0], 2: [0.0, 1.0]})
    backend = FakeBackend(knn=[(1, 0.2), (2, 1.8)])

    results = search(conn, "", [1.0, 0.0], Filters(), mode="semantic", limit=5, backend=backend)

    assert [r.uuid for r in results] == ["uuid-1", "uuid-2"]
    # cosine_distance in [0,2] → score = 1 - d/2. 0.2 → 0.9; 1.8 → 0.1.
    assert results[0].score == pytest.approx(0.9, abs=1e-4)
    assert results[1].score == pytest.approx(0.1, abs=1e-4)
    assert all(0.0 <= r.score <= 1.0 for r in results)


def test_lexical_mode_returns_fts_ordered(conn):
    _insert_node(conn, 10, summary="schema design")
    _insert_node(conn, 11, summary="something else")
    backend = FakeBackend(fts=[(10, -5.0), (11, -1.0)])

    results = search(conn, "schema", None, Filters(), mode="lexical", limit=5, backend=backend)

    assert [r.uuid for r in results] == ["uuid-10", "uuid-11"]
    assert results[0].score > results[1].score
    assert results[0].score == pytest.approx(1.0)


def test_hybrid_mode_rrf_merges_and_normalizes(conn):
    for i in range(1, 4):
        _insert_node(conn, i, summary=f"n{i}")
    _install_vec_fixture(conn, {1: [1.0, 0.0], 2: [0.0, 1.0], 3: [1.0, 1.0]})
    backend = FakeBackend(
        knn=[(1, 0.1), (2, 0.2), (3, 0.3)],
        fts=[(3, -2.0), (2, -1.0), (1, -0.5)],
    )

    results = search(conn, "q", [1.0, 0.0], Filters(), mode="hybrid", limit=3, backend=backend)

    assert len(results) == 3
    assert results[0].score == pytest.approx(1.0)
    # All hybrid scores in [0,1]; monotone non-increasing after MMR.
    assert all(0.0 <= r.score <= 1.0 for r in results)


def test_hybrid_with_missing_query_vec_falls_back_to_lexical(conn):
    _insert_node(conn, 1, summary="hit")
    backend = FakeBackend(fts=[(1, -2.0)])

    results = search(conn, "hit", None, Filters(), mode="hybrid", limit=5, backend=backend)

    assert len(results) == 1
    assert results[0].uuid == "uuid-1"


def test_recent_mode_orders_by_captured_at_desc(conn):
    _insert_node(conn, 1, summary="old", created_at=1)
    _insert_node(conn, 2, summary="new", created_at=100)
    _insert_node(conn, 3, summary="mid", created_at=50)
    backend = FakeBackend(fts=[(1, -1.0), (2, -1.0), (3, -1.0)])

    results = search(conn, "q", None, Filters(), mode="recent", limit=3, backend=backend)

    assert [r.uuid for r in results] == ["uuid-2", "uuid-3", "uuid-1"]


def test_recent_mode_without_query_uses_all_nodes(conn):
    _insert_node(conn, 1, summary="a", created_at=10)
    _insert_node(conn, 2, summary="b", created_at=20)
    backend = FakeBackend()

    results = search(conn, "", None, Filters(), mode="recent", limit=5, backend=backend)

    assert [r.uuid for r in results] == ["uuid-2", "uuid-1"]


def test_unknown_mode_raises(conn):
    with pytest.raises(ValueError):
        search(conn, "q", None, Filters(), mode="banana", backend=FakeBackend())


def test_limit_zero_returns_empty(conn):
    assert search(conn, "", None, Filters(), mode="recent", limit=0, backend=FakeBackend()) == []


# ---------------------------------------------------------------------------
# Filter pushdown
# ---------------------------------------------------------------------------


def test_project_filter_prunes_nodes_whose_cwd_does_not_match(conn):
    _insert_node(conn, 1, summary="keep")
    _link_event(conn, 1, 10, cwd="/work/hippo/src")
    _insert_node(conn, 2, summary="drop")
    _link_event(conn, 2, 11, cwd="/other/repo")

    backend = FakeBackend(fts=[(1, -1.0), (2, -1.0)])
    results = search(
        conn, "q", None, Filters(project="/work/hippo"), mode="lexical", limit=5, backend=backend
    )
    assert [r.uuid for r in results] == ["uuid-1"]


def test_since_filter_respects_epoch_ms_lower_bound(conn):
    _insert_node(conn, 1, summary="old", created_at=50)
    _insert_node(conn, 2, summary="new", created_at=500)

    backend = FakeBackend(fts=[(1, -1.0), (2, -1.0)])
    results = search(
        conn, "q", None, Filters(since_ms=200), mode="lexical", limit=5, backend=backend
    )
    assert [r.uuid for r in results] == ["uuid-2"]


def test_source_filter_shell_keeps_only_nodes_linked_to_events(conn):
    _insert_node(conn, 1, summary="shell")
    _link_event(conn, 1, 10)
    _insert_node(conn, 2, summary="claude")
    _link_claude(conn, 2, 20)

    backend = FakeBackend(fts=[(1, -1.0), (2, -1.0)])
    results = search(
        conn, "q", None, Filters(source="shell"), mode="lexical", limit=5, backend=backend
    )
    assert [r.uuid for r in results] == ["uuid-1"]


def test_source_filter_claude(conn):
    _insert_node(conn, 1, summary="shell")
    _link_event(conn, 1, 10)
    _insert_node(conn, 2, summary="claude")
    _link_claude(conn, 2, 20)

    backend = FakeBackend(fts=[(1, -1.0), (2, -1.0)])
    results = search(
        conn, "q", None, Filters(source="claude"), mode="lexical", limit=5, backend=backend
    )
    assert [r.uuid for r in results] == ["uuid-2"]


def test_source_filter_workflow_keeps_only_nodes_linked_to_workflow_runs(conn):
    _insert_node(conn, 1, summary="shell")
    _link_event(conn, 1, 10)
    _insert_node(conn, 2, summary="workflow")
    _link_workflow(conn, 2, 30)

    backend = FakeBackend(fts=[(1, -1.0), (2, -1.0)])
    results = search(
        conn, "q", None, Filters(source="workflow"), mode="lexical", limit=5, backend=backend
    )
    assert [r.uuid for r in results] == ["uuid-2"]


def test_branch_filter(conn):
    _insert_node(conn, 1)
    _link_event(conn, 1, 10, branch="main")
    _insert_node(conn, 2)
    _link_event(conn, 2, 11, branch="feature/x")

    backend = FakeBackend(fts=[(1, -1.0), (2, -1.0)])
    results = search(
        conn, "q", None, Filters(branch="main"), mode="lexical", limit=5, backend=backend
    )
    assert [r.uuid for r in results] == ["uuid-1"]


def test_entity_filter(conn):
    _insert_node(conn, 1, summary="a")
    _link_entity(conn, 1, 100, "sqlite")
    _insert_node(conn, 2, summary="b")
    _link_entity(conn, 2, 101, "lancedb")

    backend = FakeBackend(fts=[(1, -1.0), (2, -1.0)])
    results = search(
        conn, "q", None, Filters(entity="sqlite"), mode="lexical", limit=5, backend=backend
    )
    assert [r.uuid for r in results] == ["uuid-1"]


def test_bad_source_filter_raises(conn):
    _insert_node(conn, 1)
    backend = FakeBackend(fts=[(1, -1.0)])
    with pytest.raises(ValueError):
        search(conn, "q", None, Filters(source="nonsense"), mode="lexical", backend=backend)


# ---------------------------------------------------------------------------
# MMR diversification
# ---------------------------------------------------------------------------


def test_mmr_prefers_diverse_hits_over_near_duplicates(conn):
    # Three candidates: 1 is the top hit; 2 is near-duplicate of 1; 3 is
    # orthogonal. With λ=0.7, MMR should pick 1 then 3 (diverse) even though
    # 2 has a slightly higher raw score than 3.
    for i in (1, 2, 3):
        _insert_node(conn, i, summary=f"n{i}")
    _install_vec_fixture(
        conn,
        {
            1: [1.0, 0.0],
            2: [0.99, 0.01],  # nearly parallel to 1
            3: [0.0, 1.0],  # orthogonal to 1
        },
    )
    backend = FakeBackend(knn=[(1, 0.1), (2, 0.11), (3, 0.5)])

    results = search(conn, "", [1.0, 0.0], Filters(), mode="semantic", limit=2, backend=backend)

    uuids = [r.uuid for r in results]
    assert uuids[0] == "uuid-1"
    assert uuids[1] == "uuid-3", f"expected diversified pick, got {uuids}"


# ---------------------------------------------------------------------------
# SearchResult shape
# ---------------------------------------------------------------------------


def test_search_result_includes_linked_event_ids_and_metadata(conn):
    _insert_node(
        conn,
        1,
        summary="hello",
        embed_text="world",
        tags=["a", "b"],
        outcome="ok",
        created_at=0,
    )
    _link_event(conn, 1, 10, timestamp=1000, cwd="/proj", branch="main")
    _link_event(conn, 1, 11, timestamp=2000, cwd="/proj", branch="main")

    backend = FakeBackend(fts=[(1, -1.0)])
    [result] = search(conn, "hello", None, Filters(), mode="lexical", limit=5, backend=backend)

    assert isinstance(result, SearchResult)
    assert result.uuid == "uuid-1"
    assert result.summary == "hello"
    assert result.embed_text == "world"
    assert result.tags == ["a", "b"]
    assert result.outcome == "ok"
    assert result.cwd == "/proj"
    assert result.git_branch == "main"
    assert result.captured_at == 2000  # latest event timestamp wins
    assert sorted(result.linked_event_ids) == [10, 11]


def test_constants_match_spec():
    assert RRF_K == 60
    assert MMR_LAMBDA == pytest.approx(0.7)
