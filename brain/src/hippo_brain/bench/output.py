"""JSONL record shapes + writer for bench runs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RunManifestRecord:
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
    # Inference backend version. Currently only populated when the backend is
    # LM Studio (via `lms --version`); other backends (oMLX, ollama, vLLM)
    # leave this None until backend-specific probes are added.
    inference_backend_version: str | None = None
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
class AttemptRecord:
    run_id: str
    model: dict[str, Any]
    event: dict[str, Any]
    attempt_idx: int
    purpose: str  # "main" or "self_consistency"
    timestamps: dict[str, Any]
    raw_output: str
    parsed_output: dict | None
    gates: dict[str, Any]
    system_snapshot: dict[str, Any]
    timeout: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"record_type": "attempt"}
        d.update(asdict(self))
        return d


@dataclass
class ModelSummaryRecord:
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
    # BT-04: structured capture of failures inside run_one_model that
    # previously got swallowed by `except Exception: pass`. Each entry has
    # {"step": <stage name>, "type": <exc class>, "error": <str(exc)>}.
    errors: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"record_type": "model_summary"}
        d.update(asdict(self))
        return d


@dataclass
class RunEndRecord:
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


class RunWriter:
    """JSONL writer for a single bench run."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self.path.open("w", encoding="utf-8")

    def _write(self, obj: dict) -> None:
        self._f.write(json.dumps(obj, sort_keys=True))
        self._f.write("\n")
        self._f.flush()

    def write_manifest(self, r: RunManifestRecord) -> None:
        self._write(r.to_dict())

    def write_attempt(self, r: AttemptRecord) -> None:
        self._write(r.to_dict())

    def write_model_summary(self, r: ModelSummaryRecord) -> None:
        self._write(r.to_dict())

    def write_run_end(self, r: RunEndRecord) -> None:
        self._write(r.to_dict())

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> "RunWriter":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
