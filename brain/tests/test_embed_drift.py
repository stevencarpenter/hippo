"""Tests for embedding-model drift guard (R-06)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hippo_brain import vector_store
from hippo_brain.client import MockLMStudioClient
from hippo_brain.embeddings import (
    EMBED_DIM,
    _pad_or_truncate,
    embed_knowledge_node,
    get_or_create_table,
    open_vector_db,
)
from hippo_brain.vector_store import (
    EmbedDriftError,
    check_embed_model_drift,
    get_stored_embed_model,
    record_embed_model,
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
def db_with_schema():
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = open_vector_db(tmpdir)
        try:
            conn.executescript(_SCHEMA_BOOTSTRAP)
            conn.commit()
            get_or_create_table(conn)
            yield conn
        finally:
            conn.close()


@pytest.fixture
def mock_client():
    return MockLMStudioClient()


def _seed_node(conn, node_id: int, embed_text: str = "test text") -> None:
    import json

    content = json.dumps({"summary": "test", "commands_raw": ""})
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, outcome, tags) "
        "VALUES (?, ?, ?, ?, 'success', '[]')",
        (node_id, f"uuid-{node_id}", content, embed_text),
    )
    conn.commit()


async def test_embed_drift_blocks_writes(db_with_schema, mock_client):
    """Writes must be refused when the live model differs from the stored corpus model."""
    _seed_node(db_with_schema, 1)

    # Manually record a stored model that differs from what we'll try to use.
    record_embed_model(db_with_schema, "old-embed-model-768d")
    db_with_schema.commit()

    with pytest.raises(EmbedDriftError, match="old-embed-model-768d"):
        await embed_knowledge_node(
            mock_client,
            db_with_schema,
            {"id": 1, "embed_text": "test text"},
            embed_model="new-embed-model-1024d",
        )


async def test_embed_drift_blocks_writes_async(db_with_schema, mock_client):
    """Async variant: drift guard raises EmbedDriftError before any write occurs."""
    _seed_node(db_with_schema, 1)
    record_embed_model(db_with_schema, "old-model")
    db_with_schema.commit()

    count_before = db_with_schema.execute("SELECT count(*) FROM knowledge_vectors").fetchone()[0]

    with pytest.raises(EmbedDriftError):
        await embed_knowledge_node(
            mock_client,
            db_with_schema,
            {"id": 1, "embed_text": "test text"},
            embed_model="new-model",
        )

    count_after = db_with_schema.execute("SELECT count(*) FROM knowledge_vectors").fetchone()[0]
    assert count_after == count_before, "no vectors must be written when drift is detected"


async def test_embed_drift_allows_switch_flag(db_with_schema, mock_client):
    """allow_embed_switch=True permits writes despite model mismatch."""
    _seed_node(db_with_schema, 1)
    record_embed_model(db_with_schema, "old-model")
    db_with_schema.commit()

    await embed_knowledge_node(
        mock_client,
        db_with_schema,
        {"id": 1, "embed_text": "test text"},
        embed_model="new-model",
        allow_embed_switch=True,
    )

    count = db_with_schema.execute("SELECT count(*) FROM knowledge_vectors").fetchone()[0]
    assert count == 1


async def test_embed_drift_empty_corpus_no_op(db_with_schema, mock_client):
    """First-run with no stored model: any model is accepted."""
    _seed_node(db_with_schema, 1)

    assert get_stored_embed_model(db_with_schema) is None

    await embed_knowledge_node(
        mock_client,
        db_with_schema,
        {"id": 1, "embed_text": "test text"},
        embed_model="brand-new-model",
    )

    assert get_stored_embed_model(db_with_schema) == "brand-new-model"


async def test_embed_model_recorded_after_write(db_with_schema, mock_client):
    """Model name is persisted to embed_model_meta after a successful write."""
    _seed_node(db_with_schema, 1)

    await embed_knowledge_node(
        mock_client,
        db_with_schema,
        {"id": 1, "embed_text": "test text"},
        embed_model="nomic-embed-text",
    )

    assert get_stored_embed_model(db_with_schema) == "nomic-embed-text"


def test_vector_dim_mismatch_raises_clearly():
    """Wrong-dimension vectors from the LLM raise ValueError, not silent coercion."""
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = vector_store.open_conn(Path(tmpdir) / "db.sqlite")
        try:
            # insert_vectors already enforces dim; this tests the message is clear
            short_vec = [0.1] * 64
            full_vec = [0.1] * EMBED_DIM
            with pytest.raises(ValueError, match="vector length mismatch"):
                vector_store.insert_vectors(conn, 1, short_vec, full_vec)
        finally:
            conn.close()


async def test_embed_node_raises_on_wrong_dim(db_with_schema):
    """embed_knowledge_node raises if the LLM returns a wrong-dim vector."""
    _seed_node(db_with_schema, 1)

    class BadDimClient(MockLMStudioClient):
        async def embed(self, texts, model=""):
            # Return 64-dim vectors regardless of EMBED_DIM
            return [[0.1] * 64 for _ in texts]

    with pytest.raises(ValueError, match="64 dimensions"):
        await embed_knowledge_node(
            BadDimClient(),
            db_with_schema,
            {"id": 1, "embed_text": "test"},
            embed_model="bad-model",
        )


def test_check_embed_model_drift_empty_corpus():
    """Empty corpus: check_embed_model_drift is a no-op for any model."""
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = vector_store.open_conn(Path(tmpdir) / "db.sqlite")
        try:
            check_embed_model_drift(conn, "any-model")  # must not raise
        finally:
            conn.close()


def test_check_embed_model_drift_match():
    """Matching models: no-op."""
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = vector_store.open_conn(Path(tmpdir) / "db.sqlite")
        try:
            record_embed_model(conn, "nomic-embed-text")
            conn.commit()
            check_embed_model_drift(conn, "nomic-embed-text")  # must not raise
        finally:
            conn.close()


def test_check_embed_model_drift_empty_string_stored_is_noop():
    """Legacy row with model='' must be treated as empty corpus, not a mismatch.

    A row with an empty-string model used to brick all writes: every live
    model name mismatched '' and raised EmbedDriftError. The guard now
    treats falsy stored values the same as a missing row.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = vector_store.open_conn(Path(tmpdir) / "db.sqlite")
        try:
            # Bypass record_embed_model's guard to simulate a legacy bad row.
            conn.execute("INSERT INTO embed_model_meta (id, model) VALUES (1, '')")
            conn.commit()
            check_embed_model_drift(conn, "any-live-model")  # must not raise
        finally:
            conn.close()


def test_record_embed_model_rejects_empty():
    """record_embed_model refuses empty / whitespace model names."""
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = vector_store.open_conn(Path(tmpdir) / "db.sqlite")
        try:
            with pytest.raises(ValueError, match="non-empty"):
                record_embed_model(conn, "")
            with pytest.raises(ValueError, match="non-empty"):
                record_embed_model(conn, "   ")
        finally:
            conn.close()


def test_check_embed_model_drift_mismatch_raises():
    """Model mismatch without allow_switch raises EmbedDriftError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = vector_store.open_conn(Path(tmpdir) / "db.sqlite")
        try:
            record_embed_model(conn, "nomic-embed-text")
            conn.commit()
            with pytest.raises(EmbedDriftError):
                check_embed_model_drift(conn, "mxbai-embed-large")
        finally:
            conn.close()


def test_pad_or_truncate_still_available_for_query_path():
    """_pad_or_truncate remains importable for use on query vectors."""
    vec = [0.5] * 100
    padded = _pad_or_truncate(vec, EMBED_DIM)
    assert len(padded) == EMBED_DIM
    truncated = _pad_or_truncate([0.5] * 1000, EMBED_DIM)
    assert len(truncated) == EMBED_DIM
