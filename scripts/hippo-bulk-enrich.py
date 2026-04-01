#!/usr/bin/env python3
"""Bulk enrichment: process all pending events using the bulk enrichment model."""

import asyncio
import sqlite3
import sys
import time
import tomllib
from pathlib import Path

from hippo_brain.client import LMStudioClient
from hippo_brain.embeddings import (
    embed_knowledge_node,
    get_or_create_table,
    open_vector_db,
)
from hippo_brain.enrichment import (
    SYSTEM_PROMPT,
    build_enrichment_prompt,
    mark_queue_failed,
    parse_enrichment_response,
    write_knowledge_node,
    claim_pending_events_by_cwd,
    claim_pending_events_by_session,
)


async def main():
    config_path = Path.home() / ".config" / "hippo" / "config.toml"
    if not config_path.exists():
        print("Error: config not found at", config_path)
        sys.exit(1)
    with config_path.open("rb") as f:
        config = tomllib.load(f)

    models = config.get("models", {})
    # Use bulk model if available, fall back to regular enrichment model
    enrichment_model = models.get("enrichment_bulk") or models.get("enrichment", "")
    embedding_model = models.get("embedding", "")

    if not enrichment_model:
        print(
            "Error: no enrichment model configured"
            " (set models.enrichment_bulk or models.enrichment)"
        )
        sys.exit(1)

    # Allow CLI override
    if len(sys.argv) > 1:
        enrichment_model = sys.argv[1]

    lmstudio_url = config.get("lmstudio", {}).get(
        "base_url", "http://localhost:1234/v1"
    )
    brain_config = config.get("brain", {})
    max_per_chunk = brain_config.get(
        "max_events_per_chunk", brain_config.get("enrichment_batch_size", 30)
    )

    storage = config.get("storage", {})
    data_dir = Path(
        storage.get("data_dir", str(Path.home() / ".local" / "share" / "hippo"))
    ).expanduser()
    db_path = data_dir / "hippo.db"

    print(f"Bulk enrichment model: {enrichment_model}")
    print(f"Embedding model: {embedding_model}")
    print(f"Max events per chunk: {max_per_chunk}")
    print()

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")

    client = LMStudioClient(base_url=lmstudio_url, timeout=120.0)

    # Set up vector store
    vector_table = None
    if embedding_model:
        try:
            vector_db = open_vector_db(str(data_dir))
            vector_table = get_or_create_table(vector_db)
        except Exception as e:
            print(f"Warning: could not initialize vector store: {e}")

    worker_id = "bulk-enrichment"
    total_enriched = 0
    total_failed = 0

    # Use --by-session flag to fall back to session-based grouping
    use_cwd = "--by-session" not in sys.argv

    while True:
        if use_cwd:
            chunks = claim_pending_events_by_cwd(conn, worker_id)
        else:
            chunks = claim_pending_events_by_session(
                conn, max_per_chunk, worker_id, stale_secs=0
            )
        if not chunks:
            break

        for events in chunks:
            event_ids = [e["id"] for e in events]
            prompt = build_enrichment_prompt(events)
            cmds_preview = "; ".join(e.get("command", "")[:40] for e in events[:3])
            print(f"  Processing {len(event_ids)} events: {cmds_preview}...")

            result = None
            last_err = None
            for attempt in range(3):
                try:
                    messages = [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ]
                    if attempt > 0:
                        messages.append(
                            {
                                "role": "user",
                                "content": "Your previous response was not valid JSON."
                                " Output ONLY a JSON object.",
                            }
                        )
                    raw = await client.chat(
                        messages=messages,
                        model=enrichment_model,
                    )
                    result = parse_enrichment_response(raw)
                    break
                except Exception as e:
                    last_err = e
                    print(f"    Attempt {attempt + 1} failed: {e}")

            if result is None:
                print(f"    FAILED after 3 attempts: {last_err}")
                mark_queue_failed(conn, event_ids, str(last_err))
                total_failed += len(event_ids)
                continue

            node_id = write_knowledge_node(conn, result, event_ids, enrichment_model)
            total_enriched += len(event_ids)
            print(f"    -> node {node_id}: {result.summary[:80]}")

            if vector_table and embedding_model:
                try:
                    node_dict = {
                        "id": node_id,
                        "session_id": events[0].get("session_id", 0),
                        "captured_at": int(time.time() * 1000),
                        "commands_raw": " ; ".join(
                            e.get("command", "") for e in events
                        ),
                        "cwd": events[0].get("cwd", ""),
                        "git_branch": events[0].get("git_branch", ""),
                        "git_repo": "",
                        "outcome": result.outcome,
                        "tags": result.tags,
                        "entities": (
                            result.entities if isinstance(result.entities, dict) else {}
                        ),
                        "embed_text": result.embed_text,
                        "summary": result.summary,
                        "key_decisions": result.key_decisions,
                        "problems_encountered": result.problems_encountered,
                        "enrichment_model": enrichment_model,
                    }
                    await embed_knowledge_node(
                        client,
                        vector_table,
                        node_dict,
                        embed_model=embedding_model,
                    )
                except Exception as e:
                    print(f"    Embedding failed (non-fatal): {e}")

    conn.close()
    print(f"\nDone. Enriched: {total_enriched} events, Failed: {total_failed} events")


if __name__ == "__main__":
    asyncio.run(main())
