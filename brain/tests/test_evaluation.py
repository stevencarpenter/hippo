"""Tests for the hippo evaluation harness (brain/src/hippo_brain/evaluation.py).

Covers every metric function on synthetic fixtures, plus a small end-to-end
smoke test that runs the harness against an in-memory SQLite DB with 3
questions and a canned retrieval adapter.
"""

from __future__ import annotations

import sqlite3

import pytest

from hippo_brain import evaluation as evmod
from hippo_brain.evaluation import (
    Question,
    SearchResult,
    coverage_gap_score,
    derive_sources,
    groundedness,
    keyword_match,
    load_questions,
    mrr,
    ndcg_at_k,
    near_duplicate_density,
    recall_at_k,
    render_markdown,
    run_benchmark,
    source_diversity,
    summary_coherence,
)


# ---------------------------------------------------------------------------
# Pure metric tests
# ---------------------------------------------------------------------------


class TestRecallAtK:
    def test_all_relevant_in_top_k(self):
        assert recall_at_k(["a", "b", "c"], {"a", "b"}, 3) == 1.0

    def test_partial(self):
        assert recall_at_k(["a", "x", "y"], {"a", "b"}, 3) == 0.5

    def test_empty_relevant_is_none(self):
        assert recall_at_k(["a"], set(), 3) is None

    def test_k_zero(self):
        assert recall_at_k(["a"], {"a"}, 0) == 0.0

    def test_respects_k_cutoff(self):
        assert recall_at_k(["x", "a"], {"a"}, 1) == 0.0


class TestMRR:
    def test_first_hit(self):
        assert mrr(["a", "b", "c"], {"a"}) == 1.0

    def test_third_hit(self):
        assert mrr(["x", "y", "a"], {"a"}) == pytest.approx(1 / 3)

    def test_no_hit(self):
        assert mrr(["x", "y"], {"a"}) == 0.0

    def test_empty_relevant_is_none(self):
        assert mrr(["a"], set()) is None


class TestNDCG:
    def test_perfect_ordering(self):
        rel = {"a": 3.0, "b": 2.0, "c": 1.0}
        val = ndcg_at_k(["a", "b", "c"], rel, 3)
        assert val == pytest.approx(1.0)

    def test_reversed_ordering_is_worse(self):
        rel = {"a": 3.0, "b": 2.0, "c": 1.0}
        perfect = ndcg_at_k(["a", "b", "c"], rel, 3)
        reversed_ = ndcg_at_k(["c", "b", "a"], rel, 3)
        assert reversed_ < perfect

    def test_empty_is_none(self):
        assert ndcg_at_k(["a"], {}, 3) is None


class TestSourceDiversity:
    def test_single_source_is_zero(self):
        assert source_diversity([["shell"], ["shell"]]) == 0.0

    def test_balanced_is_one(self):
        val = source_diversity([["shell"], ["claude"]])
        assert val == pytest.approx(1.0)

    def test_empty_is_zero(self):
        assert source_diversity([]) == 0.0

    def test_multisource_hits_contribute_multiple_times(self):
        val = source_diversity([["shell", "claude"], ["claude"]])
        assert 0.0 < val < 1.0


class TestNearDuplicateDensity:
    def test_identical_vectors_are_one(self):
        val = near_duplicate_density([[1.0, 0.0], [1.0, 0.0]])
        assert val == pytest.approx(1.0)

    def test_orthogonal_is_zero(self):
        val = near_duplicate_density([[1.0, 0.0], [0.0, 1.0]])
        assert val == pytest.approx(0.0)

    def test_too_few_is_none(self):
        assert near_duplicate_density([[1.0, 0.0]]) is None


class TestCoverageGap:
    def test_all_strong(self):
        assert coverage_gap_score([0.9, 0.8, 0.7]) == 0.0

    def test_all_weak(self):
        assert coverage_gap_score([0.1, 0.2]) == 1.0

    def test_empty_is_full_gap(self):
        assert coverage_gap_score([]) == 1.0

    def test_threshold_respected(self):
        assert coverage_gap_score([0.3, 0.6], threshold=0.5) == 0.5


class TestCoherenceAndKeyword:
    def test_summary_coherence_hit(self):
        assert summary_coherence("Ran Cargo tests", ["cargo", "test"])

    def test_summary_coherence_miss(self):
        assert not summary_coherence("Ran shell commands", ["python"])

    def test_summary_coherence_empty(self):
        assert not summary_coherence("", ["anything"])
        assert not summary_coherence("text", [])

    def test_keyword_match_case_insensitive(self):
        assert keyword_match("We use RRF fusion", ["rrf"])

    def test_keyword_match_empty(self):
        assert not keyword_match("", ["x"])
        assert not keyword_match("text", [])


# ---------------------------------------------------------------------------
# Groundedness (LLM-judge) — fake client
# ---------------------------------------------------------------------------


