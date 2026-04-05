"""Tests for the Hippo RAG (retrieval-augmented generation) module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hippo_brain.rag import (
    _build_rag_prompt,
    _format_timestamp,
    _shape_rag_sources,
    ask,
    format_rag_response,
)


# -- Fixtures ---------------------------------------------------------------

SAMPLE_HITS = [
    {
        "_distance": 0.08,
        "summary": "Configured Firefox native messaging",
        "embed_text": "Set up NM host manifest for hippo daemon",
        "commands_raw": "cargo build --release && hippo daemon install --force",
        "cwd": "/Users/carpenter/projects/hippo",
        "git_branch": "main",
        "captured_at": 1743379200000,  # 2025-03-31
        "outcome": "success",
        "tags": '["firefox", "native-messaging"]',
        "key_decisions": "[]",
        "problems_encountered": "[]",
    },
    {
        "_distance": 0.15,
        "summary": "Added browser event schema v4",
        "embed_text": "Created browser_events table and enrichment queue",
        "commands_raw": "cargo test -p hippo-core",
        "cwd": "/Users/carpenter/projects/hippo",
        "git_branch": "main",
        "captured_at": 1743292800000,  # 2025-03-30
        "outcome": "success",
        "tags": '["schema", "browser"]',
        "key_decisions": "[]",
        "problems_encountered": "[]",
    },
]


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
        assert src["cwd"] == "/Users/carpenter/projects/hippo"
        assert src["git_branch"] == "main"
        assert src["timestamp"] == 1743379200000
        assert "cargo build" in src["commands_raw"]

    def test_empty_hits(self):
        assert _shape_rag_sources([]) == []

    def test_missing_fields_default_gracefully(self):
        sources = _shape_rag_sources([{"_distance": 0.1}])
        src = sources[0]
        assert src["summary"] == ""
        assert src["cwd"] == ""
        assert src["timestamp"] == 0


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
        assert "/Users/carpenter/projects/hippo" in user

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


class TestFormatRagResponse:
    def test_formats_answer_and_sources(self):
        result = {
            "answer": "You set it up by running install.",
            "sources": _shape_rag_sources(SAMPLE_HITS),
            "model": "test-model",
        }
        text = format_rag_response(result)
        assert "You set it up by running install." in text
        assert "[0.92]" in text
        assert "Configured Firefox native messaging" in text
        assert "test-model" in text

    def test_formats_error_result(self):
        result = {"error": "LM Studio down", "sources": [], "model": "test-model"}
        text = format_rag_response(result)
        assert "LM Studio down" in text

    def test_formats_empty_sources(self):
        result = {"answer": "No data found.", "sources": [], "model": "m"}
        text = format_rag_response(result)
        assert "No data found." in text


class TestAsk:
    @pytest.mark.asyncio
    async def test_returns_answer_and_sources(self):
        mock_client = AsyncMock()
        mock_client.embed.return_value = [[0.1] * 768]
        mock_client.chat.return_value = "The answer is 42."

        with patch("hippo_brain.rag.search_similar", return_value=SAMPLE_HITS):
            result = await ask(
                "what is the answer?",
                mock_client,
                MagicMock(),
                "test-model",
                "embed-model",
            )

        assert result["answer"] == "The answer is 42."
        assert result["model"] == "test-model"
        assert len(result["sources"]) == 2
        assert result["sources"][0]["score"] == 0.92

    @pytest.mark.asyncio
    async def test_passes_query_model_to_chat(self):
        mock_client = AsyncMock()
        mock_client.embed.return_value = [[0.1] * 768]
        mock_client.chat.return_value = "answer"

        with patch("hippo_brain.rag.search_similar", return_value=SAMPLE_HITS):
            await ask("q", mock_client, MagicMock(), "big-model", "embed-model")

        mock_client.chat.assert_called_once()
        assert mock_client.chat.call_args.kwargs["model"] == "big-model"

    @pytest.mark.asyncio
    async def test_embed_failure_returns_error(self):
        mock_client = AsyncMock()
        mock_client.embed.side_effect = Exception("connection refused")

        result = await ask("q", mock_client, MagicMock(), "m", "e")

        assert "error" in result
        assert "connection refused" in result["error"]
        assert result["sources"] == []

    @pytest.mark.asyncio
    async def test_chat_failure_returns_sources_without_answer(self):
        mock_client = AsyncMock()
        mock_client.embed.return_value = [[0.1] * 768]
        mock_client.chat.side_effect = Exception("model not loaded")

        with patch("hippo_brain.rag.search_similar", return_value=SAMPLE_HITS):
            result = await ask("q", mock_client, MagicMock(), "m", "e")

        assert "error" in result
        assert "model not loaded" in result["error"]
        assert len(result["sources"]) == 2

    @pytest.mark.asyncio
    async def test_no_results_returns_no_knowledge_message(self):
        mock_client = AsyncMock()
        mock_client.embed.return_value = [[0.1] * 768]

        with patch("hippo_brain.rag.search_similar", return_value=[]):
            result = await ask("q", mock_client, MagicMock(), "m", "e")

        assert "answer" in result
        assert "No relevant knowledge" in result["answer"]
        assert result["sources"] == []
        mock_client.chat.assert_not_called()
