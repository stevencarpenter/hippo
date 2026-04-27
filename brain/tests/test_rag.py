"""Tests for the Hippo RAG (retrieval-augmented generation) module."""

import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from hippo_brain.rag import (
    DEFAULT_MAX_CONTEXT_CHARS,
    _build_rag_prompt,
    _format_timestamp,
    _shape_rag_sources,
    ask,
    format_rag_response,
)
from hippo_brain.retrieval import Filters, SearchResult


# -- Fixtures ---------------------------------------------------------------

SAMPLE_HITS = [
    {
        "_distance": 0.08,
        "summary": "Configured Firefox native messaging",
        "embed_text": "Set up NM host manifest for hippo daemon",
        "commands_raw": "cargo build --release && hippo daemon install --force",
        "cwd": "/home/user/projects/hippo",
        "git_branch": "main",
        "captured_at": 1743379200000,  # 2025-03-31
        "outcome": "success",
        "tags": '["firefox", "native-messaging"]',
        "uuid": "uuid-1",
        "key_decisions": "[]",
        "problems_encountered": "[]",
    },
    {
        "_distance": 0.15,
        "summary": "Added browser event schema v4",
        "embed_text": "Created browser_events table and enrichment queue",
        "commands_raw": "cargo test -p hippo-core",
        "cwd": "/home/user/projects/hippo",
        "git_branch": "main",
        "captured_at": 1743292800000,  # 2025-03-30
        "outcome": "success",
        "tags": '["schema", "browser"]',
        "uuid": "uuid-2",
        "key_decisions": "[]",
        "problems_encountered": "[]",
    },
]


def _healthy_client(chat_return="The answer is 42."):
    """AsyncMock wired with a passing health_check + embed + chat."""
    client = AsyncMock()
    client.base_url = "http://mock:1234/v1"
    client.health_check.return_value = {"ok": True, "reason": None, "loaded_models": ["m"]}
    client.embed.return_value = [[0.1] * 768]
    client.chat.return_value = chat_return
    return client


# -- Pure helpers -----------------------------------------------------------


class TestFormatTimestamp:
    def test_formats_epoch_ms_to_date(self):
        assert _format_timestamp(1743379200000) == "2025-03-31"

    def test_zero_returns_epoch(self):
        assert _format_timestamp(0) == "1970-01-01"


class TestShapeRagSources:
    def test_converts_distance_to_score(self):
        sources = _shape_rag_sources(SAMPLE_HITS)
        assert sources[0]["score"] == 0.92
        assert sources[1]["score"] == 0.85

    def test_includes_required_fields(self):
        sources = _shape_rag_sources(SAMPLE_HITS)
        src = sources[0]
        assert src["summary"] == "Configured Firefox native messaging"
        assert src["cwd"] == "/home/user/projects/hippo"
        assert src["git_branch"] == "main"
        assert src["timestamp"] == 1743379200000
        assert "cargo build" in src["commands_raw"]
        assert src["uuid"] == "uuid-1"

    def test_empty_hits(self):
        assert _shape_rag_sources([]) == []

    def test_missing_fields_default_gracefully(self):
        sources = _shape_rag_sources([{"_distance": 0.1}])
        src = sources[0]
        assert src["summary"] == ""
        assert src["cwd"] == ""
        assert src["timestamp"] == 0

    def test_filters_low_score_sources(self):
        hits = [
            {"_distance": 0.08, "summary": "good"},
            {"_distance": 1.5, "summary": "bad"},  # score = -0.5
        ]
        sources = _shape_rag_sources(hits)
        assert len(sources) == 1
        assert sources[0]["summary"] == "good"

    def test_caps_at_default_limit(self):
        from hippo_brain.rag import DEFAULT_SOURCES_LIMIT

        hits = [{"_distance": 0.1, "summary": f"hit {i}"} for i in range(DEFAULT_SOURCES_LIMIT + 5)]
        sources = _shape_rag_sources(hits)
        assert len(sources) == DEFAULT_SOURCES_LIMIT

    def test_respects_explicit_limit(self):
        hits = [{"_distance": 0.1, "summary": f"hit {i}"} for i in range(10)]
        sources = _shape_rag_sources(hits, limit=3)
        assert len(sources) == 3


