"""BT-19: golden-output regression test.

A frozen Q/A fixture + a deterministic search-stub produces known-good
Hit@K / MRR / NDCG@10 values. This catches scoring-formula regressions
that the unit tests in test_bench_downstream_proxy.py would not — those
hard-code expected values inside the test, so a buggy formula change
that broke production would also "match" the buggy unit-test
expectation.

The fixture lives under brain/tests/fixtures/golden_corpus_v1/ as
JSON+JSONL (no binary SQLite blob) — the scoring code accepts a
search_fn parameter so we mock at that boundary.

Two tests:
1. test_golden_retrieval_scores — clean path, exact metric match.
2. test_golden_catches_rank_regression — inject a rank-flip on three
   items, assert metrics drop. Proves the bench actually catches
   regressions, not just runs to completion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hippo_brain.bench.downstream_proxy import run_downstream_proxy_pass

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "golden_corpus_v1"


# Per-qa_id ranked lists. The golden_event_id appears at position `rank`
# in each list (1-indexed). All three modes use the same layout — keeps
# the math simple and the test focused on the scoring formula, not the
# retrieval mechanism (which is mocked).
GOLDEN_RANKED_RESULTS: dict[str, list[str]] = {
    "g1": ["shell-001", "shell-002", "browser-004"],
    "g2": ["shell-002", "claude-003", "shell-001"],
    "g3": ["browser-004", "shell-001", "claude-003"],
    "g4": ["browser-004", "claude-003", "shell-001"],
    "g5": ["shell-005", "shell-001", "claude-003"],
    "g6": ["shell-001", "claude-003", "workflow-006"],
    "g7": ["shell-005", "claude-007", "browser-004"],
    "g8": ["shell-001", "claude-003", "shell-002", "browser-004", "browser-008"],
}


def _load_qa() -> list[dict]:
    items: list[dict] = []
    with (FIXTURE_DIR / "qa.jsonl").open() as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _stub_search_factory(ranked: dict[str, list[str]]):
    """Returns a search_fn(conn, query, query_vec, mode, limit) that
    looks up the qa_id by question text and returns the pre-computed
    ranked list as dicts with event_id keys."""
    qa = _load_qa()
    by_question = {item["question"]: item["qa_id"] for item in qa}

    def _search(_conn, query, _query_vec, mode=None, limit=10):
        qa_id = by_question[query]
        ids = ranked[qa_id][:limit]
        return [{"event_id": eid} for eid in ids]

    return _search


def test_golden_retrieval_scores() -> None:
    """Clean path: scoring against the golden fixture matches expected_scores.json
    to 4 decimal places. If anyone changes score_single_retrieval's formula,
    this test fails immediately with a clear delta."""
    qa = _load_qa()
    expected = json.loads((FIXTURE_DIR / "expected_scores.json").read_text())

    search_fn = _stub_search_factory(GOLDEN_RANKED_RESULTS)
    result = run_downstream_proxy_pass(
        conn=object(),  # never used by stub
        qa_items=qa,
        embedding_fn=lambda q: [0.0] * 8,
        search_fn=search_fn,
    )

    assert result["qa_count"] == expected["qa_count"]
    for mode_name in ("hybrid", "semantic", "lexical"):
        actual_mode = result["modes"][mode_name]
        expected_mode = expected["modes"][mode_name]
        for metric in ("hit_at_1", "hit_at_3", "hit_at_5", "hit_at_10", "mrr", "ndcg_at_10"):
            assert actual_mode[metric] == pytest.approx(expected_mode[metric], abs=1e-4), (
                f"{mode_name}.{metric}: got {actual_mode[metric]}, expected {expected_mode[metric]}"
            )


def test_golden_catches_rank_regression() -> None:
    """Inject a rank-flip on three Q/A items (g1, g4, g5 — all three were
    rank-1, drop them to rank-5). A real regression would do something
    similar: a bug in the retrieval ranker that pushes correct results
    further down. This test proves the metrics drop visibly."""
    regressed = dict(GOLDEN_RANKED_RESULTS)
    # Demote the golden event to rank 5 by prepending decoys.
    for qa_id in ("g1", "g4", "g5"):
        gold_list = GOLDEN_RANKED_RESULTS[qa_id]
        gold = next(eid for eid in gold_list if eid.endswith(qa_id[1:].zfill(3)))
        # Build a list where the gold is at index 4 (rank 5).
        regressed[qa_id] = ["decoy-a", "decoy-b", "decoy-c", "decoy-d", gold]

    qa = _load_qa()
    expected = json.loads((FIXTURE_DIR / "expected_scores.json").read_text())
    expected_hit_at_1 = expected["modes"]["hybrid"]["hit_at_1"]
    expected_mrr = expected["modes"]["hybrid"]["mrr"]

    search_fn = _stub_search_factory(regressed)
    result = run_downstream_proxy_pass(
        conn=object(),
        qa_items=qa,
        embedding_fn=lambda q: [0.0] * 8,
        search_fn=search_fn,
    )

    actual_hit_at_1 = result["modes"]["hybrid"]["hit_at_1"]
    actual_mrr = result["modes"]["hybrid"]["mrr"]

    # Three perfect-rank items dropped to rank 5: Hit@1 must fall by 3/8 = 0.375.
    assert (expected_hit_at_1 - actual_hit_at_1) >= 0.30, (
        f"Hit@1 should drop by >=0.30 after rank-flip; got {expected_hit_at_1} -> {actual_hit_at_1}"
    )
    # MRR drops because three 1.0 contributions become 0.2.
    assert (expected_mrr - actual_mrr) >= 0.10, (
        f"MRR should drop by >=0.10 after rank-flip; got {expected_mrr} -> {actual_mrr}"
    )
