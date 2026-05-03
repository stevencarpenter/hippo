"""v2 JSONL record shapes. Imports RunWriter from output.py (unchanged)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from hippo_brain.bench.output import RunWriter  # reuse writer unchanged

__all__ = [
    "RunManifestRecordV2",
    "ModelSummaryRecordV2",
    "RunEndRecordV2",
    "RunWriter",
]


@dataclass
class RunManifestRecordV2:
    run_id: str
    started_at_iso: str
    host: dict[str, Any]
    preflight_checks: list[dict[str, Any]]
    candidate_models: list[str]
    finished_at_iso: str | None = None
    bench_version: str = "0.2.0"
    corpus_version: str = ""
    corpus_content_hash: str = ""
    gate_thresholds: dict[str, Any] = field(default_factory=dict)
    self_consistency_spec: dict[str, Any] = field(default_factory=dict)
    lmstudio_version: str | None = None
    corpus_schema_version: int = 0
    eval_qa_version: str = "eval-qa-v1"
    embedding_model: str = ""
    host_baseline: dict[str, Any] = field(default_factory=dict)
    prod_state_at_start: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"record_type": "run_manifest"}
        d.update(asdict(self))
        return d


@dataclass
class ModelSummaryRecordV2:
    run_id: str
    model: dict[str, Any]
    events_attempted: int
    attempts_total: int
    gates: dict[str, Any]
    system_peak: dict[str, Any]
    tier0_verdict: dict[str, Any]
    cooldown_timeout: bool = False
    process_ready_ms: int = 0
    queue_drain_wall_clock_sec: int = 0
    downstream_proxy: dict[str, Any] = field(default_factory=dict)
    prod_brain_restarted_during_bench: bool = False
    timeout_during_drain: bool = False
    # BT-04: structured capture of failures inside run_one_model_v2 that
    # previously got swallowed by `except Exception: pass`. Each entry has
    # {"step": <stage name>, "type": <exc class>, "error": <str(exc)>}.
    errors: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"record_type": "model_summary"}
        d.update(asdict(self))
        return d


@dataclass
class RunEndRecordV2:
    run_id: str
    finished_at_iso: str
    models_completed: list[str]
    models_errored: list[str]
    reason: str | None = None
    prod_brain_resumed_ok: bool = True
    models_with_prod_restart_event: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"record_type": "run_end"}
        d.update(asdict(self))
        return d