class TestBuildRagPrompt:
    def test_returns_system_and_user_messages(self):
        messages = _build_rag_prompt("how did I set up NM?", SAMPLE_HITS)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_system_prompt_contains_instructions(self):
        messages = _build_rag_prompt("test", SAMPLE_HITS)
        system = messages[0]["content"]
        assert "ONLY the context" in system
        assert "Do not make up" in system

    def test_user_message_contains_question(self):
        messages = _build_rag_prompt("how did I set up NM?", SAMPLE_HITS)
        user = messages[1]["content"]
        assert "how did I set up NM?" in user

    def test_user_message_contains_context_from_hits(self):
        messages = _build_rag_prompt("test", SAMPLE_HITS)
        user = messages[1]["content"]
        assert "Configured Firefox native messaging" in user
        assert "cargo build --release" in user
        assert "/home/user/projects/hippo" in user

    def test_context_blocks_are_numbered(self):
        messages = _build_rag_prompt("test", SAMPLE_HITS)
        user = messages[1]["content"]
        assert "[1]" in user
        assert "[2]" in user

    def test_tags_parsed_from_json_string(self):
        messages = _build_rag_prompt("test", SAMPLE_HITS)
        user = messages[1]["content"]
        assert "firefox" in user
        assert "native-messaging" in user

    def test_malformed_tags_do_not_crash(self):
        hit = dict(SAMPLE_HITS[0])
        hit["tags"] = "{{not json"
        messages = _build_rag_prompt("test", [hit])
        assert "Tags:" not in messages[1]["content"]

    def test_context_budget_truncates_oversized_payload(self):
        """Long fields must be truncated when total exceeds max_chars."""
        huge = "X" * 50_000
        hits = [
            dict(SAMPLE_HITS[0], embed_text=huge, commands_raw=huge),
            dict(SAMPLE_HITS[1], embed_text=huge, commands_raw=huge),
        ]
        messages = _build_rag_prompt("q", hits, max_chars=4000)
        context = messages[1]["content"]
        # Leave headroom for system message + question text.
        assert len(context) < 4000 + 500
        assert "Configured Firefox native messaging" in context  # structural preserved
        assert "XXXX" in context  # payload truncated but still present
        # Ellipsis sentinel indicating truncation occurred
        assert "…" in context

    def test_small_context_passes_through_unchanged(self):
        messages = _build_rag_prompt("q", SAMPLE_HITS, max_chars=DEFAULT_MAX_CONTEXT_CHARS)
        context = messages[1]["content"]
        # No truncation ellipsis should appear for this small corpus
        assert "…" not in context

    def test_design_decisions_rendered_in_context(self):
        """Issue #98 F3: when a hit carries `design_decisions`, the
        considered/chosen/reason structure must surface in the synthesis prompt
        so the LLM can answer "why X over Y" questions accurately.
        """
        hit = dict(
            SAMPLE_HITS[0],
            design_decisions=[
                {
                    "considered": "Stage GUI inside /Applications/.hippo-staging/HippoGUI.app",
                    "chosen": "Stage GUI in $TMPDIR, mv to /Applications atomically",
                    "reason": "Launch Services crawls /Applications recursively",
                }
            ],
        )
        messages = _build_rag_prompt("why $TMPDIR?", [hit])
        user = messages[1]["content"]
        assert "Design decisions:" in user
        assert "Stage GUI inside /Applications/.hippo-staging" in user
        assert "Stage GUI in $TMPDIR" in user
        assert "Launch Services crawls /Applications" in user

    def test_design_decisions_omitted_when_empty(self):
        hit = dict(SAMPLE_HITS[0], design_decisions=[])
        messages = _build_rag_prompt("test", [hit])
        assert "Design decisions:" not in messages[1]["content"]

    def test_design_decisions_partial_entry_skipped_in_render(self):
        hit = dict(
            SAMPLE_HITS[0],
            design_decisions=[
                {"considered": "X", "chosen": "Y", "reason": "Z"},
                {"considered": "A", "chosen": "B"},  # missing reason — render must skip
                "garbage",  # wrong type — render must skip
            ],
        )
        messages = _build_rag_prompt("test", [hit])
        user = messages[1]["content"]
        assert "considered 'X'" in user
        assert "considered 'A'" not in user

    def test_design_decisions_are_truncated_with_context_budget(self):
        hit = dict(
            SAMPLE_HITS[0],
            embed_text="",
            commands_raw="",
            design_decisions=[
                {
                    "considered": "A" * 400,
                    "chosen": "B" * 400,
                    "reason": "C" * 400,
                }
            ],
        )
        messages = _build_rag_prompt("test", [hit], max_chars=220)
        user = messages[1]["content"]
        assert "Design decisions:" in user
        assert "…" in user
        assert ("A" * 200) not in user


