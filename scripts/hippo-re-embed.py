#!/usr/bin/env python3
"""Re-embed derived knowledge through the shared sqlite-vec exclusion boundary.

This is an operator command. Tests call re_embed_nodes with synthetic stores;
never run the live mise re-embed task without separate operational authority.
"""

import argparse
import asyncio
import struct
import sys
import tomllib
from pathlib import Path
from tempfile import TemporaryFile

from hippo_brain.client import InferenceClient
from hippo_brain.embeddings import (
    clean_credential_orphans,
    open_vector_db,
    prepare_node_vectors,
    store_node_vectors,
)
from hippo_brain.vector_store import EMBED_DIM, get_stored_embed_model, record_embed_model


def _corpus_inputs(conn):
    rows = conn.execute(
        "SELECT id, uuid, content, embed_text, tags FROM knowledge_nodes ORDER BY id"
    ).fetchall()
    return [
        (
            tuple(row),
            tuple(
                command[0]
                for command in conn.execute(
                    "SELECT e.command FROM knowledge_node_events kne "
                    "JOIN events e ON e.id = kne.event_id "
                    "WHERE kne.knowledge_node_id = ? ORDER BY e.timestamp, e.id",
                    (row[0],),
                )
            ),
        )
        for row in rows
    ]


async def re_embed_nodes(
    conn, client, embedding_model: str, command_model: str = ""
) -> int:
    """Stage inference, then atomically publish a corpus whose inputs did not change."""
    conn.execute("BEGIN")
    with conn:
        inputs = _corpus_inputs(conn)
        old_model = get_stored_embed_model(conn)
    vector_pair = struct.Struct(f"<{EMBED_DIM * 2}d")
    # Keep validated vector payloads on disk, not one Python pair per node.
    with TemporaryFile() as staged:
        for row, commands in inputs:
            node = {
                "id": row[0],
                "embed_text": row[3],
                "commands_raw": " ; ".join(commands),
            }
            vectors = await prepare_node_vectors(
                client, node, embedding_model, command_model
            )
            staged.write(vector_pair.pack(*vectors[0], *vectors[1]))
            del vectors
        staged.seek(0)
        conn.execute("BEGIN IMMEDIATE")
        with conn:
            if _corpus_inputs(conn) != inputs or get_stored_embed_model(conn) != old_model:
                raise RuntimeError(
                    "corpus changed during inference; migration not published"
                )
            for row, _ in inputs:
                values = vector_pair.unpack(staged.read(vector_pair.size))
                store_node_vectors(
                    conn,
                    {"id": row[0]},
                    (list(values[:EMBED_DIM]), list(values[EMBED_DIM:])),
                    embedding_model,
                    allow_embed_switch=True,
                    replace_existing=True,
                    commit=False,
                )
            clean_credential_orphans(conn)
            record_embed_model(conn, embedding_model)
    return len(inputs)


async def main():
    argparse.ArgumentParser(
        prog="hippo-re-embed",
        description="Re-embed all knowledge_nodes using the current configured embedding model.",
    ).parse_args()
    config_path = Path.home() / ".config" / "hippo" / "config.toml"
    if not config_path.exists():
        print("Error: config not found at", config_path)
        sys.exit(1)
    with config_path.open("rb") as f:
        config = tomllib.load(f)
    models = config.get("models", {})
    embedding_model = models.get("embedding", "")
    if not embedding_model:
        print("Error: no embedding model configured")
        sys.exit(1)
    if "lmstudio" in config and "inference" not in config:
        print(
            "Error: config.toml uses the deprecated [lmstudio] section. Rename it to [inference]."
        )
        sys.exit(1)
    inference_url = config.get("inference", {}).get(
        "base_url", "http://localhost:8000/v1"
    )
    data_dir = Path(
        config.get("storage", {}).get(
            "data_dir", str(Path.home() / ".local" / "share" / "hippo")
        )
    ).expanduser()
    if not (data_dir / "hippo.db").exists():
        print("Error: database not found at", data_dir / "hippo.db")
        sys.exit(1)
    conn = open_vector_db(data_dir)
    client = InferenceClient(base_url=inference_url)
    try:
        count = await re_embed_nodes(
            conn, client, embedding_model, models.get("command_embedding", "")
        )
        print(f"Done. {count} knowledge nodes re-embedded.")
    finally:
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())
