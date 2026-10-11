#!/usr/bin/env python3
"""Re-embed derived knowledge through the shared sqlite-vec exclusion boundary.

This is an operator command. Tests call re_embed_nodes with synthetic stores;
never run the live mise re-embed task without separate operational authority.
"""

import argparse
import asyncio
import sys
import tomllib
from pathlib import Path

from hippo_brain.client import InferenceClient
from hippo_brain.embeddings import embed_knowledge_node, open_vector_db
from hippo_brain.vector_store import record_embed_model


async def re_embed_nodes(
    conn, client, embedding_model: str, command_model: str = ""
) -> int:
    """Replace vectors without changing raw events or dropping the existing store."""
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        nodes = conn.execute(
            "SELECT id, embed_text FROM knowledge_nodes ORDER BY id"
        ).fetchall()
        for node_id, embed_text in nodes:
            commands = conn.execute(
                "SELECT e.command FROM knowledge_node_events kne "
                "JOIN events e ON e.id = kne.event_id "
                "WHERE kne.knowledge_node_id = ? ORDER BY e.timestamp, e.id",
                (node_id,),
            ).fetchall()
            await embed_knowledge_node(
                client,
                conn,
                {
                    "id": node_id,
                    "embed_text": embed_text,
                    "commands_raw": " ; ".join(r[0] for r in commands),
                },
                embed_model=embedding_model,
                command_model=command_model,
                allow_embed_switch=True,
                replace_existing=True,
                commit=False,
            )
        record_embed_model(conn, embedding_model)
    return len(nodes)


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