class TestFormatRagResponse:
    def test_formats_answer_and_sources(self):
        result = {
            "answer": "You set it up by running install.",
            "sources": _shape_rag_sources(SAMPLE_HITS),
            "model": "test-model",
            "degraded": False,
        }
        text = format_rag_response(result)
        assert "You set it up by running install." in text
        assert "[92%]" in text
        assert "Configured Firefox native messaging" in text
        assert "Sources:" in text

    def test_formats_error_result(self):
        result = {"error": "LM Studio down", "sources": [], "model": "test-model"}
        text = format_rag_response(result)
        assert "LM Studio down" in text

    def test_formats_empty_sources(self):
        result = {"answer": "No data found.", "sources": [], "model": "m"}
        text = format_rag_response(result)
        assert "No data found." in text

    def test_sources_are_numbered(self):
        result = {
            "answer": "answer",
            "sources": _shape_rag_sources(SAMPLE_HITS),
            "model": "m",
            "degraded": False,
        }
        text = format_rag_response(result)
        assert "  1. [" in text
        assert "  2. [" in text

    def test_degraded_renders_raw_notes_header(self):
        result = {
            "answer": None,
            "error": "synthesize failed [TimeoutException] model='m' endpoint=x: read timeout",
            "sources": _shape_rag_sources(SAMPLE_HITS),
            "model": "m",
            "degraded": True,
        }
        text = format_rag_response(result)
        assert "Raw notes" in text
        assert "(degraded:" in text
        assert "read timeout" in text
        # Degraded output should surface commands so the agent still gets signal.
        assert "cargo build" in text


# -- ask() integration -------------------------------------------------------


