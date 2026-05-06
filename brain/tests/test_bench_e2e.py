"""End-to-end test of bench orchestration without real LM Studio or shadow brain.

Drives `orchestrate_run` against a minimal real corpus produced by
`init_corpus`, with the per-model heavy lifting (`run_one_model`) replaced by
a synthetic ModelRunResult. Asserts the JSONL composition (manifest first,
attempts in middle, run_end last) and that completed/errored book-keeping
matches.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest

from hippo_brain.bench.coordinator import ModelRunResult
from hippo_brain.bench.corpus import init_corpus
from hippo_brain.bench.orchestrate import orchestrate_run
from hippo_brain.bench.output import AttemptRecord


def _seed_minimal_full_schema(conn: sqlite3.Connection) -> None:
    """Insert one row per source against the real (conftest tmp_db) hippo schema.
    Uses recent timestamps so the default 90-day window picks them up.
    """
    import time

    now_ms = int(time.time() * 1000)
    recent = now_ms - 1 * 86_400_000  # 1 day ago

    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'host', 'user')",
        (recent,),
    )
    conn.execute(
        "INSERT INTO events "
        "(session_id, timestamp, command, stdout, stderr, exit_code, "
        " duration_ms, cwd, hostname, shell, source_kind) "
        "VALUES (1, ?, 'cargo build', 'Compiling', '', 0, 1500, "
        " '/repo', 'host', 'zsh', 'shell')",
        (recent,),
    )
    conn.execute(
        "INSERT INTO claude_sessions "
        "(session_id, project_dir, cwd, segment_index, start_time, end_time, "
        " summary_text, tool_calls_json, user_prompts_json, message_count, source_file) "
        "VALUES ('s1', '/proj', '/proj', 0, ?, ?, 'a summary', '[\"Bash\"]', '[]', 8, '/log.jsonl')",
        (recent, recent + 1000),
    )
    conn.execute(
        "INSERT INTO browser_events "
        "(timestamp, url, title, domain, dwell_ms) "
        "VALUES (?, 'https://docs.python.org/3/', 'docs', 'docs.python.org', 60000)",
        (recent,),
    )
    conn.commit()


@pytest.fixture
def real_corpus(tmp_db, tmp_path):
    """Build a real shadow-DB corpus from tmp_db's full hippo schema."""
    conn, db_path = tmp_db
    _seed_minimal_full_schema(conn)

    dest_sqlite = tmp_path / "corpus.sqlite"
    dest_jsonl = tmp_path / "corpus.jsonl"
    manifest = tmp_path / "corpus.manifest.json"

    init_corpus(
        db_path=db_path,
        dest_sqlite=dest_sqlite,
        dest_jsonl=dest_jsonl,
        manifest_path=manifest,
        corpus_days=90,
        corpus_buckets=9,
        shell_min=1,
        claude_min=1,
        browser_min=1,
        workflow_min=0,
        seed=42,
    )
    return dest_sqlite, manifest


def test_e2e_bench_run_composes_cleanly(real_corpus, tmp_path):
    """End-to-end: orchestrate writes manifest → attempts → model_summary →
    run_end, with `run_one_model` mocked to return a synthetic clean result.
    """
    sqlite, manifest = real_corpus
    out = tmp_path / "run.jsonl"

    sample_attempt = AttemptRecord(
        run_id="run-x",
        model={"id": "m1"},
        event={"event_id": "shell-1", "source": "shell", "content_hash": "h"},
        attempt_idx=0,
        purpose="self_consistency",
        timestamps={
            "start_iso": "2026-05-05T00:00:00Z",
            "start_monotonic_ns": 1,
            "ttft_ms": 10,
            "total_ms": 100,
        },
        raw_output='{"summary": "ok"}',
        parsed_output={"summary": "ok"},
        gates={"schema_valid": True},
        system_snapshot={},
    )
    fake_result = ModelRunResult(
        model="m1",
        attempts=[sample_attempt],
        per_event_vectors=[],
        peak_metrics={},
        wall_clock_sec=1,
        cooldown_timeout=False,
        process_ready_ms=10,
        queue_drain_wall_clock_sec=0,
        downstream_proxy={"modes": {"hybrid": {"mrr": 0.4, "hit_at_1": 0.5}}},
        prod_brain_restarted_during_bench=False,
        timeout_during_drain=False,
        errors=[],
    )

    with (
        patch("hippo_brain.bench.orchestrate.run_one_model", return_value=fake_result),
        patch("hippo_brain.bench.orchestrate.PauseRpcClient") as PauseClient,
    ):
        PauseClient.return_value.probe_health.return_value = None
        result = orchestrate_run(
            candidate_models=["m1"],
            corpus_sqlite=sqlite,
            manifest_path=manifest,
            out_path=out,
            skip_checks=True,
            skip_prod_pause=True,
            dry_run=False,
        )

    records = [json.loads(line) for line in out.read_text().splitlines() if line]
    assert records[0]["record_type"] == "run_manifest"
    assert any(r["record_type"] == "attempt" for r in records)
    summaries = [r for r in records if r["record_type"] == "model_summary"]
    assert len(summaries) == 1
    assert summaries[0]["downstream_proxy"]["modes"]["hybrid"]["mrr"] == 0.4
    assert records[-1]["record_type"] == "run_end"
    assert result.models_completed == ["m1"]
