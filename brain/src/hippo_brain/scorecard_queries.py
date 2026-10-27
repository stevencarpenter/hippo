"""Per-agent scorecards: aggregates over agent sessions and agent-eval records (HIPO-67).

Design: docs/superpowers/specs/2026-10-25-agent-eval-schema-design.md, sections
3.4 and 5.2. The result feeds the MCP tool ``get_agent_scorecard`` (an outbound
flow) and the brain endpoint ``GET /agents/scorecard``. It holds counts, sums,
rates, and record ids only. No query here selects ``claim``, ``line_text``, or
``note``; the one ``note`` reference is a ``LIKE`` test that yields a boolean.

The ``agent`` identity is the registry agent name, and both sources must carry
it: ``agentic_sessions.agent`` (the Claude Code subagent type) and
``eval_review_runs.agent``. A producer that writes a different name for the same
agent produces a second, split scorecard. Benchmarks have no agent column; see
``BENCHES_BY_AGENT``.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections.abc import Iterable
from typing import Any

# A rate whose denominator is below this is marked low_sample.
LOW_SAMPLE_MIN = 5
VERDICT_RATES = ("accepted", "false-positive", "not-useful", "likely-addressed")
TOKEN_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "token_count",
)
# Benchmarks carry no agent column. An agent owns the benches listed here, else
# only the bench named exactly like the agent. Exact names, not a prefix rule, so
# agent ``code`` never claims bench ``code-reviewer``.
BENCHES_BY_AGENT: dict[str, tuple[str, ...]] = {
    "blind-reviewer": ("blind-review", "blind-review-mechanical", "blind-review-private"),
}
AGENT_BY_BENCH = {bench: agent for agent, benches in BENCHES_BY_AGENT.items() for bench in benches}
ESCAPE_ATTRIBUTION = "latest-run-in-repo"
# Marker the HIPO-66 linkage job writes on inferred (not observed) fix commits.
INFERRED_NOTE_PREFIX = "inferred:"

# Filters shared by the per-agent and the all-agents queries; the agent clause is
# added by each query.
_SESSION_SCOPE = """
    s.probe_tag IS NULL
    AND (:project IS NULL OR s.cwd LIKE :project OR s.project_dir LIKE :project)
    AND (:model IS NULL OR s.model = :model OR s.requested_model = :model)
"""

_RUN_SCOPE = """
    r.started_at >= :since
    AND (:project IS NULL OR r.repo_identity LIKE :project OR r.repo LIKE :project)
    AND (:model IS NULL OR r.requested_model = :model
         OR ',' || r.model_id || ',' LIKE '%,' || :model || ',%')
"""

# One row per invocation, a (session_id, harness) pair: a subagent transcript
# is its own session_id. ``since`` applies to the invocation's first segment.
_INVOCATIONS_SQL = f"""
    SELECT s.session_id, s.harness,
           MAX(s.is_subagent) AS is_subagent,
           COALESCE(NULLIF(MAX(s.parent_session_id), ''), s.session_id) AS parent,
           MAX(s.project_dir) AS project_dir,
           MIN(s.start_time) AS first_start,
           MAX(s.end_time) - MIN(s.start_time) AS span_ms,
           SUM(s.input_tokens), SUM(s.output_tokens), SUM(s.cache_read_tokens),
           SUM(s.cache_creation_tokens), SUM(s.token_count),
           f.duration_ms, f.cost_usd
    FROM agentic_sessions s
    LEFT JOIN agentic_session_facts f
      ON f.session_id = s.session_id AND f.harness = s.harness
    WHERE s.agent = :agent AND s.agent <> '' AND {_SESSION_SCOPE}
    GROUP BY s.session_id, s.harness
    HAVING MIN(s.start_time) >= :since
"""

_EFFECTIVE_VERDICTS_SQL = f"""
    SELECT finding_id, verdict, inferred
    FROM (
        SELECT v.finding_id, v.verdict,
               v.note LIKE '{INFERRED_NOTE_PREFIX}%' AS inferred,
               ROW_NUMBER() OVER (
                   PARTITION BY v.finding_id
                   ORDER BY (v.auto = 0) DESC, v.decided_at DESC, v.verdict_id DESC
               ) AS rn
        FROM eval_verdicts v
    )
    WHERE rn = 1
"""

_FINDINGS_SQL = f"""
    WITH runs AS (
        SELECT r.run_id FROM eval_review_runs r WHERE r.agent = :agent AND {_RUN_SCOPE}
    ),
         effective AS ({_EFFECTIVE_VERDICTS_SQL})
    SELECT f.severity, f.category, e.verdict,
           e.verdict = 'likely-addressed' AND e.inferred
    FROM eval_findings f
    JOIN runs USING (run_id)
    LEFT JOIN effective e USING (finding_id)
