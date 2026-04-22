"""Per-model lifecycle: unload → load → warmup → main → self-consistency → unload → cooldown."""

from __future__ import annotations

import time
from dataclasses import dataclass

from hippo_brain.bench import lms
from hippo_brain.bench.corpus import CorpusEntry
from hippo_brain.bench.enrich_call import call_enrichment
from hippo_brain.bench.metrics import MetricsSampler
from hippo_brain.bench.output import AttemptRecord
from hippo_brain.bench.runner import run_model_main_pass, run_self_consistency_pass


@dataclass
class ModelRunResult:
    model: str
    attempts: list[AttemptRecord]
    per_event_vectors: list[list[list[float]]]
    peak_metrics: dict
    wall_clock_sec: int
    cooldown_timeout: bool


def _snapshot_fn(sampler: MetricsSampler):
    def fn() -> dict:
        s = sampler.latest()
        if s is None:
            return {}
        return {
            "lmstudio_rss_mb": s.lmstudio_rss_mb,
            "lmstudio_cpu_pct": s.lmstudio_cpu_pct,
            "load_avg_1m": s.load_avg_1m,
            "mem_free_mb": s.mem_free_mb,
        }

    return fn


def run_one_model(
    *,
    model: str,
    base_url: str,
    entries: list[CorpusEntry],
    sc_entries: list[CorpusEntry],
    runs_per_event: int,
    embedding_model: str,
    timeout_sec: int,
    warmup_calls: int,
    cooldown_max_sec: int,
    run_id: str,
    temperature: float = 0.7,
) -> ModelRunResult:
    lms.unload_all()
    time.sleep(1)
    lms.load(model)

    # Warmup.
    for _ in range(warmup_calls):
        try:
            call_enrichment(
                base_url=base_url,
                model=model,
                payload="warmup",
                source="shell",
                timeout_sec=timeout_sec,
                temperature=temperature,
            )
        except Exception:  # noqa: BLE001 — warmup failures don't block the run
            pass

    sampler = MetricsSampler(sample_interval_ms=250)
    sampler.start()
    start = time.monotonic()
    cooldown_timeout = False
    # Bind up-front so a mid-pass exception preserves whatever partial data
    # was collected. Otherwise an exception in main_pass leaves `attempts`
    # unbound and the ModelRunResult construction below crashes, losing
    # every attempt that succeeded before the failure.
    main_attempts: list[AttemptRecord] = []
    sc_attempts: list[AttemptRecord] = []
    per_event_vectors: list[list[list[float]]] = []
    try:
        main_attempts = run_model_main_pass(
            base_url=base_url,
            model=model,
            entries=entries,
            timeout_sec=timeout_sec,
            metrics_snapshot=_snapshot_fn(sampler),
            temperature=temperature,
            run_id=run_id,
        )
        sc_attempts, per_event_vectors = run_self_consistency_pass(
            base_url=base_url,
            model=model,
            entries=sc_entries,
            runs_per_event=runs_per_event,
            embedding_model=embedding_model,
            timeout_sec=timeout_sec,
            metrics_snapshot=_snapshot_fn(sampler),
            temperature=temperature,
            run_id=run_id,
        )
    finally:
        attempts = main_attempts + sc_attempts
        sampler.stop()
        wall_clock_sec = int(time.monotonic() - start)
        peak = sampler.peak()
        try:
            lms.unload(model)
        except lms.LmsError:
            pass

    # Cooldown is load-driven, NOT thermal. A hot thermal state with low load
    # can still throttle the next model's CPU/GPU, but we don't have a headless
    # signal for that on macOS (powermetrics requires sudo). Document honestly:
    # this loop waits for scheduling contention to drop, not for the SoC to cool.
    cooldown_start = time.monotonic()
    while time.monotonic() - cooldown_start < cooldown_max_sec:
        s = sampler._sample_once(None)  # ad-hoc probe; sampler already stopped
        if s.load_avg_1m < 2.0:
            break
        time.sleep(2)
    else:
        if cooldown_max_sec > 0:
            cooldown_timeout = True

    return ModelRunResult(
        model=model,
        attempts=attempts,
        per_event_vectors=per_event_vectors,
        peak_metrics=peak,
        wall_clock_sec=wall_clock_sec,
        cooldown_timeout=cooldown_timeout,
    )
