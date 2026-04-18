"""Knowledge-node embedding pipeline, backed by SQLite + sqlite-vec.

The public surface is preserved from the LanceDB era so existing callers
(mcp.py, server.py, rag.py, tests) keep working:

- ``EMBED_DIM``
- ``_pad_or_truncate``
- ``open_vector_db(data_dir)`` — returns a handle (now a sqlite3 connection)
- ``get_or_create_table(handle)`` — idempotent; returns the same handle
- ``embed_knowledge_node(client, handle, node_dict, ...)``
- ``search_similar(handle, query_vec, column=..., limit=...)``

Under the hood, vectors are written to the ``knowledge_vectors`` vec0 table
in the main hippo SQLite DB. ``search_similar`` joins that table against
``knowledge_nodes`` so callers receive the same dict shape they did before.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from hippo_brain import vector_store
from hippo_brain.telemetry import get_meter
from hippo_brain.vector_store import (
    EMBED_DIM,
    EmbedDriftError,
    check_embed_model_drift,
    open_conn,
    record_embed_model,
)

_meter = get_meter()
_embed_duration = (
    _meter.create_histogram(
        "hippo.brain.embedding.duration",
        description="Time to embed a knowledge node",
        unit="ms",
    )
    if _meter
    else None
)
_embed_failures = (
    _meter.create_counter(
        "hippo.brain.embedding.failures",
        description="Failed embedding attempts",
    )
    if _meter
    else None
)

__all__ = [
    "EMBED_DIM",
    "EmbedDriftError",
    "_pad_or_truncate",
    "open_vector_db",
    "get_or_create_table",
    "embed_knowledge_node",
    "search_similar",
]


def _safe_json(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError, TypeError:
        return default


def _pad_or_truncate(vec: list[float], target_dim: int) -> list[float]:
    if len(vec) >= target_dim:
        return vec[:target_dim]
    return vec + [0.0] * (target_dim - len(vec))


def open_vector_db(data_dir: str | Path) -> sqlite3.Connection:
    """Open the shared hippo SQLite DB with sqlite-vec loaded.

    ``data_dir`` is the XDG data dir (``~/.local/share/hippo``); the DB file
    lives at ``<data_dir>/hippo.db``. Connection is returned in the same
    state every other brain subsystem expects (WAL, foreign_keys,
    busy_timeout=5000) and the vec0 virtual table is guaranteed to exist.
    """
    db_path = Path(data_dir) / "hippo.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return open_conn(db_path)


def get_or_create_table(handle: sqlite3.Connection) -> sqlite3.Connection:
    """Idempotent no-op for API compatibility.

    ``open_vector_db`` already ensures the vec0 table exists. Previous
    LanceDB-era callers would chain ``get_or_create_table(open_vector_db(...))``;
    the same chain still works.
    """
    vector_store.ensure_vec_table(handle)
    return handle


def _check_vec_dim(vec: list[float], label: str) -> None:
    if len(vec) != EMBED_DIM:
        raise ValueError(
            f"{label} vector has {len(vec)} dimensions; expected {EMBED_DIM}. "
            "The embedding model may have changed — re-embed the corpus or "
            "update config.embeddings.model to match the loaded model."
        )


async def embed_knowledge_node(
    client: Any,
    handle: sqlite3.Connection,
    node_dict: dict,
    embed_model: str = "",
    command_model: str = "",
    *,
    allow_embed_switch: bool = False,
) -> None:
    """Embed a knowledge node and persist both vectors to ``knowledge_vectors``.

    ``node_dict`` must carry at least ``id`` (the knowledge_nodes PK) and
    ``embed_text``. If ``node_dict["id"]`` is 0 or missing, nothing is
    written — the caller is responsible for providing the foreign key.

    Raises ``EmbedDriftError`` if ``embed_model`` differs from the model used
    to build the existing corpus (unless ``allow_embed_switch=True``).
    """
    t0 = time.monotonic()
    try:
        node_id = int(node_dict.get("id", 0) or 0)
        if node_id <= 0:
            raise ValueError(
                "embed_knowledge_node requires node_dict['id'] to be the "
                "knowledge_nodes primary key"
            )

        check_embed_model_drift(handle, embed_model, allow_switch=allow_embed_switch)

        embed_text = node_dict.get("embed_text", "") or ""
        commands_raw = node_dict.get("commands_raw", "") or ""

        cmd_model = command_model or embed_model
        cmd_text = commands_raw or embed_text

        if cmd_model == embed_model:
            vecs = await client.embed([embed_text, cmd_text], model=embed_model)
            _check_vec_dim(vecs[0], "knowledge")
            _check_vec_dim(vecs[1], "command")
            vec_knowledge = vecs[0]
            vec_command = vecs[1]
        else:
            knowledge_vecs = await client.embed([embed_text], model=embed_model)
            _check_vec_dim(knowledge_vecs[0], "knowledge")
            vec_knowledge = knowledge_vecs[0]
            command_vecs = await client.embed([cmd_text], model=cmd_model)
            _check_vec_dim(command_vecs[0], "command")
            vec_command = command_vecs[0]

        vector_store.insert_vectors(handle, node_id, vec_knowledge, vec_command)
        record_embed_model(handle, embed_model)
        handle.commit()

        if _embed_duration:
            _embed_duration.record((time.monotonic() - t0) * 1000)
    except Exception:
        if _embed_failures:
            _embed_failures.add(1)
        raise


# Column allow-list matches vector_store.knn_search.
_VALID_VECTOR_COLUMNS = ("vec_knowledge", "vec_command")

# Static SQL pulling knowledge_nodes metadata joined with FTS-extracted summary.
_JOIN_SQL = (
    "SELECT n.id, n.uuid, n.content, n.embed_text, n.outcome, n.tags, "
    "n.enrichment_model, n.created_at "
    "FROM knowledge_nodes n WHERE n.id = ?"
)


def search_similar(
    handle: sqlite3.Connection,
    query_vec: list[float],
    column: str = "vec_knowledge",
    limit: int = 10,
) -> list[dict]:
    """KNN search returning LanceDB-shaped dicts for backwards compat.

    Shape mirrors the previous LanceDB rows callers expect (``embed_text``,
    ``summary``, ``outcome``, etc.), with the addition of ``score`` (cosine
    similarity in [0, 1]) and ``_distance`` (raw cosine distance). Values
    that used to live only in LanceDB (``cwd``, ``git_branch``, ``git_repo``,
    ``entities_json``, ``key_decisions``, ``problems_encountered``) are now
    parsed out of ``knowledge_nodes.content`` JSON.
    """
    if column not in _VALID_VECTOR_COLUMNS:
        raise ValueError(f"unsupported vector column: {column}")

    hits = vector_store.knn_search(handle, query_vec, column=column, limit=limit)
    if not hits:
        return []

    out: list[dict] = []
    for hit in hits:
        row = handle.execute(_JOIN_SQL, (hit["knowledge_node_id"],)).fetchone()
        if row is None:
            continue
        node_id, uuid, content_json, embed_text, outcome, tags_json, model, created = row
        content = _safe_json(content_json, {})
        tags = _safe_json(tags_json, [])

        out.append(
            {
                "id": node_id,
                "uuid": uuid,
                "embed_text": embed_text or "",
                "commands_raw": content.get("commands_raw", ""),
                "summary": content.get("summary", ""),
                "key_decisions": content.get("key_decisions", []),
                "problems_encountered": content.get("problems_encountered", []),
                "outcome": outcome or "",
                "tags": tags,
                "cwd": content.get("cwd", ""),
                "git_branch": content.get("git_branch", ""),
                "git_repo": content.get("git_repo", ""),
                "entities_json": json.dumps(content.get("entities", {})),
                "enrichment_model": model or "",
                "captured_at": created,
                "session_id": content.get("session_id", 0),
                "score": hit["score"],
                "_distance": hit["distance"],
            }
        )
    return out
