"""Tests for hippo_brain.bench.downstream_proxy.

Covers Q/A filtering, Hit@K, MRR, NDCG@10, mode aggregation, and
ask-synthesis sampling.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from hippo_brain.bench.downstream_proxy import (
    load_qa_items,
    run_ask_synthesis_sample,
    run_downstream_proxy_pass,
    score_single_retrieval,
)


def _write_qa(path: Path, items: list[dict]) -> Path:
    with path.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item) + "\n")
    return path


def _make_qa(qa_id: str, golden: str, keywords: list[str] | None = None) -> dict:
    return {
        "qa_id": qa_id,
        "question": f"question for {qa_id}",
        "golden_event_id": golden,
        "source_filter": "shell",
        "acceptable_answer_keywords": keywords or ["foo"],
        "tags": ["lookup"],
    }


def _result(event_id: str) -> dict:
    return {"event_id": event_id, "score": 1.0}


def test_load_qa_items_filters_missing(tmp_path: Path) -> None:
    items = [
        _make_qa("qa-001", "shell-1"),
        _make_qa("qa-002", "shell-2"),
        _make_qa("qa-003", "shell-3"),
        _make_qa("qa-004", "shell-missing-1"),
        _make_qa("qa-005", "shell-missing-2"),
    ]
    qa_path = _write_qa(tmp_path / "qa.jsonl", items)
    corpus_ids = {"shell-1", "shell-2", "shell-3"}

    included, filtered = load_qa_items(qa_path, corpus_ids)

    assert filtered == 2
    assert len(included) == 3
    assert {it["qa_id"] for it in included} == {"qa-001", "qa-002", "qa-003"}


def test_score_hit_at_1() -> None:
    results = [_result("shell-1"), _result("shell-2"), _result("shell-3")]
    score = score_single_retrieval(results, "shell-1")

    assert score["rank"] == 1
    assert score["hit_at_k"][1] is True
    assert score["hit_at_k"][3] is True
    assert score["hit_at_k"][5] is True
    assert score["hit_at_k"][10] is True
    assert score["mrr"] == 1.0


def test_score_hit_at_5_not_1() -> None:
    results = [
        _result("shell-other-1"),
        _result("shell-other-2"),
        _result("shell-target"),
        _result("shell-other-3"),
        _result("shell-other-4"),
    ]
    score = score_single_retrieval(results, "shell-target")

    assert score["rank"] == 3
    assert score["hit_at_k"][1] is False
    assert score["hit_at_k"][3] is True
    assert score["hit_at_k"][5] is True
    assert score["mrr"] == pytest.approx(1.0 / 3.0)


def test_score_not_found() -> None:
    results = [_result("shell-a"), _result("shell-b"), _result("shell-c")]
    score = score_single_retrieval(results, "shell-missing")

    assert score["rank"] is None
    assert score["hit_at_k"][1] is False
    assert score["hit_at_k"][3] is False
    assert score["hit_at_k"][5] is False
    assert score["hit_at_k"][10] is False
    assert score["mrr"] == 0.0
    assert score["ndcg_at_10"] == 0.0


def test_ndcg_perfect() -> None:
    results = [_result("shell-target")] + [_result(f"shell-x{i}") for i in range(9)]
    score = score_single_retrieval(results, "shell-target")

    assert score["rank"] == 1
    assert score["ndcg_at_10"] == pytest.approx(1.0)


def test_ndcg_at_rank_3() -> None:
    results = [
        _result("shell-x1"),
        _result("shell-x2"),
        _result("shell-target"),
    ]
    score = score_single_retrieval(results, "shell-target")

    assert score["rank"] == 3
    assert score["ndcg_at_10"] == pytest.approx(1.0 / math.log2(4))


def test_mode_aggregation_mean_mrr() -> None:
    qa_items = [
        _make_qa("qa-1", "shell-1"),
        _make_qa("qa-2", "shell-2"),
        _make_qa("qa-3", "shell-3"),
    ]

    def fake_embedding(_q: str) -> list[float]:
        return [0.0]

    def fake_search(_conn, query, _vec, *, mode, limit):
        if mode == "hybrid":
            for item in qa_items:
                if item["question"] == query:
                    return [_result(item["golden_event_id"])]
            return []
        return [_result("shell-irrelevant")]

    out = run_downstream_proxy_pass(
        conn=None,
        qa_items=qa_items,
        embedding_fn=fake_embedding,
        modes=("hybrid", "semantic", "lexical"),
        k=10,
        search_fn=fake_search,
    )

    assert out["qa_count"] == 3
    assert out["k"] == 10
    assert out["modes"]["hybrid"]["mrr"] == pytest.approx(1.0)
    assert out["modes"]["hybrid"]["hit_at_1"] == pytest.approx(1.0)
    assert out["modes"]["semantic"]["mrr"] == pytest.approx(0.0)
    assert out["modes"]["lexical"]["mrr"] == pytest.approx(0.0)
    assert out["modes"]["hybrid"]["scored_count"] == 3


def test_ask_synthesis_keyword_hit() -> None:
    qa_items = [
        _make_qa(f"qa-{i:03d}", f"shell-{i}", keywords=["rebase", "main"]) for i in range(20)
    ]

    def ask_with_keyword(_q: str) -> str:
        return "I ran a git rebase onto main earlier today."

    out = run_ask_synthesis_sample(qa_items, ask_with_keyword, sample_size=10, seed=42)

    assert out["sampled"] == 10
    assert out["keyword_hit_rate"] == pytest.approx(1.0)


def test_ask_synthesis_keyword_miss() -> None:
    qa_items = [_make_qa(f"qa-{i:03d}", f"shell-{i}", keywords=["rebase"]) for i in range(20)]

    def ask_no_keyword(_q: str) -> str:
        return "Sorry, I don't know."

    out = run_ask_synthesis_sample(qa_items, ask_no_keyword, sample_size=5, seed=42)

    assert out["sampled"] == 5
    assert out["keyword_hit_rate"] == pytest.approx(0.0)


def test_ask_synthesis_sample_deterministic() -> None:
    qa_items = [_make_qa(f"qa-{i:03d}", f"shell-{i}", keywords=[f"kw-{i}"]) for i in range(20)]

    seen_first: list[str] = []
    seen_second: list[str] = []

    def ask_recording(target: list[str]):
        def _ask(question: str) -> str:
            target.append(question)
            return ""

        return _ask

    run_ask_synthesis_sample(qa_items, ask_recording(seen_first), sample_size=7, seed=42)
    run_ask_synthesis_sample(qa_items, ask_recording(seen_second), sample_size=7, seed=42)

    assert seen_first == seen_second
    assert len(seen_first) == 7
