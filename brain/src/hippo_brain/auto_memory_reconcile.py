"""Continuous reconciliation: debounce, stable-read, and ingest orchestration."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hippo_brain.auto_memory_constants import (
    DEFAULT_DEBOUNCE_MS,
    DEFAULT_STABLE_IDLE_MS,
    DEFAULT_STABLE_SAMPLE_MS,
    DEFAULT_STABLE_TIMEOUT_MS,
)
from hippo_brain.auto_memory_lifecycle import (
    RevisionRetention,
    reconcile_configured_sources,
)

SleepFn = Callable[[float], None]
ClockFn = Callable[[], int]


@dataclass(frozen=True)
class ReconcileConfig:
    debounce_ms: int = DEFAULT_DEBOUNCE_MS
    stable_idle_ms: int = DEFAULT_STABLE_IDLE_MS
    stable_sample_ms: int = DEFAULT_STABLE_SAMPLE_MS
    stable_timeout_ms: int = DEFAULT_STABLE_TIMEOUT_MS


@dataclass(frozen=True)
class ReconcileResult:
    path: str
    outcome: str
    changed: bool
    revision_id: int | None
    projection_status: str | None
    pending_enrichment: int
    failed_enrichment: int


def reconcile_config_from_dict(auto_memory: dict[str, Any]) -> ReconcileConfig:
    return ReconcileConfig(
        debounce_ms=max(int(auto_memory.get("debounce_ms", DEFAULT_DEBOUNCE_MS)), 0),
        stable_idle_ms=max(int(auto_memory.get("stable_idle_ms", DEFAULT_STABLE_IDLE_MS)), 1),
        stable_sample_ms=max(
            int(auto_memory.get("stable_sample_ms", DEFAULT_STABLE_SAMPLE_MS)), 1
        ),
        stable_timeout_ms=max(
            int(auto_memory.get("stable_timeout_ms", DEFAULT_STABLE_TIMEOUT_MS)), 1
        ),
    )


def file_stat_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return int(stat.st_mtime * 1000), stat.st_size


def wait_for_stable_file(
    path: Path,
    config: ReconcileConfig,
    *,
    clock_ms: ClockFn | None = None,
    sleep: SleepFn | None = None,
) -> bool:
    """Return True when mtime+size stay unchanged for ``stable_idle_ms``."""
    now_ms = clock_ms or (lambda: int(time.time() * 1000))
    sleep_fn = sleep or time.sleep
    deadline = now_ms() + config.stable_timeout_ms
    last: tuple[int, int] | None = None
    stable_since: int | None = None
    while now_ms() < deadline:
        signature = file_stat_signature(path)
        if signature is None:
            return False
        current = now_ms()
        if last == signature:
            if stable_since is None:
                stable_since = current
            elif current - stable_since >= config.stable_idle_ms:
                return True
        else:
            last = signature
            stable_since = current
        sleep_fn(config.stable_sample_ms / 1000.0)
    return False


def _document_queue_counts(conn: sqlite3.Connection, document_id: int) -> tuple[int, int]:
    row = conn.execute(
        "SELECT current_revision_id FROM memory_documents WHERE id = ?", (document_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return 0, 0
    revision_id = int(row[0])
    pending = conn.execute(
        "SELECT COUNT(*) FROM memory_enrichment_queue "
        "WHERE revision_id = ? AND status IN ('pending', 'processing')",
        (revision_id,),
    ).fetchone()[0]
    failed = conn.execute(
        "SELECT COUNT(*) FROM memory_enrichment_queue "
        "WHERE revision_id = ? AND status = 'failed'",
        (revision_id,),
    ).fetchone()[0]
    return int(pending), int(failed)


def reconcile_source(
    conn: sqlite3.Connection,
    source: dict[str, Any],
    *,
    retention: RevisionRetention,
    reconcile: ReconcileConfig,
    now_ms: int | None = None,
    require_stable: bool = True,
    clock_ms: ClockFn | None = None,
    sleep: SleepFn | None = None,
) -> ReconcileResult:
    """Reconcile one configured source with optional stable-read gating."""
    from hippo_brain.auto_memory import ingest_memory_file

    path_value = source.get("path")
    if not path_value:
        raise ValueError("auto-memory source requires path")
    path = Path(path_value).expanduser()
    resolved = str(path.resolve())
    observed_at = now_ms if now_ms is not None else int(time.time() * 1000)

    if not path.is_file():
        tombstoned = reconcile_configured_sources(
            conn, [source], retention=retention, now_ms=observed_at
        )
        outcome = "tombstoned" if tombstoned else "missing"
        return ReconcileResult(
            path=resolved,
            outcome=outcome,
            changed=False,
            revision_id=None,
            projection_status=None,
            pending_enrichment=0,
            failed_enrichment=0,
        )

    if require_stable and not wait_for_stable_file(
        path, reconcile, clock_ms=clock_ms, sleep=sleep
    ):
        return ReconcileResult(
            path=resolved,
            outcome="unstable",
            changed=False,
            revision_id=None,
            projection_status=None,
            pending_enrichment=0,
            failed_enrichment=0,
        )

    result = ingest_memory_file(
        conn,
        path,
        repository=source.get("repository"),
        logical_path=source.get("logical_path"),
        now_ms=observed_at,
        retention=retention,
    )
    projection_status = conn.execute(
        "SELECT projection_status FROM memory_documents WHERE id = ?",
        (result.document_id,),
    ).fetchone()
    pending, failed = _document_queue_counts(conn, result.document_id)
    outcome = "changed" if result.changed else "unchanged"
    return ReconcileResult(
        path=resolved,
        outcome=outcome,
        changed=result.changed,
        revision_id=result.revision_id,
        projection_status=projection_status[0] if projection_status else None,
        pending_enrichment=pending,
        failed_enrichment=failed,
    )


def reconcile_sources(
    conn: sqlite3.Connection,
    sources: list[dict[str, Any]],
    *,
    retention: RevisionRetention | None = None,
    reconcile: ReconcileConfig | None = None,
    now_ms: int | None = None,
    require_stable: bool = True,
) -> dict[str, Any]:
    """Reconcile every configured source; returns summary JSON for operators."""
    retention_policy = retention or RevisionRetention()
    reconcile_policy = reconcile or ReconcileConfig()
    observed_at = now_ms if now_ms is not None else int(time.time() * 1000)
    changed = 0
    results: list[dict[str, Any]] = []
    for source in sources:
        if not source.get("path"):
            continue
        item = reconcile_source(
            conn,
            source,
            retention=retention_policy,
            reconcile=reconcile_policy,
            now_ms=observed_at,
            require_stable=require_stable,
        )
        if item.changed:
            changed += 1
        results.append(
            {
                "path": item.path,
                "outcome": item.outcome,
                "changed": item.changed,
                "revision_id": item.revision_id,
                "projection_status": item.projection_status,
                "pending_enrichment": item.pending_enrichment,
                "failed_enrichment": item.failed_enrichment,
            }
        )
    reconcile_configured_sources(
        conn, sources, retention=retention_policy, now_ms=observed_at
    )
    pending_total = sum(r["pending_enrichment"] for r in results)
    failed_total = sum(r["failed_enrichment"] for r in results)
    return {
        "changed": changed,
        "pending_enrichment": pending_total,
        "failed_enrichment": failed_total,
        "sources": results,
    }


def load_sources_from_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    auto_memory = config.get("auto_memory", {})
    sources: list[dict[str, Any]] = []
    for source in auto_memory.get("sources", []):
        if not isinstance(source, dict):
            continue
        sources.append(
            {
                "path": str(Path(source.get("path", "")).expanduser()),
                "repository": source.get("repository"),
                "logical_path": source.get("logical_path"),
            }
        )
    return sources
