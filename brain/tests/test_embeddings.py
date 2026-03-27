import tempfile

import pytest

from hippo_brain.client import MockLMStudioClient
from hippo_brain.embeddings import (
    embed_knowledge_node,
    get_or_create_table,
    open_vector_db,
    search_similar,
    _pad_or_truncate,
)


@pytest.fixture
def vector_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = open_vector_db(tmpdir)
        table = get_or_create_table(db)
        yield db, table


@pytest.fixture
def mock_client():
    return MockLMStudioClient()


def sample_node(node_id: int = 1, embed_text: str = "cargo test hippo-core") -> dict:
    return {
        "id": node_id,
        "session_id": 1,
        "captured_at": 1000000,
        "commands_raw": "cargo test -p hippo-core",
        "cwd": "/project/hippo",
        "git_branch": "main",
        "git_repo": "hippo",
        "outcome": "success",
        "tags": ["rust", "testing"],
        "entities": {"tools": ["cargo"]},
        "embed_text": embed_text,
        "summary": "Ran hippo-core tests",
        "enrichment_model": "test-model",
        "enrichment_version": 1,
    }


async def test_embed_and_search(vector_db, mock_client):
    db, table = vector_db

    node = sample_node()
    await embed_knowledge_node(mock_client, table, node, embed_model="test")

    # Search using the same text to get a matching vector
    vecs = await mock_client.embed(["cargo test hippo-core"])
    query_vec = _pad_or_truncate(vecs[0], 2560)

    results = search_similar(table, query_vec, column="vec_knowledge", limit=5)
    assert len(results) >= 1
    assert results[0]["embed_text"] == "cargo test hippo-core"


async def test_multiple_nodes(vector_db, mock_client):
    db, table = vector_db

    for i in range(3):
        node = sample_node(node_id=i + 1, embed_text=f"command {i}")
        await embed_knowledge_node(mock_client, table, node, embed_model="test")

    assert table.count_rows() == 3