class TestAsk:
    @pytest.mark.asyncio
    async def test_returns_answer_and_sources(self):
        client = _healthy_client()
        with patch("hippo_brain.rag.search_similar", return_value=SAMPLE_HITS):
            result = await ask("what is the answer?", client, MagicMock(), "m", "e")

        assert result["answer"] == "The answer is 42."
        assert result["model"] == "m"
        assert result["degraded"] is False
        assert len(result["sources"]) == 2
        assert result["sources"][0]["score"] == 0.92

    @pytest.mark.asyncio
    async def test_passes_query_model_to_chat(self):
        client = _healthy_client(chat_return="answer")
        with patch("hippo_brain.rag.search_similar", return_value=SAMPLE_HITS):
            await ask("q", client, MagicMock(), "big-model", "embed-model")

        client.chat.assert_called_once()
        assert client.chat.call_args.kwargs["model"] == "big-model"

    @pytest.mark.asyncio
    async def test_preflight_failure_returns_degraded_without_calling_embed(self):
        client = _healthy_client()
        client.health_check.return_value = {
            "ok": False,
            "reason": "query model 'big' not loaded. Loaded: ['small']",
            "loaded_models": ["small"],
        }

        result = await ask("q", client, MagicMock(), "big", "embed")

        assert result["degraded"] is True
        assert result["answer"] is None
        assert result["stage"] == "preflight"
        assert "not loaded" in result["error"]
        client.embed.assert_not_called()
        client.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_preflight_exception_is_trapped(self):
        client = _healthy_client()
        client.health_check.side_effect = RuntimeError("socket gone")

        result = await ask("q", client, MagicMock(), "m", "e")

        assert result["degraded"] is True
        assert result["stage"] == "preflight"
        assert "socket gone" in result["error"]
        assert "RuntimeError" in result["error"]

    @pytest.mark.asyncio
    async def test_skip_preflight_bypasses_health_check(self):
        client = _healthy_client()
        with patch("hippo_brain.rag.search_similar", return_value=SAMPLE_HITS):
            result = await ask("q", client, MagicMock(), "m", "e", skip_preflight=True)
        assert result["degraded"] is False
        client.health_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_embed_timeout_surfaces_type_model_endpoint(self):
        client = _healthy_client()
        client.embed.side_effect = httpx.TimeoutException("read timeout")

        result = await ask("q", client, MagicMock(), "m", "e-model")

        assert result["degraded"] is True
        assert result["stage"] == "embed"
        assert "TimeoutException" in result["error"]
        assert "e-model" in result["error"]
        assert "http://mock:1234/v1" in result["error"]
        assert "read timeout" in result["error"]
        assert result["sources"] == []

    @pytest.mark.asyncio
    async def test_embed_generic_exception_with_empty_message(self):
        """Regression: the old code produced 'Synthesis failed: ' with no detail."""
        client = _healthy_client()
        # Exception with empty str — previously rendered as empty error.
        client.embed.side_effect = Exception("")

        result = await ask("q", client, MagicMock(), "m", "e")

        assert result["degraded"] is True
        # Even with empty str(e), error must carry structural info.
        assert "Exception" in result["error"]
        assert "embed" in result["error"]
        assert result["error"].strip() != "embed failed:"

    @pytest.mark.asyncio
    async def test_embed_empty_response_degrades(self):
        client = _healthy_client()
        client.embed.return_value = []

        result = await ask("q", client, MagicMock(), "m", "e")

        assert result["degraded"] is True
        assert result["stage"] == "embed"
        assert "no vectors" in result["error"]

    @pytest.mark.asyncio
    async def test_synthesis_failure_returns_degraded_with_sources(self):
        client = _healthy_client()
        client.chat.side_effect = httpx.HTTPError("model not loaded")

        with patch("hippo_brain.rag.search_similar", return_value=SAMPLE_HITS):
            result = await ask("q", client, MagicMock(), "query-m", "e")

        assert result["degraded"] is True
        assert result["answer"] is None
        assert result["stage"] == "synthesize"
        assert "HTTPError" in result["error"]
        assert "query-m" in result["error"]
        assert "model not loaded" in result["error"]
        assert len(result["sources"]) == 2

    @pytest.mark.asyncio
    async def test_synthesis_empty_response_degrades(self):
        client = _healthy_client(chat_return="   ")

        with patch("hippo_brain.rag.search_similar", return_value=SAMPLE_HITS):
            result = await ask("q", client, MagicMock(), "m", "e")

        assert result["degraded"] is True
        assert result["stage"] == "synthesize"
        assert "empty" in result["error"].lower()
        assert len(result["sources"]) == 2

    @pytest.mark.asyncio
    async def test_no_results_returns_no_knowledge_message(self):
        client = _healthy_client()

        with patch("hippo_brain.rag.search_similar", return_value=[]):
            result = await ask("q", client, MagicMock(), "m", "e")

        assert result["degraded"] is False
        assert "No relevant knowledge" in result["answer"]
        assert result["sources"] == []
        client.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_retrieval_exception_degrades(self):
        client = _healthy_client()

        with patch("hippo_brain.rag.search_similar", side_effect=RuntimeError("index corrupt")):
            result = await ask("q", client, MagicMock(), "m", "e")

        assert result["degraded"] is True
        assert result["stage"] == "retrieve"
        assert "index corrupt" in result["error"]
        assert "RuntimeError" in result["error"]
        client.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_oversized_context_is_capped_before_chat(self):
        """When retrieval returns huge hits, the chat prompt must respect the budget."""
        client = _healthy_client(chat_return="ok")
        huge_hit = dict(SAMPLE_HITS[0], embed_text="Z" * 100_000, commands_raw="Z" * 100_000)

        with patch("hippo_brain.rag.search_similar", return_value=[huge_hit, huge_hit]):
            await ask("q", client, MagicMock(), "m", "e", max_context_chars=3000)

        messages = (
            client.chat.call_args.args[0]
            if client.chat.call_args.args
            else client.chat.call_args.kwargs.get("messages")
        )
        if messages is None:
            # chat was called as chat(messages, model=...)
            messages = client.chat.call_args[0][0]
        user_content = messages[1]["content"]
        assert len(user_content) < 4500

    @pytest.mark.asyncio
    async def test_limit_caps_sources_returned(self):
        """ask(limit=N) must forward N to source shaping so sources <= N."""
        client = _healthy_client(chat_return="ok")
        many_hits = [dict(SAMPLE_HITS[0], uuid=f"u-{i}") for i in range(8)]

        with patch("hippo_brain.rag.search_similar", return_value=many_hits):
            result = await ask("q", client, MagicMock(), "m", "e", limit=3)

        assert len(result["sources"]) == 3


