"""Tests for hippo_brain.vector_store (sqlite-vec + FTS5)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hippo_brain import vector_store
from hippo_brain.vector_store import EMBED_DIM


def _fresh_conn(tmp: Path):
    """Build a connection against a DB seeded with the minimum v6 schema.

    The migration is owned by Rust, but for an isolated Python test we
    replicate the bits vector_store/FTS need: knowledge_nodes + FTS5
    virtual table + triggers. This keeps the test self-contained and
    independent of the Rust build.
    """
    db_path = tmp / "hippo.db"
    conn = vector_store.open_conn(db_path)
    conn.executescript(
        """
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
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
            summary, embed_text, content,
            tokenize = 'porter unicode61 remove_diacritics 2'
        );
        CREATE TRIGGER IF NOT EXISTS knowledge_nodes_fts_ai
        AFTER INSERT ON knowledge_nodes BEGIN
            INSERT INTO knowledge_fts (rowid, summary, embed_text, content)
            VALUES (
                NEW.id,
                COALESCE(CASE WHEN json_valid(NEW.content) THEN json_extract(NEW.content, '$.summary') END, ''),
                NEW.embed_text,
                NEW.content
            );
        END;
        CREATE TRIGGER IF NOT EXISTS knowledge_nodes_fts_ad
        AFTER DELETE ON knowledge_nodes BEGIN
            DELETE FROM knowledge_fts WHERE rowid = OLD.id;
        END;
        """
    )
    conn.commit()
    return conn


def _vec(seed: int) -> list[float]:
    """Deterministic unit-ish vector for tests."""
    import math

    return [math.sin(seed + i * 0.01) for i in range(EMBED_DIM)]


def test_open_conn_creates_vec_table():
    with tempfile.TemporaryDirectory() as td:
        conn = vector_store.open_conn(Path(td) / "db.sqlite")
        try:
            row = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='knowledge_vectors'"
            ).fetchone()
            assert row[0] == 1
        finally:
            conn.close()


def test_insert_vectors_length_mismatch_raises():
    with tempfile.TemporaryDirectory() as td:
        conn = vector_store.open_conn(Path(td) / "db.sqlite")
        try:
            with pytest.raises(ValueError):
                vector_store.insert_vectors(conn, 1, [0.0], _vec(1))
        finally:
            conn.close()


def test_knn_search_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_conn(Path(td))
        try:
            conn.execute(
                "INSERT INTO knowledge_nodes (id, uuid, content, embed_text) "
                "VALUES (1, 'a', '{\"summary\":\"alpha\"}', 'body a'), "
                "       (2, 'b', '{\"summary\":\"beta\"}',  'body b')"
            )
            vector_store.insert_vectors(conn, 1, _vec(0), _vec(100))
            vector_store.insert_vectors(conn, 2, _vec(5), _vec(105))
            conn.commit()

            hits = vector_store.knn_search(conn, _vec(0), limit=5)
            assert len(hits) == 2
            assert hits[0]["knowledge_node_id"] == 1  # exact match on node 1
            for h in hits:
                assert 0.0 <= h["score"] <= 1.0
                assert "distance" in h
        finally:
            conn.close()


def test_knn_search_command_column():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_conn(Path(td))
        try:
            conn.execute(
                "INSERT INTO knowledge_nodes (id, uuid, content, embed_text) "
                "VALUES (1, 'a', '{}', 'x'), (2, 'b', '{}', 'y')"
            )
            vector_store.insert_vectors(conn, 1, _vec(0), _vec(200))
            vector_store.insert_vectors(conn, 2, _vec(50), _vec(210))
            conn.commit()

            hits = vector_store.knn_search(conn, _vec(200), column="vec_command", limit=5)
            assert hits[0]["knowledge_node_id"] == 1
        finally:
            conn.close()


def test_knn_search_rejects_unknown_column():
    with tempfile.TemporaryDirectory() as td:
        conn = vector_store.open_conn(Path(td) / "db.sqlite")
        try:
            with pytest.raises(ValueError):
                vector_store.knn_search(conn, _vec(0), column="vec_bogus")
        finally:
            conn.close()


def test_fts_search_finds_inserted_rows():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_conn(Path(td))
        try:
            conn.execute(
                "INSERT INTO knowledge_nodes (id, uuid, content, embed_text) "
                "VALUES (1, 'a', '{\"summary\":\"sqlite-vec migration design\"}', "
                "'hybrid retrieval over sqlite fts')"
            )
            conn.commit()

            hits = vector_store.fts_search(conn, "migration")
            assert len(hits) == 1
            assert hits[0]["knowledge_node_id"] == 1
            assert hits[0]["score"] > 0.0
        finally:
            conn.close()


def test_fts_search_respects_limit():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_conn(Path(td))
        try:
            for i in range(5):
                conn.execute(
                    "INSERT INTO knowledge_nodes (id, uuid, content, embed_text) "
                    "VALUES (?, ?, '{\"summary\":\"retrieval\"}', 'retrieval text')",
                    (i + 1, f"u-{i}"),
                )
            conn.commit()

            hits = vector_store.fts_search(conn, "retrieval", limit=3)
            assert len(hits) == 3
        finally:
            conn.close()


def test_delete_vectors_removes_row():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_conn(Path(td))
        try:
            conn.execute(
                "INSERT INTO knowledge_nodes (id, uuid, content, embed_text) VALUES (1, 'a', '{}', 'x')"
            )
            vector_store.insert_vectors(conn, 1, _vec(0), _vec(0))
            conn.commit()

            vector_store.delete_vectors(conn, 1)
            conn.commit()

            hits = vector_store.knn_search(conn, _vec(0), limit=5)
            assert hits == []
        finally:
            conn.close()
