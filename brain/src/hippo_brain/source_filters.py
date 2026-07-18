"""Shared knowledge-node source filter SQL for retrieval and MCP queries."""

from __future__ import annotations

import sqlite3

from hippo_brain.auto_memory_constants import PROBE_REPOSITORY

CLAUDE_AUTO_MEMORY_SOURCE = "claude-auto-memory"

# Logical source families keyed by linked_source_ids prefix (see retrieval._fetch_details).
_AGENTIC_LINK_PREFIXES = ("claude-", "codex-", "cursor-", "opencode-")


def source_kind_from_linked_id(link: str) -> str | None:
    """Map a ``SearchResult.linked_source_ids`` entry to a logical source family."""
    if link.startswith("shell-"):
        return "shell"
    if link.startswith("browser-"):
        return "browser"
    if link.startswith("workflow-"):
        return "workflow"
    if link.startswith("memory-"):
        return CLAUDE_AUTO_MEMORY_SOURCE
    for prefix in _AGENTIC_LINK_PREFIXES:
        if link.startswith(prefix):
            return prefix.removesuffix("-")
    return None


_MEMORY_PROBE_EXCLUDE = f"AND md.repository != '{PROBE_REPOSITORY}'"

_MEMORY_SOURCE_EXISTS = (
    "EXISTS (SELECT 1 FROM knowledge_node_memory_chunks knmc "
    "JOIN memory_chunks mc ON mc.id = knmc.memory_chunk_id "
    "JOIN memory_revisions mr ON mr.id = mc.revision_id "
    "JOIN memory_documents md ON md.id = mr.document_id "
    "WHERE knmc.knowledge_node_id = kn.id "
    "AND md.active_revision_id = mr.id AND md.state = 'active' "
    f"{_MEMORY_PROBE_EXCLUDE})"
)

_SOURCE_EXISTS: dict[str, str] = {
    "shell": (
        "EXISTS (SELECT 1 FROM knowledge_node_events kne_s WHERE kne_s.knowledge_node_id = kn.id)"
    ),
    CLAUDE_AUTO_MEMORY_SOURCE: _MEMORY_SOURCE_EXISTS,
}


_MEMORY_PROJECT_EXISTS = (
    "EXISTS (SELECT 1 FROM knowledge_node_memory_chunks knmc "
    "JOIN memory_chunks mc ON mc.id = knmc.memory_chunk_id "
    "JOIN memory_revisions mr ON mr.id = mc.revision_id "
    "JOIN memory_documents md ON md.id = mr.document_id "
    "WHERE knmc.knowledge_node_id = kn.id "
    "AND md.active_revision_id = mr.id AND md.state = 'active' "
    f"{_MEMORY_PROBE_EXCLUDE} "
    "AND (md.repository LIKE ? OR md.source_path LIKE ?))"
)

_MEMORY_CATEGORY_EXISTS = (
    "EXISTS (SELECT 1 FROM knowledge_node_memory_chunks knmc "
    "JOIN memory_chunks mc ON mc.id = knmc.memory_chunk_id "
    "JOIN memory_revisions mr ON mr.id = mc.revision_id "
    "JOIN memory_documents md ON md.id = mr.document_id "
    "JOIN memory_document_categories mdc ON mdc.document_id = md.id "
    "WHERE knmc.knowledge_node_id = kn.id "
    "AND md.active_revision_id = mr.id AND md.state = 'active' "
    f"{_MEMORY_PROBE_EXCLUDE} "
    "AND mdc.category = ?)"
)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?)",
        (table,),
    ).fetchone()
    return bool(row and row[0])


def knowledge_memory_project_clause(conn: sqlite3.Connection | None = None) -> str | None:
    """EXISTS fragment matching auto-memory nodes by project (repository/source_path).

    Binds two ``?`` params: the project LIKE pattern for ``repository`` then for
    ``source_path``. Returns None when the memory tables are absent (older schema)
    so callers can skip it. Shared by retrieval and the MCP query builder so the
    project-by-memory predicate is defined in exactly one place.
    """
    if conn is not None and not table_exists(conn, "knowledge_node_memory_chunks"):
        return None
    return _MEMORY_PROJECT_EXISTS


def knowledge_memory_category_clause(conn: sqlite3.Connection | None = None) -> str | None:
    """EXISTS fragment matching auto-memory nodes by document category."""
    if conn is not None and not table_exists(conn, "memory_document_categories"):
        return None
    return _MEMORY_CATEGORY_EXISTS


def knowledge_source_exists_clause(
    source: str,
    conn: sqlite3.Connection | None = None,
    *,
    claude_link_table: str | None = None,
    claude_link_column: str | None = None,
    claude_session_table: str | None = None,
    include_excluded: bool = False,
) -> str | None:
    """Return an EXISTS clause for ``source``, or None when unsupported."""
    from hippo_brain.retrieval_eligibility import (
        agentic_session_eligible_sql,
        probe_tag_sql,
        workflow_run_eligible_sql,
    )

    if source == "claude" and claude_link_table and claude_link_column and claude_session_table:
        return (
            f"EXISTS (SELECT 1 FROM {claude_link_table} link "
            f"  JOIN {claude_session_table} s ON s.id = link.{claude_link_column} "
            f"  WHERE link.knowledge_node_id = kn.id "
            f"AND {agentic_session_eligible_sql('s', include_excluded=include_excluded)})"
        )
    if source == "claude":
        return (
            "EXISTS (SELECT 1 FROM knowledge_node_agentic_sessions knc_s "
            "JOIN agentic_sessions asx_s ON asx_s.id = knc_s.agentic_session_id "
            f"WHERE knc_s.knowledge_node_id = kn.id "
            f"AND {agentic_session_eligible_sql('asx_s', include_excluded=include_excluded)})"
        )
    if source == "browser":
        return (
            "EXISTS (SELECT 1 FROM knowledge_node_browser_events knb_s "
            "JOIN browser_events be_s ON be_s.id = knb_s.browser_event_id "
            f"WHERE knb_s.knowledge_node_id = kn.id "
            f"AND {probe_tag_sql('be_s.probe_tag', include_excluded=include_excluded)})"
        )
    if source == "workflow":
        return (
            "EXISTS (SELECT 1 FROM knowledge_node_workflow_runs knwr_s "
            "JOIN workflow_runs wr_s ON wr_s.id = knwr_s.run_id "
            f"WHERE knwr_s.knowledge_node_id = kn.id "
            f"AND {workflow_run_eligible_sql('wr_s', include_excluded=include_excluded)})"
        )
    if source == CLAUDE_AUTO_MEMORY_SOURCE:
        if conn is not None and not table_exists(conn, "knowledge_node_memory_chunks"):
            return None
        return _MEMORY_SOURCE_EXISTS
    return _SOURCE_EXISTS.get(source)