class _FakeLMClient:
    def __init__(self, response: str = "0.8"):
        self.response = response
        self.calls: list[list[dict]] = []

    async def chat(self, messages, model="", temperature=0.0, max_tokens=32):
        self.calls.append(messages)
        return self.response


@pytest.mark.asyncio
async def test_groundedness_parses_float():
    client = _FakeLMClient("0.75\nExplanation follows.")
    val = await groundedness("Ran cargo test.", [{"summary": "cargo test"}], client, "m")
    assert val == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_groundedness_none_when_client_errors():
    class Boom:
        async def chat(self, *a, **kw):
            raise RuntimeError("down")

    val = await groundedness("ans", [{"summary": "s"}], Boom(), "m")
    assert val is None


@pytest.mark.asyncio
async def test_groundedness_none_when_unparseable():
    client = _FakeLMClient("not a number at all")
    val = await groundedness("ans", [{"summary": "s"}], client, "m")
    assert val is None


@pytest.mark.asyncio
async def test_groundedness_clamps():
    client = _FakeLMClient("1.5")
    val = await groundedness("ans", [{"summary": "s"}], client, "m")
    assert val == 1.0


# ---------------------------------------------------------------------------
# derive_sources
# ---------------------------------------------------------------------------


@pytest.fixture
def mini_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE knowledge_nodes (id INTEGER PRIMARY KEY, uuid TEXT);
        CREATE TABLE knowledge_node_events (knowledge_node_id INTEGER, event_id INTEGER);
        CREATE TABLE knowledge_node_claude_sessions (
            knowledge_node_id INTEGER, claude_session_id INTEGER
        );
        CREATE TABLE knowledge_node_browser_events (
            knowledge_node_id INTEGER, browser_event_id INTEGER
        );
        CREATE TABLE knowledge_node_workflow_runs (
            knowledge_node_id INTEGER, workflow_run_id INTEGER
        );
        INSERT INTO knowledge_nodes VALUES (1, 'u1'), (2, 'u2'), (3, 'u3');
        INSERT INTO knowledge_node_events VALUES (1, 100), (3, 101);
        INSERT INTO knowledge_node_claude_sessions VALUES (2, 200), (3, 201);
        """
    )
    yield conn
    conn.close()


def test_derive_sources_multi(mini_db):
    out = derive_sources(mini_db, ["u1", "u2", "u3"])
    assert out["u1"] == ["shell"]
    assert out["u2"] == ["claude"]
    assert set(out["u3"]) == {"shell", "claude"}


def test_derive_sources_nil_conn():
    assert derive_sources(None, ["u1"]) == {}


def test_derive_sources_empty_uuids():
    conn = sqlite3.connect(":memory:")
    try:
        assert derive_sources(conn, []) == {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Q/A set — the shipped file must be well-formed
# ---------------------------------------------------------------------------


def test_shipped_question_set_loads():
    from hippo_brain.evaluation import _DEFAULT_QUESTIONS

    qs = load_questions(_DEFAULT_QUESTIONS)
    assert len(qs) >= 30
    assert all(q.id and q.question for q in qs)
    assert all(q.acceptable_answer_keywords for q in qs)
    adversarial = [q for q in qs if q.intent == "adversarial"]
    assert len(adversarial) >= 4


# ---------------------------------------------------------------------------
# End-to-end integration: 3 questions against an in-memory DB with a fake
# semantic retriever. Exercises the harness pipeline without LanceDB.
# ---------------------------------------------------------------------------


_SMOKE_SCHEMA = """
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
    git_repo TEXT,
    git_branch TEXT
);
CREATE TABLE knowledge_node_events (
    knowledge_node_id INTEGER,
    event_id INTEGER,
    PRIMARY KEY (knowledge_node_id, event_id)
);
CREATE TABLE claude_sessions (
    id INTEGER PRIMARY KEY,
    start_time INTEGER,
    cwd TEXT,
    project_dir TEXT,
    git_branch TEXT
);
CREATE TABLE knowledge_node_claude_sessions (
    knowledge_node_id INTEGER,
    claude_session_id INTEGER,
    PRIMARY KEY (knowledge_node_id, claude_session_id)
);
CREATE TABLE browser_events (id INTEGER PRIMARY KEY, timestamp INTEGER);
CREATE TABLE knowledge_node_browser_events (
    knowledge_node_id INTEGER,
    browser_event_id INTEGER,
    PRIMARY KEY (knowledge_node_id, browser_event_id)
);
CREATE TABLE knowledge_node_workflow_runs (
    knowledge_node_id INTEGER,
    workflow_run_id INTEGER,
    PRIMARY KEY (knowledge_node_id, workflow_run_id)
);
CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT, name TEXT, canonical TEXT);
CREATE TABLE knowledge_node_entities (knowledge_node_id INTEGER, entity_id INTEGER);
"""


def _smoke_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SMOKE_SCHEMA)
    rows = [
        (1, "u1", '{"summary": "sqlite-vec replaces LanceDB"}', "sqlite-vec fts5 rrf", 1000),
        (
            2,
            "u2",
            '{"summary": "asyncio.gather concurrency"}',
            "enrichment concurrent asyncio",
            1100,
        ),
        (3, "u3", '{"summary": "Firefox native messaging"}', "allowlist native messaging", 1200),
    ]
    for nid, uuid, content, embed, ts in rows:
        conn.execute(
            "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, outcome, tags, created_at) "
            "VALUES (?, ?, ?, ?, NULL, '[]', ?)",
            (nid, uuid, content, embed, ts),
        )
    conn.execute(
        "INSERT INTO events (id, timestamp, cwd, git_repo, git_branch) "
        "VALUES (10, 1000, '/p', 'r', 'main')"
    )
    conn.execute("INSERT INTO knowledge_node_events VALUES (1, 10)")
    conn.execute(
        "INSERT INTO claude_sessions (id, start_time, cwd, project_dir, git_branch) "
        "VALUES (20, 1100, '/p', '/p', 'main')"
    )
    conn.execute("INSERT INTO knowledge_node_claude_sessions VALUES (2, 20)")
    conn.execute("INSERT INTO browser_events (id, timestamp) VALUES (30, 1200)")
    conn.execute("INSERT INTO knowledge_node_browser_events VALUES (3, 30)")
    conn.commit()
    return conn


@pytest.mark.asyncio
async def test_smoke_end_to_end(monkeypatch):
    """Drive the harness through semantic mode with a canned retriever."""
    canned: dict[str, list[SearchResult]] = {
        "sqlite-vec replacement?": [SearchResult(uuid="u1", score=0.9, node_id=1)],
        "How is enrichment asyncio used?": [SearchResult(uuid="u2", score=0.85, node_id=2)],
        "native messaging details?": [SearchResult(uuid="u3", score=0.8, node_id=3)],
    }

    async def fake_retrieve(conn, vector_table, lm_client, question, embedding_model, limit):
        return canned.get(question, [])

    monkeypatch.setattr(evmod, "_retrieve_semantic", fake_retrieve)

    conn = _smoke_conn()
    questions = [
        Question(
            id="s1",
            question="sqlite-vec replacement?",
            intent="why-decision",
            relevant_knowledge_node_uuids=["u1"],
            acceptable_answer_keywords=["sqlite"],
            source_bias="claude",
        ),
        Question(
            id="s2",
            question="How is enrichment asyncio used?",
            intent="how-it-works",
            relevant_knowledge_node_uuids=["u2"],
            acceptable_answer_keywords=["gather"],
            source_bias="claude",
        ),
        Question(
            id="s3",
            question="native messaging details?",
            intent="how-it-works",
            relevant_knowledge_node_uuids=["u3"],
            acceptable_answer_keywords=["native"],
            source_bias="browser",
        ),
    ]
    report = await run_benchmark(
        questions=questions,
        conn=conn,
        vector_table=object(),  # opaque, not used by the fake retriever
        lm_client=None,
        embedding_model="dummy",
        query_model="",
        mode="semantic",
        limit=5,
        run_synthesis=False,
        run_judge=False,
    )
    assert len(report.results) == 3
    for r in report.results:
        assert r.mrr == 1.0
        assert r.recall_at_k == 1.0
    md = render_markdown(report)
    assert "Hippo Evaluation Scorecard" in md
    assert "recall@k" in md
    assert "| s1 |" in md


@pytest.mark.asyncio
async def test_unsupported_mode_returns_degraded():
    """Hybrid/lexical/recent modes produce a degraded result with a clear error."""
    conn = _smoke_conn()
    questions = [
        Question(
            id="u1",
            question="anything",
            relevant_knowledge_node_uuids=[],
            acceptable_answer_keywords=["x"],
        ),
    ]
    report = await run_benchmark(
        questions=questions,
        conn=conn,
        vector_table=None,
        lm_client=None,
        embedding_model="",
        query_model="",
        mode="hybrid",
        limit=5,
        run_synthesis=False,
        run_judge=False,
    )
    assert len(report.results) == 1
    r = report.results[0]
    assert r.degraded is True
    assert r.error is not None
    assert "not available on this branch" in r.error


def test_render_markdown_handles_empty():
    from hippo_brain.evaluation import ScoreReport

    report = ScoreReport(
        results=[],
        config={
            "mode": "semantic",
            "limit": 10,
            "run_synthesis": False,
            "run_judge": False,
            "embedding_model": "",
            "query_model": "",
        },
        corpus={},
        started_at=0.0,
        finished_at=0.0,
    )
    md = render_markdown(report)
    assert "Summary" in md
