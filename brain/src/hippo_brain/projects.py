"""Canonical project identity from captured provenance, never model entity guesses."""

from __future__ import annotations

import re
import sqlite3
import tomllib
from pathlib import Path

from hippo_brain import _default_config_path
from hippo_brain.entity_resolver import _resolve_project_roots, canonicalize, strip_worktree_prefix
from hippo_brain.source_filters import table_exists


def load_aliases() -> dict[str, str]:
    """Read the editable alias -> canonical-name map at ingest and query time."""
    try:
        with _default_config_path().open("rb") as stream:
            aliases = tomllib.load(stream).get("projects", {}).get("aliases", {})
    except FileNotFoundError:
        return {}
    if not isinstance(aliases, dict) or any(
        not isinstance(value, str) or not value.strip() for value in aliases.values()
    ):
        raise ValueError("[projects.aliases] must map aliases to nonempty project names")
    return aliases


def normalize(value: str, *, directory: bool = False, roots: list[str] | None = None) -> str:
    """Strip remote/owner spelling; preserve known separate-project subpaths."""
    value = strip_worktree_prefix(value.strip().lower().rstrip("/"))
    if directory or value.startswith("/"):
        roots = _resolve_project_roots(roots)
        protected = canonicalize("project", value, project_roots=roots)
        if any(value.startswith(root.lower().rstrip("/") + "/") for root in roots):
            return protected
        value = Path(protected).name
    elif "/" in value:
        value = value.rsplit("/", 1)[-1]
    value = value.removesuffix(".git").lstrip(".")
    return " ".join(value.split())


def canonical_name(value: str, aliases: dict[str, str], **kwargs) -> str:
    name = normalize(value, **kwargs)
    mapping = {normalize(alias): normalize(target) for alias, target in aliases.items()}
    seen = set()
    while name in mapping and mapping[name] != name:
        if name in seen:
            raise ValueError("cycle in [projects.aliases]")
        seen.add(name)
        name = mapping[name]
    if not name:
        raise ValueError("empty canonical project name")
    return name


def upsert_project(
    conn, value: str, first: int, last: int, aliases: dict[str, str], **kwargs
) -> int:
    name = canonical_name(value, aliases, **kwargs)
    project_id = conn.execute(
        "INSERT INTO projects (canonical_name, first_seen, last_seen) VALUES (?, ?, ?) "
        "ON CONFLICT(canonical_name) DO UPDATE SET "
        "first_seen=min(first_seen, excluded.first_seen), last_seen=max(last_seen, excluded.last_seen) "
        "RETURNING id",
        (name, first, last),
    ).fetchone()[0]
    spellings = {name, value.strip().lower(), normalize(value, **kwargs)}
    spellings.update(
        alias.strip().lower() for alias in aliases if canonical_name(alias, aliases) == name
    )
    conn.executemany(
        "INSERT INTO project_aliases (canonical_id, alias) VALUES (?, ?) "
        "ON CONFLICT(alias) DO UPDATE SET canonical_id=excluded.canonical_id",
        [(project_id, alias) for alias in sorted(spellings) if alias],
    )
    return project_id


def _node_sources(conn, node_id: int):
    """Rank remote-derived repo > local repo > session directory, without git I/O."""
    sources = []
    for repo, timestamp in conn.execute(
        "SELECT e.git_repo, e.timestamp FROM knowledge_node_events l "
        "JOIN events e ON e.id=l.event_id WHERE l.knowledge_node_id=? "
        "AND e.probe_tag IS NULL AND trim(coalesce(e.git_repo, '')) != ''",
        (node_id,),
    ):
        sources.append((3 if "/" in repo else 2, repo, timestamp, timestamp, False))
    for directory, start, end in conn.execute(
        "SELECT s.project_dir, s.start_time, s.end_time FROM knowledge_node_agentic_sessions l "
        "JOIN agentic_sessions s ON s.id=l.agentic_session_id "
        "WHERE l.knowledge_node_id=? AND s.probe_tag IS NULL",
        (node_id,),
    ):
        if directory.strip():
            sources.append((1, directory, start, end, True))
    # Captured git metadata can be correlated by exact session cwd and time.
    for repo, timestamp in conn.execute(
        "SELECT e.git_repo, e.timestamp FROM knowledge_node_agentic_sessions l "
        "JOIN agentic_sessions s ON s.id=l.agentic_session_id "
        "JOIN events e ON e.cwd IN (s.cwd, s.project_dir) "
        "AND e.timestamp BETWEEN s.start_time AND s.end_time "
        "WHERE l.knowledge_node_id=? AND s.probe_tag IS NULL AND e.probe_tag IS NULL "
        "AND trim(coalesce(e.git_repo, '')) != ''",
        (node_id,),
    ):
        sources.append((3 if "/" in repo else 2, repo, timestamp, timestamp, False))
    if table_exists(conn, "knowledge_node_workflow_runs"):
        for repo, timestamp in conn.execute(
            "SELECT r.repo, r.first_seen_at FROM knowledge_node_workflow_runs l "
            "JOIN workflow_runs r ON r.id=l.run_id WHERE l.knowledge_node_id=?",
            (node_id,),
        ):
            if repo:
                sources.append((3, repo, timestamp, timestamp, False))
    if table_exists(conn, "knowledge_node_memory_chunks"):
        for repo, timestamp in conn.execute(
            "SELECT d.repository, n.created_at FROM knowledge_node_memory_chunks l "
            "JOIN memory_chunks c ON c.id=l.memory_chunk_id JOIN memory_revisions r ON r.id=c.revision_id "
            "JOIN memory_documents d ON d.id=r.document_id "
            "JOIN knowledge_nodes n ON n.id=l.knowledge_node_id WHERE l.knowledge_node_id=?",
            (node_id,),
        ):
            if repo:
                sources.append((2, repo, timestamp, timestamp, False))
    return sources


