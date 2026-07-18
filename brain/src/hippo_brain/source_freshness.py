"""Capture freshness and coverage signals for evidence responses (SNUG-125).

Reads ``source_health`` and raw-table coverage counts only — never enrichment
queue depth — so agents can judge whether cited evidence came from healthy,
stale, idle, or failing capture paths.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Sequence

from hippo_brain.source_filters import CLAUDE_AUTO_MEMORY_SOURCE, table_exists

# Evidence ``source_kind`` → ``source_health.source`` keys.
SOURCE_KIND_HEALTH: dict[str, str] = {
    "shell": "shell",
    "claude-tool": "claude-tool",
    "claude": "agentic-session-claude",
    "codex": "agentic-session-codex",
    "cursor": "agentic-session-cursor",
    "opencode": "agentic-session-opencode",
    "browser": "browser",
    "workflow": "workflow",
    CLAUDE_AUTO_MEMORY_SOURCE: "claude-auto-memory",
}

BURSTY_SOURCES = frozenset(
    {
        "agentic-session-opencode",
        "agentic-session-codex",
        "agentic-session-cursor",
    }
)

POLLER_FAILURE_THRESHOLD = 3
_HOUR_MS = 3600 * 1000
_DAY_MS = 24 * _HOUR_MS

# Soft / hard staleness thresholds (epoch ms), aligned with doctor probes.
_THRESHOLDS_MS: dict[str, tuple[int, int]] = {
    "shell": (24 * _HOUR_MS, 7 * _DAY_MS),
    "claude-tool": (24 * _HOUR_MS, 7 * _DAY_MS),
    "agentic-session-claude": (12 * _HOUR_MS, 7 * _DAY_MS),
    "browser": (48 * _HOUR_MS, 14 * _DAY_MS),
    "workflow": (3 * _DAY_MS, 30 * _DAY_MS),
    "agentic-session-opencode": (3 * _DAY_MS, 30 * _DAY_MS),
    "agentic-session-codex": (3 * _DAY_MS, 30 * _DAY_MS),
    "agentic-session-cursor": (3 * _DAY_MS, 30 * _DAY_MS),
    "claude-auto-memory": (7 * _DAY_MS, 30 * _DAY_MS),
}

# Raw-table coverage probes (COUNT, MAX(ts)), probe rows excluded where applicable.
_COVERAGE_SQL: dict[str, str] = {
    "shell": (
        "SELECT COUNT(*), MAX(timestamp) FROM events "
        "WHERE source_kind = 'shell' AND probe_tag IS NULL"
    ),
    "claude-tool": (
        "SELECT COUNT(*), MAX(timestamp) FROM events "
        "WHERE source_kind = 'claude-tool' AND probe_tag IS NULL"
    ),
    "agentic-session-claude": (
        "SELECT COUNT(*), MAX(end_time) FROM agentic_sessions "
        "WHERE harness = 'claude-code' AND probe_tag IS NULL"
    ),
    "browser": "SELECT COUNT(*), MAX(timestamp) FROM browser_events WHERE probe_tag IS NULL",
    "workflow": "SELECT COUNT(*), MAX(started_at) FROM workflow_runs",
    "agentic-session-opencode": (
        "SELECT COUNT(*), MAX(end_time) FROM agentic_sessions "
        "WHERE harness = 'opencode' AND probe_tag IS NULL"
    ),
    "agentic-session-codex": (
        "SELECT COUNT(*), MAX(end_time) FROM agentic_sessions "
        "WHERE harness = 'codex' AND probe_tag IS NULL"
    ),
    "agentic-session-cursor": (
        "SELECT COUNT(*), MAX(end_time) FROM agentic_sessions "
        "WHERE harness = 'cursor' AND probe_tag IS NULL"
    ),
    "claude-auto-memory": (
        "SELECT COUNT(*), MAX(md.updated_at) FROM memory_documents md "
        "WHERE md.state = 'active' AND md.repository != 'hippo/__hippo_probe__'"
    ),
}

# Map capture invariants to source_health keys for active alarm surfacing.
_INVARIANT_SOURCES: dict[str, str] = {
    "I-1": "shell",
    "I-2": "agentic-session-claude",
    "I-3": "claude-tool",
    "I-4": "browser",
    "I-11": "agentic-session-opencode",
    "I-13": "agentic-session-codex",
    "I-15": "agentic-session-cursor",
}


@dataclass(frozen=True)
class CoverageSnapshot:
    row_count: int
    max_event_ts: int | None


def health_key_for_source_kind(source_kind: str) -> str | None:
    return SOURCE_KIND_HEALTH.get(source_kind)


def _thresholds_ms(source_key: str) -> tuple[int, int]:
    return _THRESHOLDS_MS.get(source_key, (24 * _HOUR_MS, 7 * _DAY_MS))


def _load_coverage(conn: sqlite3.Connection, source_key: str) -> CoverageSnapshot:
    sql = _COVERAGE_SQL.get(source_key)
    if sql is None:
        return CoverageSnapshot(0, None)
    row = conn.execute(sql).fetchone()
    if row is None:
        return CoverageSnapshot(0, None)
    count, max_ts = row[0], row[1]
    return CoverageSnapshot(int(count or 0), int(max_ts) if max_ts else None)


def _load_health_row(conn: sqlite3.Connection, source_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT source, last_event_ts, last_success_ts, last_error_ts, last_error_msg,
               consecutive_failures, events_last_1h, events_last_24h,
               probe_ok, probe_lag_ms, probe_last_run_ts, last_heartbeat_ts, updated_at
        FROM source_health WHERE source = ?
        """,
        (source_key,),
    ).fetchone()
    if row is None:
        return None
    keys = (
        "source",
        "last_event_ts",
        "last_success_ts",
        "last_error_ts",
        "last_error_msg",
        "consecutive_failures",
        "events_last_1h",
        "events_last_24h",
        "probe_ok",
        "probe_lag_ms",
        "probe_last_run_ts",
        "last_heartbeat_ts",
        "updated_at",
    )
    return dict(zip(keys, row, strict=True))


