"""End-to-end test of bench orchestration without real LM Studio."""

import json
import sqlite3
from unittest.mock import patch

from hippo_brain.bench.corpus import sample_from_hippo_db, write_corpus
from hippo_brain.bench.enrich_call import CallResult
from hippo_brain.bench.orchestrate import orchestrate_run


def _seed_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE shell_events (
            id INTEGER PRIMARY KEY, command TEXT, stdout TEXT, stderr TEXT,
            duration_ms INTEGER, exit_code INTEGER, cwd TEXT, ts INTEGER
        );
        CREATE TABLE claude_sessions (
            id INTEGER PRIMARY KEY, session_id TEXT, transcript TEXT,
            message_count INTEGER, tool_calls_json TEXT, ts INTEGER
        );
        CREATE TABLE browser_events (
            id INTEGER PRIMARY KEY, url TEXT, title TEXT, dwell_ms INTEGER,
            scroll_depth REAL, ts INTEGER
        );
        CREATE TABLE workflow_runs (
            id INTEGER PRIMARY KEY, repo TEXT, workflow_name TEXT,
            conclusion TEXT, annotations_json TEXT, ts INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO shell_events (command, stdout, stderr, duration_ms, exit_code, cwd, ts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("ls -la", "listing", "", 10, 0, "/tmp", 0),
    )
    conn.execute(
        "INSERT INTO claude_sessions (session_id, transcript, message_count, tool_calls_json, ts)"
        " VALUES (?, ?, ?, ?, ?)",
        ("s1", "hello", 3, "[]", 0),
    )
    conn.commit()
    conn.close()


def _fake_enrich(**_kwargs):
    content = json.dumps(
        {
            "summary": "Synthetic enrichment for bench test",
            "intent": "test",
            "outcome": "success",
            "entities": {
                "projects": ["hippo"],
                "tools": ["pytest"],
                "files": [],
                "services": [],
                "errors": [],
            },
        }
    )
    return CallResult(raw_output=content, ttft_ms=None, total_ms=50, timeout=False)


def _fake_embed(**_kwargs):
    return [1.0, 0.0, 0.0]


@patch("hippo_brain.bench.runner.call_enrichment", side_effect=_fake_enrich)
@patch("hippo_brain.bench.runner.call_embedding", side_effect=_fake_embed)
@patch("hippo_brain.bench.coordinator.call_enrichment", side_effect=_fake_enrich)
@patch("hippo_brain.bench.coordinator.lms")
@patch("hippo_brain.bench.orchestrate.run_all_preflight", return_value=[])
def test_e2e_bench_run_composes_cleanly(_pf, mock_lms, _warmup, _embed, _main, tmp_path):
    mock_lms.list_loaded.return_value = []
    db = tmp_path / "hippo.db"
    _seed_db(db)

    fixture = tmp_path / "corpus-v1.jsonl"
    manifest = tmp_path / "corpus-v1.manifest.json"
    entries = sample_from_hippo_db(
        db_path=db,
        source_counts={"shell": 1, "claude": 1, "browser": 0, "workflow": 0},
        seed=1,
    )
    write_corpus(entries, fixture, manifest, "corpus-v1", 1)

    out = tmp_path / "run.jsonl"
    result = orchestrate_run(
        candidate_models=["m1"],
        corpus_version="corpus-v1",
        fixture_path=fixture,
        manifest_path=manifest,
        base_url="http://localhost:1234/v1",
        embedding_model="nomic",
        out_path=out,
        timeout_sec=5,
        self_consistency_events=1,
        self_consistency_runs=2,
        skip_checks=True,
        dry_run=False,
    )

    records = [json.loads(line) for line in out.read_text().splitlines() if line]
    assert records[0]["record_type"] == "run_manifest"
    assert any(r["record_type"] == "attempt" for r in records)
    assert any(r["record_type"] == "model_summary" for r in records)
    assert result.models_completed == ["m1"]
