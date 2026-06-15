"""Export the knowledge base into an Obsidian vault (one-way projection).

Orchestrates query -> render -> full reconcile over a single read snapshot.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

VAULT_FORMAT_VERSION = 1
_META_NAME = "_vault_meta.json"


def fetch_exportable_node_ids(conn: sqlite3.Connection) -> list[int]:
    """Node ids that have >=1 non-probe source row (AP-6 at the export surface)."""
    rows = conn.execute(
        """
        SELECT DISTINCT kn.id
        FROM knowledge_nodes kn
        WHERE EXISTS (SELECT 1 FROM knowledge_node_agentic_sessions l
                        JOIN agentic_sessions s ON s.id = l.agentic_session_id
                       WHERE l.knowledge_node_id = kn.id AND s.probe_tag IS NULL)
           OR EXISTS (SELECT 1 FROM knowledge_node_events l
                        JOIN events e ON e.id = l.event_id
                       WHERE l.knowledge_node_id = kn.id AND e.probe_tag IS NULL)
           OR EXISTS (SELECT 1 FROM knowledge_node_browser_events l
                        JOIN browser_events b ON b.id = l.browser_event_id
                       WHERE l.knowledge_node_id = kn.id AND b.probe_tag IS NULL)
           OR EXISTS (SELECT 1 FROM knowledge_node_workflow_runs l
                       WHERE l.knowledge_node_id = kn.id)
        ORDER BY kn.id
        """
    ).fetchall()
    return [r[0] for r in rows]


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)  # atomic on POSIX


def reconcile_files(root: Path, desired: dict, managed_subdirs: list[str]) -> dict:
    """Write changed files, skip unchanged, delete orphans within managed subdirs."""
    written = unchanged = deleted = 0
    desired = {Path(p): c for p, c in desired.items()}

    for path, content in desired.items():
        if path.exists() and path.read_text(encoding="utf-8") == content:
            unchanged += 1
            continue
        _atomic_write(path, content)
        written += 1

    desired_paths = set(desired)
    for sub in managed_subdirs:
        base = root / sub
        if not base.exists():
            continue
        for existing in base.rglob("*.md"):
            if existing not in desired_paths:
                existing.unlink()
                deleted += 1

    return {"written": written, "unchanged": unchanged, "deleted": deleted}


def assert_safe_target(root: Path) -> None:
    """Refuse to write into a directory that is a foreign Obsidian vault."""
    root = Path(root)
    if (root / ".obsidian").exists() and not (root / _META_NAME).exists():
        raise RuntimeError(
            f"{root} looks like a foreign Obsidian vault (.obsidian present, no hippo "
            f"{_META_NAME}). Refusing to write. Use a dedicated hippo vault dir."
        )


def write_vault_meta(root: Path, hippo_version: str, schema_version: int, config_hash: str) -> None:
    Path(root).mkdir(parents=True, exist_ok=True)
    (Path(root) / _META_NAME).write_text(
        json.dumps(
            {
                "vault_format_version": VAULT_FORMAT_VERSION,
                "hippo_version": hippo_version,
                "schema_version": schema_version,
                "config_hash": config_hash,
            },
            indent=2,
        )
    )


def write_gitignore(root: Path) -> None:
    gi = Path(root) / ".gitignore"
    if not gi.exists():
        gi.write_text("# hippo vault is a regenerated projection; do not commit\n*\n")


def check_format_version(root: Path) -> bool:
    """True if the on-disk vault matches our format version (or is fresh)."""
    meta_path = Path(root) / _META_NAME
    if not meta_path.exists():
        return True
    try:
        meta = json.loads(meta_path.read_text())
    except ValueError, OSError:
        return False
    return meta.get("vault_format_version") == VAULT_FORMAT_VERSION