"""

_RUNS_SQL = f"""
    SELECT COUNT(*), COALESCE(SUM(r.units_failed > 0), 0),
           COALESCE(SUM(r.units_total), 0), COALESCE(SUM(r.units_failed), 0),
           COALESCE(SUM(r.input_tokens), 0), COALESCE(SUM(r.output_tokens), 0),
           COALESCE(SUM(r.cache_read_tokens), 0), COALESCE(SUM(r.cache_creation_tokens), 0)
    FROM eval_review_runs r
    WHERE r.agent = :agent AND {_RUN_SCOPE}
"""

# Each escape goes to one run: the latest run of any agent in the same repository
# (same ``repo`` path and same ``repo_identity``) that started before the escape
# was recorded. It counts for this agent when that run is the agent's and passes
# the filters. Runs do not record the paths they reviewed, so attribution is per
# repository, not per path.
_ESCAPES_SQL = f"""
    SELECT COUNT(*)
    FROM eval_escapes x
    JOIN eval_review_runs r ON r.run_id = (
        SELECT p.run_id FROM eval_review_runs p
        WHERE p.repo = x.repo AND p.repo_identity IS x.repo_identity
          AND p.started_at < x.recorded_at
        ORDER BY p.started_at DESC, p.run_id DESC
        LIMIT 1
    )
    WHERE x.recorded_at >= :since AND r.agent = :agent AND {_RUN_SCOPE}
"""

# ``since`` and ``project`` do not apply to benchmarks.
_BENCH_MODEL_SCOPE = """
    (:model IS NULL OR model = :model
     OR EXISTS (SELECT 1 FROM json_each(model_ids_json) WHERE value = :model))
"""

_BENCH_SQL = f"""
    SELECT bench_run_id, bench, recorded_at, model, repo_commit, reps, cases,
           cases_sha256, failed_reviews, tokens, scores_json
    FROM eval_bench_runs
    WHERE bench IN (SELECT value FROM json_each(:benches)) AND {_BENCH_MODEL_SCOPE}
    ORDER BY bench, model, recorded_at DESC, bench_run_id DESC
"""

_BENCH_NAMES_SQL = f"SELECT DISTINCT bench FROM eval_bench_runs WHERE {_BENCH_MODEL_SCOPE}"

_AGENTS_SQL = f"""
    SELECT agent FROM (
        SELECT s.agent AS agent FROM agentic_sessions s
        WHERE s.agent <> '' AND {_SESSION_SCOPE}
        GROUP BY s.agent, s.session_id, s.harness
        HAVING MIN(s.start_time) >= :since
        UNION
        SELECT r.agent FROM eval_review_runs r WHERE {_RUN_SCOPE}
    )
"""


def rate(count: int, denominator: int) -> dict[str, Any]:
    """Return a rate with its denominator; under LOW_SAMPLE_MIN it is low_sample."""
    return {
        "count": count,
        "denominator": denominator,
        "rate": round(count / denominator, 4) if denominator else None,
        "low_sample": denominator < LOW_SAMPLE_MIN,
    }


def percentile(values: list[int], pct: float) -> int | None:
    """Nearest-rank percentile: the smallest value with at least pct of values at or below it."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct * len(ordered)) - 1)]


def _numeric_scores(scores_json: str) -> dict[str, float | bool]:
    """Keep only number and boolean scores so producer text never leaves."""
    try:
        scores = json.loads(scores_json)
    except ValueError:
        return {}
    if not isinstance(scores, dict):
        return {}
    return {k: v for k, v in scores.items() if isinstance(v, (int, float))}


