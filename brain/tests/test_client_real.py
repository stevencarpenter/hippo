"""Tests for the real InferenceClient HTTP methods using httpx mock transport."""

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from hippo_brain import client as client_module
from hippo_brain.client import InferenceClient


@pytest.fixture
def client():
    return InferenceClient(base_url="http://localhost:1234/v1", timeout=5.0)


def _mock_response(status_code: int, body: dict) -> httpx.Response:
    """Create an httpx.Response with a fake request attached (needed for raise_for_status)."""
    resp = httpx.Response(
        status_code,
        json=body,
        request=httpx.Request("POST", "http://localhost:1234/v1/fake"),
    )
    return resp


async def test_chat_parses_response(client):
    """chat() should POST to /chat/completions and extract message content."""
    mock_resp = _mock_response(
        200,
        {"choices": [{"message": {"role": "assistant", "content": "Hello from the model"}}]},
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        result = await client.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="test-model",
        )
    assert result == "Hello from the model"


async def test_chat_raises_on_http_error(client):
    """chat() should raise on non-200 response."""
    mock_resp = _mock_response(500, {"error": "server error"})
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(httpx.HTTPStatusError):
            await client.chat(messages=[{"role": "user", "content": "hi"}])


async def test_chat_400_includes_response_body_in_error(client):
    """4xx responses must surface LM Studio's error body, not just the status string.

    Without the body, the brain logs only `400 Bad Request for url ...`, hiding
    the actual reason LM Studio rejected the request (e.g. "Context history must
    not be empty.", "max_tokens exceeds context window", etc.).
    """
    mock_resp = _mock_response(400, {"error": "Context history must not be empty."})
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(httpx.HTTPStatusError, match="Context history must not be empty"):
            await client.chat(messages=[{"role": "user", "content": "hi"}])


async def test_embed_400_includes_response_body_in_error(client):
    """Same body-capture behavior must apply to embeddings."""
    mock_resp = _mock_response(400, {"error": "embedding model not loaded"})
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(httpx.HTTPStatusError, match="embedding model not loaded"):
            await client.embed(texts=["hi"])


