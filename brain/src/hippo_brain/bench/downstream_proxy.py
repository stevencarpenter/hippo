"""Downstream-proxy gate: per-mode retrieval scoring + ask-synthesis sample.

For each Q/A item this module:

- runs ``retrieval.search`` against the bench DB in 3 modes
  (hybrid / semantic / lexical),
- computes Hit@K, MRR, and NDCG@10 with binary relevance against the
  fixture's ``golden_event_id``,
- aggregates per-mode means and returns the spec-shaped
  ``downstream_proxy`` block consumed by ``coordinator_v2``.

A separate :func:`run_ask_synthesis_sample` does a deterministic
sample over Q/A items (sorted by ``qa_id``) and checks
``acceptable_answer_keywords`` against an injected ``ask_fn``.

The ``embedding_fn``, ``ask_fn``, and ``search_fn`` parameters are
callables, so the module is testable without LM Studio or a live DB.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hippo_brain import retrieval

DEFAULT_K_VALUES: tuple[int, ...] = (1, 3, 5, 10)
DEFAULT_MODES: tuple[str, ...] = ("hybrid", "semantic", "lexical")


def load_qa_items(qa_path: Path, corpus_event_ids: set[str]) -> tuple[list[dict], int]:
    """Load Q/A JSONL and drop items whose golden event isn't in the corpus.

    ``corpus_event_ids`` is the set of ``{source}-{id}`` identifiers
    produced by ``corpus_v2.sample_from_hippo_db_v2``. An item with a
    missing golden event is unscoreable, so we bin it and return the
    count for visibility in the run manifest.
    """
    included: list[dict] = []
    filtered = 0
    with qa_path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            item = json.loads(line)
            if item.get("golden_event_id") in corpus_event_ids:
                included.append(item)
            else:
                filtered += 1
    return included, filtered


def _result_event_id(result: Any) -> str | None:
    """Best-effort extraction of an event identifier from a search result.

    Tests typically pass dicts with an ``event_id`` key; production callers
    pass :class:`hippo_brain.retrieval.SearchResult` instances which expose
    a ``uuid``. Whichever attribute is present is stringified and returned.
    """
    if isinstance(result, dict):
        for key in ("event_id", "uuid"):
            value = result.get(key)
            if value is not None:
                return str(value)
        return None
    for attr in ("event_id", "uuid"):
        value = getattr(result, attr, None)
        if value is not None:
            return str(value)
    return None


def score_single_retrieval(
    results: list,
    golden_event_id: str,
    k_values: list[int] | tuple[int, ...] = DEFAULT_K_VALUES,
) -> dict:
    """Score one query's ranked results against its golden event.

    Returns ``hit_at_k`` for each requested k, the 1-based ``rank`` if
    the golden appears anywhere in the list (else ``None``), MRR
    (``1/rank`` or 0), and NDCG@10 with binary relevance —
    ``1/log2(rank+1)`` if the golden is in the top 10, else 0.
    """
    rank: int | None = None
    for i, r in enumerate(results):
        if _result_event_id(r) == golden_event_id:
            rank = i + 1
            break

    hit_at_k: dict[int, bool] = {k: rank is not None and rank <= k for k in k_values}
    mrr = 1.0 / rank if rank is not None else 0.0
    ndcg_at_10 = 1.0 / math.log2(rank + 1) if (rank is not None and rank <= 10) else 0.0
    return {
        "hit_at_k": hit_at_k,
        "rank": rank,
        "mrr": mrr,
        "ndcg_at_10": ndcg_at_10,
    }


def _aggregate_mode(scored: list[dict]) -> dict:
    """Mean Hit@1/3/5/10, MRR, NDCG@10 across scored items."""
    n = len(scored) or 1
    return {
        "hit_at_1": sum(1 for s in scored if s["hit_at_k"].get(1)) / n,
        "hit_at_3": sum(1 for s in scored if s["hit_at_k"].get(3)) / n,
        "hit_at_5": sum(1 for s in scored if s["hit_at_k"].get(5)) / n,
        "hit_at_10": sum(1 for s in scored if s["hit_at_k"].get(10)) / n,
        "mrr": sum(s["mrr"] for s in scored) / n,
        "ndcg_at_10": sum(s["ndcg_at_10"] for s in scored) / n,
        "scored_count": len(scored),
    }


def run_downstream_proxy_pass(
    conn: sqlite3.Connection,
    qa_items: list[dict],
    embedding_fn: Callable[[str], list[float]],
    modes: list[str] | tuple[str, ...] = DEFAULT_MODES,
    k: int = 10,
    *,
    search_fn: Callable[..., list[Any]] | None = None,
) -> dict:
    """Run retrieval × ``modes`` over ``qa_items`` and aggregate.

    ``embedding_fn`` takes a question and returns a query vector.
    ``search_fn`` is the retrieval callable; defaults to
    :func:`hippo_brain.retrieval.search` so tests can inject a stub
    without monkeypatching.
    """
    search = search_fn or retrieval.search
    per_mode: dict[str, dict] = {}
    per_item_scores: list[dict] = []

    for mode in modes:
        scored: list[dict] = []
        for item in qa_items:
            query = item["question"]
            query_vec = embedding_fn(query)
            results = search(conn, query, query_vec, mode=mode, limit=k)
            score = score_single_retrieval(results, item["golden_event_id"])
            score["qa_id"] = item.get("qa_id")
            score["mode"] = mode
            scored.append(score)
            per_item_scores.append(score)
        per_mode[mode] = _aggregate_mode(scored)

    return {
        "modes": per_mode,
        "qa_count": len(qa_items),
        "k": k,
        "per_item": per_item_scores,
    }


def run_ask_synthesis_sample(
    qa_items: list[dict],
    ask_fn: Callable[[str], str],
    sample_size: int = 10,
    seed: int = 42,
) -> dict:
    """Deterministically sample ``sample_size`` items and check keyword hits.

    Items are sorted by ``qa_id``; the same ``seed`` always picks the same
    indices. For each sampled item, ``ask_fn(question)`` is called and
    any ``acceptable_answer_keyword`` appearing in the response
    (case-insensitive) counts as a hit.
    """
    if not qa_items or sample_size <= 0:
        return {"sampled": 0, "keyword_hit_rate": 0.0}

    items_sorted = sorted(qa_items, key=lambda it: it.get("qa_id", ""))
    n = len(items_sorted)
    picks = [items_sorted[(seed + i) % n] for i in range(sample_size)]

    hits = 0
    for item in picks:
        response = (ask_fn(item["question"]) or "").lower()
        keywords = item.get("acceptable_answer_keywords", []) or []
        if any(kw.lower() in response for kw in keywords):
            hits += 1

    return {"sampled": sample_size, "keyword_hit_rate": hits / sample_size}
