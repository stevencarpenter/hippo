"""Tests for the real LMStudioClient HTTP methods using httpx mock transport."""

import httpx
import pytest
from unittest.mock import AsyncMock, patch

from hippo_brain.client import LMStudioClient


@pytest.fixture
def client():
    return LMStudioClient(base_url="http://localhost:1234/v1", timeout=5.0)


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
    c = LMStudioClient(base_url="http://localhost:1234/v1/")
    assert c.base_url == "http://localhost:1234/v1"


def test_client_default_timeout():
    """Default timeout is 30.0."""
    c = LMStudioClient()
    assert c.timeout == 30.0