async def test_chat_error_with_empty_body_does_not_blow_up(client):
    """If LM Studio returns an HTTP error with no body, behavior matches the
    original raise_for_status (no synthetic 'Body:' suffix)."""
    mock_resp = httpx.Response(
        503,
        content=b"",
        request=httpx.Request("POST", "http://localhost:1234/v1/fake"),
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.chat(messages=[{"role": "user", "content": "hi"}])
    assert "Body:" not in str(exc_info.value)


async def test_chat_400_with_crash_body_increments_crash_counter(client, monkeypatch):
    """LM Studio's "model has crashed" body must increment the crash counter so
    diagnostic dashboards can track worker kills independently of which capture
    path triggered them. The signal is otherwise hidden when queue-level retry
    absorbs the failure."""
    mock_counter = MagicMock()
    monkeypatch.setattr(client_module, "_inference_crashes", mock_counter)
    mock_resp = _mock_response(
        400,
        {"error": "The model has crashed without additional information. (Exit code: null)"},
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(httpx.HTTPStatusError):
            await client.chat(messages=[{"role": "user", "content": "hi"}])
    mock_counter.add.assert_called_once_with(1)


async def test_chat_400_with_non_crash_body_does_not_increment_crash_counter(client, monkeypatch):
    """Other 400 reasons (malformed input, context overflow, etc.) must NOT be
    counted as crashes — false positives would erode the signal."""
    mock_counter = MagicMock()
    monkeypatch.setattr(client_module, "_inference_crashes", mock_counter)
    mock_resp = _mock_response(400, {"error": "Context history must not be empty."})
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(httpx.HTTPStatusError):
            await client.chat(messages=[{"role": "user", "content": "hi"}])
    mock_counter.add.assert_not_called()


async def test_embed_400_with_crash_body_also_increments_crash_counter(client, monkeypatch):
    """Counter is path-agnostic: embed() crashes are equally diagnostic."""
    mock_counter = MagicMock()
    monkeypatch.setattr(client_module, "_inference_crashes", mock_counter)
    mock_resp = _mock_response(
        400,
        {"error": "The model has crashed without additional information."},
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(httpx.HTTPStatusError):
            await client.embed(texts=["hi"])
    mock_counter.add.assert_called_once_with(1)


async def test_chat_400_crash_match_is_case_insensitive(client, monkeypatch):
    """Capitalization drift across LM Studio versions (e.g. "Model Has Crashed"
    or all-caps) must still increment the counter — the substring match is
    intentionally case-insensitive."""
    mock_counter = MagicMock()
    monkeypatch.setattr(client_module, "_inference_crashes", mock_counter)
    mock_resp = _mock_response(400, {"error": "MODEL HAS CRASHED unexpectedly during inference."})
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(httpx.HTTPStatusError):
            await client.chat(messages=[{"role": "user", "content": "hi"}])
    mock_counter.add.assert_called_once_with(1)


async def test_chat_400_body_extraction_failure_does_not_mask_http_error(client):
    """If reading resp.text itself raises (decode error, body unread, etc.), the
    helper must still raise the original HTTPStatusError — not the body-extraction
    exception. Otherwise a transient extraction failure would silently replace the
    real LM Studio error in caller view (silent-fallback anti-pattern)."""
    mock_resp = _mock_response(400, {"error": "real LM Studio reason"})
    with patch.object(httpx.Response, "text", new_callable=PropertyMock) as mock_text:
        mock_text.side_effect = UnicodeDecodeError("utf-8", b"", 0, 1, "bad")
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await client.chat(messages=[{"role": "user", "content": "hi"}])
    # Original HTTPStatusError surfaced — extraction error did not mask it.
    assert isinstance(exc_info.value, httpx.HTTPStatusError)
    assert "Body:" not in str(exc_info.value)
    # Status code survives — guards against a regression where the synthetic
    # error is raised but with empty/missing fields.
    assert exc_info.value.response.status_code == 400


async def test_embed_returns_embeddings(client):
    """embed() should POST to /embeddings and return list of vectors."""
    mock_resp = _mock_response(
        200,
        {
            "data": [
                {"embedding": [0.1, 0.2, 0.3]},
                {"embedding": [0.4, 0.5, 0.6]},
            ]
        },
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        result = await client.embed(texts=["hello", "world"], model="embed-model")
    assert len(result) == 2
    assert result[0] == [0.1, 0.2, 0.3]
    assert result[1] == [0.4, 0.5, 0.6]


async def test_embed_raises_on_http_error(client):
    """embed() should raise on non-200 response."""
    mock_resp = _mock_response(500, {"error": "server error"})
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(httpx.HTTPStatusError):
            await client.embed(texts=["hello"])


async def test_is_reachable_returns_true(client):
    """is_reachable() returns True when /models returns 200."""
    mock_resp = httpx.Response(
        200,
        json={"data": []},
        request=httpx.Request("GET", "http://localhost:1234/v1/models"),
    )
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        assert await client.is_reachable() is True


async def test_is_reachable_returns_false_on_connection_error(client):
    """is_reachable() returns False on connection error."""
    with patch(
        "httpx.AsyncClient.get",
        new_callable=AsyncMock,
        side_effect=httpx.ConnectError("Connection refused"),
    ):
        assert await client.is_reachable() is False


async def test_is_reachable_returns_false_on_non_200(client):
    """is_reachable() returns False when status is not 200."""
    mock_resp = httpx.Response(
        503,
        json={"error": "unavailable"},
        request=httpx.Request("GET", "http://localhost:1234/v1/models"),
    )
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        assert await client.is_reachable() is False


async def test_chat_with_custom_params(client):
    """chat() passes temperature and max_tokens correctly."""
    mock_resp = _mock_response(
        200,
        {"choices": [{"message": {"role": "assistant", "content": "result"}}]},
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as m:
        await client.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="my-model",
            temperature=0.7,
            max_tokens=512,
        )
        call_kwargs = m.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["temperature"] == 0.7
        assert body["max_tokens"] == 512
        assert body["model"] == "my-model"


def test_client_init_strips_trailing_slash():
    """base_url trailing slash should be stripped."""
    c = InferenceClient(base_url="http://localhost:1234/v1/")
    assert c.base_url == "http://localhost:1234/v1"


def test_client_default_timeout():
    """Default timeout is 300.0 (large prompts need time for local LLM inference)."""
    c = InferenceClient()
    assert c.timeout == 300.0
