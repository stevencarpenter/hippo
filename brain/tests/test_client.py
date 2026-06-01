import math
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from hippo_brain import client as client_mod
from hippo_brain.client import InferenceClient, MockInferenceClient, _parse_embed_response


@pytest.fixture
def mock_client():
    return MockInferenceClient()


async def test_mock_chat(mock_client):
    result = await mock_client.chat(
        messages=[{"role": "user", "content": "hello"}],
        model="test-model",
    )
    assert "summary" in result
    assert len(mock_client.chat_calls) == 1
    assert mock_client.chat_calls[0]["model"] == "test-model"


async def test_mock_embed(mock_client):
    vectors = await mock_client.embed(["test text"], model="embed-model")
    assert len(vectors) == 1
    from hippo_brain.embeddings import EMBED_DIM

    assert len(vectors[0]) == EMBED_DIM

    # Check normalized magnitude is ~1.0
    magnitude = math.sqrt(sum(x * x for x in vectors[0]))
    assert abs(magnitude - 1.0) < 1e-6

    assert len(mock_client.embed_calls) == 1


async def test_mock_embed_deterministic(mock_client):
    v1 = await mock_client.embed(["same input"])
    v2 = await mock_client.embed(["same input"])
    assert v1 == v2

    v3 = await mock_client.embed(["different input"])
    assert v1 != v3


async def test_mock_reachable(mock_client):
    assert await mock_client.is_reachable() is True


def test_deterministic_vector_non_multiple_of_8_dims():
    """When dims is not a multiple of 8, the inner break on line 97 fires."""
    vec = MockInferenceClient._deterministic_vector("test", 10)
    assert len(vec) == 10
    magnitude = math.sqrt(sum(x * x for x in vec))
    assert abs(magnitude - 1.0) < 1e-6


def test_parse_embed_response_accepts_valid_floats():
    response = {
        "data": [
            {"embedding": [0.1, 0.2, 0.3]},
            {"embedding": [-0.4, 0.0, 0.5]},
        ],
    }
    result = _parse_embed_response(response, source="http://test/v1")
    assert result == [[0.1, 0.2, 0.3], [-0.4, 0.0, 0.5]]


def test_parse_embed_response_raises_on_null_element():
    """Defense-in-depth: oMLX has a known bug where batched embeddings of
    disparate-length inputs return all-null vectors for the shorter item.
    The parser must refuse rather than let nulls reach _vec_blob/struct.pack.
    """
    response = {
        "data": [
            {"embedding": [0.1, 0.2, 0.3]},
            {"embedding": [None, None, None]},
        ],
    }
    with pytest.raises(ValueError, match=r"None at item\[1\]\.embedding\[0\]"):
        _parse_embed_response(response, source="http://omlx-test/v1")


def test_parse_embed_response_raises_on_null_buried_mid_vector():
    response = {
        "data": [
            {"embedding": [0.1, 0.2, None, 0.4]},
        ],
    }
    with pytest.raises(ValueError, match=r"None at item\[0\]\.embedding\[2\]"):
        _parse_embed_response(response, source="http://test/v1")


# --- Retry-with-backoff tests for the HTTP boundary in chat()/embed() ---


def _fake_chat_response():
    """A fake httpx.Response that passes _raise_with_body and yields chat content."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock(return_value=None)
    resp.json = MagicMock(return_value={"choices": [{"message": {"content": "hello-from-server"}}]})
    return resp


def _fake_embed_response():
    resp = MagicMock()
    resp.raise_for_status = MagicMock(return_value=None)
    resp.json = MagicMock(return_value={"data": [{"embedding": [0.1, 0.2, 0.3]}]})
    return resp


def _patch_async_client(monkeypatch, post_mock):
    """Patch httpx.AsyncClient so its async-context client's .post is post_mock."""
    fake_client = MagicMock()
    fake_client.post = post_mock
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=fake_client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", MagicMock(return_value=ctx))
    return post_mock


async def test_chat_retries_on_transient_then_succeeds(monkeypatch):
    post = AsyncMock(
        side_effect=[
            httpx.RemoteProtocolError("peer closed connection"),
            _fake_chat_response(),
        ]
    )
    _patch_async_client(monkeypatch, post)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(client_mod, "_sleep", sleep_mock)

    c = InferenceClient(base_url="http://x/v1", timeout=1.0)
    result = await c.chat(messages=[{"role": "user", "content": "hi"}])

    assert result == "hello-from-server"
    assert post.await_count == 2
    sleep_mock.assert_awaited_once_with(0.5)


