"""Top-level orchestrator tests — dry-run manifest, output-dir creation,
per-model failure isolation. Mocks `run_one_model` and the prod-pause RPC so
the test doesn't need a live LM Studio or shadow brain.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hippo_brain.bench.coordinator import ModelRunResult
from hippo_brain.bench.orchestrate import orchestrate_run
from hippo_brain.bench.output import AttemptRecord


@pytest.fixture
def stub_corpus(tmp_path: Path) -> tuple[Path, Path]:
    """Minimal corpus stubs — orchestrate reads the manifest with .get() fallbacks
    so a present-but-bare manifest is enough for dry-run paths."""
    sqlite = tmp_path / "corpus.sqlite"
    sqlite.write_bytes(b"")  # bytes never read in dry-run / mocked paths
    manifest = tmp_path / "corpus.manifest.json"
    manifest.write_text(json.dumps({"corpus_content_hash": "sha256:test", "schema_version": 0}))
    return sqlite, manifest


def test_dry_run_writes_manifest_then_run_end(stub_corpus, tmp_path):
    """`--dry-run` short-circuits past preflight and per-model loop, producing
    only a manifest record + a run_end record with reason='dry_run'."""
    sqlite, manifest = stub_corpus
    out = tmp_path / "run.jsonl"

    with patch("hippo_brain.bench.orchestrate.PauseRpcClient") as PauseClient:
        PauseClient.return_value.probe_health.return_value = None
        result = orchestrate_run(
            candidate_models=["m1"],
            corpus_sqlite=sqlite,
            manifest_path=manifest,
            out_path=out,
            dry_run=True,
            skip_prod_pause=True,
        )

    assert out.exists()
    records = [json.loads(line) for line in out.read_text().splitlines() if line]
    assert records[0]["record_type"] == "run_manifest"
    assert records[-1]["record_type"] == "run_end"
    assert records[-1]["reason"] == "dry_run"
    assert result.models_completed == []
    assert result.preflight_aborted is False


def test_orchestrate_creates_output_dir(stub_corpus, tmp_path):
    """orchestrate_run mkdirs the JSONL parent before writing — even when the
    operator points --out at a nested path that doesn't exist yet."""
    sqlite, manifest = stub_corpus
    out = tmp_path / "nested" / "subdir" / "run.jsonl"

    with patch("hippo_brain.bench.orchestrate.PauseRpcClient") as PauseClient:
        PauseClient.return_value.probe_health.return_value = None
        orchestrate_run(
            candidate_models=[],
            corpus_sqlite=sqlite,
            manifest_path=manifest,
            out_path=out,
            dry_run=True,
            skip_prod_pause=True,
        )

    assert out.parent.is_dir()
    assert out.exists()