def attribute_node(conn, node_id: int, aliases: dict[str, str] | None = None) -> int | None:
    """Set one project key from linked sources inside the caller's transaction.

    Cross-project batches keep a NULL key rather than picking an arbitrary project.
    Older readable schemas have no project storage and retain their original behavior.
    """
    if not table_exists(conn, "projects"):
        return None
    aliases = load_aliases() if aliases is None else aliases
    sources = _node_sources(conn, node_id)
    if not sources:
        return None
    rank = max(source[0] for source in sources)
    preferred = [source for source in sources if source[0] == rank]
    names = {
        canonical_name(value, aliases, directory=directory)
        for _, value, _, _, directory in preferred
    }
    project_ids = {
        upsert_project(conn, value, first, last, aliases, directory=directory)
        for _, value, first, last, directory in preferred
    }
    project_id = None
    if len(names) == 1:
        project_id = conn.execute(
            "SELECT id FROM projects WHERE canonical_name=?", (names.pop(),)
        ).fetchone()[0]
        assert project_id in project_ids
    conn.execute("UPDATE knowledge_nodes SET project_id=? WHERE id=?", (project_id, node_id))
    return project_id


def backfill(conn, aliases: dict[str, str] | None = None) -> int:
    """Idempotently derive identities and keys without changing captured metadata."""
    aliases = load_aliases() if aliases is None else aliases
    if not table_exists(conn, "projects"):
        raise ValueError("canonical project backfill requires schema v27+")
    with conn:
        for repo, timestamp in conn.execute(
            "SELECT git_repo, timestamp FROM events WHERE probe_tag IS NULL "
            "AND trim(coalesce(git_repo, '')) != ''"
        ).fetchall():
            upsert_project(conn, repo, timestamp, timestamp, aliases)
        for directory, cwd, start, end in conn.execute(
            "SELECT project_dir, cwd, start_time, end_time FROM agentic_sessions "
            "WHERE probe_tag IS NULL AND trim(project_dir) != ''"
        ).fetchall():
            repos = conn.execute(
                "SELECT DISTINCT git_repo FROM events WHERE cwd IN (?, ?) "
                "AND timestamp BETWEEN ? AND ? AND probe_tag IS NULL "
                "AND trim(coalesce(git_repo, '')) != ''",
                (directory, cwd, start, end),
            ).fetchall()
            if repos:
                for (repo,) in repos:
                    upsert_project(conn, repo, start, end, aliases)
            else:
                upsert_project(conn, directory, start, end, aliases, directory=True)
        node_ids = [row[0] for row in conn.execute("SELECT id FROM knowledge_nodes")]
        for node_id in node_ids:
            attribute_node(conn, node_id, aliases)
    return len(node_ids)


def project_clause(conn, project: str, node_alias: str = "kn") -> tuple[str, list] | None:
    """Resolve configured and stored aliases to exact project IDs, without writes."""
    if not table_exists(conn, "projects"):
        return None
    aliases = load_aliases()
    target = canonical_name(project, aliases)
    ids = set()
    for project_id, name in conn.execute("SELECT id, canonical_name FROM projects"):
        if canonical_name(name, aliases) == target:
            ids.add(project_id)
    for project_id, alias in conn.execute("SELECT canonical_id, alias FROM project_aliases"):
        if canonical_name(alias, aliases) == target:
            ids.add(project_id)
    if not ids:
        return None
    placeholders = ",".join("?" for _ in ids)
    return f"{node_alias}.project_id IN ({placeholders})", sorted(ids)


def infer_project(conn, query: str) -> str | None:
    """Scope a single known project mention; do not scope a longer employer name."""
    if not table_exists(conn, "projects"):
        return None
    aliases = load_aliases()
    names = {row[0] for row in conn.execute("SELECT canonical_name FROM projects")}
    names.update(row[0] for row in conn.execute("SELECT alias FROM project_aliases"))
    names.update(aliases)
    text = query.lower()
    # A longer, explicitly named non-project referent wins over its short prefix.
    nonprojects = []
    if table_exists(conn, "entities"):
        nonprojects = [
            row[0].lower()
            for row in conn.execute(
                "SELECT name FROM entities WHERE type != 'project' AND instr(?, lower(name)) > 0",
                (text,),
            )
        ]
    found = set()
    for name in names:
        if re.search(r"(?<![\w.-])" + re.escape(name.lower()) + r"(?![\w.-])", text):
            if any(
                name.lower() in other and other in text and other != name.lower()
                for other in nonprojects
            ):
                continue
            found.add(canonical_name(name, aliases))
    return found.pop() if len(found) == 1 else None


def query_terms(conn, project: str | None) -> list[str]:
    if not project or not table_exists(conn, "projects"):
        return []
    aliases = load_aliases()
    target = canonical_name(project, aliases)
    terms = {target}
    for (alias,) in conn.execute("SELECT alias FROM project_aliases"):
        if canonical_name(alias, aliases) == target:
            terms.add(alias)
    terms.update(alias for alias in aliases if canonical_name(alias, aliases) == target)
    return sorted(terms)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Backfill canonical projects from captured provenance"
    )
    parser.add_argument("--db", type=Path, required=True, help="Explicit existing database path")
    args = parser.parse_args()
    conn = sqlite3.connect(args.db.resolve().as_uri() + "?mode=rw", uri=True)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA secure_delete=ON")
        print(f"Attributed {backfill(conn)} knowledge nodes")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
