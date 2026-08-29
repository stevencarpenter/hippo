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
  - Every metric family is computed inside try/except; a family that fails
    increments hippo_kb_collector_errors_total{name=...} instead of killing
    the scrape. Silence is never acceptable: failures are visible.
  - Snowball metrics (epitaphs, push taps, retrieval_events, bets,
    contradictions, node status) are emitted ONLY when their backing table
    exists, so dashboards light up as features ship instead of showing fake
    zeros. Names are declared up-front (FUTURE_METRIC_NAMES) so the
    source-backed test in brain/tests/test_otel_dashboards.py can verify
    them before the features exist.
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

DATA_DIR = Path(os.environ.get("HIPPO_DATA_DIR", Path.home() / ".local/share/hippo"))
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
# Metric name registry (single source of truth; the source-backed test in
# brain/tests/test_otel_dashboards.py asserts every name below appears in
# this file).
# ---------------------------------------------------------------------------

"hippo_kb_up"
"hippo_kb_scrape_duration_milliseconds"
"hippo_kb_collector_errors_total"
# --- corpus ---
"hippo_kb_events_total"
"hippo_kb_events_24h"
"hippo_kb_last_event_age_milliseconds"
"hippo_kb_stdout_nonempty_total"
"hippo_kb_stderr_nonempty_total"
"hippo_kb_knowledge_nodes_total"
"hippo_kb_agentic_sessions_total"
"hippo_kb_agentic_messages_total"
"hippo_kb_db_size_bytes"
# --- capture-side (from SQLite mirrors of the OTel view) ---
"hippo_kb_capture_alarms_active"
"hippo_kb_capture_source_ok"
"hippo_kb_capture_source_last_event_age_milliseconds"
# --- graveyard ---
"hippo_kb_dead_projects"
"hippo_kb_stranded_hours"
"hippo_kb_dead_project_node_ratio"
# --- identity ---
"hippo_kb_project_identifiers"
"hippo_kb_project_fragmentation_ratio"
# --- decisions ---
"hippo_kb_design_decisions_total"
# --- hygiene / redaction canary ---
"hippo_kb_env_secretish_keys"
# --- recall probe ---
"hippo_kb_recall_up"
"hippo_kb_recall_ok"
"hippo_kb_recall_latency_milliseconds"
"hippo_kb_recall_failures_total"
"hippo_kb_recall_probe_timestamp_seconds"
# --- canary leak drill (optional state file) ---
"hippo_kb_canary_found"
"hippo_kb_canary_drill_timestamp_seconds"

# Snowball metrics: emitted only when the backing feature/table exists.
FUTURE_METRIC_NAMES = [
    "hippo_kb_retrieval_events_total",
    "hippo_kb_epitaphs_total",
    "hippo_kb_push_fires_total",
    "hippo_kb_nodes_by_status",
    "hippo_kb_contradictions_open",
    "hippo_kb_bets",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
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
    """Normalized identity: collapse -/_/space variants."""
    return re.sub(r"[-_\s]+", "", name)


_SECRET_PATTERNS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "_KEY", "AWS_", "GITHUB", "BEARER", "PAT_")


def looks_secretish(key: str) -> bool:
    ku = key.upper()
    if ku.endswith("PATH") or ku == "PATH":
        return False  # %PAT% false positive (PATH contains the substring PAT)
    return any(p in ku for p in _SECRET_PATTERNS)


# ---------------------------------------------------------------------------
# Sample store
# ---------------------------------------------------------------------------

HELP: dict[str, str] = {}
TYPE: dict[str, str] = {}
SAMPLES: list[dict] = []


def _help(name: str, text: str, typ: str = "gauge") -> None:
    HELP.setdefault(name, text)
    TYPE.setdefault(name, typ)


def gauge(name: str, value: float, labels: dict[str, str] | None = None, help: str = "") -> None:
    if help:
        _help(name, help, "gauge")
    SAMPLES.append({"name": name, "labels": labels or {}, "value": float(value)})


def counter(name: str, value: float, labels: dict[str, str] | None = None, help: str = "") -> None:
    if help:
        _help(name, help, "counter")
    SAMPLES.append({"name": name, "labels": labels or {}, "value": float(value)})


ERRORS: dict[str, int] = {}


