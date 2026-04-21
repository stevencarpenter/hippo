"""BenchConfig dataclass + threshold defaults."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_THRESHOLDS: dict[str, float | int] = {
    "schema_validity_min": 0.95,
    "refusal_max": 0.0,
    "echo_similarity_max": 0.5,
    "latency_p95_max_ms": 60_000,
    "self_consistency_min": 0.7,
    "entity_sanity_min": 0.9,
}


@dataclass
class BenchConfig:
    corpus_version: str
    candidate_models: list[str]
    self_consistency_events: int
    self_consistency_runs_per_event: int
    latency_ceiling_sec: int
    thresholds: dict[str, float | int]
    fixture_path: Path
    out_path: Path
    skip_checks: bool
    warmup_calls: int = 3
    metrics_sample_interval_ms: int = 250
    cooldown_max_sec: int = 90

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus_version": self.corpus_version,
            "candidate_models": list(self.candidate_models),
            "self_consistency_events": self.self_consistency_events,
            "self_consistency_runs_per_event": self.self_consistency_runs_per_event,
            "latency_ceiling_sec": self.latency_ceiling_sec,
            "thresholds": dict(self.thresholds),
            "fixture_path": str(self.fixture_path),
            "out_path": str(self.out_path),
            "skip_checks": self.skip_checks,
            "warmup_calls": self.warmup_calls,
            "metrics_sample_interval_ms": self.metrics_sample_interval_ms,
            "cooldown_max_sec": self.cooldown_max_sec,
        }
