"""Top-level orchestrator tests — dry-run manifest, output-dir creation,
per-model failure isolation. Mocks `run_one_model` and the prod-pause RPC so
the test doesn't need a live LM Studio or shadow brain.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hippo_brain.bench.coordinator import ModelRunResult
from hippo_brain.bench.orchestrate import _inference_backend_version, orchestrate_run
from hippo_brain.bench.output import AttemptRecord
from hippo_brain.bench.preflight import CheckResult


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


def test_orchestrate_preflight_receives_v1_normalized_url(stub_corpus, tmp_path):
    """orchestrate_run normalizes the inference URL to include `/v1` BEFORE calling
    run_all_preflight, so check_inference_reachable probes the correct route.
    A bare URL like `http://localhost:1234` must be expanded to
    `http://localhost:1234/v1` before preflight; the CLI default already includes
    `/v1` but programmatic callers may omit it.
    """
    sqlite, manifest = stub_corpus
    out = tmp_path / "run.jsonl"
    captured_preflight_url: list[str] = []

    def fake_preflight(*, brain_url, corpus_sqlite, manifest, inference_url, skip_prod_pause, **kw):
        captured_preflight_url.append(inference_url)
        return [], False  # no checks, not aborted

    with (
        patch("hippo_brain.bench.orchestrate.run_all_preflight", side_effect=fake_preflight),
        patch("hippo_brain.bench.orchestrate.PauseRpcClient") as PauseClient,
    ):
        PauseClient.return_value.probe_health.return_value = None
        orchestrate_run(
            candidate_models=[],
            corpus_sqlite=sqlite,
            manifest_path=manifest,
            out_path=out,
            inference_url="http://localhost:1234",  # bare URL, no /v1
            skip_checks=False,
            skip_prod_pause=True,
            dry_run=False,
        )

    assert len(captured_preflight_url) == 1
    assert captured_preflight_url[0] == "http://localhost:1234/v1"


def test_orchestrate_preflight_does_not_double_normalize_v1_url(stub_corpus, tmp_path):
    """When the caller already passes `.../v1`, preflight receives it unchanged."""
    sqlite, manifest = stub_corpus
    out = tmp_path / "run.jsonl"
    captured_preflight_url: list[str] = []

    def fake_preflight(*, brain_url, corpus_sqlite, manifest, inference_url, skip_prod_pause, **kw):
        captured_preflight_url.append(inference_url)
        return [], False

    with (
        patch("hippo_brain.bench.orchestrate.run_all_preflight", side_effect=fake_preflight),
        patch("hippo_brain.bench.orchestrate.PauseRpcClient") as PauseClient,
    ):
        PauseClient.return_value.probe_health.return_value = None
        orchestrate_run(
            candidate_models=[],
            corpus_sqlite=sqlite,
            manifest_path=manifest,
            out_path=out,
            inference_url="http://localhost:1234/v1",
            skip_checks=False,
            skip_prod_pause=True,
            dry_run=False,
        )

    assert captured_preflight_url[0] == "http://localhost:1234/v1"


def test_orchestrate_forwards_preflight_warnings(stub_corpus, tmp_path):
    """A `warn`-status preflight check is forwarded on OrchestrationResult so the
    CLI can surface it. The canonical case: a missing Q/A fixture warns (run is
    NOT aborted) and QA scoring is skipped. Only `warn` checks are forwarded —
    `pass` checks are not."""
    sqlite, manifest = stub_corpus
    out = tmp_path / "run.jsonl"

    def fake_preflight(*, brain_url, corpus_sqlite, manifest, inference_url, skip_prod_pause, **kw):
        return [
            CheckResult(name="qa_scoreable", status="warn", detail="Q/A fixture missing: /x"),
            CheckResult(name="corpus", status="pass", detail="ok"),
        ], False

    with (
        patch("hippo_brain.bench.orchestrate.run_all_preflight", side_effect=fake_preflight),
        patch("hippo_brain.bench.orchestrate.PauseRpcClient") as PauseClient,
    ):
        PauseClient.return_value.probe_health.return_value = None
        result = orchestrate_run(
            candidate_models=[],
            corpus_sqlite=sqlite,
            manifest_path=manifest,
            out_path=out,
            inference_url="http://localhost:1234/v1",
            skip_checks=False,
            skip_prod_pause=True,
            dry_run=False,
        )

    assert result.preflight_warnings == ["qa_scoreable: Q/A fixture missing: /x"]
    assert result.preflight_aborted is False


def test_orchestrate_forwards_min_scoreable_qa_to_preflight(stub_corpus, tmp_path):
    """The publish-grade Q/A gate must reach the run path. orchestrate_run forwards
    its `min_scoreable_qa` to run_all_preflight; without this plumbing, the run
    silently uses the default of 1 and can publish MRR over a single Q/A item even
    when an operator asked for the 100-item gate."""
    sqlite, manifest = stub_corpus
    out = tmp_path / "run.jsonl"
    captured: list[int] = []

    def fake_preflight(
        *, brain_url, corpus_sqlite, manifest, inference_url, skip_prod_pause, min_scoreable_qa=1
    ):
        captured.append(min_scoreable_qa)
        return [], False

    with (
        patch("hippo_brain.bench.orchestrate.run_all_preflight", side_effect=fake_preflight),
        patch("hippo_brain.bench.orchestrate.PauseRpcClient") as PauseClient,
    ):
        PauseClient.return_value.probe_health.return_value = None
        orchestrate_run(
            candidate_models=[],
            corpus_sqlite=sqlite,
            manifest_path=manifest,
            out_path=out,
            inference_url="http://localhost:1234/v1",
            skip_checks=False,
            skip_prod_pause=True,
            dry_run=False,
            min_scoreable_qa=100,
        )

    assert captured == [100]


def test_orchestrate_min_scoreable_qa_defaults_to_one(stub_corpus, tmp_path):
    """Default behaviour is unchanged: omitting min_scoreable_qa forwards 1, so
    enrichment-only and ad-hoc runs are not gated on a full 100-item fixture
    unless the operator opts in."""
    sqlite, manifest = stub_corpus
    out = tmp_path / "run.jsonl"
    captured: list[int] = []

    def fake_preflight(
        *, brain_url, corpus_sqlite, manifest, inference_url, skip_prod_pause, min_scoreable_qa=1
    ):
        captured.append(min_scoreable_qa)
        return [], False

    with (
        patch("hippo_brain.bench.orchestrate.run_all_preflight", side_effect=fake_preflight),
        patch("hippo_brain.bench.orchestrate.PauseRpcClient") as PauseClient,
    ):
        PauseClient.return_value.probe_health.return_value = None
        orchestrate_run(
            candidate_models=[],
            corpus_sqlite=sqlite,
            manifest_path=manifest,
            out_path=out,
            inference_url="http://localhost:1234/v1",
            skip_checks=False,
            skip_prod_pause=True,
            dry_run=False,
        )

    assert captured == [1]


def test_inference_backend_version_skips_lms_probe_on_omlx(monkeypatch):
    """On the default oMLX backend, version provenance is None WITHOUT shelling
    out to `lms` — that subprocess is a guaranteed failure on an oMLX-only box."""
    monkeypatch.setenv("HIPPO_BENCH_MODEL_LIFECYCLE", "omlx")
    with patch("hippo_brain.bench.orchestrate.subprocess.run") as run:
        assert _inference_backend_version() is None
    run.assert_not_called()


def test_inference_backend_version_probes_lms_when_selected(monkeypatch):
    """When the LM Studio backend is explicitly selected, the version is read
    from `lms --version`."""
    monkeypatch.setenv("HIPPO_BENCH_MODEL_LIFECYCLE", "lms")
    completed = subprocess.CompletedProcess(
        args=["lms", "--version"], returncode=0, stdout="1.2.3\n"
    )
    with patch("hippo_brain.bench.orchestrate.subprocess.run", return_value=completed) as run:
        assert _inference_backend_version() == "1.2.3"
    run.assert_called_once()


def test_inference_backend_version_none_on_unknown_backend(monkeypatch):
    """A misconfigured selector yields None (best-effort provenance), not a crash;
    the bad value still fails the run loudly via get_model_lifecycle elsewhere."""
    monkeypatch.setenv("HIPPO_BENCH_MODEL_LIFECYCLE", "bogus")
    with patch("hippo_brain.bench.orchestrate.subprocess.run") as run:
        assert _inference_backend_version() is None
    run.assert_not_called()


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
