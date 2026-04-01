#!/usr/bin/env python3
"""Re-embed all knowledge nodes without re-enriching. Use after changing embedding models."""

import asyncio
import json
import shutil
import sqlite3
import sys
import tomllib
from pathlib import Path

from hippo_brain.client import LMStudioClient
from hippo_brain.embeddings import (
    EMBED_DIM,
    _pad_or_truncate,
    get_or_create_table,
    open_vector_db,
)


async def main():
    # Load config
    config_path = Path.home() / ".config" / "hippo" / "config.toml"
    if not config_path.exists():
        print("Error: config not found at", config_path)
        sys.exit(1)
    with config_path.open("rb") as f:
        config = tomllib.load(f)

    embedding_model = config.get("models", {}).get("embedding", "")
    if not embedding_model:
        print("Error: no embedding model configured")
        sys.exit(1)

    lmstudio_url = config.get("lmstudio", {}).get(
        "base_url", "http://localhost:1234/v1"
    )
    data_dir = Path(
        config.get("storage", {}).get(
            "data_dir", str(Path.home() / ".local" / "share" / "hippo")
        )
    ).expanduser()
    db_path = data_dir / "hippo.db"

    if not db_path.exists():
        print("Error: database not found at", db_path)
        sys.exit(1)

    # Connect to SQLite
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    # Load all knowledge nodes
    cursor = conn.execute(
        """SELECT kn.id, kn.embed_text, kn.outcome, kn.tags, kn.content,
                  kn.enrichment_model, kn.created_at
           FROM knowledge_nodes kn
           ORDER BY kn.created_at ASC"""
    )
    nodes = cursor.fetchall()
    total = len(nodes)

    if total == 0:
        print("No knowledge nodes to embed.")
        sys.exit(0)

    print(f"Re-embedding {total} knowledge nodes with model: {embedding_model}")

    # Nuke and recreate vector store
    vectors_dir = data_dir / "vectors"
    if vectors_dir.exists():
        shutil.rmtree(vectors_dir)
        print(f"Deleted {vectors_dir}")

    db = open_vector_db(str(data_dir))
    table = get_or_create_table(db)

    client = LMStudioClient(base_url=lmstudio_url)

    for i, (
        node_id,
        embed_text,
        outcome,
        tags_json,
        content_json,
        enr_model,
        created_at,
    ) in enumerate(nodes, 1):
        # Parse content JSON for extra fields
        try:
            content = json.loads(content_json) if content_json else {}
        except json.JSONDecodeError:
            content = {}

        # Get linked event data
        event_cursor = conn.execute(
            """SELECT e.session_id, e.cwd, e.git_branch, e.git_repo, e.command
               FROM knowledge_node_events kne
               JOIN events e ON kne.event_id = e.id
               WHERE kne.knowledge_node_id = ?
               ORDER BY e.timestamp ASC""",
            (node_id,),
        )
        event_rows = event_cursor.fetchall()

        session_id = event_rows[0][0] if event_rows else 0
        cwd = event_rows[0][1] if event_rows else ""
        git_branch = event_rows[0][2] if event_rows else ""
        git_repo = event_rows[0][3] if event_rows else ""
        commands_raw = " ; ".join(r[4] for r in event_rows) if event_rows else ""

        # Embed both texts in a single API call
        text_knowledge = embed_text or ""
        text_command = commands_raw or embed_text or ""
        vecs = await client.embed([text_knowledge, text_command], model=embedding_model)
        vec_knowledge = _pad_or_truncate(vecs[0], EMBED_DIM)
        vec_command = _pad_or_truncate(vecs[1], EMBED_DIM)

        row = {
            "id": node_id,
            "session_id": session_id,
            "captured_at": created_at or 0,
            "commands_raw": commands_raw,
            "cwd": cwd or "",
            "git_branch": git_branch or "",
            "git_repo": git_repo or "",
            "outcome": outcome or "",
            "tags": tags_json or "[]",
            "entities_json": json.dumps(content.get("entities", {})),
            "embed_text": embed_text or "",
            "summary": content.get("summary", ""),
            "key_decisions": json.dumps(content.get("key_decisions", [])),
            "problems_encountered": json.dumps(content.get("problems_encountered", [])),
            "vec_knowledge": vec_knowledge,
            "vec_command": vec_command,
            "enrichment_model": enr_model or "",
        }

        table.add([row])
        preview = (embed_text or "")[:80]
        print(f"  [{i}/{total}] node {node_id}: {preview}...")

    conn.close()
    print(f"\nDone. {total} nodes re-embedded into {vectors_dir}")


if __name__ == "__main__":
    asyncio.run(main())
