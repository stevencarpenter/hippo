"""Optional LLM listwise reranking of retrieval results.

Config-gated behind ``[retrieval] rerank`` (off by default — it adds one
synthesis-model round-trip of latency per query). When enabled, the RAG
pipeline over-fetches ``rerank_pool`` candidates from hybrid retrieval, asks
the local query model to order them by relevance to the question in a single
listwise call, and keeps the top ``limit``.

The reranker only *reorders* — retrieval scores are preserved on each result
so downstream score displays and confidence factors stay meaningful. Any
failure (LLM error, unparseable output) falls back to the original retrieval
order: reranking must never make a query fail.
"""

from __future__ import annotations

import json
import logging
import re

from hippo_brain.retrieval import SearchResult

logger = logging.getLogger("hippo_brain.rerank")

# Per-candidate text budget in the rerank prompt. Summaries are short; the
# embed_text slice adds identifier density without blowing up the prompt.
_SUMMARY_CAP = 300
_DETAIL_CAP = 300

_RERANK_SYSTEM_PROMPT = (
    "You rank search results for relevance to a question about a developer's "
    "past activity. You are given a question and a numbered list of candidate "
    "notes. Respond with ONLY a JSON array of the candidate numbers ordered "
    "from most to least relevant, e.g. [3, 1, 2]. Include every number "
    "exactly once. No other text."
)


def _clip(text: str, cap: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= cap else text[: cap - 1] + "…"


def build_rerank_messages(question: str, results: list[SearchResult]) -> list[dict]:
    lines = [f"Question: {question}", "", "Candidates:"]
    for i, r in enumerate(results, 1):
        parts = [f"[{i}] {_clip(r.summary, _SUMMARY_CAP)}"]
        if r.embed_text:
            parts.append(f"    detail: {_clip(r.embed_text, _DETAIL_CAP)}")
        if r.commands_raw:
            parts.append(f"    commands: {_clip(r.commands_raw, _DETAIL_CAP)}")
        lines.extend(parts)
    return [
        {"role": "system", "content": _RERANK_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def parse_ranking(text: str, n: int) -> list[int] | None:
    """Parse the model's ranking into 0-based indices over ``n`` candidates.

    Tolerates prose around the JSON array (grabs the first ``[...]`` span).
    Out-of-range and duplicate entries are dropped; candidates the model
    omitted are appended in their original order so nothing is lost. Returns
    ``None`` when no usable array is found.
    """
    if not text:
        return None
    # Allow a leading "-" and "." too: a model that emits [3.0, 1.0] (a valid,
    # if unnecessary, JSON float) shouldn't lose the entire ranking just
    # because the regex's character class didn't admit '.'.
    match = re.search(r"\[[\d,.\s-]*\]", text)
    if match is None:
        return None
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, list):
        return None
    order: list[int] = []
    seen: set[int] = set()
    for entry in raw:
        if isinstance(entry, bool):
            continue
        if isinstance(entry, float):
            if not entry.is_integer():
                continue
            entry = int(entry)
        elif not isinstance(entry, int):
            continue
        idx = entry - 1
        if 0 <= idx < n and idx not in seen:
            seen.add(idx)
            order.append(idx)
    if not order:
        return None
    order.extend(i for i in range(n) if i not in seen)
    return order


async def rerank_results(
    inference_client,
    model: str,
    question: str,
    results: list[SearchResult],
    limit: int,
) -> list[SearchResult]:
    """Reorder ``results`` by LLM-judged relevance and cut to ``limit``.

    Falls back to the original order (cut to ``limit``) on any failure.
    """
    if len(results) <= 1:
        return results[:limit]
    messages = build_rerank_messages(question, results)
    try:
        answer = await inference_client.chat(messages, model=model)
    except Exception as e:
        logger.warning("rerank LLM call failed, keeping retrieval order: %s", e)
        return results[:limit]
    order = parse_ranking(str(answer or ""), len(results))
    if order is None:
        logger.warning("rerank output unparseable, keeping retrieval order: %r", answer)
        return results[:limit]
    return [results[i] for i in order][:limit]
