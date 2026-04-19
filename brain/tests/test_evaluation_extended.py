"""Extended regression tests for the hippo evaluation harness.

These pin correctness of the metric calculations and a few integration
behaviors that the core ``test_evaluation.py`` suite does not cover.
Metric bugs here would silently produce wrong eval numbers.
"""

from __future__ import annotations

import math
import sqlite3

import pytest

from hippo_brain.evaluation import (
    Question,
    derive_sources,
    ndcg_at_k,
    near_duplicate_density,
    run_benchmark,
    source_diversity,
)
from hippo_brain.evaluation import _pairwise_mean_cosine


# ---------------------------------------------------------------------------
# Metric edge cases
# ---------------------------------------------------------------------------


class TestNDCGZeroIdeal:
    """When every graded relevance is 0, idcg is 0 and NDCG must be NaN.

    Otherwise the metric would divide by zero or return a meaningless 0.0.
    """

    def test_all_zero_relevances_is_nan(self):
        rel = {"a": 0.0, "b": 0.0}
        val = ndcg_at_k(["a", "b"], rel, 3)
        assert math.isnan(val)


class TestSourceDiversityEmptyStrings:
    """Empty-string source labels must be skipped, not counted as a category.

    If empties counted, [["", "a", ""]] would be "2 sources" and the entropy
    math would be non-zero — silently rewarding a retrieval layer that emits
    empty source labels.
    """

    def test_empties_skipped(self):
        # Only "a" counts — single unique category → entropy normalized to 0.0.
        assert source_diversity([["", "a", ""]]) == 0.0

    def test_empties_do_not_inflate(self):
        # Without the filter, this would appear as {"": 2, "a": 1, "b": 1} and
        # return >0. With the filter it's {"a": 1, "b": 1} → balanced → 1.0.
        val = source_diversity([["", "a"], ["", "b"]])
        assert val == pytest.approx(1.0)


class TestNearDupDensityUndersize:
    """<2 vectors means no pairs; metric is undefined (NaN, not 0)."""

    def test_zero_vectors_nan(self):
        assert math.isnan(near_duplicate_density([]))

    def test_one_vector_nan(self):
        assert math.isnan(near_duplicate_density([[1.0, 0.0, 0.0]]))


class TestPairwiseMeanCosineSampling:
    """When pair count exceeds ``max_pairs`` the function must sample.

    We use a tiny ``max_pairs`` so the sampling branch actually fires, then
    assert the result is:
      * finite (didn't crash),
      * deterministic (uses the module-level RNG seed 1234 under the hood).

    The RNG is seeded inline, so two calls with the same input return the same
    mean — pinning that the seed stays fixed (flipping to a global RNG would
    break eval reproducibility).
    """

    def test_samples_deterministically(self):
        # 6 unit-ish vectors → 15 pairs. Force sampling by capping at 4.
        vecs = [
            [1.0, 0.0],
            [0.9, 0.1],
            [0.8, 0.2],
            [0.0, 1.0],
            [0.1, 0.9],
            [0.2, 0.8],
        ]
        a = _pairwise_mean_cosine(vecs, max_pairs=4)
        b = _pairwise_mean_cosine(vecs, max_pairs=4)
        assert math.isfinite(a)
        assert a == b  # deterministic under seeded RNG

    def test_unsampled_matches_full(self):
        # When pairs <= max_pairs, no sampling: full mean computed.
        vecs = [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]
        val = _pairwise_mean_cosine(vecs, max_pairs=100)
        # Exact pairwise cosines: (0, sqrt(1/2), sqrt(1/2)) → mean = sqrt(1/2)*2/3
        expected = (0.0 + math.sqrt(0.5) + math.sqrt(0.5)) / 3
        assert val == pytest.approx(expected)


# ---------------------------------------------------------------------------
# derive_sources DB integration
# ---------------------------------------------------------------------------