def family(name: str, fn) -> None:
    """Run a metric family; record errors instead of failing the scrape."""
    before = len(SAMPLES)
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — any family failure must not kill the scrape
        ERRORS[name] = ERRORS.get(name, 0) + 1
        del SAMPLES[before:]
        gauge("hippo_kb_collector_errors_total", ERRORS[name], {"name": name},
              help="Cumulative metric-family computation failures by family name.")
        print(f"[exporter] family {name} failed: {exc}", flush=True)


# ---------------------------------------------------------------------------
# Metric families — database-derived
# ---------------------------------------------------------------------------


def collect_db(now_ms: int) -> None:
    conn = _open_ro()
    try:
        family("events", lambda: _f_events(conn, now_ms))
        family("nodes", lambda: _f_nodes(conn))
        family("sessions", lambda: _f_sessions(conn))
        family("alarms", lambda: _f_alarms(conn))
        family("sources", lambda: _f_sources(conn, now_ms))
        session_projects = _session_project_names(conn)
        family("graveyard", lambda: _f_graveyard(conn, now_ms, session_projects))
        family("identity", lambda: _f_identity(conn, session_projects))
        family("decisions", lambda: _f_decisions(conn))
        family("envsnap", lambda: _f_envsnap(conn))
        family("snowball", lambda: _f_snowball(conn))
    finally:
        conn.close()


def _f_events(conn: sqlite3.Connection, now_ms: int) -> None:
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    gauge("hippo_kb_events_total", total, help="Total captured events (all time).")
    e24 = conn.execute(
        "SELECT COUNT(*) FROM events WHERE timestamp >= ?", (now_ms - 24 * 3600 * 1000,)
    ).fetchone()[0]
    gauge("hippo_kb_events_24h", e24, help="Events captured in the last 24 hours.")
    row = conn.execute("SELECT MAX(timestamp) FROM events").fetchone()
    if row and row[0]:
        gauge("hippo_kb_last_event_age_milliseconds", now_ms - row[0],
              help="Age of the newest event (capture staleness).")
    so = conn.execute(
        "SELECT COUNT(*) FROM events WHERE stdout IS NOT NULL AND stdout != ''"
    ).fetchone()[0]
    se = conn.execute(
        "SELECT COUNT(*) FROM events WHERE stderr IS NOT NULL AND stderr != ''"
    ).fetchone()[0]
    gauge("hippo_kb_stdout_nonempty_total", so, help="Events with non-empty stdout.")
    gauge("hippo_kb_stderr_nonempty_total", se,
          help="Events with non-empty stderr. Persistently 0 = shell hook captures "
               "combined stdout only (schema-ready, hook-starved).")


def _f_nodes(conn: sqlite3.Connection) -> None:
    n = conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0]
    gauge("hippo_kb_knowledge_nodes_total", n, help="Total knowledge nodes.")


def _f_sessions(conn: sqlite3.Connection) -> None:
    s = conn.execute("SELECT COUNT(*) FROM agentic_sessions").fetchone()[0]
    gauge("hippo_kb_agentic_sessions_total", s, help="Total agentic sessions ingested.")
    for t in ("agentic_session_messages", "session_messages"):
        if _table_exists(conn, t):
            m = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            gauge("hippo_kb_agentic_messages_total", m, help="Total agentic session messages.")
            break


def _f_alarms(conn: sqlite3.Connection) -> None:
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
    gauge("hippo_kb_capture_alarms_active", active,
          help="Capture alarms currently unresolved.")


