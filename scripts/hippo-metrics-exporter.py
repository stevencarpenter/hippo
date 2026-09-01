#!/usr/bin/env python3
"""hippo-metrics-exporter — knowledge-health metrics bridge.

Read-only bridge between hippo.db / the brain server and Prometheus. Emits
the knowledge-base metrics that the OTel capture pipeline does not cover
(graveyard, dead-project contamination, project identity fragmentation,
decision yield, hook hygiene) plus the recall probe: a synthetic /ask
round-trip that treats the retrieval path as a production dependency.
Capture has watchdogs and probes; this gives recall the same treatment.

Endpoints:
  /metrics       Prometheus text exposition (scrape target, port 9835)
  /metrics.json  Same samples as JSON (for Infinity/JSON-API consumers)
  /healthz       Liveness

Design rules:
  - Stdlib only (no deps; runs under /usr/bin/python3 for launchd).
  - The database is opened read-only (`mode=ro` + `PRAGMA query_only=ON`).
    This exporter NEVER writes to hippo.db.
  - Rendering is re-entrant. Every scrape builds its own `Registry`; there is
    no shared per-request state, so concurrent /metrics and /metrics.json
    requests cannot interleave samples into each other's response.
  - Cumulative counters (`*_total`) live in a process-lifetime store
    (`_COUNTERS`) and are emitted on every scrape once observed. They are
    monotonic for the life of the process, so `increase()`/`rate()` work.
    Anything that is a point-in-time reading is a gauge and is NOT named
    `_total` (see CLAUDE.md "Observability / OTel" for the naming rule).
  - Database-derived families are computed at most once per `HIPPO_DB_TTL`
    seconds and shared between scrapes; the full-table scans over `events`
    are the expensive part and grow with the corpus.
  - Every metric family is computed inside try/except; a family that fails
    increments hippo_kb_collector_errors_total{name=...} instead of killing
    the scrape. Silence is never acceptable: failures are visible.
  - Snowball metrics (epitaphs, push taps, retrieval_events, bets,
    contradictions, node status) are emitted ONLY when their backing table
    exists, so dashboards light up as features ship instead of showing fake
    zeros.

The metric registry is `METRIC_NAMES` / `FUTURE_METRIC_NAMES` below. It is
data, not documentation: `brain/tests/test_otel_dashboards.py` imports this
module, renders against a synthetic database, and asserts that every declared
non-snowball name is actually emitted. A name declared but never passed to
`gauge()`/`counter()` fails that test.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_DIR = Path(
    os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")
) / "hippo"
DB_PATH = Path(os.environ.get("HIPPO_DB", DATA_DIR / "hippo.db"))
PORT = int(os.environ.get("HIPPO_METRICS_PORT", "9835"))
BRAIN_URL = os.environ.get("HIPPO_BRAIN_URL", "http://127.0.0.1:9175").rstrip("/")
DB_TTL_S = float(os.environ.get("HIPPO_DB_TTL", "15"))
PROBE_TTL_S = float(os.environ.get("HIPPO_PROBE_TTL", "120"))
PROBE_TIMEOUT_S = float(os.environ.get("HIPPO_PROBE_TIMEOUT", "45"))
CANARY_FILE = Path(os.environ.get("HIPPO_CANARY_FILE", DATA_DIR / "canary_drill.json"))

# Golden questions for the recall probe. One per question-class the panel
# identified: death-reason recall, decision recall, freshness sanity.
GOLDEN_QUESTIONS = [
    "Why did I abandon the sluice-mysql prototype?",
    "What decisions did I record about hippo's capture reliability stack?",
    "What did I last work on in the hippo project?",
]

# Windows (days) for graveyard computations.
WINDOWS = (30, 60, 90)
MIN_EVENTS_FOR_PROJECT = 10

# ---------------------------------------------------------------------------
# Metric name registry (single source of truth).
#
# METRIC_NAMES: emitted on every scrape against a healthy database. The
# source-backed test renders the exporter and asserts each of these appears.
# FUTURE_METRIC_NAMES: snowball metrics, emitted only when the backing table
# exists; the test asserts they appear once those tables are created.
#
# Naming: `_total` is reserved for genuinely cumulative counters. Point-in-time
# readings are gauges with bare names.
# ---------------------------------------------------------------------------

METRIC_NAMES = [
    "hippo_kb_up",
    "hippo_kb_scrape_duration_milliseconds",
    # --- corpus ---
    "hippo_kb_events",
    "hippo_kb_events_24h",
    "hippo_kb_last_event_age_milliseconds",
    "hippo_kb_stdout_nonempty",
    "hippo_kb_stderr_nonempty",
    "hippo_kb_knowledge_nodes",
    "hippo_kb_agentic_sessions",
    "hippo_kb_agentic_messages",
    "hippo_kb_db_size_bytes",
    # --- capture-side (from SQLite mirrors of the OTel view) ---
    "hippo_kb_capture_alarms_active",
    "hippo_kb_capture_source_ok",
    "hippo_kb_capture_source_last_event_age_milliseconds",
    # --- graveyard ---
    "hippo_kb_dead_projects",
    "hippo_kb_stranded_hours",
    "hippo_kb_dead_project_node_ratio",
    # --- identity ---
    "hippo_kb_project_identifiers",
    "hippo_kb_project_fragmentation_ratio",
    # --- decisions ---
    "hippo_kb_design_decisions",
    # --- hygiene / redaction canary ---
    "hippo_kb_env_secretish_keys",
]

# Emitted once the recall probe has completed at least one pass, and once a
# canary drill state file exists. Not part of the always-on set.
DEFERRED_METRIC_NAMES = [
    "hippo_kb_recall_up",
    "hippo_kb_recall_ok",
    "hippo_kb_recall_latency_milliseconds",
    "hippo_kb_recall_probe_timestamp_seconds",
    "hippo_kb_canary_found",
    "hippo_kb_canary_drill_timestamp_seconds",
]

# Cumulative counters. Emitted from the process-lifetime counter store once
# the corresponding event has been observed at least once.
COUNTER_METRIC_NAMES = [
    "hippo_kb_collector_errors_total",
    "hippo_kb_recall_failures_total",
]

# Snowball metrics: emitted only when the backing feature/table exists.
FUTURE_METRIC_NAMES = [
    "hippo_kb_retrieval_events",
    "hippo_kb_epitaphs",
    "hippo_kb_push_fires",
    "hippo_kb_nodes_by_status",
    "hippo_kb_contradictions_open",
    "hippo_kb_bets",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_ro(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path or DB_PATH}?mode=ro", uri=True, timeout=5)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _first_col(cols: set[str], candidates: list[str]) -> str | None:
    for c in candidates:
        if c in cols:
            return c
    return None


def base_name(p: object) -> str:
    """Last path segment, lowercased, leading dot stripped ('' if empty)."""
    if not p:
        return ""
    seg = str(p).strip().rstrip("/").split("/")[-1].strip().lower()
    if seg.startswith("."):
        seg = seg[1:]
    return seg


def canon(name: str) -> str:
    """Normalized identity: collapse dot/-/_/space variants."""
    return re.sub(r"[-._\s]+", "", name.lower())


_SECRET_PATTERNS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "API_KEY",
    "_KEY",
    "AWS_",
    "GITHUB",
    "BEARER",
    "PAT_",
)


def looks_secretish(key: str) -> bool:
    ku = key.upper()
    if ku.endswith("PATH") or ku == "PATH":
        return False  # %PAT% false positive (PATH contains the substring PAT)
    return any(p in ku for p in _SECRET_PATTERNS)


# ---------------------------------------------------------------------------
# Process-lifetime cumulative counters.
#
# These MUST survive across scrapes and MUST NOT be reset, or `increase()` /
# `rate()` over them is identically zero and every alert built on them is
# structurally unfireable. A process restart resets them to zero, which is the
# normal Prometheus counter-reset case and is handled by `increase()`.
# ---------------------------------------------------------------------------

_COUNTER_LOCK = threading.Lock()
_COUNTERS: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
_COUNTER_HELP: dict[str, str] = {}


def bump(name: str, labels: dict[str, str] | None = None, by: float = 1.0,
         help: str = "") -> float:
    """Increment a process-lifetime counter series and return its new value."""
    key = (name, tuple(sorted((labels or {}).items())))
    with _COUNTER_LOCK:
        if help:
            _COUNTER_HELP.setdefault(name, help)
        _COUNTERS[key] = _COUNTERS.get(key, 0.0) + by
        return _COUNTERS[key]


def counter_series() -> list[tuple[str, dict[str, str], float]]:
    with _COUNTER_LOCK:
        return [(name, dict(lbls), val) for (name, lbls), val in _COUNTERS.items()]


def reset_counters_for_test() -> None:
    """Test hook: clear the counter store. Never called in production."""
    with _COUNTER_LOCK:
        _COUNTERS.clear()


# ---------------------------------------------------------------------------
# Registry — one per scrape, so concurrent renders cannot interleave.
# ---------------------------------------------------------------------------


class Registry:
    """Per-scrape sample collector.

    Deliberately NOT module-level state: `ThreadingHTTPServer` serves /metrics
    and /metrics.json concurrently, and a shared sample list produced responses
    containing every in-flight render's samples (duplicate series, which
    Prometheus rejects outright).
    """

    def __init__(self) -> None:
        self.help: dict[str, str] = {}
        self.type: dict[str, str] = {}
        self.samples: list[dict] = []

    def _declare(self, name: str, text: str, typ: str) -> None:
        if text:
            self.help.setdefault(name, text)
        self.type.setdefault(name, typ)

    def gauge(self, name: str, value: float, labels: dict[str, str] | None = None,
              help: str = "") -> None:
        self._declare(name, help, "gauge")
        self.samples.append({"name": name, "labels": labels or {}, "value": float(value)})

    def counter(self, name: str, value: float, labels: dict[str, str] | None = None,
                help: str = "") -> None:
        self._declare(name, help, "counter")
        self.samples.append({"name": name, "labels": labels or {}, "value": float(value)})

    def extend(self, other: Registry) -> None:
        for k, v in other.help.items():
            self.help.setdefault(k, v)
        for k, v in other.type.items():
            self.type.setdefault(k, v)
        self.samples.extend(other.samples)

    def family(self, name: str, fn) -> None:
        """Run a metric family; record errors instead of failing the scrape."""
        before = len(self.samples)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — a family failure must not kill the scrape
            del self.samples[before:]
            bump(
                "hippo_kb_collector_errors_total",
                {"name": name},
                help="Cumulative metric-family computation failures by family name, "
                     "since exporter start.",
            )
            print(f"[exporter] family {name} failed: {exc}", flush=True)


# ---------------------------------------------------------------------------
# Metric families — database-derived
# ---------------------------------------------------------------------------


def collect_db(reg: Registry, now_ms: int, db_path: Path | None = None) -> None:
    path = db_path or DB_PATH
    try:
        size = path.stat().st_size
    except OSError as exc:
        # TOCTOU unlink or permission error — surface as collector error, don't
        # raise before the outer db_snapshot try/except (and handle direct test
        # callers that bypass db_snapshot's exists() guard).
        bump(
            "hippo_kb_collector_errors_total",
            {"name": "db_size"},
            help="Cumulative metric-family computation failures by family name, "
                 "since exporter start.",
        )
        print(f"[exporter] db size stat failed for {path}: {exc}", flush=True)
        # Continue without the size gauge; still try to open the DB.
        size = None
    if size is not None:
        reg.gauge(
            "hippo_kb_db_size_bytes",
            size,
            help="On-disk size of hippo.db (main database file, excluding WAL).",
        )
    conn = _open_ro(path)
    try:
        reg.family("events", lambda: _f_events(reg, conn, now_ms))
        reg.family("nodes", lambda: _f_nodes(reg, conn))
        reg.family("sessions", lambda: _f_sessions(reg, conn))
        reg.family("alarms", lambda: _f_alarms(reg, conn))
        reg.family("sources", lambda: _f_sources(reg, conn, now_ms))
        # _project_stats is the most expensive query in the exporter (two full
        # GROUP BY passes over `events`). Compute it once and share it between
        # the graveyard and identity families.
        stats: dict[str, dict] = {}
        reg.family(
            "project_stats",
            lambda: stats.update(_project_stats(conn, _session_project_names(conn))),
        )
        reg.family("graveyard", lambda: _f_graveyard(reg, conn, now_ms, stats))
        reg.family("identity", lambda: _f_identity(reg, stats))
        reg.family("decisions", lambda: _f_decisions(reg, conn))
        reg.family("envsnap", lambda: _f_envsnap(reg, conn))
        reg.family("snowball", lambda: _f_snowball(reg, conn))
    finally:
        conn.close()


def _f_events(reg: Registry, conn: sqlite3.Connection, now_ms: int) -> None:
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    reg.gauge("hippo_kb_events", total, help="Total captured events (all time).")
    e24 = conn.execute(
        "SELECT COUNT(*) FROM events WHERE timestamp >= ?", (now_ms - 24 * 3600 * 1000,)
    ).fetchone()[0]
    reg.gauge("hippo_kb_events_24h", e24, help="Events captured in the last 24 hours.")
    row = conn.execute("SELECT MAX(timestamp) FROM events").fetchone()
    if row and row[0]:
        reg.gauge("hippo_kb_last_event_age_milliseconds", now_ms - row[0],
                  help="Age of the newest event (capture staleness).")
    so = conn.execute(
        "SELECT COUNT(*) FROM events WHERE stdout IS NOT NULL AND stdout != ''"
    ).fetchone()[0]
    se = conn.execute(
        "SELECT COUNT(*) FROM events WHERE stderr IS NOT NULL AND stderr != ''"
    ).fetchone()[0]
    reg.gauge("hippo_kb_stdout_nonempty", so, help="Events with non-empty stdout.")
    reg.gauge("hippo_kb_stderr_nonempty", se,
              help="Events with non-empty stderr. Persistently 0 = shell hook captures "
                   "combined stdout only (schema-ready, hook-starved).")


def _f_nodes(reg: Registry, conn: sqlite3.Connection) -> None:
    n = conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0]
    reg.gauge("hippo_kb_knowledge_nodes", n, help="Total knowledge nodes.")


def _f_sessions(reg: Registry, conn: sqlite3.Connection) -> None:
    s = conn.execute("SELECT COUNT(*) FROM agentic_sessions").fetchone()[0]
    reg.gauge("hippo_kb_agentic_sessions", s, help="Total agentic sessions ingested.")
    # There is no per-message table today; `message_count` on the session row is
    # the authoritative count. Fall back to a message table if one ever lands.
    for t in ("agentic_session_messages", "session_messages"):
        if _table_exists(conn, t):
            m = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            reg.gauge("hippo_kb_agentic_messages", m,
                      help="Total agentic session messages.")
            return
    if "message_count" in _cols(conn, "agentic_sessions"):
        m = conn.execute(
            "SELECT COALESCE(SUM(message_count), 0) FROM agentic_sessions"
        ).fetchone()[0]
        reg.gauge("hippo_kb_agentic_messages", m,
                  help="Total agentic session messages (summed from "
                       "agentic_sessions.message_count).")


def _f_alarms(reg: Registry, conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "capture_alarms"):
        return
    cols = _cols(conn, "capture_alarms")
    if "resolved_at" in cols:
        active = conn.execute(
            "SELECT COUNT(*) FROM capture_alarms WHERE resolved_at IS NULL"
        ).fetchone()[0]
    elif "resolved" in cols:
        active = conn.execute("SELECT COUNT(*) FROM capture_alarms WHERE resolved = 0").fetchone()[0]
    elif "active" in cols:
        active = conn.execute("SELECT COUNT(*) FROM capture_alarms WHERE active = 1").fetchone()[0]
    elif "cleared_at" in cols:
        active = conn.execute("SELECT COUNT(*) FROM capture_alarms WHERE cleared_at IS NULL").fetchone()[0]
    else:
        return
    reg.gauge("hippo_kb_capture_alarms_active", active,
              help="Capture alarms currently unresolved.")


def _f_sources(reg: Registry, conn: sqlite3.Connection, now_ms: int) -> None:
    if not _table_exists(conn, "source_health"):
        return
    cols = _cols(conn, "source_health")
    scol = _first_col(cols, ["source", "name"])
    if not scol:
        return
    okcol = _first_col(cols, ["probe_ok"])
    lagcol = _first_col(cols, ["last_event_ts", "last_event_at", "max_event_ts"])
    for row in conn.execute(f"SELECT {scol}{', ' + okcol if okcol else ''}"
                            f"{', ' + lagcol if lagcol else ''} FROM source_health"):
        src = row[0]
        idx = 1
        if okcol:
            reg.gauge("hippo_kb_capture_source_ok", 1 if row[idx] else 0, {"source": src},
                      help="Per-source probe health (1 = ok).")
            idx += 1
        if lagcol and row[idx]:
            reg.gauge("hippo_kb_capture_source_last_event_age_milliseconds", now_ms - row[idx],
                      {"source": src}, help="Per-source capture staleness.")


def _session_project_names(conn: sqlite3.Connection) -> set[str]:
    """Project basenames referenced by agentic sessions (defines 'is a project')."""
    if not _table_exists(conn, "agentic_sessions"):
        return set()
    pcol = _first_col(_cols(conn, "agentic_sessions"),
                      ["project_dir", "project", "cwd", "directory", "path"])
    if not pcol:
        return set()
    return {b for (p,) in conn.execute(f"SELECT DISTINCT {pcol} FROM agentic_sessions")
            if (b := base_name(p))}


def _project_stats(conn: sqlite3.Connection,
                   session_projects: set[str] | None = None) -> dict[str, dict]:
    """basename -> {n, mx, d} for PROJECT directories only.

    A directory counts as a project if it is a git repo (events.git_repo) or an
    agentic-session project dir; shell cwd-only directories (~/bin, /tmp, ...)
    are excluded so the graveyard tracks projects, not every directory.
    """
    stats: dict[str, dict] = {}

    def acc(sql: str, allowed: set[str] | None = None) -> None:
        for repo, mx, n, d in conn.execute(sql):
            b = base_name(repo)
            if not b or (allowed is not None and b not in allowed):
                continue
            e = stats.setdefault(b, {"n": 0, "mx": 0, "d": 0})
            e["n"] += n
            e["mx"] = max(e["mx"], mx or 0)
            e["d"] += d or 0

    # Git repos are projects by definition: accept unconditionally.
    acc("SELECT git_repo, MAX(timestamp), COUNT(*), SUM(duration_ms) FROM events "
        "WHERE git_repo IS NOT NULL AND git_repo != '' GROUP BY git_repo")
    # cwd-derived stats only for directories already known to be projects
    # (an agentic-session project dir or an already-accepted git-repo basename);
    # shell cwd-only directories (~/bin, /tmp, ...) are excluded.
    allowed = (set(session_projects) | set(stats)) if session_projects is not None else None
    acc("SELECT cwd, MAX(timestamp), COUNT(*), SUM(duration_ms) FROM events "
        "WHERE cwd IS NOT NULL AND cwd != '' GROUP BY cwd", allowed)
    return stats


def _dead_sets(stats: dict[str, dict], now_ms: int) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    for w in WINDOWS:
        cutoff = now_ms - w * 86400000
        out[w] = {b for b, e in stats.items()
                  if e["n"] >= MIN_EVENTS_FOR_PROJECT and e["mx"] < cutoff}
    return out


def _f_graveyard(reg: Registry, conn: sqlite3.Connection, now_ms: int,
                 stats: dict[str, dict]) -> None:
    dead = _dead_sets(stats, now_ms)
    for w in WINDOWS:
        reg.gauge("hippo_kb_dead_projects", len(dead[w]), {"window": f"{w}d"},
                  help="Projects with >=10 events and no activity inside the window.")
        stranded = sum(stats[b]["d"] for b in dead[w]) / 3.6e6
        reg.gauge("hippo_kb_stranded_hours", stranded, {"window": f"{w}d"},
                  help="All-time shell hours on projects dead within the window "
                       "(wasted-effort estimate).")
    # Dead-project contamination of knowledge nodes (30d window).
    dead30 = dead[30]
    ratio_help = ("Fraction of linked knowledge nodes sourced exclusively from "
                  "dead (30d) projects.")
    link_table = next((t for t in ("knowledge_node_agentic_sessions",
                                   "knowledge_node_sessions", "kn_agentic_sessions")
                       if _table_exists(conn, t)), None)
    if not link_table or not dead30:
        reg.gauge("hippo_kb_dead_project_node_ratio", 0.0, help=ratio_help)
        return
    sess_cols = _cols(conn, "agentic_sessions")
    pcol = _first_col(sess_cols, ["project_dir", "project", "cwd", "directory", "path"])
    lcols = _cols(conn, link_table)
    lnode = _first_col(lcols, ["knowledge_node_id", "node_id"])
    lsess = _first_col(lcols, ["agentic_session_id", "session_id"])
    if not (pcol and lnode and lsess):
        reg.gauge("hippo_kb_dead_project_node_ratio", 0.0, help=ratio_help)
        return
    node_projects: dict[int, set[str]] = {}
    for nid, proj in conn.execute(
        f"SELECT k.{lnode}, s.{pcol} FROM {link_table} k "
        f"JOIN agentic_sessions s ON s.id = k.{lsess}"
    ):
        b = base_name(proj)
        if b:
            node_projects.setdefault(nid, set()).add(b)
    if not node_projects:
        reg.gauge("hippo_kb_dead_project_node_ratio", 0.0, help=ratio_help)
        return
    dead_only = sum(1 for ps in node_projects.values() if ps and ps <= dead30)
    reg.gauge("hippo_kb_dead_project_node_ratio", dead_only / len(node_projects),
              help=ratio_help)


def _f_identity(reg: Registry, stats: dict[str, dict]) -> None:
    raw = len(stats)
    norm = len({canon(b) for b in stats})
    reg.gauge("hippo_kb_project_identifiers", raw, {"kind": "raw"},
              help="Distinct project basenames seen in events (pre-normalization).")
    reg.gauge("hippo_kb_project_identifiers", norm, {"kind": "normalized"},
              help="Distinct project basenames after dot/separator normalization.")
    reg.gauge("hippo_kb_project_fragmentation_ratio", (raw / norm) if norm else 1.0,
              help="raw/normalized project identifiers (>1 means alias fragmentation).")


def _f_decisions(reg: Registry, conn: sqlite3.Connection) -> None:
    cols = _cols(conn, "knowledge_nodes")
    dcol = _first_col(cols, ["design_decisions"])
    if dcol:
        n = conn.execute(
            f"SELECT COUNT(*) FROM knowledge_nodes WHERE {dcol} IS NOT NULL "
            f"AND TRIM({dcol}) NOT IN ('', '[]', 'null')"
        ).fetchone()[0]
    elif "tags" in cols:
        n = conn.execute(
            "SELECT COUNT(*) FROM knowledge_nodes WHERE tags LIKE '%decision%'"
        ).fetchone()[0]
    else:
        return
    reg.gauge("hippo_kb_design_decisions", n,
              help="Knowledge nodes carrying structured design decisions.")


def _env_snapshot_keys(conn: sqlite3.Connection) -> set[str]:
    """Distinct env-variable NAMES across all env snapshots.

    `env_snapshots` stores one JSON object per snapshot in `env_json` — there is
    no per-key column. Prefer SQLite's JSON1 `json_each` (one pass, no Python
    parsing); fall back to parsing in Python if JSON1 is unavailable.
    """
    try:
        return {
            k for (k,) in conn.execute(
                "SELECT DISTINCT j.key FROM env_snapshots e, json_each(e.env_json) j"
            ) if k
        }
    except sqlite3.OperationalError:
        keys: set[str] = set()
        for (blob,) in conn.execute("SELECT env_json FROM env_snapshots"):
            try:
                doc = json.loads(blob)
            except (TypeError, ValueError):
                continue
            if isinstance(doc, dict):
                keys.update(doc)
        return keys


def _f_envsnap(reg: Registry, conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "env_snapshots"):
        return
    if "env_json" not in _cols(conn, "env_snapshots"):
        return
    bad = sorted(k for k in _env_snapshot_keys(conn) if looks_secretish(k))
    reg.gauge("hippo_kb_env_secretish_keys", len(bad),
              help="Distinct env-snapshot key NAMES matching secret patterns (key names "
                   "only; values are never read). >0 means a redaction/allow-list "
                   "regression.")
    for k in bad[:10]:
        print(f"[exporter] secretish env key present: {k}", flush=True)


def _f_snowball(reg: Registry, conn: sqlite3.Connection) -> None:
    """Emit future-feature metrics only when their tables exist (snowball)."""
    if _table_exists(conn, "retrieval_events"):
        n = conn.execute("SELECT COUNT(*) FROM retrieval_events").fetchone()[0]
        reg.gauge("hippo_kb_retrieval_events", n, help="Retrieval feedback events logged.")
    if _table_exists(conn, "epitaphs"):
        cols = _cols(conn, "epitaphs")
        ccol = _first_col(cols, ["confirmed_by", "confirmed"])
        total = conn.execute("SELECT COUNT(*) FROM epitaphs").fetchone()[0]
        if ccol == "confirmed_by":
            yes = conn.execute(
                "SELECT COUNT(*) FROM epitaphs WHERE confirmed_by IS NOT NULL AND confirmed_by != ''"
            ).fetchone()[0]
            reg.gauge("hippo_kb_epitaphs", yes, {"confirmed": "yes"},
                      help="Epitaphs (project death records) by confirmation state.")
            reg.gauge("hippo_kb_epitaphs", total - yes, {"confirmed": "no"})
        else:
            reg.gauge("hippo_kb_epitaphs", total, {"confirmed": "unknown"},
                      help="Epitaphs (project death records) by confirmation state.")
    if _table_exists(conn, "push_trials"):
        cols = _cols(conn, "push_trials")
        ucol = _first_col(cols, ["tapped_useful"])
        ncol = _first_col(cols, ["tapped_noise"])
        total = conn.execute("SELECT COUNT(*) FROM push_trials").fetchone()[0]
        if ucol and ncol:
            u = conn.execute(f"SELECT COUNT(*) FROM push_trials WHERE {ucol} = 1").fetchone()[0]
            nz = conn.execute(f"SELECT COUNT(*) FROM push_trials WHERE {ncol} = 1").fetchone()[0]
            reg.gauge("hippo_kb_push_fires", u, {"tapped": "useful"},
                      help="Push interventions by user tap verdict.")
            reg.gauge("hippo_kb_push_fires", nz, {"tapped": "noise"})
            reg.gauge("hippo_kb_push_fires", max(0, total - u - nz), {"tapped": "none"})
        else:
            reg.gauge("hippo_kb_push_fires", total, {"tapped": "none"},
                      help="Push interventions by user tap verdict.")
    if _table_exists(conn, "nodes") and "status" in _cols(conn, "nodes"):
        for status, n in conn.execute("SELECT status, COUNT(*) FROM nodes GROUP BY status"):
            reg.gauge("hippo_kb_nodes_by_status", n, {"status": str(status)},
                      help="Write-API nodes by lifecycle status.")
    if _table_exists(conn, "contradictions"):
        cols = _cols(conn, "contradictions")
        rcol = _first_col(cols, ["resolved_by", "resolved_at"])
        where = f"WHERE {rcol} IS NULL" if rcol else ""
        n = conn.execute(f"SELECT COUNT(*) FROM contradictions {where}").fetchone()[0]
        reg.gauge("hippo_kb_contradictions_open", n,
                  help="Unresolved cross-writer contradictions.")
    if _table_exists(conn, "bets"):
        cols = _cols(conn, "bets")
        rcol = _first_col(cols, ["resolved_at"])
        total = conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
        resolved = conn.execute(
            f"SELECT COUNT(*) FROM bets WHERE {rcol} IS NOT NULL"
        ).fetchone()[0] if rcol else 0
        reg.gauge("hippo_kb_bets", total - resolved, {"state": "open"},
                  help="Bets (declared hypotheses with kill criteria) by state.")
        reg.gauge("hippo_kb_bets", resolved, {"state": "resolved"})


# ---------------------------------------------------------------------------
# Database snapshot cache
#
# The database families do several full scans over `events`; at ~67k events that
# is ~190ms and it grows with the corpus. Cache the rendered snapshot for
# HIPPO_DB_TTL seconds so a scrape burst (Prometheus + a /metrics.json consumer)
# does not multiply that cost. The lock also serializes the scans.
# ---------------------------------------------------------------------------

_db_cache_lock = threading.Lock()
_db_cache: dict = {"ts": 0.0, "reg": None}


def db_snapshot(now_ms: int) -> Registry | None:
    if not DB_PATH.exists():
        bump("hippo_kb_collector_errors_total", {"name": "db_missing"},
             help="Cumulative metric-family computation failures by family name, "
                  "since exporter start.")
        return None
    with _db_cache_lock:
        now = time.time()
        cached = _db_cache["reg"]
        if cached is not None and now - _db_cache["ts"] < DB_TTL_S:
            return cached
        reg = Registry()
        try:
            collect_db(reg, now_ms)
        except Exception as exc:  # noqa: BLE001 — a DB-level failure must not kill the scrape
            bump("hippo_kb_collector_errors_total", {"name": "db"},
                 help="Cumulative metric-family computation failures by family name, "
                      "since exporter start.")
            print(f"[exporter] db snapshot failed: {exc}", flush=True)
            return cached
        _db_cache.update({"ts": now, "reg": reg})
        return reg


def reset_db_cache_for_test() -> None:
    """Test hook: drop the cached snapshot. Never called in production."""
    with _db_cache_lock:
        _db_cache.update({"ts": 0.0, "reg": None})


# ---------------------------------------------------------------------------
# Recall probe (/ask round-trip)
# ---------------------------------------------------------------------------

_probe_lock = threading.Lock()
probe_state: dict = {"ts": 0.0, "samples": [], "all_ok": False}


def run_probe() -> None:
    now = time.time()
    if now - probe_state["ts"] < PROBE_TTL_S and probe_state["samples"]:
        return
    if not _probe_lock.acquire(blocking=False):
        return  # a probe is already in flight; serve cached state
    try:
        _run_probe_inner()
    finally:
        _probe_lock.release()


def _ask_once(question: str) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(
            BRAIN_URL + "/ask",
            data=json.dumps({"question": question, "limit": 5}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
            body = json.loads(resp.read())
        ok = isinstance(body, dict) and len(str(body.get("answer", "")).strip()) > 0
        return ok, "" if ok else "empty_answer"
    except urllib.error.HTTPError as e:
        return False, f"http_error_{e.code}"
    except urllib.error.URLError as e:
        r = getattr(e, "reason", None)
        if isinstance(r, ConnectionRefusedError):
            return False, "brain_down"
        if isinstance(r, (socket.timeout, TimeoutError)):
            return False, "llm_timeout"
        return False, "brain_error"
    except (socket.timeout, TimeoutError):
        return False, "llm_timeout"
    except Exception as e:  # noqa: BLE001
        return False, f"error_{type(e).__name__}"


def _run_probe_inner() -> None:
    now = time.time()
    samples: list[dict] = []
    all_ok = True
    for q in GOLDEN_QUESTIONS:
        t0 = time.monotonic()
        ok, reason = _ask_once(q)
        ms = (time.monotonic() - t0) * 1000
        if not ok:
            all_ok = False
            # One increment per failed probe RUN, not per scrape: this is the
            # series the recall-failure-burst alert integrates with increase().
            bump("hippo_kb_recall_failures_total",
                 {"question": q[:48], "reason": reason},
                 help="Cumulative recall probe failures by question and reason class, "
                      "since exporter start.")
            print(f"[exporter] recall probe failed ({reason}): {q}", flush=True)
        samples.append({"question": q, "ok": ok, "reason": reason, "ms": ms})
    probe_state.update({"ts": now, "samples": samples, "all_ok": all_ok})


def collect_probe(reg: Registry) -> None:
    """Serve cached probe state immediately; refresh in a background thread
    when stale so the /metrics scrape never blocks on the LLM round-trip.

    Burst scrapes (Prometheus + /metrics.json) may call this concurrently;
    the try-acquire/release + daemon thread pattern ensures at most one probe
    is in flight — `run_probe` re-checks the TTL and does its own
    `tryAcquire` on `_probe_lock`, so concurrent scrapes don't queue burst
    threads.
    """
    now = time.time()
    stale = now - probe_state["ts"] >= PROBE_TTL_S
    if stale and _probe_lock.acquire(blocking=False):
        _probe_lock.release()
        threading.Thread(target=run_probe, daemon=True).start()
    st = probe_state
    if not st.get("samples"):
        return
    reg.gauge("hippo_kb_recall_up", 1.0 if st.get("all_ok") else 0.0,
              help="1 if every golden question round-tripped /ask with a non-empty answer.")
    for s in st["samples"]:
        lbl = {"question": s["question"][:48]}
        reg.gauge("hippo_kb_recall_ok", 1.0 if s["ok"] else 0.0, lbl,
                  help="Per-golden-question recall probe result.")
        if s["ok"]:
            # Latency is emitted only for successful round-trips: a failed probe
            # (brain_down = connection refused) records millisecond-scale "latency"
            # that would drag the recall-slow SLO average down and mask a genuinely
            # slow-when-it-succeeds inference server.
            reg.gauge("hippo_kb_recall_latency_milliseconds", s["ms"], lbl,
                      help="Per-golden-question /ask round-trip latency (successful "
                           "probes only).")
    reg.gauge("hippo_kb_recall_probe_timestamp_seconds", st["ts"],
              help="Unix time of the last recall probe run.")


# ---------------------------------------------------------------------------
# Canary leak drill state (optional; written by the drill job, read here)
# ---------------------------------------------------------------------------


def collect_canary(reg: Registry) -> None:
    if not CANARY_FILE.exists():
        return
    try:
        doc = json.loads(CANARY_FILE.read_text())
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        bump(
            "hippo_kb_collector_errors_total",
            {"name": "canary"},
            help="Cumulative metric-family computation failures by family name, "
                 "since exporter start.",
        )
        print(f"[exporter] canary read failed (torn write?): {exc}", flush=True)
        return
    stores = doc.get("stores")
    if not isinstance(stores, dict):
        # Malformed drill output: record the failure rather than emitting a
        # scrape with no canary samples and no signal.
        bump(
            "hippo_kb_collector_errors_total",
            {"name": "canary"},
            help="Cumulative metric-family computation failures by family name, "
                 "since exporter start.",
        )
        print(f"[exporter] canary file has non-dict 'stores': {type(stores).__name__}",
              flush=True)
        return
    for store, found in stores.items():
        reg.gauge("hippo_kb_canary_found", 1.0 if found else 0.0, {"store": store},
                  help="Canary secrets found per store by the last canary leak drill "
                       "(>0 anywhere = leak).")
    ts = doc.get("timestamp")
    if ts:
        try:
            reg.gauge("hippo_kb_canary_drill_timestamp_seconds", float(ts),
                      help="Unix time of the last canary drill.")
        except (TypeError, ValueError):
            pass


# ---------------------------------------------------------------------------
# Exposition
# ---------------------------------------------------------------------------


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt(v: float) -> str:
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    # Shortest-round-trip float exposition (repr): full precision for gauges
    # like recall latency and stranded hours, and small increments survive.
    return repr(v)


def build_registry() -> Registry:
    """Collect one complete scrape into a fresh Registry."""
    t0 = time.monotonic()
    now_ms = int(time.time() * 1000)
    reg = Registry()
    reg.gauge("hippo_kb_up", 1.0, help="Exporter alive.")
    snap = db_snapshot(now_ms)
    if snap is not None:
        reg.extend(snap)
    reg.family("probe", lambda: collect_probe(reg))
    reg.family("canary", lambda: collect_canary(reg))
    # Cumulative counters are emitted last so that failures recorded during
    # THIS scrape are visible in it.
    for name, labels, value in counter_series():
        reg.counter(name, value, labels, help=_COUNTER_HELP.get(name, ""))
    reg.gauge("hippo_kb_scrape_duration_milliseconds", (time.monotonic() - t0) * 1000,
              help="Time the last scrape took to compute (excludes the async recall probe).")
    return reg


def render_prometheus(reg: Registry) -> bytes:
    lines: list[str] = []
    by_name: dict[str, list[dict]] = {}
    for s in reg.samples:
        by_name.setdefault(s["name"], []).append(s)
    for name in sorted(by_name):
        if name in reg.help:
            lines.append(f"# HELP {name} {_esc(reg.help[name])}")
        lines.append(f"# TYPE {name} {reg.type.get(name, 'gauge')}")
        for s in by_name[name]:
            if s["labels"]:
                lbl = ",".join(f'{k}="{_esc(v)}"' for k, v in sorted(s["labels"].items()))
                lines.append(f"{name}{{{lbl}}} {_fmt(s['value'])}")
            else:
                lines.append(f"{name} {_fmt(s['value'])}")
    return ("\n".join(lines) + "\n").encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/metrics":
            self._send(200, render_prometheus(build_registry()),
                       "text/plain; version=0.0.4; charset=utf-8")
        elif self.path == "/metrics.json":
            body = json.dumps({"samples": build_registry().samples}).encode()
            self._send(200, body, "application/json")
        elif self.path == "/healthz":
            self._send(200, b"ok\n", "text/plain")
        else:
            self._send(404, b"not found\n", "text/plain")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # keep scrape noise out of launchd logs


def main() -> None:
    threading.Thread(target=run_probe, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[exporter] hippo-metrics-exporter on 127.0.0.1:{PORT} "
          f"(db={DB_PATH}, brain={BRAIN_URL}, db_ttl={DB_TTL_S}s, "
          f"probe_ttl={PROBE_TTL_S}s)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
