"""Export the knowledge base into an Obsidian vault (one-way projection).

Orchestrates query -> render -> full reconcile over a single read snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from hippo_brain import redaction
from hippo_brain.vault_edges import compute_related
from hippo_brain.vault_render import (
    EntityRow,
    NodeRow,
    entity_slug,
    node_source_key,
    render_entity_page,
    render_node_note,
    render_root_index,
    render_sub_index,
    shard_for,
    slugify,
)

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
        Path(root).mkdir(parents=True, exist_ok=True)
        gi.write_text("# hippo vault is a regenerated projection; do not commit\n*\n")


def check_format_version(root: Path) -> bool:
    """True if the on-disk vault matches our format version (or is fresh)."""
    meta_path = Path(root) / _META_NAME
    if not meta_path.exists():
        return True
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        return False
    return meta.get("vault_format_version") == VAULT_FORMAT_VERSION


def _load_node_links(conn: sqlite3.Connection, node_id: int) -> dict:
    links: dict = {}
    rows = conn.execute(
        "SELECT s.harness, s.session_id, s.segment_index FROM knowledge_node_agentic_sessions l "
        "JOIN agentic_sessions s ON s.id = l.agentic_session_id WHERE l.knowledge_node_id = ?",
        (node_id,),
    ).fetchall()
    if rows:
        links["agentic"] = [(r[0], r[1], r[2]) for r in rows]
    for kind, table, col in (
        ("workflow", "knowledge_node_workflow_runs", "run_id"),
        ("browser", "knowledge_node_browser_events", "browser_event_id"),
        ("shell", "knowledge_node_events", "event_id"),
    ):
        ids = [
            r[0]
            for r in conn.execute(
                f"SELECT {col} FROM {table} WHERE knowledge_node_id = ?", (node_id,)
            ).fetchall()
        ]
        if ids:
            links[kind] = ids
    return links


def export_vault(
    conn: sqlite3.Connection,
    out_dir: str,
    hippo_version: str,
    related_top_k: int,
    hub_degree_cap: int,
    hub_node_list_cap: int,
    shard_by: str,
) -> dict:
    root = Path(out_dir).expanduser()
    if not check_format_version(root):
        raise RuntimeError(
            f"{root} was written by a different vault_format_version; run a full export "
            "into a clean directory."
        )
    assert_safe_target(root)

    node_ids = fetch_exportable_node_ids(conn)

    # Pull node rows + per-node entity sets (typed, from the JOIN — authoritative).
    node_meta: dict[int, dict] = {}
    node_entity_sets: dict[int, set] = {}
    for nid in node_ids:
        r = conn.execute(
            "SELECT uuid, content, embed_text, node_type, outcome, tags, created_at, updated_at "
            "FROM knowledge_nodes WHERE id = ?",
            (nid,),
        ).fetchone()
        ents = conn.execute(
            "SELECT e.id, e.type, e.name, e.canonical FROM knowledge_node_entities kne "
            "JOIN entities e ON e.id = kne.entity_id WHERE kne.knowledge_node_id = ?",
            (nid,),
        ).fetchall()
        node_meta[nid] = {"row": r, "links": _load_node_links(conn, nid), "ents": ents}
        node_entity_sets[nid] = {e[0] for e in ents}  # entity ids

    # Entity degrees over the exported node set only.
    entity_degree: dict = {}
    for ents in node_entity_sets.values():
        for eid in ents:
            entity_degree[eid] = entity_degree.get(eid, 0) + 1

    related_ids = compute_related(node_entity_sets, entity_degree, hub_degree_cap, related_top_k)

    # Stable source-key slug per node id (needed to render related/entity links).
    import json as _json

    def _headline_of(nid: int) -> str:
        try:
            c = _json.loads(node_meta[nid]["row"][1])
            return (c.get("summary") or node_meta[nid]["row"][2] or "")[:80]
        except (ValueError, TypeError):
            return node_meta[nid]["row"][2] or ""

    slug_of: dict[int, str] = {}
    for nid in node_ids:
        row = node_meta[nid]["row"]
        slug_of[nid] = node_source_key(node_meta[nid]["links"], node_type=row[3], uuid=row[0])

    desired: dict[Path, str] = {}
    projects: dict[str, list[tuple[str, str]]] = {}  # project name -> node (slug, headline)
    months: dict[str, list[tuple[str, str]]] = {}  # month shard -> node (slug, headline)
    entity_members: dict[int, list[tuple[str, str]]] = {}

    for nid in node_ids:
        uuid, content_json, embed_text, node_type, outcome, tags_json, created, updated = node_meta[
            nid
        ]["row"]
        ents = node_meta[nid]["ents"]
        entity_links = []
        for eid, etype, ename, ecanon in ents:
            target = f"entities/{etype}/{entity_slug(etype, ename, ecanon, eid)}"
            entity_links.append((etype, ename, target))
            entity_members.setdefault(eid, []).append((slug_of[nid], _headline_of(nid)))
            if etype == "project":
                projects.setdefault(ecanon or ename, []).append((slug_of[nid], _headline_of(nid)))
        related = [(slug_of[t], _headline_of(t)) for t in related_ids.get(nid, [])]
        try:
            tags = _json.loads(tags_json) if tags_json else []
        except ValueError, TypeError:
            tags = []
        node = NodeRow(
            uuid=uuid,
            source_key=slug_of[nid],
            node_type=node_type,
            outcome=outcome,
            content_json=content_json,
            embed_text=embed_text,
            tags=[slugify(str(t)) for t in tags],
            created_ms=created,
            updated_ms=updated,
            entities=entity_links,
            related=related,
            sources=[f"{k}: {v}" for k, v in node_meta[nid]["links"].items()],
        )
        md = redaction.redact(render_node_note(node))  # export-time redaction pass
        shard = shard_for(created) if shard_by == "month" else "all"
        months.setdefault(shard, []).append((slug_of[nid], _headline_of(nid)))
        desired[root / "knowledge" / shard / f"{slug_of[nid]}.md"] = md

    # Entity pages (capped member lists).
    for eid, etype, ecanon, ename, first_seen in conn.execute(
        "SELECT id, type, canonical, name, first_seen FROM entities"
    ).fetchall():
        members = entity_members.get(eid)
        if not members:
            continue
        capped = members[:hub_node_list_cap]
        page = render_entity_page(
            EntityRow(
                entity_type=etype,
                canonical=ecanon or ename,
                first_seen_ms=first_seen,
                members=capped,
                total_members=len(members),
                cap=hub_node_list_cap,
            )
        )
        slug = entity_slug(etype, ename, ecanon, eid)
        desired[root / "entities" / etype / f"{slug}.md"] = redaction.redact(page)

    # Index notes: a small root MOC + one sub-index per project and per month.
    desired[root / "_index.md"] = render_root_index(sorted(projects), sorted(months, reverse=True))
    for proj, members in projects.items():
        desired[root / "indexes" / f"project-{slugify(proj)}.md"] = render_sub_index(proj, members)
    for month, members in months.items():
        desired[root / "indexes" / f"month-{month}.md"] = render_sub_index(month, members)

    write_gitignore(root)
    schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
    config_hash = hashlib.sha256(
        f"{related_top_k}-{hub_degree_cap}-{hub_node_list_cap}-{shard_by}".encode()
    ).hexdigest()[:12]
    write_vault_meta(root, hippo_version, schema_version, config_hash)

    recon = reconcile_files(root, desired, managed_subdirs=["knowledge", "entities", "indexes"])
    return {"nodes": len(node_ids), **recon}
