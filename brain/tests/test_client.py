import math
import pytest

from hippo_brain.client import MockLMStudioClient


@pytest.fixture
def mock_client():
    return MockLMStudioClient()


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
    assert len(vectors[0]) == 384

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
    vec = MockLMStudioClient._deterministic_vector("test", 10)
    assert len(vec) == 10
    magnitude = math.sqrt(sum(x * x for x in vec))
    assert abs(magnitude - 1.0) < 1e-6
