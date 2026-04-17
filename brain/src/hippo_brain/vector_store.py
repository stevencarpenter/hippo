"""SQLite + sqlite-vec vector store.

Replaces the prior LanceDB vector store. Everything is colocated in the main
hippo SQLite DB at ``~/.local/share/hippo/hippo.db``.

Virtual tables (schema v6):

- ``knowledge_vectors`` (vec0): ``knowledge_node_id``, ``vec_knowledge``,
  ``vec_command``; cosine distance.
- ``knowledge_fts`` (fts5): ``summary``, ``embed_text``, ``content`` with
  ``porter unicode61`` tokenizer. Kept in sync with ``knowledge_nodes`` via
  triggers installed by the Rust v5→v6 migration. Summary is extracted from
  ``knowledge_nodes.content`` JSON via ``json_extract(..., '$.summary')``.

The vec0 virtual table is created on first use by this module (the Rust
daemon does not load the sqlite-vec extension, so migration cannot create it
there).
"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path
from typing import Iterable

import sqlite_vec  # type: ignore[import-untyped]

EMBED_DIM = 768

# All SQL statements are static module-level literals. No user input is ever
# concatenated into SQL — bind parameters only. Column selection in
# ``knn_search`` is a lookup against a fixed allow-list of pre-built queries.
_SQL_CREATE_VEC_TABLE = "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_vectors USING vec0(knowledge_node_id INTEGER PRIMARY KEY, vec_knowledge FLOAT[768] distance_metric=cosine, vec_command FLOAT[768] distance_metric=cosine)"  # noqa: E501
_SQL_KNN_VEC_KNOWLEDGE = "SELECT knowledge_node_id, distance FROM knowledge_vectors WHERE vec_knowledge MATCH ? AND k = ? ORDER BY distance"  # noqa: E501
_SQL_KNN_VEC_COMMAND = "SELECT knowledge_node_id, distance FROM knowledge_vectors WHERE vec_command MATCH ? AND k = ? ORDER BY distance"  # noqa: E501
_SQL_INSERT_VECTORS = "INSERT OR REPLACE INTO knowledge_vectors (knowledge_node_id, vec_knowledge, vec_command) VALUES (?, ?, ?)"  # noqa: E501
_SQL_DELETE_VECTORS = "DELETE FROM knowledge_vectors WHERE knowledge_node_id = ?"
_SQL_FTS_SEARCH = "SELECT rowid, bm25(knowledge_fts) AS score FROM knowledge_fts WHERE knowledge_fts MATCH ? ORDER BY score LIMIT ?"  # noqa: E501

_KNN_QUERIES = {
    "vec_knowledge": _SQL_KNN_VEC_KNOWLEDGE,
    "vec_command": _SQL_KNN_VEC_COMMAND,
}


def open_conn(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with the sqlite-vec extension loaded.

    Applies the standard hippo PRAGMAs (WAL, foreign_keys, busy_timeout=5000)
    and ensures the vec0 ``knowledge_vectors`` virtual table exists.
    """
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    ensure_vec_table(conn)
    return conn


def ensure_vec_table(conn: sqlite3.Connection) -> None:
    """Create the vec0 ``knowledge_vectors`` virtual table if missing.

    Idempotent. Requires sqlite-vec to already be loaded on ``conn``.
    """
    conn.execute(_SQL_CREATE_VEC_TABLE)
    conn.commit()


def insert_vectors(
    conn: sqlite3.Connection,
    knowledge_node_id: int,
    vec_knowledge: list[float],
    vec_command: list[float],
) -> None:
    """Insert (or replace) a pair of vectors for a knowledge node.

    Both vectors must be length ``EMBED_DIM``.
    """
    if len(vec_knowledge) != EMBED_DIM or len(vec_command) != EMBED_DIM:
        raise ValueError(
            f"vector length mismatch: knowledge={len(vec_knowledge)}, "
            f"command={len(vec_command)}, expected {EMBED_DIM}"
        )
    conn.execute(
        _SQL_INSERT_VECTORS,
        (knowledge_node_id, _vec_blob(vec_knowledge), _vec_blob(vec_command)),
    )


def knn_search(
    conn: sqlite3.Connection,
    query_vec: list[float],
    column: str = "vec_knowledge",
    limit: int = 10,
) -> list[dict]:
    """K-nearest-neighbor search against a vec0 column.

    Returns rows shaped like::

        {
            "knowledge_node_id": int,
            "distance": float,  # raw cosine distance in [0, 2]
            "score": float,  # normalized: 1 - distance/2, in [0, 1]
        }

    Higher ``score`` is better. Joining against ``knowledge_nodes`` is the
    caller's responsibility (the retrieval module does this).
    """
    sql = _KNN_QUERIES.get(column)
    if sql is None:
        raise ValueError(f"unsupported vector column: {column}")
    if len(query_vec) != EMBED_DIM:
        raise ValueError(f"query vector length {len(query_vec)} != expected {EMBED_DIM}")
    rows = conn.execute(sql, (_vec_blob(query_vec), limit)).fetchall()
    return [
        {
            "knowledge_node_id": nid,
            "distance": dist,
            "score": max(0.0, min(1.0, 1.0 - dist / 2.0)),
        }
        for nid, dist in rows
    ]


def fts_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
) -> list[dict]:
    """BM25 lexical search against ``knowledge_fts``.

    Returns rows shaped like::

        {
            "knowledge_node_id": int,  # == fts rowid
            "bm25": float,  # negative; lower = better match
            "score": float,  # normalized: 1 / (1 + |bm25|) in (0, 1]
        }
    """
    rows = conn.execute(_SQL_FTS_SEARCH, (query, limit)).fetchall()
    return [
        {
            "knowledge_node_id": rowid,
            "bm25": bm25,
            "score": 1.0 / (1.0 + abs(bm25)),
        }
        for rowid, bm25 in rows
    ]


def delete_vectors(conn: sqlite3.Connection, knowledge_node_id: int) -> None:
    """Remove vectors for a knowledge node. FTS5 is handled by trigger."""
    conn.execute(_SQL_DELETE_VECTORS, (knowledge_node_id,))


def _vec_blob(vec: Iterable[float]) -> bytes:
    """Serialize a float vector to the little-endian f32 blob sqlite-vec wants."""
    buf = list(vec)
    return struct.pack(f"<{len(buf)}f", *buf)
