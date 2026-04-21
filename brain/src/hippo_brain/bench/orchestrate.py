"""Top-level orchestrator: pre-flight → per-model coordinator → summarize → write JSONL."""

from __future__ import annotations

import datetime as _dt
import json
import platform
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from hippo_brain.bench import __version__
from hippo_brain.bench.config import DEFAULT_THRESHOLDS
from hippo_brain.bench.coordinator import run_one_model
from hippo_brain.bench.corpus import load_corpus
from hippo_brain.bench.gates import self_consistency_score
from hippo_brain.bench.output import (
    ModelSummaryRecord,
    RunManifestRecord,
    RunWriter,
)
from hippo_brain.bench.preflight import run_all_preflight
from hippo_brain.bench.summary import aggregate_model_summary, compute_verdict


@dataclass
class OrchestrationResult:
    run_id: str
    out_path: Path
    models_completed: list[str] = field(default_factory=list)
    preflight_aborted: bool = False


def _build_run_id() -> str:
    ts = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%dT%H%M%S")
    return f"run-{ts}-{platform.node()}"


def _host_info() -> dict:
    vm = psutil.virtual_memory()
    return {
        "hostname": platform.node(),
        "os": f"{platform.system().lower()} {platform.release()}",
        "arch": platform.machine(),
        "cpu_brand": platform.processor() or "unknown",
        "total_mem_gb": round(vm.total / (1024**3), 1),
    }


def orchestrate_run(
    *,
    candidate_models: list[str],
    corpus_version: str,
    fixture_path: Path,
    manifest_path: Path,
    base_url: str,
    embedding_model: str,
    out_path: Path,
    timeout_sec: int,
    self_consistency_events: int,
    self_consistency_runs: int,
    skip_checks: bool,
    dry_run: bool,
) -> OrchestrationResult:
    run_id = _build_run_id()

    corpus_content_hash = "sha256:unknown"
    try:
        manifest_obj = json.loads(manifest_path.read_text(encoding="utf-8"))
        corpus_content_hash = manifest_obj.get("corpus_content_hash", "sha256:unknown")
    except FileNotFoundError:
        pass

    preflight = [] if skip_checks else run_all_preflight(out_path.parent, f"{base_url}/models")
    preflight_failed = any(c.status == "fail" for c in preflight)

    writer = RunWriter(out_path)
    manifest_record = RunManifestRecord(
        run_id=run_id,
        started_at_iso=_dt.datetime.now(tz=_dt.UTC).isoformat(),
        finished_at_iso=None,
        bench_version=__version__,
        host=_host_info(),
        preflight_checks=[c.to_dict() for c in preflight],
        corpus_version=corpus_version,
        corpus_content_hash=corpus_content_hash,
        candidate_models=list(candidate_models),
        gate_thresholds=dict(DEFAULT_THRESHOLDS),
        self_consistency_spec={
            "events": self_consistency_events,
            "runs_per_event": self_consistency_runs,
        },
    )
    writer.write_manifest(manifest_record)

    if dry_run or preflight_failed or not candidate_models:
        writer.close()
        return OrchestrationResult(
            run_id=run_id,
            out_path=out_path,
            models_completed=[],
            preflight_aborted=preflight_failed,
        )

    entries = list(load_corpus(fixture_path))
    sc_entries = entries[:self_consistency_events]

    completed: list[str] = []
    for model in candidate_models:
        result = run_one_model(
            model=model,
            base_url=base_url,
            entries=entries,
            sc_entries=sc_entries,
            runs_per_event=self_consistency_runs,
            embedding_model=embedding_model,
            timeout_sec=timeout_sec,
            warmup_calls=3,
            cooldown_max_sec=90,
            run_id=run_id,
        )
        for a in result.attempts:
            writer.write_attempt(a)

        sc = self_consistency_score(result.per_event_vectors)
        gates = aggregate_model_summary(
            attempts=result.attempts,
            self_consistency_mean=sc.mean,
            self_consistency_min=sc.min,
        )
        verdict = compute_verdict(gates, DEFAULT_THRESHOLDS)

        writer.write_model_summary(
            ModelSummaryRecord(
                run_id=run_id,
                model={"id": model},
                events_attempted=len(entries),
                attempts_total=len(result.attempts),
                gates=gates,
                system_peak={
                    **result.peak_metrics,
                    "wall_clock_sec": result.wall_clock_sec,
                },
                tier0_verdict=verdict,
                cooldown_timeout=result.cooldown_timeout,
            )
        )
        completed.append(model)

    writer.close()
    return OrchestrationResult(
        run_id=run_id, out_path=out_path, models_completed=completed, preflight_aborted=False
    )
