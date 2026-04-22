import json

from hippo_brain.bench.output import (
    AttemptRecord,
    ModelSummaryRecord,
    RunEndRecord,
    RunManifestRecord,
    RunWriter,
)


def test_run_manifest_record_serializes():
    r = RunManifestRecord(
        run_id="run-x",
        started_at_iso="2026-04-21T00:00:00Z",
        finished_at_iso=None,
        bench_version="0.1.0",
        host={"hostname": "mac", "os": "darwin", "arch": "arm64"},
        preflight_checks=[{"check": "lms_cli", "status": "pass"}],
        corpus_version="corpus-v1",
        corpus_content_hash="sha256:abc",
        candidate_models=["m1"],
        gate_thresholds={"schema_validity_min": 0.95},
        self_consistency_spec={"events": 5, "runs_per_event": 5},
    )
    d = r.to_dict()
    assert d["record_type"] == "run_manifest"
    assert d["run_id"] == "run-x"


def test_attempt_record_serializes():
    r = AttemptRecord(
        run_id="run-x",
        model={"id": "m1"},
        event={"event_id": "e1", "source": "shell", "content_hash": "h"},
        attempt_idx=0,
        purpose="main",
        timestamps={"start_iso": "t", "start_monotonic_ns": 1, "ttft_ms": 10, "total_ms": 20},
        raw_output="ok",
        parsed_output={"summary": "x"},
        gates={"schema_valid": True},
        system_snapshot={"lmstudio_rss_mb": 100.0},
    )
    d = r.to_dict()
    assert d["record_type"] == "attempt"
    assert d["attempt_idx"] == 0


def test_model_summary_serializes():
    r = ModelSummaryRecord(
        run_id="run-x",
        model={"id": "m1"},
        events_attempted=10,
        attempts_total=15,
        gates={"schema_validity_rate": 0.95},
        system_peak={"rss_max_mb": 200.0, "cpu_pct_max": 90.0, "wall_clock_sec": 60},
        tier0_verdict={"passed": True, "failed_gates": [], "notes": []},
    )
    d = r.to_dict()
    assert d["record_type"] == "model_summary"


def test_run_end_record_serializes():
    r = RunEndRecord(
        run_id="run-x",
        finished_at_iso="2026-04-21T00:10:00Z",
        models_completed=["m1"],
        models_errored=["m2"],
        reason="completed",
    )
    d = r.to_dict()
    assert d["record_type"] == "run_end"
    assert d["models_completed"] == ["m1"]


def test_writer_emits_manifest_first(tmp_path):
    out = tmp_path / "run.jsonl"
    manifest = RunManifestRecord(
        run_id="r",
        started_at_iso="t",
        finished_at_iso=None,
        bench_version="0.1.0",
        host={},
        preflight_checks=[],
        corpus_version="v",
        corpus_content_hash="h",
        candidate_models=[],
        gate_thresholds={},
        self_consistency_spec={},
    )
    writer = RunWriter(out)
    writer.write_manifest(manifest)
    writer.close()

    lines = out.read_text().splitlines()
    assert len(lines) == 1
    first = json.loads(lines[0])
    assert first["record_type"] == "run_manifest"


def test_writer_appends_records(tmp_path):
    out = tmp_path / "run.jsonl"
    writer = RunWriter(out)
    writer.write_manifest(
        RunManifestRecord(
            run_id="r",
            started_at_iso="t",
            finished_at_iso=None,
            bench_version="0.1.0",
            host={},
            preflight_checks=[],
            corpus_version="v",
            corpus_content_hash="h",
            candidate_models=[],
            gate_thresholds={},
            self_consistency_spec={},
        )
    )
    writer.write_attempt(
        AttemptRecord(
            run_id="r",
            model={"id": "m"},
            event={"event_id": "e", "source": "shell", "content_hash": "h"},
            attempt_idx=0,
            purpose="main",
            timestamps={},
            raw_output="",
            parsed_output=None,
            gates={},
            system_snapshot={},
        )
    )
    writer.close()

    lines = out.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["record_type"] == "attempt"


def test_writer_overwrites_existing_run_file(tmp_path):
    out = tmp_path / "run.jsonl"
    out.write_text('{"record_type":"old"}\n')

    writer = RunWriter(out)
    writer.write_manifest(
        RunManifestRecord(
            run_id="r",
            started_at_iso="t",
            finished_at_iso=None,
            bench_version="0.1.0",
            host={},
            preflight_checks=[],
            corpus_version="v",
            corpus_content_hash="h",
            candidate_models=[],
            gate_thresholds={},
            self_consistency_spec={},
        )
    )
    writer.close()

    lines = out.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["record_type"] == "run_manifest"