def _counts(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def _sessions(conn: sqlite3.Connection, params: dict[str, Any]) -> dict[str, Any]:
    rows = conn.execute(_INVOCATIONS_SQL, params).fetchall()
    durations = [row[12] if row[12] is not None else row[6] for row in rows]
    costs = [row[13] for row in rows if row[13] is not None]
    median = statistics.median(durations) if durations else None
    return {
        "invocations": len(rows),
        "subagent_invocations": sum(1 for row in rows if row[2]),
        "distinct_sessions": len({row[3] for row in rows}),
        "distinct_projects": len({row[4] for row in rows}),
        "duration_ms": {
            "median": None if median is None else int(median),
            "p90": percentile(durations, 0.9),
            "denominator": len(durations),
            "from_facts": sum(1 for row in rows if row[12] is not None),
        },
        "tokens": {
            column: sum(row[7 + i] or 0 for row in rows) for i, column in enumerate(TOKEN_COLUMNS)
        },
        "cost_usd": {
            "total": round(sum(costs), 6) if costs else None,
            "denominator": len(costs),
        },
    }


def _findings(conn: sqlite3.Connection, params: dict[str, Any]) -> dict[str, Any]:
    rows = conn.execute(_FINDINGS_SQL, params).fetchall()
    judged = [row for row in rows if row[2] is not None]
    verdicts = _counts(row[2] for row in judged)
    return {
        "total": len(rows),
        "open": len(rows) - len(judged),
        "judged": len(judged),
        "by_severity": _counts(row[0] for row in rows),
        "by_category": _counts(row[1] for row in rows),
        "by_verdict": verdicts,
        "verdict_rates": {name: rate(verdicts.get(name, 0), len(judged)) for name in VERDICT_RATES},
        "likely_addressed_inferred": sum(1 for row in judged if row[3]),
    }


def _review_runs(conn: sqlite3.Connection, params: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute(_RUNS_SQL, params).fetchone()
    return {
        "runs": row[0],
        "failed_runs": row[1],
        "units_total": row[2],
        "units_failed": row[3],
        "tokens": dict(zip(TOKEN_COLUMNS[:4], row[4:8], strict=True)),
    }


def _bench(conn: sqlite3.Connection, params: dict[str, Any]) -> list[dict[str, Any]]:
    benches = BENCHES_BY_AGENT.get(params["agent"], (params["agent"],))
    # One entry per (bench, model): a trend never compares two models.
    by_bench: dict[tuple[str, str], list[tuple]] = {}
    for row in conn.execute(_BENCH_SQL, params | {"benches": json.dumps(benches)}):
        by_bench.setdefault((row[1], row[3]), []).append(row)
    out = []
    for (bench, _model), rows in by_bench.items():
        latest = rows[0]
        scores = _numeric_scores(latest[10])
        previous = next((row for row in rows[1:] if row[7] == latest[7]), None)
        entry: dict[str, Any] = {
            "bench": bench,
            "bench_run_id": latest[0],
            "recorded_at": latest[2],
            "model": latest[3],
            "repo_commit": latest[4],
            "reps": latest[5],
            "cases": latest[6],
            "cases_sha256": latest[7],
            "failed_reviews": latest[8],
            "tokens": latest[9],
            "scores": scores,
            "previous": None,
            "trend": {},
        }
        if previous is not None:
            before = _numeric_scores(previous[10])
            entry["previous"] = {
                "bench_run_id": previous[0],
                "recorded_at": previous[2],
                "scores": before,
            }
            entry["trend"] = {
                key: round(value - before[key], 6)
                for key, value in scores.items()
                if not isinstance(value, bool)
                and key in before
                and not isinstance(before[key], bool)
            }
        out.append(entry)
    return out


def _params(agent: str, since_ms: int, project: str, model: str) -> dict[str, Any]:
    return {
        "agent": agent,
        "since": since_ms,
        "project": f"%{project}%" if project else None,
        "model": model or None,
    }


def agent_scorecard(
    conn: sqlite3.Connection,
    agent: str,
    *,
    since_ms: int = 0,
    project: str = "",
    model: str = "",
) -> dict[str, Any]:
    """Return one agent's scorecard. All values are aggregates or record ids."""
    params = _params(agent, since_ms, project, model)
    return {
        "agent": agent,
        "sessions": _sessions(conn, params),
        "review_runs": _review_runs(conn, params),
        "findings": _findings(conn, params),
        "escapes": {
            "count": conn.execute(_ESCAPES_SQL, params).fetchone()[0],
            "attribution": ESCAPE_ATTRIBUTION,
        },
        "benchmarks": _bench(conn, params),
    }


def agent_scorecards(
    conn: sqlite3.Connection,
    agent: str = "",
    *,
    since_ms: int = 0,
    project: str = "",
    model: str = "",
) -> dict[str, Any]:
    """Return scorecards for one agent, or for every agent seen under the filters."""
    if agent:
        agents = [agent]
    else:
        params = _params("", since_ms, project, model)
        names = {row[0] for row in conn.execute(_AGENTS_SQL, params)}
        # Benchmarks carry no project, so a project filter leaves bench-only agents out.
        if not project:
            names |= {
                AGENT_BY_BENCH.get(row[0], row[0]) for row in conn.execute(_BENCH_NAMES_SQL, params)
            }
        agents = sorted(names)
    return {
        "filters": {"agent": agent, "since_ms": since_ms, "project": project, "model": model},
        "low_sample_min": LOW_SAMPLE_MIN,
        "scorecards": [
            agent_scorecard(conn, name, since_ms=since_ms, project=project, model=model)
            for name in agents
        ],
    }
