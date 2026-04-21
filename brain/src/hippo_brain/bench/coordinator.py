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
            "load_avg_5s": s.load_avg_5s,
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
            )
        except Exception:  # noqa: BLE001 — warmup failures don't block the run
            pass

    sampler = MetricsSampler(sample_interval_ms=250)
    sampler.start()
    start = time.monotonic()
    cooldown_timeout = False
    try:
        main_attempts = run_model_main_pass(
            base_url=base_url,
            model=model,
            entries=entries,
            timeout_sec=timeout_sec,
            metrics_snapshot=_snapshot_fn(sampler),
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
            run_id=run_id,
        )
        attempts = main_attempts + sc_attempts
    finally:
        sampler.stop()
        wall_clock_sec = int(time.monotonic() - start)
        peak = sampler.peak()
        try:
            lms.unload(model)
        except lms.LmsError:
            pass

    cooldown_start = time.monotonic()
    while time.monotonic() - cooldown_start < cooldown_max_sec:
        s = sampler._sample_once(None)  # ad-hoc probe
        if s.load_avg_5s < 2.0:
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
