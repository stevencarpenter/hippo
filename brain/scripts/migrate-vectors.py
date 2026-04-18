#!/usr/bin/env python3
"""One-shot re-embed of knowledge_nodes missing vec0 rows.

Usage:
    uv run --project brain python brain/scripts/migrate-vectors.py [--data-dir PATH]

Policy (see docs/superpowers/specs/2026-04-17-sqlite-vec-consolidation-design.md):
    Nuke & re-embed. The `knowledge_vectors` vec0 table is expected to be
    empty after migration from LanceDB; this script re-embeds every
    knowledge_nodes row that lacks a corresponding vec0 row.

Exits 0 on success (including zero-nodes-to-embed). Non-zero on error.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tomllib
from pathlib import Path

# Make the in-tree src importable when invoked directly via `python scripts/...`.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hippo_brain.client import LMStudioClient  # noqa: E402
from hippo_brain.embeddings import embed_knowledge_node, open_vector_db  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("migrate-vectors")


def _default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "hippo"


def _default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "hippo" / "config.toml"


def _load_models(config_path: Path) -> tuple[str, str, str]:
    """Return (base_url, embed_model, command_model) from config.toml."""
    if not config_path.exists():
        return ("http://localhost:1234/v1", "", "")
    with config_path.open("rb") as f:
        cfg = tomllib.load(f)
    models = cfg.get("models", {})
    base_url = cfg.get("lmstudio", {}).get("base_url", "http://localhost:1234/v1")
    return (
        base_url,
        models.get("embedding", ""),
        models.get("command_embedding", models.get("embedding", "")),
    )


async def run(db_path: Path | None, data_dir: Path | None, config_path: Path) -> int:
    if db_path:
        # If --db is provided, use its parent as data_dir (for config/etc)
        actual_data_dir = db_path.parent
        conn = open_vector_db(actual_data_dir)
    else:
        conn = open_vector_db(data_dir)
    base_url, embed_model, command_model = _load_models(config_path)

    rows = conn.execute(
        "SELECT n.id, n.uuid, n.content, n.embed_text "
        "FROM knowledge_nodes n "
        "LEFT JOIN knowledge_vectors v ON v.knowledge_node_id = n.id "
        "WHERE v.knowledge_node_id IS NULL "
        "ORDER BY n.id"
    ).fetchall()

    if not rows:
        log.info("no knowledge_nodes are missing vectors; nothing to do")
        return 0

    log.info("re-embedding %d knowledge_nodes", len(rows))
    client = LMStudioClient(base_url=base_url)
    failed = 0
    for node_id, uuid, _content_json, embed_text in rows:
        node_dict = {
            "id": node_id,
            "uuid": uuid,
            "embed_text": embed_text or "",
            "commands_raw": "",
        }
        try:
            await embed_knowledge_node(
                client,
                conn,
                node_dict,
                embed_model=embed_model,
                command_model=command_model,
            )
        except Exception as exc:
            log.warning("failed to re-embed node id=%s uuid=%s: %s", node_id, uuid, exc)
            failed += 1

    log.info("done; failed=%d / total=%d", failed, len(rows))
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="Path to hippo.db (if not specified, uses --data-dir/hippo.db)")
    parser.add_argument("--data-dir", type=Path, default=_default_data_dir())
    parser.add_argument("--config", type=Path, default=_default_config_path())
    args = parser.parse_args()
    return asyncio.run(run(args.db, args.data_dir, args.config))


if __name__ == "__main__":
    raise SystemExit(main())
