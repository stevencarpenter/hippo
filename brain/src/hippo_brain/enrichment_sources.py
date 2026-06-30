"""Registry of enrichment queue sources consumed by the brain poll loop."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hippo_brain.auto_memory import claim_pending_memories
from hippo_brain.browser_enrichment import claim_pending_browser_events
from hippo_brain.claude_sessions import claim_pending_claude_segments
from hippo_brain.enrichment import claim_pending_events_by_session
from hippo_brain.opencode_sessions import claim_pending_opencode_segments
from hippo_brain.workflow_enrichment import claim_pending_workflow_runs

if TYPE_CHECKING:
    from hippo_brain.server import BrainServer

logger = logging.getLogger("hippo_brain")

ClaimFn = Callable[["sqlite3.Connection", "BrainServer"], list[Any]]
EnrichFn = Callable[["BrainServer", list[Any], "sqlite3.Connection | None"], Awaitable[None]]


@dataclass(frozen=True)
class EnrichmentSource:
    name: str
    claim: ClaimFn
    enrich: EnrichFn
    # When True, a claim failure propagates to the enrichment loop (records
    # last_error + backs off) instead of being swallowed. Set for the primary
    # shell source so a structural failure (SQL error, schema drift) surfaces
    # rather than silently stopping shell enrichment with health still green
    # (AP-11). Other sources deliberately swallow-and-continue.
    propagate_claim_errors: bool = False


def _claim_shell(conn: sqlite3.Connection, server: BrainServer) -> list[Any]:
    return claim_pending_events_by_session(
        conn,
        server.enrichment_batch_size,
        "brain-enrichment",
        server.session_stale_secs,
        max_claim_batch=server.max_claim_batch,
        stale_lock_timeout_ms=server.lock_timeout_ms,
    )


def _claim_claude(conn: sqlite3.Connection, server: BrainServer) -> list[Any]:
    return claim_pending_claude_segments(
        conn,
        "brain-enrichment",
        max_claim_batch=server.max_claim_batch,
        stale_lock_timeout_ms=server.lock_timeout_ms,
    )


def _claim_browser(conn: sqlite3.Connection, server: BrainServer) -> list[Any]:
    return claim_pending_browser_events(
        conn,
        "brain-enrichment",
        stale_secs=60,
        max_claim_batch=server.max_claim_batch,
        stale_lock_timeout_ms=server.lock_timeout_ms,
        long_dwell_bypass_ms=server.long_dwell_bypass_ms,
    )


def _claim_workflow(conn: sqlite3.Connection, server: BrainServer) -> list[Any]:
    return claim_pending_workflow_runs(
        conn,
        "brain-enrichment",
        stale_lock_timeout_ms=server.lock_timeout_ms,
        max_claim_batch=server.max_claim_batch,
    )


def _claim_opencode(conn: sqlite3.Connection, server: BrainServer) -> list[Any]:
    return claim_pending_opencode_segments(
        conn,
        "brain-enrichment",
        max_claim_batch=server.max_claim_batch,
        stale_lock_timeout_ms=server.lock_timeout_ms,
    )


def _claim_memory(conn: sqlite3.Connection, server: BrainServer) -> list[Any]:
    return claim_pending_memories(
        conn,
        worker_id="brain-enrichment",
        limit=server.max_claim_batch,
        stale_lock_timeout_ms=server.lock_timeout_ms,
    )


def build_enrichment_sources() -> tuple[EnrichmentSource, ...]:
    return (
        EnrichmentSource(
            "shell",
            _claim_shell,
            lambda s, c, conn: s._enrich_shell_batches(c, conn),
            propagate_claim_errors=True,
        ),
        EnrichmentSource("claude", _claim_claude, lambda s, c, _conn: s._enrich_claude_batches(c)),
        EnrichmentSource(
            "browser", _claim_browser, lambda s, c, _conn: s._enrich_browser_batches(c)
        ),
        EnrichmentSource(
            "workflow", _claim_workflow, lambda s, c, _conn: s._enrich_workflow_runs(c)
        ),
        EnrichmentSource(
            "opencode", _claim_opencode, lambda s, c, _conn: s._enrich_opencode_batches(c)
        ),
        EnrichmentSource(
            "claude-auto-memory",
            _claim_memory,
            lambda s, c, _conn: s._enrich_memory_claims(c),
        ),
    )


def claim_all_sources(conn: sqlite3.Connection, server: BrainServer) -> dict[str, list[Any]]:
    claims: dict[str, list[Any]] = {}
    for source in build_enrichment_sources():
        try:
            claims[source.name] = source.claim(conn, server)
        except Exception as e:
            if source.propagate_claim_errors:
                # AP-11: surface structural claim failures for this source so the
                # enrichment loop records last_error and backs off instead of
                # silently enriching nothing while health reports green.
                raise
            if source.name == "claude":
                logger.debug("no claude segments to process: %s", e)
            else:
                logger.warning("%s claim error: %s", source.name, e, exc_info=True)
            claims[source.name] = []
    return claims


async def enrich_all_sources(
    server: BrainServer,
    claims: dict[str, list[Any]],
    conn: sqlite3.Connection,
) -> None:
    import asyncio

    await asyncio.gather(
        *(
            source.enrich(server, claims.get(source.name, []), conn)
            for source in build_enrichment_sources()
        )
    )
