"""Centralized user-facing retrieval eligibility policy (SNUG-121).

Every RAG, MCP, and ``/ask`` path should apply these predicates by default so
operational noise (probes, workflow journals, in-flight stubs) never satisfies
agent evidence queries. Operators may pass ``include_excluded=True`` on
:class:`~hippo_brain.retrieval.Filters` or set ``HIPPO_RETRIEVAL_INCLUDE_EXCLUDED=1``.

Per-source policy table: ``docs/capture/retrieval-eligibility.md``.
"""

from __future__ import annotations

import os
import sqlite3
import time

from hippo_brain.source_filters import _MEMORY_SOURCE_EXISTS, table_exists as _table_exists

# Align with capture probe / poller settle windows (see probe_agentic.rs).
IN_FLIGHT_SETTLE_MS = 90_000

_ENV_INCLUDE_EXCLUDED = "HIPPO_RETRIEVAL_INCLUDE_EXCLUDED"


def include_excluded_from_env() -> bool:
    """True when operator env requests excluded rows in retrieval."""
    return os.environ.get(_ENV_INCLUDE_EXCLUDED, "").lower() in ("1", "true", "yes")


def probe_tag_sql(column: str = "probe_tag", *, include_excluded: bool = False) -> str:
    """SQL fragment: exclude synthetic probe rows unless operator mode is on."""
    if include_excluded:
        return "1=1"
    return f"{column} IS NULL"


def is_agentic_workflow_journal_sql(alias: str = "asx") -> str:
    """Claude workflow orchestration journals (not user sessions)."""
    return (
        f"({alias}.source_file LIKE '%journal.jsonl' "
        f"AND {alias}.source_file LIKE '%/subagents/%' "
        f"AND {alias}.source_file LIKE '%/workflows/%')"
    )


def is_agentic_empty_stub_sql(alias: str = "asx") -> str:
    """0-turn / empty transcript stubs that carry no user-meaningful signal."""
    return f"({alias}.message_count > 0 OR length(trim({alias}.summary_text)) > 0)"


def is_agentic_in_flight_sql(alias: str = "asx", *, now_ms: int | None = None) -> str:
    """Segments whose end_time is still inside the poller settle window."""
    if now_ms is None:
        return (
            f"({alias}.end_time + {IN_FLIGHT_SETTLE_MS} "
            f"<= CAST((unixepoch('subsec') * 1000) AS INTEGER))"
        )
    return f"({alias}.end_time + {IN_FLIGHT_SETTLE_MS} <= {now_ms})"


def agentic_session_eligible_sql(
    alias: str = "asx",
    *,
    include_excluded: bool = False,
    now_ms: int | None = None,
) -> str:
    """Eligible agentic session row predicate for JOIN/WHERE clauses."""
    if include_excluded:
        return "1=1"
    return " AND ".join(
        (
            probe_tag_sql(f"{alias}.probe_tag"),
            f"NOT {is_agentic_workflow_journal_sql(alias)}",
            is_agentic_empty_stub_sql(alias),
            is_agentic_in_flight_sql(alias, now_ms=now_ms),
        )
    )


def workflow_run_eligible_sql(alias: str = "wr", *, include_excluded: bool = False) -> str:
    """Completed workflow runs only; in-progress status journals are operational."""
    if include_excluded:
        return "1=1"
    return f"({alias}.conclusion IS NOT NULL OR lower({alias}.status) = 'completed')"


def shell_event_eligible_sql(alias: str = "e", *, include_excluded: bool = False) -> str:
    return probe_tag_sql(f"{alias}.probe_tag", include_excluded=include_excluded)


def browser_event_eligible_sql(alias: str = "be", *, include_excluded: bool = False) -> str:
    return probe_tag_sql(f"{alias}.probe_tag", include_excluded=include_excluded)


def knowledge_node_eligible_exists_sql(
    conn: sqlite3.Connection,
    kn_id_ref: str = "kn.id",
    *,
    include_excluded: bool = False,
    now_ms: int | None = None,
) -> tuple[str, list[object]]:
    """Return ``(sql_fragment, params)`` requiring at least one eligible source link."""
    if include_excluded:
        return "1=1", []

    if now_ms is None:
        now_ms = int(time.time() * 1000)

    parts: list[str] = [
        (
            "EXISTS (SELECT 1 FROM knowledge_node_events kne "
            "JOIN events e ON e.id = kne.event_id "
            f"WHERE kne.knowledge_node_id = {kn_id_ref} "
            f"AND {shell_event_eligible_sql('e')})"
        ),
        (
            "EXISTS (SELECT 1 FROM knowledge_node_browser_events knbe "
            "JOIN browser_events be ON be.id = knbe.browser_event_id "
            f"WHERE knbe.knowledge_node_id = {kn_id_ref} "
            f"AND {browser_event_eligible_sql('be')})"
        ),
    ]

    if _table_exists(conn, "knowledge_node_agentic_sessions") and _table_exists(
        conn, "agentic_sessions"
    ):
        agentic = agentic_session_eligible_sql("asx", now_ms=now_ms)
        parts.append(
            "EXISTS (SELECT 1 FROM knowledge_node_agentic_sessions kncs "
            "JOIN agentic_sessions asx ON asx.id = kncs.agentic_session_id "
            f"WHERE kncs.knowledge_node_id = {kn_id_ref} AND {agentic})"
        )

    if _table_exists(conn, "knowledge_node_workflow_runs") and _table_exists(conn, "workflow_runs"):
        wr = workflow_run_eligible_sql("wr")
        parts.append(
            "EXISTS (SELECT 1 FROM knowledge_node_workflow_runs knwr "
            "JOIN workflow_runs wr ON wr.id = knwr.run_id "
            f"WHERE knwr.knowledge_node_id = {kn_id_ref} AND {wr})"
        )

    if _table_exists(conn, "knowledge_node_memory_chunks") and _table_exists(
        conn, "memory_documents"
    ):
        parts.append(_MEMORY_SOURCE_EXISTS.replace("kn.id", kn_id_ref))

    link_tables = [
        "knowledge_node_events",
        "knowledge_node_browser_events",
    ]
    if _table_exists(conn, "knowledge_node_agentic_sessions"):
        link_tables.append("knowledge_node_agentic_sessions")
    if _table_exists(conn, "knowledge_node_workflow_runs"):
        link_tables.append("knowledge_node_workflow_runs")
    if _table_exists(conn, "knowledge_node_memory_chunks"):
        link_tables.append("knowledge_node_memory_chunks")

    no_links = " AND ".join(
        f"NOT EXISTS (SELECT 1 FROM {table} x WHERE x.knowledge_node_id = {kn_id_ref})"
        for table in link_tables
    )

    return f"(({no_links}) OR ({' OR '.join(parts)}))", []
