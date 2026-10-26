"""Per-agent scorecards over sessions and agent-eval records (HIPO-67).

Design: docs/superpowers/specs/2026-10-25-agent-eval-schema-design.md, sections
3.4 (effective verdict) and 5.2 (MCP returns aggregates only).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from hippo_brain import mcp
from hippo_brain.scorecard_queries import agent_scorecard, agent_scorecards, percentile, rate
from hippo_brain.server import BrainServer
from tests.test_eval_isolation import FIXTURE, _load_fixture

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "crates" / "hippo-core" / "src" / "schema"
SHA = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"
OPUS = "claude-opus-5-5"
HAIKU = "claude-haiku-5-5"
ALPHA = ("/home/dev/alpha", "-home-dev-alpha")
BETA = ("/home/dev/beta", "-home-dev-beta")


@pytest.fixture
def db(tmp_db):
    conn, db_path = tmp_db
    # The Rust SCHEMA appends these two files to schema.sql (v28, v29).
    for name in ("agentic_eval.sql", "agent_eval.sql"):
        conn.executescript((SCHEMA_DIR / name).read_text())
    conn.commit()
    return conn, db_path


def _segment(conn, session_id, seg, start, end, *, agent="code-reviewer", project=ALPHA,
             model=OPUS, parent: str | None = "main-1", subagent=1, tokens=(0, 0, 0, 0, 0), probe=None):  # fmt: skip
    conn.execute(
        "INSERT INTO agentic_sessions (session_id, harness, segment_index, model, agent, "
        "project_dir, cwd, parent_session_id, is_subagent, summary_text, start_time, end_time, "
        "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, token_count, "
        "probe_tag) VALUES (?, 'claude-code', ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, seg, model, agent, project[1], project[0], parent, subagent, start, end,
         *tokens, probe),
    )  # fmt: skip


def _facts(conn, session_id, duration_ms, cost_usd):
    conn.execute(
        "INSERT INTO agentic_session_facts (session_id, harness, cost_usd, duration_ms, "
        "updated_at) VALUES (?, 'claude-code', ?, ?, 0)",
        (session_id, cost_usd, duration_ms),
    )


def _run(conn, run_id, agent, repo, identity, requested, model_id, started, failed=0, units=4):
    conn.execute(
        "INSERT INTO eval_review_runs (run_id, agent, mode, requested_model, model_id, repo, "
        "repo_identity, base, base_sha, head_sha, input_sha256, units_total, units_failed, "
        "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, started_at, "
        "finished_at, ingested_at) VALUES (?, ?, 'mechanical', ?, ?, ?, ?, 'main', ?, ?, 'ff', "
        "?, ?, 100, 10, 1000, 1, ?, ?, 0)",
        (run_id, agent, requested, model_id, repo, identity, SHA, SHA, units, failed, started,
         started + 1),
    )  # fmt: skip


def _finding(conn, finding_id, run_id, severity, category, repo="/home/dev/alpha"):
    conn.execute(
        "INSERT INTO eval_findings (finding_id, run_id, repo, path, side, line, line_text, "
        "severity, confidence, category, claim, logged_at, ingested_at, payload_expiry) "
        "VALUES (?, ?, ?, 'src/a.py', '+', 1, 'SECRET LINE TEXT', ?, 'high', ?, "
        "'SECRET CLAIM TEXT', 0, 0, 1)",
        (finding_id, run_id, repo, severity, category),
    )


def _verdict(conn, verdict_id, finding_id, verdict, auto, decided_at,
             note: str | None = "SECRET NOTE TEXT"):  # fmt: skip
    conn.execute(
        "INSERT INTO eval_verdicts (verdict_id, finding_id, verdict, note, auto, decided_at, "
        "ingested_at, payload_expiry) VALUES (?, ?, ?, ?, ?, ?, 0, 1)",
        (verdict_id, finding_id, verdict, note, auto, decided_at),
    )


def _escape(conn, escape_id, repo, recorded_at):
    conn.execute(
        "INSERT INTO eval_escapes (escape_id, repo, path, line, note, recorded_at, ingested_at) "
        "VALUES (?, ?, 'src/b.py', 1, 'SECRET ESCAPE NOTE', ?, 0)",
        (escape_id, repo, recorded_at),
    )


def _bench(conn, bench_run_id, bench, recorded_at, cases_sha, scores, model="opus"):
    conn.execute(
        "INSERT INTO eval_bench_runs (bench_run_id, bench, recorded_at, repo_commit, model, "
        "model_ids_json, reps, cases, cases_sha256, brief_sha256, scores_json, ingested_at) "
        "VALUES (?, ?, ?, 'abc1234', ?, ?, 3, 15, ?, 'bb', ?, 0)",
        (bench_run_id, bench, recorded_at, model, json.dumps([OPUS]), cases_sha,
         json.dumps(scores)),
    )  # fmt: skip


@pytest.fixture
def seeded(db):
    conn, db_path = db
    # code-reviewer: three invocations (A has two segments) across two parents,
    # two projects, and two models; one probe row that must never count.
    _segment(conn, "sub-a", 0, 1_000_000, 1_060_000, tokens=(100, 10, 1000, 50, 1160))
    _segment(conn, "sub-a", 1, 1_070_000, 1_100_000, tokens=(200, 20, 2000, 0, 2220))
    _facts(conn, "sub-a", 90_000, 0.5)
    _segment(conn, "sub-b", 0, 2_000_000, 2_030_000, tokens=(10, 1, 0, 0, 11))
    _segment(conn, "sub-c", 0, 3_000_000, 3_060_000, project=BETA, model=HAIKU, parent="main-2",
             tokens=(5, 5, 5, 5, 20))  # fmt: skip
    _facts(conn, "sub-c", 50_000, 0.25)
    _segment(conn, "sub-probe", 0, 3_500_000, 3_600_000, tokens=(9_999,) * 5, probe="probe-1")
    _segment(conn, "main-1", 0, 900_000, 2_100_000, agent="", parent=None, subagent=0)
    _segment(conn, "sub-d", 0, 4_000_000, 4_010_000, agent="blind-review", parent="main-3")

    _run(conn, "run-1", "code-reviewer", "/home/dev/alpha", "dev/alpha", "opus", OPUS, 1_000_000)
    _run(conn, "run-2", "code-reviewer", "/home/dev/beta", "dev/beta", "haiku", HAIKU, 5_000_000,
         failed=1, units=3)  # fmt: skip
    _run(conn, "run-3", "blind-review", "/home/dev/alpha", "dev/alpha", "opus", OPUS, 3_000_000)
    for finding_id, run_id, severity, category in [
        ("f1", "run-1", "high", "security"),
        ("f2", "run-1", "high", "correctness"),
        ("f3", "run-1", "medium", "correctness"),
        ("f4", "run-1", "low", "style"),
        ("f5", "run-1", "medium", "security"),
        ("f6", "run-2", "low", "correctness"),
        ("f7", "run-2", "high", "security"),
        ("f8", "run-3", "high", "security"),
    ]:
        _finding(conn, finding_id, run_id, severity, category)
    _verdict(conn, "v1", "f1", "accepted", 0, 10)
    # Human false-positive beats a LATER automatic likely-addressed.
    _verdict(conn, "v2a", "f2", "false-positive", 0, 10)
    _verdict(conn, "v2b", "f2", "likely-addressed", 1, 20, note=None)
    _verdict(
        conn, "v3", "f3", "likely-addressed", 1, 10, note="inferred: line absent at walk start"
    )
    # Latest human verdict wins over an earlier human one.
    _verdict(conn, "v4a", "f4", "not-useful", 0, 10)
    _verdict(conn, "v4b", "f4", "accepted", 0, 20)
    _verdict(conn, "v6", "f6", "likely-addressed", 1, 10, note=None)
    _verdict(conn, "v7", "f7", "duplicate", 0, 10)
    _verdict(conn, "v8", "f8", "false-positive", 0, 10)
    _escape(conn, "x1", "/home/dev/alpha", 2_000_000)  # after run-1 only
    _escape(conn, "x2", "/home/dev/beta", 4_000_000)  # before run-2: no agent
    _escape(conn, "x3", "/home/dev/gamma", 9_000_000)  # no run in the repo
    _escape(conn, "x4", "/home/dev/alpha", 9_000_000)  # after run-1 and run-3

    _bench(conn, "b1", "code-reviewer-mechanical", 100, "aaa", {"recall": 0.8, "extra": 0.1})
    _bench(conn, "b2", "code-reviewer-mechanical", 200, "bbb", {"recall": 0.5, "extra": 0.9})
    _bench(conn, "b3", "code-reviewer-mechanical", 300, "aaa",
           {"recall": 0.9, "extra": 0.05, "stable": True, "label": "SECRET BENCH TEXT"})  # fmt: skip
    _bench(conn, "b4", "code-reviewer2-mechanical", 400, "aaa", {"recall": 0.1})
    conn.commit()
    return conn, db_path


def test_rate_marks_low_sample_and_carries_its_denominator():
    assert rate(2, 6) == {"count": 2, "denominator": 6, "rate": 0.3333, "low_sample": False}
    assert rate(1, 4)["low_sample"] is True
    assert rate(0, 0) == {"count": 0, "denominator": 0, "rate": None, "low_sample": True}


def test_percentile_is_nearest_rank():
    assert percentile([30, 50, 90], 0.9) == 90
    assert percentile(list(range(1, 11)), 0.9) == 9
    assert percentile([], 0.9) is None


def test_session_aggregates(seeded):
    conn, _ = seeded
    sessions = agent_scorecard(conn, "code-reviewer")["sessions"]
    assert sessions == {
        "invocations": 3,
        "subagent_invocations": 3,
        "distinct_sessions": 2,
        "distinct_projects": 2,
        # A from facts (90 s), B from end - start (30 s), C from facts (50 s).
        "duration_ms": {"median": 50_000, "p90": 90_000, "denominator": 3, "from_facts": 2},
        "tokens": {
            "input_tokens": 315,
            "output_tokens": 36,
            "cache_read_tokens": 3005,
            "cache_creation_tokens": 55,
            "token_count": 3411,
        },
        "cost_usd": {"total": 0.75, "denominator": 2},
    }


def test_session_filters_and_probe_exclusion(seeded):
    conn, _ = seeded
    by_project = agent_scorecard(conn, "code-reviewer", project="beta")["sessions"]
    by_model = agent_scorecard(conn, "code-reviewer", model=HAIKU)["sessions"]
    by_since = agent_scorecard(conn, "code-reviewer", since_ms=2_500_000)["sessions"]
    for sessions in (by_project, by_model, by_since):
        assert sessions["invocations"] == 1
        assert sessions["tokens"]["token_count"] == 20
        assert sessions["cost_usd"] == {"total": 0.25, "denominator": 1}
    # The probe row (agent code-reviewer, 9,999 tokens) never counts.
    everything = agent_scorecard(conn, "code-reviewer")["sessions"]
    assert everything["tokens"]["input_tokens"] < 9_999


def test_review_findings_verdicts_and_rates(seeded):
    conn, _ = seeded
    card = agent_scorecard(conn, "code-reviewer")
    assert card["review_runs"] == {
        "runs": 2,
        "failed_runs": 1,
        "units_total": 7,
        "units_failed": 1,
        "tokens": {
            "input_tokens": 200,
            "output_tokens": 20,
            "cache_read_tokens": 2000,
            "cache_creation_tokens": 2,
        },
    }
    findings = card["findings"]
    assert findings["total"] == 7
    assert findings["open"] == 1
    assert findings["judged"] == 6
    assert findings["by_severity"] == {"high": 3, "low": 2, "medium": 2}
    assert findings["by_category"] == {"correctness": 3, "security": 3, "style": 1}
    assert findings["by_verdict"] == {
        "accepted": 2,
        "duplicate": 1,
        "false-positive": 1,
        "likely-addressed": 2,
    }
    rates = findings["verdict_rates"]
    assert rates["accepted"] == {"count": 2, "denominator": 6, "rate": 0.3333, "low_sample": False}
    assert rates["false-positive"]["count"] == 1
    assert rates["not-useful"] == {"count": 0, "denominator": 6, "rate": 0.0, "low_sample": False}
    assert rates["likely-addressed"]["count"] == 2
    assert findings["likely_addressed_inferred"] == 1


def test_low_sample_when_fewer_than_five_judged(seeded):
    conn, _ = seeded
    findings = agent_scorecard(conn, "code-reviewer", project="beta")["findings"]
    assert findings["judged"] == 2
    assert all(r["low_sample"] for r in findings["verdict_rates"].values())
    assert findings["verdict_rates"]["likely-addressed"]["rate"] == 0.5
    assert agent_scorecard(conn, "code-reviewer", model="haiku")["findings"]["total"] == 2


def test_escapes_attach_to_runs_in_the_same_repo_before_the_escape(seeded):
    conn, _ = seeded
    assert agent_scorecard(conn, "code-reviewer")["escapes"] == 2
    assert agent_scorecard(conn, "blind-review")["escapes"] == 1
    assert agent_scorecard(conn, "code-reviewer", since_ms=3_000_000)["escapes"] == 0


def test_latest_benchmark_trend_uses_the_previous_run_with_the_same_cases(seeded):
    conn, _ = seeded
    [bench] = agent_scorecard(conn, "code-reviewer")["benchmarks"]
    assert bench["bench"] == "code-reviewer-mechanical"
    assert bench["bench_run_id"] == "b3"
    assert bench["scores"] == {"recall": 0.9, "extra": 0.05, "stable": True}
    assert bench["previous"]["bench_run_id"] == "b1"
    assert bench["trend"] == {"recall": 0.1, "extra": -0.05}
    assert bench["trend"]["recall"] > 0
    assert agent_scorecard(conn, "code-reviewer", model="haiku")["benchmarks"] == []


def test_all_agents_lists_both_sources_and_skips_main_sessions(seeded):
    conn, _ = seeded
    result = agent_scorecards(conn)
    assert [card["agent"] for card in result["scorecards"]] == ["blind-review", "code-reviewer"]
    assert result["low_sample_min"] == 5
    only_beta = agent_scorecards(conn, project="beta")
    assert [card["agent"] for card in only_beta["scorecards"]] == ["code-reviewer"]


def test_response_holds_no_eval_text(seeded):
    conn, _ = seeded
    text = json.dumps(agent_scorecards(conn))
    assert "SECRET" not in text


def test_mcp_tool_and_http_endpoint_return_the_same_scorecard(seeded, tmp_path):
    conn, db_path = seeded
    previous = mcp._state.db_path
    mcp._state.db_path = str(db_path)
    try:
        tool = asyncio.run(mcp.get_agent_scorecard(agent="code-reviewer", project="alpha"))
    finally:
        mcp._state.db_path = previous
    server = BrainServer(db_path=str(db_path), data_dir=str(tmp_path))
    client = TestClient(Starlette(routes=server.get_routes()))
    resp = client.get("/agents/scorecard", params={"agent": "code-reviewer", "project": "alpha"})
    assert resp.status_code == 200
    assert resp.json() == tool
    assert tool["scorecards"][0]["sessions"]["invocations"] == 2
    bad = client.get("/agents/scorecard", params={"since": "30 days"})
    assert bad.status_code == 400


def test_canary_fixture_never_leaves_through_the_mcp_tool(db):
    """Spec 5.5 canary through the outbound flow: no claim, line, note, or token text."""
    conn, db_path = db
    assert _load_fixture(conn, 1_792_990_000_000) == 5
    # Raw, unredacted token in every text column: the worst case if redaction failed.
    token = "ghp_" + "".join(["Zq7Xk2Lm9P", "w4Rt8Vb3Nc", "6Hd1Jf5Gs0", "Ya2Ue4"])
    conn.execute(
        "INSERT INTO eval_findings (finding_id, run_id, repo, path, side, line, line_text, "
        "severity, confidence, category, claim, logged_at, ingested_at, payload_expiry) "
        "VALUES ('finding-canary', '7f3a9c1e2b4d5a60', '/home/dev/example-repo', 'src/auth.rs', "
        "'+', 3, ?, 'high', 'high', 'security', ?, 0, 0, 1)",
        (f'let auth = "{token}";', f"Hardcoded credential {token} in source."),
    )
    conn.execute(
        "INSERT INTO eval_verdicts (verdict_id, finding_id, verdict, note, auto, decided_at, "
        "ingested_at, payload_expiry) VALUES ('verdict-canary', 'finding-canary', 'accepted', "
        "?, 0, 1, 0, 1)",
        (f"Rotated {token}.",),
    )
    conn.commit()
    records = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]
    texts = [r[key] for r in records for key in ("claim", "line_text", "note") if key in r]
    assert len(texts) == 4

    previous = mcp._state.db_path
    mcp._state.db_path = str(db_path)
    try:
        result = asyncio.run(mcp.get_agent_scorecard(agent="blind-review"))
    finally:
        mcp._state.db_path = previous

    body = json.dumps(result)
    findings = result["scorecards"][0]["findings"]
    assert findings["total"] == 2
    assert findings["by_verdict"] == {"accepted": 2}
    assert result["scorecards"][0]["escapes"] == 1
    assert "ghp_" not in body
    for text in texts:
        assert text not in body