async def test_chat_exhausts_retries_then_raises(monkeypatch):
    post = AsyncMock(side_effect=httpx.RemoteProtocolError("peer closed connection"))
    _patch_async_client(monkeypatch, post)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(client_mod, "_sleep", sleep_mock)

    c = InferenceClient(base_url="http://x/v1", timeout=1.0, max_retries=3)
    with pytest.raises(httpx.RemoteProtocolError):
        await c.chat(messages=[{"role": "user", "content": "hi"}])

    assert post.await_count == 3
    # Sleeps before the 2nd and 3rd attempts only: exponential schedule.
    assert [call.args[0] for call in sleep_mock.await_args_list] == [0.5, 1.0]


async def test_chat_does_not_retry_on_http_status_error(monkeypatch):
    def raise_500(*_a, **_k):
        raise httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())

    resp = MagicMock()
    resp.raise_for_status = MagicMock(side_effect=raise_500)
    post = AsyncMock(return_value=resp)
    _patch_async_client(monkeypatch, post)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(client_mod, "_sleep", sleep_mock)

    # _raise_with_body re-raises HTTPStatusError; must propagate without retry.
    c = InferenceClient(base_url="http://x/v1", timeout=1.0)
    with pytest.raises(httpx.HTTPStatusError):
        await c.chat(messages=[{"role": "user", "content": "hi"}])

    assert post.await_count == 1
    sleep_mock.assert_not_awaited()


def test_inference_client_rejects_max_retries_zero():
    """max_retries=0 would produce an empty retry loop; __init__ must reject it."""
    with pytest.raises(ValueError, match="max_retries must be >= 1"):
        InferenceClient(base_url="http://x/v1", max_retries=0)


def test_inference_client_rejects_negative_max_retries():
    with pytest.raises(ValueError, match="max_retries must be >= 1"):
        InferenceClient(base_url="http://x/v1", max_retries=-1)


async def test_embed_retries_on_transient_then_succeeds(monkeypatch):
    post = AsyncMock(
        side_effect=[
            httpx.ConnectError("connection refused"),
            _fake_embed_response(),
        ]
    )
    _patch_async_client(monkeypatch, post)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(client_mod, "_sleep", sleep_mock)

    c = InferenceClient(base_url="http://x/v1", timeout=1.0)
    result = await c.embed(["some text"])

    assert result == [[0.1, 0.2, 0.3]]
    assert post.await_count == 2
    sleep_mock.assert_awaited_once_with(0.5)


# --- error_type label tests (no network) ---


async def test_chat_error_type_transport(monkeypatch):
    """TransportError raises -> error_type='transport'."""
    post = AsyncMock(side_effect=httpx.RemoteProtocolError("peer closed"))
    _patch_async_client(monkeypatch, post)
    monkeypatch.setattr(client_mod, "_sleep", AsyncMock())

    recorded: list[dict] = []
    counter = MagicMock()
    counter.add = MagicMock(side_effect=lambda n, attrs: recorded.append(attrs))
    monkeypatch.setattr(client_mod, "_inference_errors", counter)

    c = InferenceClient(base_url="http://x/v1", timeout=1.0, max_retries=1)
    with pytest.raises(httpx.TransportError):
        await c.chat(messages=[{"role": "user", "content": "hi"}])

    assert len(recorded) == 1
    assert recorded[0] == {"method": "chat", "error_type": "transport"}


async def test_chat_error_type_status(monkeypatch):
    """HTTPStatusError raises -> error_type='status'."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())
    )
    post = AsyncMock(return_value=resp)
    _patch_async_client(monkeypatch, post)
    monkeypatch.setattr(client_mod, "_sleep", AsyncMock())

    recorded: list[dict] = []
    counter = MagicMock()
    counter.add = MagicMock(side_effect=lambda n, attrs: recorded.append(attrs))
    monkeypatch.setattr(client_mod, "_inference_errors", counter)

    c = InferenceClient(base_url="http://x/v1", timeout=1.0)
    with pytest.raises(httpx.HTTPStatusError):
        await c.chat(messages=[{"role": "user", "content": "hi"}])

    assert len(recorded) == 1
    assert recorded[0] == {"method": "chat", "error_type": "status"}


async def test_embed_error_type_parse(monkeypatch):
    """ValueError from _parse_embed_response -> error_type='parse'."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock(return_value=None)
    resp.json = MagicMock(return_value={"data": [{"embedding": [None, None]}]})
    post = AsyncMock(return_value=resp)
    _patch_async_client(monkeypatch, post)
    monkeypatch.setattr(client_mod, "_sleep", AsyncMock())

    recorded: list[dict] = []
    counter = MagicMock()
    counter.add = MagicMock(side_effect=lambda n, attrs: recorded.append(attrs))
    monkeypatch.setattr(client_mod, "_inference_errors", counter)

    c = InferenceClient(base_url="http://x/v1", timeout=1.0)
    with pytest.raises(ValueError):
        await c.embed(["text"])

    assert len(recorded) == 1
    assert recorded[0] == {"method": "embed", "error_type": "parse"}
