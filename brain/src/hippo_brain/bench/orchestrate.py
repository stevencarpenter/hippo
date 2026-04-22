"""Top-level orchestrator: pre-flight → per-model coordinator → summarize → write JSONL."""

from __future__ import annotations

import datetime as _dt
import json
import platform
import subprocess
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


def _cpu_brand() -> str:
    """Best-effort CPU brand. On macOS, sysctl is authoritative; fall back to platform."""
    try:
        out = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except OSError, subprocess.SubprocessError:
        pass
    return platform.processor() or "unknown"


def _lms_version() -> str | None:
    """Best-effort lms CLI version. Returns None if lms is absent or errors."""
    try:
        out = subprocess.run(
            ["lms", "--version"], capture_output=True, text=True, check=False, timeout=5
        )
    except OSError, subprocess.SubprocessError:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


@dataclass
class OrchestrationResult:
    run_id: str
    out_path: Path
    models_completed: list[str] = field(default_factory=list)
    models_errored: list[str] = field(default_factory=list)
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
        "cpu_brand": _cpu_brand(),
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
    temperature: float = 0.7,
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
            "temperature": temperature,
        },
        lmstudio_version=_lms_version(),
    )
    writer.write_manifest(manifest_record)

    if dry_run or preflight_failed or not candidate_models:
        # Always write a run_end record so consumers can tell complete from partial.
        writer._write(
            {
                "record_type": "run_end",
                "run_id": run_id,
                "finished_at_iso": _dt.datetime.now(tz=_dt.UTC).isoformat(),
                "models_completed": [],
                "models_errored": [],
                "reason": (
                    "dry_run"
                    if dry_run
                    else ("preflight_aborted" if preflight_failed else "no_models")
                ),
            }
        )
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
    errored: list[str] = []
    for model in candidate_models:
        try:
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
                temperature=temperature,
            )
        except Exception as e:  # noqa: BLE001 — per-model isolation: never let one failure tank the run
            # Emit a model_summary with an explicit error note so the run record
            # preserves the fact that this candidate was attempted. No attempts,
            # no gates, no verdict — just the failure reason.
            writer.write_model_summary(
                ModelSummaryRecord(
                    run_id=run_id,
                    model={"id": model},
                    events_attempted=len(entries),
                    attempts_total=0,
                    gates={},
                    system_peak={},
                    tier0_verdict={
                        "passed": False,
                        "failed_gates": [],
                        "skipped_gates": [],
                        "notes": [f"run_one_model raised: {type(e).__name__}: {e}"],
                    },
                )
            )
            errored.append(model)
            continue

        for a in result.attempts:
            writer.write_attempt(a)

        # None propagates if self-consistency collected no vectors.
        sc = self_consistency_score(result.per_event_vectors)
        sc_mean: float | None
        sc_min: float | None
        if not sc.per_event_scores:
            sc_mean = None
            sc_min = None
        else:
            sc_mean = sc.mean
            sc_min = sc.min

        gates = aggregate_model_summary(
            attempts=result.attempts,
            self_consistency_mean=sc_mean,
            self_consistency_min=sc_min,
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

    # Write a finalization record so downstream tooling can tell complete
    # from partial runs. Re-emitting the manifest with finished_at_iso is
    # overkill; instead a lightweight "run_end" record.
    writer._write(
        {
            "record_type": "run_end",
            "run_id": run_id,
            "finished_at_iso": _dt.datetime.now(tz=_dt.UTC).isoformat(),
            "models_completed": list(completed),
            "models_errored": list(errored),
        }
    )
    writer.close()
    return OrchestrationResult(
        run_id=run_id,
        out_path=out_path,
        models_completed=completed,
        models_errored=errored,
        preflight_aborted=False,
    )
