#!/usr/bin/env python3
"""Ingest Claude Code session logs into Hippo's knowledge base."""

import asyncio
import sqlite3
import sys
import time
import tomllib
from pathlib import Path

from hippo_brain.claude_sessions import (
    CLAUDE_SYSTEM_PROMPT,
    build_claude_enrichment_prompt,
    claim_pending_claude_segments,
    ensure_claude_tables,
    extract_segments,
    insert_segment,
    iter_session_files,
    mark_claude_queue_failed,
    write_claude_knowledge_node,
)
from hippo_brain.enrichment import parse_enrichment_response


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Ingest Claude Code session logs")
    parser.add_argument("--enrich", action="store_true", help="Also run enrichment after ingestion")
    parser.add_argument("--model", type=str, default="", help="Override enrichment model")
    parser.add_argument("--project", type=str, default="", help="Only ingest sessions for a specific project cwd")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be ingested without writing")
    parser.add_argument("--claude-dir", type=str, default="", help="Override Claude projects directory")
    args = parser.parse_args()

    # Load config
    config_path = Path.home() / ".config" / "hippo" / "config.toml"
    config = {}
    if config_path.exists():
        with config_path.open("rb") as f:
            config = tomllib.load(f)

    storage = config.get("storage", {})
    data_dir = Path(
        storage.get("data_dir", str(Path.home() / ".local" / "share" / "hippo"))
    ).expanduser()
    db_path = data_dir / "hippo.db"

    if not db_path.exists():
        print(f"Error: database not found at {db_path}")
        sys.exit(1)

    claude_dir = Path(args.claude_dir) if args.claude_dir else Path.home() / ".claude" / "projects"
    if not claude_dir.is_dir():
        print(f"Error: Claude projects directory not found at {claude_dir}")
        sys.exit(1)

    # Discover session files
    session_files = iter_session_files(claude_dir)
    if not session_files:
        print("No Claude session files found.")
        return

    print(f"Discovered {len(session_files)} session files")

    # Filter by project if specified
    if args.project:
        session_files = [
            sf for sf in session_files
            if args.project in sf.project_dir or args.project in str(sf.path)
        ]
        print(f"Filtered to {len(session_files)} files matching '{args.project}'")

    if not session_files:
        print("No matching session files.")
        return

    # Connect to database
    if not args.dry_run:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        ensure_claude_tables(conn)
    else:
        conn = None

    total_segments = 0
    total_inserted = 0
    total_skipped = 0

    for sf in session_files:
        segments = extract_segments(sf)
        if not segments:
            continue

        total_segments += len(segments)
        project_cwd = segments[0].cwd if segments else "unknown"

        if args.dry_run:
            main_or_sub = "subagent" if sf.is_subagent else "main"
            print(f"  {sf.path.name} ({main_or_sub}): {len(segments)} segments, cwd={project_cwd}")
            for seg in segments:
                prompts_preview = "; ".join(p[:60] for p in seg.user_prompts[:3])
                tools_count = len(seg.tool_calls)
                print(f"    seg {seg.segment_index}: {seg.message_count} msgs, {tools_count} tools, prompts: {prompts_preview}")
            continue

        for seg in segments:
            seg_id = insert_segment(conn, seg)
            if seg_id is not None:
                total_inserted += 1
            else:
                total_skipped += 1

    if args.dry_run:
        print(f"\nDry run: {total_segments} segments across {len(session_files)} files")
        return

    print(f"\nIngested: {total_inserted} new segments, skipped {total_skipped} duplicates")

    if conn:
        conn.close()

    # Run enrichment on any pending segments (new or previously queued)
    if args.enrich:
        pending = 0
        check_conn = sqlite3.connect(str(db_path))
        try:
            pending = check_conn.execute(
                "SELECT COUNT(*) FROM claude_enrichment_queue WHERE status = 'pending'"
            ).fetchone()[0]
        finally:
            check_conn.close()
        if pending > 0:
            print(f"\nStarting enrichment ({pending} segments pending)...")
            asyncio.run(run_enrichment(config, args.model, db_path, data_dir))
        else:
            print("\nNo pending segments to enrich.")