def test_orchestrate_isolates_failing_model(stub_corpus, tmp_path):
    """When run_one_model raises for one model, later models still run; the
    JSONL gets a model_summary error record for the raiser. This is the BT-03/
    BT-04 family contract — a per-model failure must not tank the rest of the
    run.
    """
    sqlite, manifest = stub_corpus
    out = tmp_path / "run.jsonl"

    clean_result = ModelRunResult(
        model="m2",
        attempts=[],
        per_event_vectors=[],
        peak_metrics={},
        wall_clock_sec=1,
        cooldown_timeout=False,
        process_ready_ms=10,
        queue_drain_wall_clock_sec=0,
        downstream_proxy={},
        prod_brain_restarted_during_bench=False,
        timeout_during_drain=False,
        errors=[],
    )

    with (
        patch("hippo_brain.bench.orchestrate.run_one_model") as mock_run_one,
        patch("hippo_brain.bench.orchestrate.PauseRpcClient") as PauseClient,
    ):
        PauseClient.return_value.probe_health.return_value = None
        # First model raises; second returns cleanly.
        mock_run_one.side_effect = [
            RuntimeError("simulated lms crash"),
            clean_result,
        ]
        result = orchestrate_run(
            candidate_models=["m1", "m2"],
            corpus_sqlite=sqlite,
            manifest_path=manifest,
            out_path=out,
            skip_checks=True,
            skip_prod_pause=True,
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

    # run_end record should be last.
    assert records[-1]["record_type"] == "run_end"
    assert records[-1]["models_completed"] == ["m2"]
    assert records[-1]["models_errored"] == ["m1"]


def test_orchestrate_no_models_emits_run_end_with_reason(stub_corpus, tmp_path):
    """Empty candidate_models list short-circuits with reason='no_models'."""
    sqlite, manifest = stub_corpus
    out = tmp_path / "run.jsonl"

    with patch("hippo_brain.bench.orchestrate.PauseRpcClient") as PauseClient:
        PauseClient.return_value.probe_health.return_value = None
        result = orchestrate_run(
            candidate_models=[],
            corpus_sqlite=sqlite,
            manifest_path=manifest,
            out_path=out,
            skip_checks=True,
            skip_prod_pause=True,
            dry_run=False,
        )

    records = [json.loads(line) for line in out.read_text().splitlines() if line]
    assert records[-1]["reason"] == "no_models"
    assert result.models_completed == []


def test_orchestrate_passes_real_embedding_fn_to_model_runner(stub_corpus, tmp_path, monkeypatch):
    sqlite, manifest = stub_corpus
    out = tmp_path / "run.jsonl"
    captured = {}

    clean_result = ModelRunResult(
        model="m1",
        attempts=[],
        per_event_vectors=[],
        peak_metrics={},
        wall_clock_sec=1,
        cooldown_timeout=False,
        process_ready_ms=10,
        queue_drain_wall_clock_sec=0,
        downstream_proxy={},
        prod_brain_restarted_during_bench=False,
        timeout_during_drain=False,
        errors=[],
    )

    def fake_call_embedding(*, base_url, model, text, timeout_sec):
        captured["embedding_call"] = {
            "base_url": base_url,
            "model": model,
            "text": text,
            "timeout_sec": timeout_sec,
        }
        return [0.1, 0.2, 0.3]

    def fake_run_one_model(**kwargs):
        captured["embedding_fn"] = kwargs["embedding_fn"]
        return clean_result

    monkeypatch.setattr("hippo_brain.bench.orchestrate.call_embedding", fake_call_embedding)

    with (
        patch("hippo_brain.bench.orchestrate.run_one_model", side_effect=fake_run_one_model),
        patch("hippo_brain.bench.orchestrate.PauseRpcClient") as PauseClient,
    ):
        PauseClient.return_value.probe_health.return_value = None
        orchestrate_run(
            candidate_models=["m1"],
            corpus_sqlite=sqlite,
            manifest_path=manifest,
            out_path=out,
            inference_url="http://localhost:1234/v1",
            embedding_model="embed-test",
            skip_checks=True,
            skip_prod_pause=True,
            dry_run=False,
        )

    assert captured["embedding_fn"]("question text") == [0.1, 0.2, 0.3]
    assert captured["embedding_call"] == {
        "base_url": "http://localhost:1234/v1",
        "model": "embed-test",
        "text": "question text",
        "timeout_sec": 120,
    }


def test_orchestrate_writes_computed_gates_instead_of_hardcoded_pass(stub_corpus, tmp_path):
    sqlite, manifest = stub_corpus
    out = tmp_path / "run.jsonl"
    bad_attempt = AttemptRecord(
        run_id="run-x",
        model={"id": "m1"},
        event={"event_id": "shell-1", "source": "shell", "content_hash": "h"},
        attempt_idx=0,
        purpose="main",
        timestamps={"total_ms": 100},
        raw_output="not json",
        parsed_output=None,
        gates={
            "schema_valid": False,
            "refusal_detected": False,
            "echo_similarity": 0.1,
            "entity_type_sanity": {},
        },
        system_snapshot={},
    )
    fake_result = ModelRunResult(
        model="m1",
        attempts=[bad_attempt],
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
        orchestrate_run(
            candidate_models=["m1"],
            corpus_sqlite=sqlite,
            manifest_path=manifest,
            out_path=out,
            skip_checks=True,
            skip_prod_pause=True,
            dry_run=False,
        )

    records = [json.loads(line) for line in out.read_text().splitlines() if line]
    summary = next(r for r in records if r["record_type"] == "model_summary")
    assert summary["gates"]["schema_validity_rate"] == 0.0
    assert summary["tier0_verdict"]["passed"] is False
    assert "schema_validity_rate" in summary["tier0_verdict"]["failed_gates"]
