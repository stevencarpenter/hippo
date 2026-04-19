#!/usr/bin/env python3
"""Ingest GitHub Copilot (Codex) session logs from Xcode into Hippo's knowledge base."""

import sqlite3
import sys
import tomllib
from pathlib import Path

from hippo_brain.claude_sessions import ensure_claude_tables, insert_segment
from hippo_brain.codex_sessions import extract_codex_segments, iter_codex_session_files


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Ingest Codex session logs from Xcode")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be ingested without writing")
    parser.add_argument(
        "--codex-dir",
        type=str,
        default="",
        help="Override Codex data directory (default: ~/Library/Developer/Xcode/CodingAssistant/codex)",
    )
    args = parser.parse_args()

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

    codex_dir = (
        Path(args.codex_dir)
        if args.codex_dir
        else Path.home() / "Library" / "Developer" / "Xcode" / "CodingAssistant" / "codex"
    )
    if not codex_dir.is_dir():
        print(f"Error: Codex directory not found at {codex_dir}")
        sys.exit(1)

    session_files = iter_codex_session_files(codex_dir)
    if not session_files:
        print("No Codex session files found.")
        return

    print(f"Discovered {len(session_files)} session files")

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
        segments = extract_codex_segments(sf)
        if not segments:
            continue

        total_segments += len(segments)

        if args.dry_run:
            print(f"  {sf.path.name}: {len(segments)} segments, cwd={segments[0].cwd}")
            for seg in segments:
                prompts_preview = "; ".join(p[:60] for p in seg.user_prompts[:3])
                print(f"    seg {seg.segment_index}: {seg.message_count} msgs, {len(seg.tool_calls)} tools, prompts: {prompts_preview}")
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


if __name__ == "__main__":
    main()