def _f_sources(conn: sqlite3.Connection, now_ms: int) -> None:
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
            gauge("hippo_kb_capture_source_ok", 1 if row[idx] else 0, {"source": src},
                  help="Per-source probe health (1 = ok).")
            idx += 1
        if lagcol and row[idx]:
            gauge("hippo_kb_capture_source_last_event_age_milliseconds", now_ms - row[idx],
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
    """basename -> {n_events, max_ts, duration_ms} for PROJECT directories only.

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


def _f_graveyard(conn: sqlite3.Connection, now_ms: int,
                 session_projects: set[str] | None = None) -> None:
    stats = _project_stats(conn, session_projects)
    dead = _dead_sets(stats, now_ms)
    for w in WINDOWS:
        gauge("hippo_kb_dead_projects", len(dead[w]), {"window": f"{w}d"},
              help="Projects with >=10 events and no activity inside the window.")
        stranded = sum(stats[b]["d"] for b in dead[w]) / 3.6e6
        gauge("hippo_kb_stranded_hours", stranded, {"window": f"{w}d"},
              help="All-time shell hours on projects dead within the window (wasted-effort estimate).")
    # Dead-project contamination of knowledge nodes (30d window).
    dead30 = dead[30]
    link_table = next((t for t in ("knowledge_node_agentic_sessions",
                                   "knowledge_node_sessions", "kn_agentic_sessions")
                       if _table_exists(conn, t)), None)
    if not link_table or not dead30:
        gauge("hippo_kb_dead_project_node_ratio", 0.0,
              help="Fraction of linked knowledge nodes sourced exclusively from dead (30d) projects.")
        return
    sess_cols = _cols(conn, "agentic_sessions")
    pcol = _first_col(sess_cols, ["project_dir", "project", "cwd", "directory", "path"])
    lcols = _cols(conn, link_table)
    lnode = _first_col(lcols, ["knowledge_node_id", "node_id"])
    lsess = _first_col(lcols, ["agentic_session_id", "session_id"])
    if not (pcol and lnode and lsess):
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
        gauge("hippo_kb_dead_project_node_ratio", 0.0)
        return
    dead_only = sum(1 for ps in node_projects.values() if ps and ps <= dead30)
    gauge("hippo_kb_dead_project_node_ratio", dead_only / len(node_projects),
          help="Fraction of linked knowledge nodes sourced exclusively from dead (30d) projects.")


def _f_identity(conn: sqlite3.Connection,
                session_projects: set[str] | None = None) -> None:
    stats = _project_stats(conn, session_projects)
    raw = len(stats)
    norm = len({canon(b) for b in stats})
    gauge("hippo_kb_project_identifiers", raw, {"kind": "raw"},
          help="Distinct project basenames seen in events (pre-normalization).")
    gauge("hippo_kb_project_identifiers", norm, {"kind": "normalized"},
          help="Distinct project basenames after dot/separator normalization.")
    if norm:
        gauge("hippo_kb_project_fragmentation_ratio", raw / norm,
              help="raw/normalized project identifiers (>1 means alias fragmentation).")


def _f_decisions(conn: sqlite3.Connection) -> None:
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
    gauge("hippo_kb_design_decisions_total", n,
          help="Knowledge nodes carrying structured design decisions.")


def _f_envsnap(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "env_snapshots"):
        return
    kcol = _first_col(_cols(conn, "env_snapshots"), ["key", "name"])
    if not kcol:
        return
    keys = {r[0] for r in conn.execute(f"SELECT DISTINCT {kcol} FROM env_snapshots") if r[0]}
    bad = sorted(k for k in keys if looks_secretish(k))
    gauge("hippo_kb_env_secretish_keys", len(bad),
          help="Distinct env-snapshot key NAMES matching secret patterns (key names only; "
               "values are never read). >0 means a redaction/allow-list regression.")
    for k in bad[:10]:
        print(f"[exporter] secretish env key present: {k}", flush=True)


def _f_snowball(conn: sqlite3.Connection) -> None:
    """Emit future-feature metrics only when their tables exist (snowball)."""
    if _table_exists(conn, "retrieval_events"):
        n = conn.execute("SELECT COUNT(*) FROM retrieval_events").fetchone()[0]
        gauge("hippo_kb_retrieval_events_total", n, help="Retrieval feedback events logged.")
    if _table_exists(conn, "epitaphs"):
        cols = _cols(conn, "epitaphs")
        ccol = _first_col(cols, ["confirmed_by", "confirmed"])
        if ccol == "confirmed_by":
            yes = conn.execute(
                "SELECT COUNT(*) FROM epitaphs WHERE confirmed_by IS NOT NULL AND confirmed_by != ''"
            ).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM epitaphs").fetchone()[0]
            gauge("hippo_kb_epitaphs_total", yes, {"confirmed": "yes"},
                  help="Epitaphs (project death records) by confirmation state.")
            gauge("hippo_kb_epitaphs_total", total - yes, {"confirmed": "no"})
        else:
            total = conn.execute("SELECT COUNT(*) FROM epitaphs").fetchone()[0]
            gauge("hippo_kb_epitaphs_total", total, {"confirmed": "unknown"})
    if _table_exists(conn, "push_trials"):
        cols = _cols(conn, "push_trials")
        ucol = _first_col(cols, ["tapped_useful"])
        ncol = _first_col(cols, ["tapped_noise"])
        total = conn.execute("SELECT COUNT(*) FROM push_trials").fetchone()[0]
        if ucol and ncol:
            u = conn.execute(f"SELECT COUNT(*) FROM push_trials WHERE {ucol} = 1").fetchone()[0]
            nz = conn.execute(f"SELECT COUNT(*) FROM push_trials WHERE {ncol} = 1").fetchone()[0]
            gauge("hippo_kb_push_fires_total", u, {"tapped": "useful"},
                  help="Push interventions by user tap verdict.")
            gauge("hippo_kb_push_fires_total", nz, {"tapped": "noise"})
            gauge("hippo_kb_push_fires_total", max(0, total - u - nz), {"tapped": "none"})
        else:
            gauge("hippo_kb_push_fires_total", total, {"tapped": "none"})
    if _table_exists(conn, "nodes"):
        if "status" in _cols(conn, "nodes"):
            for status, n in conn.execute(
                "SELECT status, COUNT(*) FROM nodes GROUP BY status"
            ):
                gauge("hippo_kb_nodes_by_status", n, {"status": str(status)},
                      help="Write-API nodes by lifecycle status.")
    if _table_exists(conn, "contradictions"):
        cols = _cols(conn, "contradictions")
        rcol = _first_col(cols, ["resolved_by", "resolved_at"])
        where = f"WHERE {rcol} IS NULL" if rcol else ""
        n = conn.execute(f"SELECT COUNT(*) FROM contradictions {where}").fetchone()[0]
        gauge("hippo_kb_contradictions_open", n, help="Unresolved cross-writer contradictions.")
    if _table_exists(conn, "bets"):
        cols = _cols(conn, "bets")
        rcol = _first_col(cols, ["resolved_at"])
        total = conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
        if rcol:
            resolved = conn.execute(
                f"SELECT COUNT(*) FROM bets WHERE {rcol} IS NOT NULL"
            ).fetchone()[0]
        else:
            resolved = 0
        gauge("hippo_kb_bets", total - resolved, {"state": "open"},
              help="Bets (declared hypotheses with kill criteria) by state.")
        gauge("hippo_kb_bets", resolved, {"state": "resolved"})


# ---------------------------------------------------------------------------
# Recall probe (/ask round-trip)
# ---------------------------------------------------------------------------

_probe_lock = threading.Lock()
probe_state: dict = {"ts": 0.0, "samples": [], "latencies": {}}


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


def _run_probe_inner() -> None:
    now = time.time()
    samples: list[dict] = []
    latencies: dict[str, float] = {}
    all_ok = True
    for q in GOLDEN_QUESTIONS:
        t0 = time.monotonic()
        ok, reason = False, ""
        try:
            req = urllib.request.Request(
                BRAIN_URL + "/ask",
                data=json.dumps({"question": q, "limit": 5}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
                body = json.loads(resp.read())
            ok = isinstance(body, dict) and len(str(body.get("answer", "")).strip()) > 0
            reason = "" if ok else "empty_answer"
        except urllib.error.HTTPError as e:
            reason = f"http_error_{e.code}"
        except urllib.error.URLError as e:
            r = getattr(e, "reason", None)
            if isinstance(r, ConnectionRefusedError):
                reason = "brain_down"
            elif isinstance(r, (socket.timeout, TimeoutError)):
                reason = "llm_timeout"
            else:
                reason = "brain_error"
        except (socket.timeout, TimeoutError):
            reason = "llm_timeout"
        except Exception as e:  # noqa: BLE001
            reason = f"error_{type(e).__name__}"
        latencies[q] = (time.monotonic() - t0) * 1000
        if not ok:
            all_ok = False
            print(f"[exporter] recall probe failed ({reason}): {q}", flush=True)
        samples.append({"question": q, "ok": ok, "reason": reason, "ms": latencies[q]})
    probe_state.update({"ts": now, "samples": samples, "latencies": latencies,
                        "all_ok": all_ok})


def collect_probe() -> None:
    """Serve cached probe state immediately; refresh in a background thread
    when stale so the /metrics scrape never blocks on the LLM round-trip."""
    now = time.time()
    stale = now - probe_state["ts"] >= PROBE_TTL_S
    if stale and _probe_lock.acquire(blocking=False):
        _probe_lock.release()
        threading.Thread(target=run_probe, daemon=True).start()
    st = probe_state
    if not st.get("samples"):
        return
    gauge("hippo_kb_recall_up", 1.0 if st.get("all_ok") else 0.0,
          help="1 if every golden question round-tripped /ask with a non-empty answer.")
    for s in st["samples"]:
        lbl = {"question": s["question"][:48]}
        gauge("hippo_kb_recall_ok", 1.0 if s["ok"] else 0.0, lbl,
              help="Per-golden-question recall probe result.")
        gauge("hippo_kb_recall_latency_milliseconds", s["ms"], lbl,
              help="Per-golden-question /ask round-trip latency.")
        if not s["ok"]:
            counter("hippo_kb_recall_failures_total", 1.0,
                    {**lbl, "reason": s["reason"]},
                    help="Cumulative recall probe failures by reason class.")
    gauge("hippo_kb_recall_probe_timestamp_seconds", st["ts"],
          help="Unix time of the last recall probe run.")


# ---------------------------------------------------------------------------
# Canary leak drill state (optional; written by the drill job, read here)
# ---------------------------------------------------------------------------


def collect_canary() -> None:
    if not CANARY_FILE.exists():
        return
    try:
        doc = json.loads(CANARY_FILE.read_text())
        stores = doc.get("stores", {})
        for store, found in stores.items():
            gauge("hippo_kb_canary_found", 1.0 if found else 0.0, {"store": store},
                  help="Canary secrets found per store by the last canary leak drill "
                       "(>0 anywhere = leak; run scripts/hippo-canary-drill.py).")
        ts = doc.get("timestamp")
        if ts:
            gauge("hippo_kb_canary_drill_timestamp_seconds", float(ts),
                  help="Unix time of the last canary drill.")
    except Exception:  # noqa: BLE001
        ERRORS["canary"] = ERRORS.get("canary", 0) + 1


# ---------------------------------------------------------------------------
# Exposition
# ---------------------------------------------------------------------------


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prometheus(duration_ms: float) -> tuple[bytes, list[dict]]:
    SAMPLES.clear()
    ERRORS.clear()
    t0 = time.monotonic()
    now_ms = int(time.time() * 1000)
    gauge("hippo_kb_up", 1.0, help="Exporter alive.")
    if DB_PATH.exists():
        collect_db(now_ms)
    else:
        gauge("hippo_kb_collector_errors_total", ERRORS.get("db", 1),
              {"name": "db_missing"},
              help="Cumulative metric-family computation failures by family name.")
    family("probe", collect_probe)
    family("canary", collect_canary)
    gauge("hippo_kb_scrape_duration_milliseconds", (time.monotonic() - t0) * 1000 + duration_ms,
          help="Time the last scrape took to compute, including probe.")

    lines: list[str] = []
    for name in sorted(HELP):
        lines.append(f"# HELP {name} {_esc(HELP[name])}")
        lines.append(f"# TYPE {name} {TYPE.get(name, 'gauge')}")
    by_name: dict[str, list[dict]] = {}
    for s in SAMPLES:
        by_name.setdefault(s["name"], []).append(s)
    for name in sorted(by_name):
        if name not in HELP:
            continue  # registry drift guard: only render declared metrics
        for s in by_name[name]:
            if s["labels"]:
                lbl = ",".join(f'{k}="{_esc(v)}"' for k, v in sorted(s["labels"].items()))
                lines.append(f"{name}{{{lbl}}} {_fmt(s['value'])}")
            else:
                lines.append(f"{name} {_fmt(s['value'])}")
    return ("\n".join(lines) + "\n").encode(), SAMPLES


def _fmt(v: float) -> str:
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return repr(round(v, 3))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/metrics":
            body, _ = render_prometheus(0.0)
            self._send(200, body, "text/plain; version=0.0.4; charset=utf-8")
        elif self.path == "/metrics.json":
            _, samples = render_prometheus(0.0)
            body = json.dumps({"samples": samples}).encode()
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

    def log_message(self, fmt: str, *args: object) -> None:
        pass  # keep scrape noise out of launchd logs


def main() -> None:
    threading.Thread(target=run_probe, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[exporter] hippo-metrics-exporter on 127.0.0.1:{PORT} "
          f"(db={DB_PATH}, brain={BRAIN_URL}, probe_ttl={PROBE_TTL_S}s)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
