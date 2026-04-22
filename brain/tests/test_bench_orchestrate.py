import json
import sqlite3
from unittest.mock import patch

from hippo_brain.bench.corpus import sample_from_hippo_db, write_corpus
from hippo_brain.bench.orchestrate import orchestrate_run


def _seed_minimal_db(db_path):
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
    conn.commit()
    conn.close()


def test_orchestrate_dry_run_produces_manifest_only(tmp_path):
    fixture = tmp_path / "corpus-v1.jsonl"
    manifest = tmp_path / "corpus-v1.manifest.json"
    fixture.write_text("")
    manifest.write_text('{"corpus_content_hash": "sha256:empty", "corpus_version": "corpus-v1"}')
    out = tmp_path / "run.jsonl"

    with patch("hippo_brain.bench.orchestrate.run_all_preflight") as mock_pf:
        mock_pf.return_value = []
        result = orchestrate_run(
            candidate_models=[],
            corpus_version="corpus-v1",
            fixture_path=fixture,
            manifest_path=manifest,
            base_url="http://localhost:1234/v1",
            embedding_model="nomic",
            out_path=out,
            timeout_sec=60,
            self_consistency_events=0,
            self_consistency_runs=0,
            skip_checks=True,
            dry_run=True,
        )
    assert out.exists()
    lines = out.read_text().splitlines()
    records = [json.loads(line) for line in lines]
    assert records[0]["record_type"] == "run_manifest"
    assert records[-1]["record_type"] == "run_end"
    assert records[-1]["reason"] in ("dry_run", "no_models")
    assert result.models_completed == []


def test_orchestrate_creates_output_dir_before_preflight(tmp_path):
    fixture = tmp_path / "corpus-v1.jsonl"
    manifest = tmp_path / "corpus-v1.manifest.json"
    fixture.write_text("")
    manifest.write_text('{"corpus_content_hash": "sha256:empty", "corpus_version": "corpus-v1"}')
    out = tmp_path / "nested" / "runs" / "run.jsonl"

    with patch("hippo_brain.bench.orchestrate.run_all_preflight") as mock_pf:
        mock_pf.return_value = []
        orchestrate_run(
            candidate_models=[],
            corpus_version="corpus-v1",
            fixture_path=fixture,
            manifest_path=manifest,
            base_url="http://localhost:1234/v1",
            embedding_model="nomic",
            out_path=out,
            timeout_sec=60,
            self_consistency_events=0,
            self_consistency_runs=0,
            skip_checks=False,
            dry_run=True,
        )

    assert out.parent.exists()
    assert mock_pf.call_args.args[0] == out.parent


@patch("hippo_brain.bench.orchestrate.run_one_model")
def test_orchestrate_isolates_failing_model(mock_run_one, tmp_path):
    """When one model raises, later models still run; JSONL gets an error record."""
    db = tmp_path / "hippo.db"
    _seed_minimal_db(db)

    fixture = tmp_path / "corpus-v1.jsonl"
    manifest = tmp_path / "corpus-v1.manifest.json"
    entries = sample_from_hippo_db(
        db_path=db,
        source_counts={"shell": 1, "claude": 0, "browser": 0, "workflow": 0},
        seed=1,
    )
    write_corpus(entries, fixture, manifest, "corpus-v1", 1)

    from hippo_brain.bench.coordinator import ModelRunResult

    # First model raises; second returns a clean empty result.
    mock_run_one.side_effect = [
        RuntimeError("simulated lms crash"),
        ModelRunResult(
            model="m2",
            attempts=[],
            per_event_vectors=[],
            peak_metrics={},
            wall_clock_sec=1,
            cooldown_timeout=False,
        ),
    ]

    out = tmp_path / "run.jsonl"
    result = orchestrate_run(
        candidate_models=["m1", "m2"],
        corpus_version="corpus-v1",
        fixture_path=fixture,
        manifest_path=manifest,
        base_url="http://x/v1",
        embedding_model="nomic",
        out_path=out,
        timeout_sec=5,
        self_consistency_events=0,
        self_consistency_runs=0,
        skip_checks=True,
        dry_run=False,
    )

    assert result.models_completed == ["m2"]
    assert result.models_errored == ["m1"]

    records = [json.loads(line) for line in out.read_text().splitlines() if line]
    summaries = [r for r in records if r["record_type"] == "model_summary"]
    assert len(summaries) == 2
    m1_summary = next(s for s in summaries if s["model"]["id"] == "m1")
    assert m1_summary["tier0_verdict"]["passed"] is False
    assert m1_summary["attempts_total"] == 0
    assert any("simulated lms crash" in n for n in m1_summary["tier0_verdict"]["notes"])

    # run_end record should be last
    assert records[-1]["record_type"] == "run_end"
    assert records[-1]["models_completed"] == ["m2"]
    assert records[-1]["models_errored"] == ["m1"]
