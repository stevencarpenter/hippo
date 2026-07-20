"""Unit tests for hippo_brain.rerank (LLM listwise reranking)."""

from __future__ import annotations

import asyncio

from hippo_brain.rerank import build_rerank_messages, parse_ranking, rerank_results
from hippo_brain.retrieval import SearchResult


def _result(uuid: str, score: float = 0.5) -> SearchResult:
    return SearchResult(
        uuid=uuid,
        score=score,
        summary=f"summary {uuid}",
        embed_text=f"detail {uuid}",
        outcome=None,
        tags=[],
        cwd="",
        git_branch="",
        captured_at=1_700_000_000_000,
    )


class FakeClient:
    def __init__(self, answer=None, exc: Exception | None = None):
        self.answer = answer
        self.exc = exc
        self.calls: list[list[dict]] = []

    async def chat(self, messages, model=""):
        self.calls.append(messages)
        if self.exc is not None:
            raise self.exc
        return self.answer


class TestParseRanking:
    def test_clean_array(self):
        assert parse_ranking("[3, 1, 2]", 3) == [2, 0, 1]

    def test_array_embedded_in_prose(self):
        assert parse_ranking("Sure! The ranking is [2, 1].", 2) == [1, 0]

    def test_omitted_candidates_appended_in_original_order(self):
        assert parse_ranking("[3]", 4) == [2, 0, 1, 3]

    def test_duplicates_and_out_of_range_dropped(self):
        assert parse_ranking("[2, 2, 9, 1]", 3) == [1, 0, 2]

    def test_garbage_returns_none(self):
        assert parse_ranking("no array here", 3) is None
        assert parse_ranking("", 3) is None
        assert parse_ranking("[]", 3) is None


class TestRerankResults:
    def test_reorders_and_cuts_to_limit(self):
        results = [_result("a"), _result("b"), _result("c")]
        client = FakeClient(answer="[3, 2, 1]")
        out = asyncio.run(rerank_results(client, "m", "q", results, limit=2))
        assert [r.uuid for r in out] == ["c", "b"]
        # Retrieval scores preserved — rerank only reorders.
        assert all(r.score == 0.5 for r in out)

    def test_llm_error_keeps_retrieval_order(self):
        results = [_result("a"), _result("b")]
        client = FakeClient(exc=RuntimeError("model offline"))
        out = asyncio.run(rerank_results(client, "m", "q", results, limit=2))
        assert [r.uuid for r in out] == ["a", "b"]

    def test_unparseable_output_keeps_retrieval_order(self):
        results = [_result("a"), _result("b")]
        client = FakeClient(answer="I cannot rank these.")
        out = asyncio.run(rerank_results(client, "m", "q", results, limit=2))
        assert [r.uuid for r in out] == ["a", "b"]

    def test_single_result_skips_llm_call(self):
        results = [_result("a")]
        client = FakeClient(answer="[1]")
        out = asyncio.run(rerank_results(client, "m", "q", results, limit=5))
        assert [r.uuid for r in out] == ["a"]
        assert client.calls == []


def test_build_rerank_messages_numbers_candidates():
    results = [_result("a"), _result("b")]
    messages = build_rerank_messages("why did the build fail?", results)
    assert messages[0]["role"] == "system"
    user = messages[1]["content"]
    assert "why did the build fail?" in user
    assert "[1] summary a" in user
    assert "[2] summary b" in user


class TestParseRankingRegressions:
    def test_accepts_integer_valued_floats(self):
        assert parse_ranking("[3.0, 1.0, 2.0]", 3) == [2, 0, 1]

    def test_rejects_non_integer_floats(self):
        # 1.5 isn't a valid 1-based rank; it's dropped like any other
        # out-of-range/malformed entry, not accepted as index 0 or 1.
        assert parse_ranking("[1.5, 2]", 2) == [1, 0]