def _load_active_alarms(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not table_exists(conn, "capture_alarms"):
        return []
    rows = conn.execute(
        """
        SELECT invariant_id, raised_at, details_json
        FROM capture_alarms
        WHERE acked_at IS NULL AND resolved_at IS NULL
        ORDER BY raised_at DESC
        LIMIT 20
        """
    ).fetchall()
    return [
        {"invariant_id": invariant_id, "raised_at": raised_at, "details": details_json}
        for invariant_id, raised_at, details_json in rows
    ]


def _alarms_for_source(
    all_alarms: Sequence[dict[str, Any]], source_key: str
) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for alarm in all_alarms:
        invariant_id = str(alarm.get("invariant_id", ""))
        mapped = _INVARIANT_SOURCES.get(invariant_id)
        if mapped is not None and mapped != source_key:
            continue
        if mapped is None and source_key not in str(alarm.get("details") or ""):
            continue
        matched.append(alarm)
    return matched


def _active_alarms_for_source(conn: sqlite3.Connection, source_key: str) -> list[dict[str, Any]]:
    return _alarms_for_source(_load_active_alarms(conn), source_key)


def classify_capture_status(
    *,
    source_key: str,
    health: dict[str, Any] | None,
    coverage: CoverageSnapshot,
    alarms: Sequence[dict[str, Any]],
    now_ms: int,
) -> str:
    """Return one of: fresh, stale, failing, suppressed_idle, expected_absent, unknown."""
    if coverage.row_count == 0 and health is None:
        return "expected_absent"

    if alarms:
        return "failing"

    if health is None:
        return "expected_absent" if coverage.row_count == 0 else "unknown"

    consecutive = int(health.get("consecutive_failures") or 0)
    probe_ok = health.get("probe_ok")
    events_24h = int(health.get("events_last_24h") or 0)
    last_error_ts = health.get("last_error_ts")

    if source_key in BURSTY_SOURCES and consecutive > POLLER_FAILURE_THRESHOLD:
        return "failing"

    if consecutive > 0 and last_error_ts and (now_ms - int(last_error_ts)) < _DAY_MS:
        return "failing"

    if probe_ok == 0 and health.get("probe_last_run_ts"):
        return "failing"

    if coverage.row_count == 0:
        return "expected_absent"

    ref_ts = health.get("last_event_ts") or coverage.max_event_ts
    if ref_ts is None:
        return "unknown"

    age_ms = max(0, now_ms - int(ref_ts))
    soft_ms, hard_ms = _thresholds_ms(source_key)

    if age_ms <= soft_ms:
        return "fresh"

    if events_24h == 0 and coverage.row_count > 0:
        if source_key in BURSTY_SOURCES or source_key == "browser":
            return "suppressed_idle"

    if age_ms > hard_ms:
        return "stale"

    return "stale"


def build_freshness_snapshot(
    conn: sqlite3.Connection,
    source_key: str,
    *,
    now_ms: int | None = None,
    active_alarms: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a capture-freshness dict for one ``source_health`` key."""
    now_ms = now_ms or int(time.time() * 1000)
    health = _load_health_row(conn, source_key)
    coverage = _load_coverage(conn, source_key)
    alarms = (
        _alarms_for_source(active_alarms, source_key)
        if active_alarms is not None
        else _active_alarms_for_source(conn, source_key)
    )
    status = classify_capture_status(
        source_key=source_key,
        health=health,
        coverage=coverage,
        alarms=alarms,
        now_ms=now_ms,
    )

    ref_ts = None
    if health:
        ref_ts = health.get("last_event_ts")
    if ref_ts is None:
        ref_ts = coverage.max_event_ts
    age_ms = (now_ms - int(ref_ts)) if ref_ts else None

    capture_health: dict[str, Any] | None = None
    if health:
        capture_health = {
            "last_event_ts": health.get("last_event_ts"),
            "last_heartbeat_ts": health.get("last_heartbeat_ts"),
            "probe_last_run_ts": health.get("probe_last_run_ts"),
            "probe_ok": health.get("probe_ok"),
            "probe_lag_ms": health.get("probe_lag_ms"),
            "consecutive_failures": health.get("consecutive_failures") or 0,
            "events_last_1h": health.get("events_last_1h") or 0,
            "events_last_24h": health.get("events_last_24h") or 0,
        }

    return {
        "source": source_key,
        "status": status,
        "present": health is not None,
        "age_ms": age_ms,
        "stale": status in {"stale", "failing"},
        "capture_health": capture_health,
        "coverage": {
            "row_count": coverage.row_count,
            "max_event_ts": coverage.max_event_ts,
        },
        "active_alarms": list(alarms),
    }


def freshness_for_source_keys(
    conn: sqlite3.Connection,
    source_keys: Sequence[str],
    *,
    now_ms: int | None = None,
) -> dict[str, dict[str, Any]]:
    if not table_exists(conn, "source_health"):
        return {}
    unique = sorted({k for k in source_keys if k})
    active_alarms = _load_active_alarms(conn)
    return {
        key: build_freshness_snapshot(conn, key, now_ms=now_ms, active_alarms=active_alarms)
        for key in unique
    }


def freshness_for_evidence_packets(
    conn: sqlite3.Connection,
    packets: Sequence[dict[str, Any]],
    *,
    now_ms: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Map ``source_health`` keys to freshness snapshots cited by evidence packets."""
    keys: list[str] = []
    for pkt in packets:
        kind = pkt.get("source_kind")
        if isinstance(kind, str):
            mapped = health_key_for_source_kind(kind)
            if mapped:
                keys.append(mapped)
    return freshness_for_source_keys(conn, keys, now_ms=now_ms)


def attach_freshness_to_packets(
    conn: sqlite3.Connection,
    packets: list[dict[str, Any]],
    *,
    now_ms: int | None = None,
) -> None:
    """Mutate evidence packet dicts in place with inline ``freshness`` metadata."""
    if not packets or not table_exists(conn, "source_health"):
        return
    snapshots = freshness_for_evidence_packets(conn, packets, now_ms=now_ms)
    for pkt in packets:
        kind = pkt.get("source_kind")
        if not isinstance(kind, str):
            continue
        key = health_key_for_source_kind(kind)
        if key and key in snapshots:
            pkt["freshness"] = snapshots[key]


def attach_freshness_to_results(
    conn: sqlite3.Connection,
    results: Sequence[Any],
    *,
    now_ms: int | None = None,
) -> None:
    """Attach inline freshness to every evidence packet on retrieval results."""
    all_packets: list[dict[str, Any]] = []
    for result in results:
        packets = getattr(result, "evidence", None)
        if packets:
            all_packets.extend(packets)
    attach_freshness_to_packets(conn, all_packets, now_ms=now_ms)


def aggregate_freshness_from_packets(
    packets: Sequence[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Build a source-keyed freshness map from inline packet metadata."""
    out: dict[str, dict[str, Any]] = {}
    for pkt in packets:
        fresh = pkt.get("freshness")
        if not isinstance(fresh, dict):
            continue
        key = fresh.get("source")
        if isinstance(key, str) and key:
            out[key] = fresh
    return out