@pytest.fixture
def all_sources_db():
    """Mini DB covering every link table so each branch of derive_sources runs."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE knowledge_nodes (id INTEGER PRIMARY KEY, uuid TEXT);
        CREATE TABLE knowledge_node_events (
            knowledge_node_id INTEGER, event_id INTEGER
        );
        CREATE TABLE knowledge_node_claude_sessions (
            knowledge_node_id INTEGER, claude_session_id INTEGER
        );
        CREATE TABLE knowledge_node_browser_events (
            knowledge_node_id INTEGER, browser_event_id INTEGER
        );
        CREATE TABLE knowledge_node_workflow_runs (
            knowledge_node_id INTEGER, workflow_run_id INTEGER
        );
        INSERT INTO knowledge_nodes VALUES
            (1, 'shell-only'),
            (2, 'claude-only'),
            (3, 'browser-only'),
            (4, 'workflow-only'),
            (5, 'all-four');
        INSERT INTO knowledge_node_events VALUES (1, 100), (5, 105);
        INSERT INTO knowledge_node_claude_sessions VALUES (2, 200), (5, 205);
        INSERT INTO knowledge_node_browser_events VALUES (3, 300), (5, 305);
        INSERT INTO knowledge_node_workflow_runs VALUES (4, 400), (5, 405);
        """
    )
    yield conn
    conn.close()


def test_derive_sources_exercises_every_link_table(all_sources_db):
    """Each link table must feed into the right label.

    A regression where, e.g., workflow queries returned shell labels would
    silently break source-diversity and stratification reports.
    """
    out = derive_sources(
        all_sources_db,
        ["shell-only", "claude-only", "browser-only", "workflow-only", "all-four"],
    )
    assert out["shell-only"] == ["shell"]
    assert out["claude-only"] == ["claude"]
    assert out["browser-only"] == ["browser"]
    assert out["workflow-only"] == ["workflow"]
    assert set(out["all-four"]) == {"shell", "claude", "browser", "workflow"}


def test_derive_sources_unknown_uuids_returns_empty(all_sources_db):
    """uuids not in knowledge_nodes → the id_to_uuid map is empty → return {}.

    Pins the early-exit after the initial lookup (line ~330).
    """
    out = derive_sources(all_sources_db, ["does-not-exist", "also-missing"])
    assert out == {}


# ---------------------------------------------------------------------------
# score_question: retrieval crash → degraded result, not exception
# ---------------------------------------------------------------------------


class _ExplodingBackend:
    """Fake vector_store backend whose FTS path raises.

    Goes through ``retrieval.search`` in ``lexical`` mode so the error
    propagates into ``score_question``.
    """

    def knn_search(self, conn, query_vec, column="vec_knowledge", limit=10):
        return []

    def fts_search(self, conn, query, limit=10):
        raise RuntimeError("fts5 index exploded")


_MIN_SCHEMA = """
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
"""


@pytest.mark.asyncio
async def test_retrieval_failure_yields_degraded_result(monkeypatch):
    """A crashing retrieval must produce a QuestionResult with ``degraded=True``
    and a populated ``error`` — not propagate the exception and kill the run.

    Without this guarantee, one bad question takes down the whole scorecard.
    """
    import hippo_brain.retrieval as retrieval_mod

    monkeypatch.setattr(retrieval_mod, "_default_backend", lambda: _ExplodingBackend())

    conn = sqlite3.connect(":memory:")
    conn.executescript(_MIN_SCHEMA)
    try:
        report = await run_benchmark(
            questions=[
                Question(
                    id="boom",
                    question="anything",
                    intent="smoke",
                    relevant_knowledge_node_uuids=["u1"],
                    acceptable_answer_keywords=["x"],
                )
            ],
            conn=conn,
            lm_client=None,
            embedding_model="",
            query_model="",
            mode="lexical",
            limit=5,
            run_synthesis=False,
            run_judge=False,
        )
        assert len(report.results) == 1
        r = report.results[0]
        assert r.degraded is True
        assert r.error is not None
        assert "retrieval" in r.error
        assert r.retrieval == []
        assert math.isnan(r.recall_at_k)
        assert r.coverage_gap_score == 1.0
    finally:
        conn.close()
