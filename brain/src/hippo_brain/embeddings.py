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
from hippo_brain.redaction import index_text
from hippo_brain.source_filters import table_exists
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
    "embed_knowledge_node",
    "get_or_create_table",
    "open_vector_db",
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


def embed_dict_from_result(node_id: int, embed_text: str) -> dict:
    """Minimal payload for :func:`embed_knowledge_node`."""
    return {"id": node_id, "embed_text": embed_text}


def _check_vec_dim(vec: list[float], label: str) -> None:
    if len(vec) != EMBED_DIM:
        raise ValueError(
            f"{label} vector has {len(vec)} dimensions; expected {EMBED_DIM}. "
            "The embedding model may have changed — re-embed the corpus or "
            "update config.embeddings.model to match the loaded model."
        )


async def prepare_node_vectors(
    client: Any, node_dict: dict, embed_model: str = "", command_model: str = ""
) -> tuple[list[float], list[float]]:
    """Generate both validated vector arms without opening a database transaction."""
    embed_text = index_text(node_dict.get("embed_text", "") or "")
    commands_raw = index_text(node_dict.get("commands_raw", "") or "")
    # Separate requests avoid the inference server's mixed-length batch bug.
    knowledge_vecs = await client.embed([embed_text], model=embed_model)
    _check_vec_dim(knowledge_vecs[0], "knowledge")
    command_vecs = await client.embed(
        [commands_raw or embed_text], model=command_model or embed_model
    )
    _check_vec_dim(command_vecs[0], "command")
    return knowledge_vecs[0], command_vecs[0]


def _delete_orphan_entity(handle: sqlite3.Connection, entity_id: int) -> None:
    handle.execute(
        "DELETE FROM entities WHERE id=? "
        "AND NOT EXISTS(SELECT 1 FROM knowledge_node_entities WHERE entity_id=entities.id) "
        "AND NOT EXISTS(SELECT 1 FROM event_entities WHERE entity_id=entities.id) "
        "AND NOT EXISTS(SELECT 1 FROM relationships WHERE from_entity_id=entities.id OR to_entity_id=entities.id)",
        (entity_id,),
    )


def _clean_node_entities(handle: sqlite3.Connection, node_id: int) -> None:
    if not table_exists(handle, "knowledge_node_entities"):
        return
    from hippo_brain.classification import OWNER_METADATA, source_entity_canonical
    from hippo_brain.enrichment import upsert_entities
    from hippo_brain.entity_resolver import canonicalize

    rows = handle.execute(
        "SELECT e.id,e.type,e.name,e.canonical,e.metadata FROM entities e "
        "JOIN knowledge_node_entities ne ON ne.entity_id=e.id WHERE ne.knowledge_node_id=?",
        (node_id,),
    ).fetchall()
    for entity_id, kind, name, canonical, metadata in rows:
        if metadata == OWNER_METADATA:
            continue
        clean_name = index_text(name)
        if clean_name == name and index_text(canonical or "") == (canonical or ""):
            continue
        handle.execute(
            "DELETE FROM knowledge_node_entities WHERE knowledge_node_id=? AND entity_id=?",
            (node_id, entity_id),
        )
        upsert_entities(
            handle, node_id, {kind: [clean_name]}, {kind: kind}, time.time_ns() // 1_000_000
        )
        target = handle.execute(
            "SELECT id,name FROM entities WHERE type=? AND canonical=?",
            (kind, source_entity_canonical(kind, canonicalize(kind, clean_name))),
        ).fetchone()
        if target and index_text(target[1]) != target[1]:
            handle.execute(
                "UPDATE entities SET name=? WHERE id=?", (index_text(target[1]), target[0])
            )
        _delete_orphan_entity(handle, entity_id)


def clean_credential_orphans(handle: sqlite3.Connection) -> None:
    """Remove unreferenced credential-shaped derived entities once per corpus rebuild."""
    if not table_exists(handle, "entities"):
        return
    from hippo_brain.classification import OWNER_METADATA

    rows = handle.execute(
        "SELECT e.id,e.name,e.canonical,e.metadata FROM entities e "
        "WHERE NOT EXISTS(SELECT 1 FROM knowledge_node_entities WHERE entity_id=e.id) "
        "AND NOT EXISTS(SELECT 1 FROM event_entities WHERE entity_id=e.id) "
        "AND NOT EXISTS(SELECT 1 FROM relationships WHERE from_entity_id=e.id OR to_entity_id=e.id)"
    ).fetchall()
    for entity_id, name, canonical, metadata in rows:
        if metadata != OWNER_METADATA and (
            index_text(name) != name or index_text(canonical or "") != (canonical or "")
        ):
            _delete_orphan_entity(handle, entity_id)


def store_node_vectors(
    handle: sqlite3.Connection,
    node_dict: dict,
    vectors: tuple[list[float], list[float]],
    embed_model: str = "",
    *,
    allow_embed_switch: bool = False,
    replace_existing: bool = False,
    commit: bool = True,
) -> None:
    """Publish prepared vectors and derived cleanup in the caller's transaction."""
    node_id = int(node_dict.get("id", 0) or 0)
    if node_id <= 0:
        raise ValueError("embedding requires the knowledge_nodes primary key")
    _check_vec_dim(vectors[0], "knowledge")
    _check_vec_dim(vectors[1], "command")
    if replace_existing and not handle.in_transaction:
        handle.execute("BEGIN IMMEDIATE")
    try:
        check_embed_model_drift(handle, embed_model, allow_switch=allow_embed_switch)
        stored = None
        if table_exists(handle, "knowledge_nodes"):
            stored = handle.execute(
                "SELECT content, embed_text, tags FROM knowledge_nodes WHERE id = ?", (node_id,)
            ).fetchone()
        if stored:
            clean = tuple(index_text(text) if text is not None else None for text in stored)
            if clean != tuple(stored):
                handle.execute(
                    "UPDATE knowledge_nodes SET content = ?, embed_text = ?, tags = ? WHERE id = ?",
                    (*clean, node_id),
                )
                if clean[:2] != tuple(stored)[:2]:
                    from hippo_brain.classification import enqueue_node

                    enqueue_node(handle, node_id)
            _clean_node_entities(handle, node_id)
        if replace_existing:
            handle.execute("DELETE FROM knowledge_vectors WHERE knowledge_node_id = ?", (node_id,))
        vector_store.insert_vectors(handle, node_id, *vectors)
        record_embed_model(handle, embed_model)
        if commit:
            handle.commit()
    except Exception:
        handle.rollback()
        raise


async def embed_knowledge_node(
    client: Any,
    handle: sqlite3.Connection,
    node_dict: dict,
    embed_model: str = "",
    command_model: str = "",
    *,
    allow_embed_switch: bool = False,
    replace_existing: bool = False,
    commit: bool = True,
) -> None:
    """Embed a knowledge node and persist both vectors to ``knowledge_vectors``.

    ``node_dict`` must carry at least ``id`` (the knowledge_nodes PK) and
    ``embed_text``. A nonpositive or missing ``node_dict["id"]`` raises
    ``ValueError``; the caller must provide the foreign key.

    With ``commit=False``, the caller owns the transaction and must commit or
    roll back the complete operation, including failures during inference.

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

        vectors = await prepare_node_vectors(client, node_dict, embed_model, command_model)
        store_node_vectors(
            handle,
            node_dict,
            vectors,
            embed_model,
            allow_embed_switch=allow_embed_switch,
            replace_existing=replace_existing,
            commit=commit,
        )

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