async def run_enrichment(config: dict, model_override: str, db_path: Path, data_dir: Path):
    """Run enrichment on pending Claude session segments."""
    from hippo_brain.client import LMStudioClient
    from hippo_brain.embeddings import (
        embed_knowledge_node,
        get_or_create_table,
        open_vector_db,
    )

    models = config.get("models", {})
    enrichment_model = model_override or models.get("enrichment_bulk") or models.get("enrichment", "")
    embedding_model = models.get("embedding", "")

    if not enrichment_model:
        print("Error: no enrichment model configured")
        return

    lmstudio_url = config.get("lmstudio", {}).get("base_url", "http://localhost:1234/v1")
    client = LMStudioClient(base_url=lmstudio_url, timeout=120.0)

    # Vector store
    vector_table = None
    if embedding_model:
        try:
            vector_db = open_vector_db(str(data_dir))
            vector_table = get_or_create_table(vector_db)
        except Exception as e:
            print(f"Warning: could not initialize vector store: {e}")

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")

    worker_id = "claude-ingestion"
    total_enriched = 0
    total_failed = 0

    while True:
        batches = claim_pending_claude_segments(conn, worker_id)
        if not batches:
            break

        for segments in batches:
            segment_ids = [s["id"] for s in segments]
            # Build prompt from summary_text (already pre-formatted)
            prompt = "\n---\n\n".join(s["summary_text"] for s in segments)

            cwd = segments[0].get("cwd", "unknown")
            print(f"  Enriching {len(segments)} segments from {cwd}...")

            result = None
            last_err = None
            for attempt in range(3):
                try:
                    messages = [
                        {"role": "system", "content": CLAUDE_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ]
                    if attempt > 0:
                        messages.append(
                            {
                                "role": "user",
                                "content": "Your previous response was not valid JSON. Output ONLY a JSON object.",
                            }
                        )
                    raw = await client.chat(messages=messages, model=enrichment_model)
                    result = parse_enrichment_response(raw)
                    break
                except Exception as e:
                    last_err = e
                    print(f"    Attempt {attempt + 1} failed: {e}")

            if result is None:
                print(f"    FAILED after 3 attempts: {last_err}")
                mark_claude_queue_failed(conn, segment_ids, str(last_err))
                total_failed += len(segments)
                continue

            node_id = write_claude_knowledge_node(conn, result, segment_ids, enrichment_model)
            total_enriched += len(segments)
            print(f"    -> node {node_id}: {result.summary[:80]}")

            if vector_table and embedding_model:
                try:
                    # Collect commands from tool calls
                    all_tools = []
                    for s in segments:
                        try:
                            tools = json.loads(s.get("tool_calls_json", "[]"))
                            all_tools.extend(
                                f"{t['name']}: {t['summary']}" for t in tools
                            )
                        except (json.JSONDecodeError, KeyError):
                            pass

                    node_dict = {
                        "id": node_id,
                        "session_id": 0,
                        "captured_at": int(time.time() * 1000),
                        "commands_raw": " ; ".join(all_tools[:50]),
                        "cwd": cwd,
                        "git_branch": segments[0].get("git_branch", ""),
                        "git_repo": "",
                        "outcome": result.outcome,
                        "tags": result.tags,
                        "entities": result.entities if isinstance(result.entities, dict) else {},
                        "embed_text": result.embed_text,
                        "summary": result.summary,
                        "key_decisions": result.key_decisions,
                        "problems_encountered": result.problems_encountered,
                        "enrichment_model": enrichment_model,
                    }
                    await embed_knowledge_node(
                        client, vector_table, node_dict, embed_model=embedding_model
                    )
                except Exception as e:
                    print(f"    Embedding failed (non-fatal): {e}")

    conn.close()
    print(f"\nEnrichment done. Enriched: {total_enriched} segments, Failed: {total_failed}")


if __name__ == "__main__":
    main()