# -- Retrieval-router plumbing ----------------------------------------------


def _fake_search_result(**overrides):
    base = dict(
        uuid="abc-123",
        score=0.91,
        summary="Filtered result",
        embed_text="filtered embed",
        outcome="success",
        tags=["alpha", "beta"],
        cwd="/home/user/projects/hippo",
        git_branch="postgres",
        captured_at=1743379200000,
        design_decisions=[],
        linked_event_ids=[42, 43],
    )
    base.update(overrides)
    return SearchResult(**base)


class TestFilteredRetrievalRouting:
    @pytest.mark.asyncio
    async def test_flat_kwargs_route_through_retrieval_search(self):
        """Passing flat filter kwargs routes via retrieval.search with a Filters."""
        client = _healthy_client(chat_return="answered")
        sentinel_conn = sqlite3.connect(":memory:")
        try:
            with (
                patch("hippo_brain.rag.retrieval_search") as retrieval_mock,
                patch("hippo_brain.rag.search_similar") as legacy_mock,
            ):
                retrieval_mock.return_value = [_fake_search_result()]
                result = await ask(
                    "q",
                    client,
                    None,
                    "m",
                    "e",
                    project="/home/user/projects/hippo",
                    since=1743292800000,
                    source="claude",
                    branch="postgres",
                    conn=sentinel_conn,
                )

            assert result["degraded"] is False
            legacy_mock.assert_not_called()
            retrieval_mock.assert_called_once()
            kwargs = retrieval_mock.call_args.kwargs
            assert retrieval_mock.call_args.args[0] is sentinel_conn
            passed = kwargs["filters"]
            assert isinstance(passed, Filters)
            assert passed.project == "/home/user/projects/hippo"
            assert passed.since_ms == 1743292800000
            assert passed.source == "claude"
            assert passed.branch == "postgres"
            assert kwargs["mode"] == "hybrid"
            assert kwargs["limit"] == 10
        finally:
            sentinel_conn.close()

    @pytest.mark.asyncio
    async def test_filters_object_passed_through_unchanged(self):
        """Explicit Filters object is used verbatim (not rebuilt)."""
        client = _healthy_client(chat_return="ok")
        sentinel_conn = sqlite3.connect(":memory:")
        my_filters = Filters(project="/x", since_ms=100, source="shell")
        try:
            with patch("hippo_brain.rag.retrieval_search") as retrieval_mock:
                retrieval_mock.return_value = [_fake_search_result()]
                await ask(
                    "q",
                    client,
                    None,
                    "m",
                    "e",
                    filters=my_filters,
                    conn=sentinel_conn,
                    mode="semantic",
                )

            assert retrieval_mock.call_args.kwargs["filters"] is my_filters
            assert retrieval_mock.call_args.kwargs["mode"] == "semantic"
        finally:
            sentinel_conn.close()

    @pytest.mark.asyncio
    async def test_no_filters_with_non_sqlite_handle_uses_legacy(self):
        """Backward-compat: if the handle isn't a sqlite3.Connection, fall back to legacy."""
        client = _healthy_client(chat_return="legacy")
        table = MagicMock(name="lancedb_table")

        with (
            patch("hippo_brain.rag.retrieval_search") as rs_mock,
            patch("hippo_brain.rag.search_similar", return_value=SAMPLE_HITS) as legacy_mock,
        ):
            result = await ask("q", client, table, "m", "e")

        assert result["answer"] == "legacy"
        rs_mock.assert_not_called()
        legacy_mock.assert_called_once()
        assert legacy_mock.call_args.args[0] is table

    @pytest.mark.asyncio
    async def test_no_filters_with_sqlite_conn_uses_hybrid(self):
        """Issue #28 fix: vanilla ask with a real sqlite3.Connection must route
        through retrieval.search (hybrid RRF + MMR) so diverse nodes surface
        even when no filters are supplied. Prior behavior was pure-semantic KNN,
        which biased toward a single cluster for broad questions.
        """
        client = _healthy_client(chat_return="hybrid")
        conn = sqlite3.connect(":memory:")
        try:
            with (
                patch("hippo_brain.rag.retrieval_search") as rs_mock,
                patch("hippo_brain.rag.search_similar") as legacy_mock,
            ):
                rs_mock.return_value = [_fake_search_result()]
                result = await ask("q", client, conn, "m", "e")

            assert result["answer"] == "hybrid"
            legacy_mock.assert_not_called()
            rs_mock.assert_called_once()
            assert rs_mock.call_args.kwargs["filters"] is None
            assert rs_mock.call_args.kwargs["mode"] == "hybrid"
        finally:
            conn.close()

    @pytest.mark.asyncio
    async def test_filters_path_surfaces_uuid_and_linked_events_in_sources(self):
        """SearchResult fields (uuid, linked_event_ids) must flow into sources."""
        client = _healthy_client(chat_return="ok")
        sentinel_conn = sqlite3.connect(":memory:")
        try:
            with patch("hippo_brain.rag.retrieval_search") as retrieval_mock:
                retrieval_mock.return_value = [
                    _fake_search_result(uuid="u-1", linked_event_ids=[1, 2, 3]),
                    _fake_search_result(uuid="u-2", score=0.5, linked_event_ids=[]),
                ]
                result = await ask(
                    "q",
                    client,
                    None,
                    "m",
                    "e",
                    project="/home/user",
                    conn=sentinel_conn,
                )

            assert [s["uuid"] for s in result["sources"]] == ["u-1", "u-2"]
            assert result["sources"][0]["linked_event_ids"] == [1, 2, 3]
            assert result["sources"][1]["linked_event_ids"] == []
            # Score round-trips from SearchResult.score through the _distance adapter.
            assert result["sources"][0]["score"] == pytest.approx(0.91, abs=0.001)
        finally:
            sentinel_conn.close()

    @pytest.mark.asyncio
    async def test_filters_path_surfaces_design_decisions_in_prompt(self):
        client = _healthy_client(chat_return="ok")
        sentinel_conn = sqlite3.connect(":memory:")
        try:
            with patch("hippo_brain.rag.retrieval_search") as retrieval_mock:
                retrieval_mock.return_value = [
                    _fake_search_result(
                        design_decisions=[
                            {
                                "considered": "sqlite direct reads",
                                "chosen": "hybrid retrieval",
                                "reason": "better recall",
                            }
                        ]
                    )
                ]
                await ask(
                    "why hybrid?", client, None, "m", "e", project="/home/user", conn=sentinel_conn
                )

            prompt = client.chat.call_args.args[0][1]["content"]
            assert "Design decisions:" in prompt
            assert "sqlite direct reads" in prompt
            assert "hybrid retrieval" in prompt
            assert "better recall" in prompt
        finally:
            sentinel_conn.close()

    @pytest.mark.asyncio
    async def test_filters_without_connection_degrades(self):
        """Filters requested but no conn (and vector_table is None) → degraded."""
        client = _healthy_client()

        with patch("hippo_brain.rag.retrieval_search") as retrieval_mock:
            result = await ask("q", client, None, "m", "e", project="/x")

        assert result["degraded"] is True
        assert result["stage"] == "retrieve"
        assert "no sqlite connection" in result["error"]
        retrieval_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_filters_path_degrades_when_retrieval_raises(self):
        client = _healthy_client()
        sentinel_conn = sqlite3.connect(":memory:")
        try:
            with patch(
                "hippo_brain.rag.retrieval_search", side_effect=RuntimeError("vec0 missing")
            ):
                result = await ask(
                    "q",
                    client,
                    None,
                    "m",
                    "e",
                    filters=Filters(project="/x"),
                    conn=sentinel_conn,
                )

            assert result["degraded"] is True
            assert result["stage"] == "retrieve"
            assert "vec0 missing" in result["error"]
            client.chat.assert_not_called()
        finally:
            sentinel_conn.close()
