"""Top-level orchestrator: pre-flight → pause prod → per-model coordinator → resume → JSONL."""

from __future__ import annotations

import atexit
import datetime as _dt
import json
import os
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar, cast

import psutil

from hippo_brain.bench import __version__
from hippo_brain.bench.coordinator import run_one_model
from hippo_brain.bench.output import (
    ModelSummaryRecord,
    RunEndRecord,
    RunManifestRecord,
    RunWriter,
)
from hippo_brain.bench.paths import (
    corpus_manifest_path,
    corpus_sqlite_path,
)
from hippo_brain.bench.pause_rpc import PauseRpcClient
from hippo_brain.bench.preflight import run_all_preflight
from hippo_brain.bench.prod_config import default_prod_brain_url
from hippo_brain.schema_version import EXPECTED_SCHEMA_VERSION

_T = TypeVar("_T")


@dataclass
class OrchestrationResult:
    run_id: str
    out_path: Path
    models_completed: list[str] = field(default_factory=list)
    models_errored: list[str] = field(default_factory=list)
    preflight_aborted: bool = False
    prod_brain_resumed_ok: bool = True


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


def _cpu_brand() -> str:
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
    except (OSError, subprocess.SubprocessError):  # fmt: skip
        pass
    return platform.processor() or "unknown"


def _lms_version() -> str | None:
    try:
        out = subprocess.run(
            ["lms", "--version"], capture_output=True, text=True, check=False, timeout=5
        )
    except (OSError, subprocess.SubprocessError):  # fmt: skip
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _read_manifest_field(manifest_path: Path, key: str, default: _T) -> _T:
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):  # fmt: skip
        return default
    if not isinstance(data, dict):
        return default
    value = data.get(key)
    if value is None:
        return default
    return cast(_T, value)


