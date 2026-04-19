"""Tests for hippo_brain.embeddings — sqlite-vec-backed."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hippo_brain.client import MockLMStudioClient
from hippo_brain.embeddings import (
    EMBED_DIM,
    _pad_or_truncate,
    embed_knowledge_node,
    get_or_create_table,
    open_vector_db,
    search_similar,
)

_SCHEMA_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS knowledge_nodes (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    embed_text TEXT NOT NULL,
    node_type TEXT NOT NULL DEFAULT 'observation',
    outcome TEXT,
    tags TEXT,
    enrichment_model TEXT,
    enrichment_version INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    updated_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
);
"""


@pytest.fixture
def vector_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = open_vector_db(tmpdir)
        try:
            conn.executescript(_SCHEMA_BOOTSTRAP)
            conn.commit()
            handle = get_or_create_table(conn)
            yield conn, handle
        finally:
            conn.close()


@pytest.fixture
def mock_client():
    return MockLMStudioClient()


def _seed_node(conn, node_id: int, embed_text: str, *, summary: str = "") -> None:
    import json

    content = json.dumps({"summary": summary, "commands_raw": "cargo test"})
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, outcome, tags) "
        "VALUES (?, ?, ?, ?, 'success', '[]')",
        (node_id, f"uuid-{node_id}", content, embed_text),
    )
    conn.commit()


def sample_node(node_id: int = 1, embed_text: str = "cargo test hippo-core") -> dict:
    return {
        "id": node_id,
        "uuid": f"uuid-{node_id}",
        "embed_text": embed_text,
        "commands_raw": "cargo test -p hippo-core",
    }


async def test_embed_and_search(vector_db, mock_client):
    conn, handle = vector_db
    _seed_node(conn, 1, "cargo test hippo-core", summary="Ran hippo-core tests")

    await embed_knowledge_node(mock_client, handle, sample_node(), embed_model="test")

    vecs = await mock_client.embed(["cargo test hippo-core"])
    query_vec = _pad_or_truncate(vecs[0], EMBED_DIM)

    results = search_similar(handle, query_vec, column="vec_knowledge", limit=5)
    assert len(results) == 1
    assert results[0]["embed_text"] == "cargo test hippo-core"
    assert results[0]["summary"] == "Ran hippo-core tests"
    assert 0.0 <= results[0]["score"] <= 1.0


async def test_multiple_nodes(vector_db, mock_client):
    conn, handle = vector_db
    for i in range(3):
        _seed_node(conn, i + 1, f"command {i}")
        await embed_knowledge_node(
            mock_client,
            handle,
            sample_node(node_id=i + 1, embed_text=f"command {i}"),
            embed_model="test",
        )

    count = conn.execute("SELECT count(*) FROM knowledge_vectors").fetchone()[0]
    assert count == 3


async def test_embed_requires_node_id(vector_db, mock_client):
    _, handle = vector_db
    with pytest.raises(ValueError, match="primary key"):
        await embed_knowledge_node(mock_client, handle, {"embed_text": "x"}, embed_model="test")


def test_search_similar_rejects_unknown_column(vector_db):
    _, handle = vector_db
    with pytest.raises(ValueError):
        search_similar(handle, [0.0] * EMBED_DIM, column="vec_bogus")


def test_open_vector_db_creates_parent_dir(tmp_path: Path):
    target = tmp_path / "nested" / "hippo-data"
    conn = open_vector_db(target)
    assert (target / "hippo.db").exists()
    conn.close()
