import math
import pytest

from hippo_brain.client import MockInferenceClient, _parse_embed_response


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