def orchestrate_run(
    *,
    candidate_models: list[str],
    corpus_version: str = "corpus-v2",
    corpus_sqlite: Path | None = None,
    manifest_path: Path | None = None,
    out_path: Path,
    brain_url: str | None = None,
    inference_url: str = "http://localhost:8000",
    embedding_model: str = "",
    drain_timeout_sec: float = 3600.0,
    skip_prod_pause: bool = False,
    dry_run: bool = False,
    skip_checks: bool = False,
) -> OrchestrationResult:
    """Top-level orchestration loop."""
    # Resolve brain_url at call time, not at import time, so the default
    # tracks the real prod-brain port (`[brain].port` from config.toml,
    # falling back to `DEFAULT_BRAIN_PORT`). A literal default at signature
    # level would freeze the wrong port and silently shadow `inference_url`
    # when both default to the same host:port.
    if brain_url is None:
        brain_url = default_prod_brain_url()

    run_id = _build_run_id()
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    if corpus_sqlite is None:
        corpus_sqlite = corpus_sqlite_path(corpus_version)
    if manifest_path is None:
        manifest_path = corpus_manifest_path(corpus_version)

    corpus_content_hash = _read_manifest_field(
        manifest_path, "corpus_content_hash", "sha256:unknown"
    )
    corpus_schema_version = _read_manifest_field(
        manifest_path, "schema_version", EXPECTED_SCHEMA_VERSION
    )

    try:
        load_1m, load_5m, _ = os.getloadavg()
    except (OSError, AttributeError):  # fmt: skip
        load_1m, load_5m = 0.0, 0.0

    host_baseline = {
        "load_avg_1m_at_start": load_1m,
        "load_avg_5m_at_start": load_5m,
    }

    pause_client = PauseRpcClient(brain_url, skip=skip_prod_pause or dry_run)

    prod_state_at_start: dict = {
        "brain_pid": None,
        "brain_paused": False,
        "daemon_pid": None,
        "daemon_running": False,
    }
    if not dry_run:
        health = pause_client.probe_health()
        if health:
            prod_state_at_start = {
                "brain_pid": health.get("pid"),
                "brain_paused": health.get("paused", False),
                "daemon_pid": None,
                "daemon_running": True,
            }

    writer = RunWriter(out_path)
    try:
        started_at_iso = _dt.datetime.now(tz=_dt.UTC).isoformat()

        if dry_run:
            manifest_record = RunManifestRecord(
                run_id=run_id,
                started_at_iso=started_at_iso,
                finished_at_iso=None,
                host=_host_info(),
                preflight_checks=[],
                candidate_models=list(candidate_models),
                bench_version=__version__,
                corpus_version=corpus_version,
                corpus_content_hash=corpus_content_hash,
                corpus_schema_version=corpus_schema_version,
                host_baseline=host_baseline,
                prod_state_at_start=prod_state_at_start,
            )
            writer.write_manifest(manifest_record)
            writer.write_run_end(
                RunEndRecord(
                    run_id=run_id,
                    finished_at_iso=_dt.datetime.now(tz=_dt.UTC).isoformat(),
                    models_completed=[],
                    models_errored=[],
                    reason="dry_run",
                )
            )
            return OrchestrationResult(
                run_id=run_id,
                out_path=out_path,
            )

        if not skip_checks:
            preflight_checks, aborted = run_all_preflight(
                brain_url=brain_url,
                corpus_sqlite=corpus_sqlite,
                manifest=manifest_path,
                inference_url=inference_url,
                skip_prod_pause=skip_prod_pause,
            )
        else:
            preflight_checks, aborted = [], False

        manifest_record = RunManifestRecord(
            run_id=run_id,
            started_at_iso=started_at_iso,
            finished_at_iso=None,
            host=_host_info(),
            preflight_checks=[c.to_dict() for c in preflight_checks],
            candidate_models=list(candidate_models),
            bench_version=__version__,
            corpus_version=corpus_version,
            corpus_content_hash=corpus_content_hash,
            corpus_schema_version=corpus_schema_version,
            embedding_model=embedding_model,
            host_baseline=host_baseline,
            prod_state_at_start=prod_state_at_start,
            lmstudio_version=_lms_version(),
        )
        writer.write_manifest(manifest_record)

        if aborted or not candidate_models:
            writer.write_run_end(
                RunEndRecord(
                    run_id=run_id,
                    finished_at_iso=_dt.datetime.now(tz=_dt.UTC).isoformat(),
                    models_completed=[],
                    models_errored=[],
                    reason="preflight_aborted" if aborted else "no_models",
                )
            )
            return OrchestrationResult(
                run_id=run_id,
                out_path=out_path,
                preflight_aborted=aborted,
            )

        atexit.register(pause_client.resume)
        if not skip_prod_pause:
            try:
                pause_client.pause()
            except Exception:  # noqa: BLE001
                pass

        completed: list[str] = []
        errored: list[str] = []
        models_with_prod_restart_event: list[str] = []

        for model in candidate_models:
            try:
                result = run_one_model(
                    model=model,
                    run_id=run_id,
                    corpus_sqlite=corpus_sqlite,
                    inference_url=inference_url
                    if inference_url.endswith("/v1")
                    else f"{inference_url.rstrip('/')}/v1",
                    embedding_model=embedding_model,
                    drain_timeout_sec=drain_timeout_sec,
                    prod_brain_url=brain_url,
                    skip_prod_pause=skip_prod_pause,
                )
            except Exception as e:  # noqa: BLE001 — per-model isolation
                writer.write_model_summary(
                    ModelSummaryRecord(
                        run_id=run_id,
                        model={"id": model},
                        events_attempted=0,
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

            for attempt in result.attempts:
                writer.write_attempt(attempt)

            if result.prod_brain_restarted_during_bench:
                models_with_prod_restart_event.append(model)

            writer.write_model_summary(
                ModelSummaryRecord(
                    run_id=run_id,
                    model={"id": model},
                    events_attempted=len(result.attempts),
                    attempts_total=len(result.attempts),
                    gates={},
                    system_peak={
                        **result.peak_metrics,
                        "wall_clock_sec": result.wall_clock_sec,
                    },
                    tier0_verdict={
                        "passed": True,
                        "failed_gates": [],
                        "skipped_gates": [],
                        "notes": [],
                    },
                    cooldown_timeout=result.cooldown_timeout,
                    process_ready_ms=result.process_ready_ms,
                    queue_drain_wall_clock_sec=result.queue_drain_wall_clock_sec,
                    downstream_proxy=result.downstream_proxy,
                    prod_brain_restarted_during_bench=result.prod_brain_restarted_during_bench,
                    timeout_during_drain=result.timeout_during_drain,
                    errors=result.errors,
                )
            )
            completed.append(model)

        prod_brain_resumed_ok = True
        try:
            pause_client.resume()
        except Exception:  # noqa: BLE001
            prod_brain_resumed_ok = False

        writer.write_run_end(
            RunEndRecord(
                run_id=run_id,
                finished_at_iso=_dt.datetime.now(tz=_dt.UTC).isoformat(),
                models_completed=list(completed),
                models_errored=list(errored),
                prod_brain_resumed_ok=prod_brain_resumed_ok,
                models_with_prod_restart_event=models_with_prod_restart_event,
            )
        )

        return OrchestrationResult(
            run_id=run_id,
            out_path=out_path,
            models_completed=completed,
            models_errored=errored,
            prod_brain_resumed_ok=prod_brain_resumed_ok,
        )
    finally:
        writer.close()
