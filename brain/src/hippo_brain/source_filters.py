"""Shared knowledge-node source filter SQL for retrieval and MCP queries."""

from __future__ import annotations

import sqlite3

CLAUDE_AUTO_MEMORY_SOURCE = "claude-auto-memory"

_MEMORY_SOURCE_EXISTS = (
    "EXISTS (SELECT 1 FROM knowledge_node_memory_chunks knmc "
    "JOIN memory_chunks mc ON mc.id = knmc.memory_chunk_id "
    "JOIN memory_revisions mr ON mr.id = mc.revision_id "
    "JOIN memory_documents md ON md.id = mr.document_id "
    "WHERE knmc.knowledge_node_id = kn.id "
    "AND md.active_revision_id = mr.id AND md.state = 'active')"
)

_SOURCE_EXISTS: dict[str, str] = {
    "shell": (
        "EXISTS (SELECT 1 FROM knowledge_node_events kne_s WHERE kne_s.knowledge_node_id = kn.id)"
    ),
    "claude": (
        "EXISTS (SELECT 1 FROM knowledge_node_agentic_sessions knc_s "
        "JOIN agentic_sessions asx_s ON asx_s.id = knc_s.agentic_session_id "
        "WHERE knc_s.knowledge_node_id = kn.id AND asx_s.probe_tag IS NULL)"
    ),
    "browser": (
        "EXISTS (SELECT 1 FROM knowledge_node_browser_events knb_s "
        "WHERE knb_s.knowledge_node_id = kn.id)"
    ),
    "workflow": (
        "EXISTS (SELECT 1 FROM knowledge_node_workflow_runs knwr_s "
        "WHERE knwr_s.knowledge_node_id = kn.id)"
    ),
    CLAUDE_AUTO_MEMORY_SOURCE: _MEMORY_SOURCE_EXISTS,
}


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?)",
        (table,),
    ).fetchone()
    return bool(row and row[0])


def knowledge_source_exists_clause(
    source: str,
    conn: sqlite3.Connection | None = None,
    *,
    claude_link_table: str | None = None,
    claude_link_column: str | None = None,
    claude_session_table: str | None = None,
) -> str | None:
    """Return an EXISTS clause for ``source``, or None when unsupported."""
    if source == "claude" and claude_link_table and claude_link_column and claude_session_table:
        return (
            f"EXISTS (SELECT 1 FROM {claude_link_table} link "
            f"  JOIN {claude_session_table} s ON s.id = link.{claude_link_column} "
            "  WHERE link.knowledge_node_id = kn.id AND s.probe_tag IS NULL)"
        )
    if source == CLAUDE_AUTO_MEMORY_SOURCE:
        if conn is not None and not table_exists(conn, "knowledge_node_memory_chunks"):
            return None
        return _MEMORY_SOURCE_EXISTS
    return _SOURCE_EXISTS.get(source)
