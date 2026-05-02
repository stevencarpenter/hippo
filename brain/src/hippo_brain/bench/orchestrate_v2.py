"""Top-level v2 orchestrator: pre-flight → pause prod → per-model coordinator → resume → JSONL."""

from __future__ import annotations

import atexit
import datetime as _dt
import json
import os
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from hippo_brain.bench.coordinator_v2 import run_one_model_v2
from hippo_brain.bench.output_v2 import (
    ModelSummaryRecordV2,
    RunEndRecordV2,
    RunManifestRecordV2,
    RunWriter,
)
from hippo_brain.bench.paths import (
    corpus_v2_manifest_path,
    corpus_v2_sqlite_path,
)
from hippo_brain.bench.pause_rpc import PauseRpcClient
from hippo_brain.bench.preflight_v2 import run_all_preflight_v2
from hippo_brain.schema_version import EXPECTED_SCHEMA_VERSION


@dataclass
class OrchestrationResultV2:
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
    except OSError, subprocess.SubprocessError:
        pass
    return platform.processor() or "unknown"


def _lms_version() -> str | None:
    try:
        out = subprocess.run(
            ["lms", "--version"], capture_output=True, text=True, check=False, timeout=5
        )
    except OSError, subprocess.SubprocessError:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _read_manifest_field(manifest_path: Path, key: str, default=None):
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return data.get(key, default)
    except FileNotFoundError, json.JSONDecodeError:
        return default


def orchestrate_run_v2(
    *,
    candidate_models: list[str],
    corpus_version: str = "corpus-v2",
    corpus_sqlite: Path | None = None,
    manifest_path: Path | None = None,
    out_path: Path,
    brain_url: str = "http://localhost:8000",
    lmstudio_url: str = "http://localhost:1234",
    embedding_model: str = "",
    drain_timeout_sec: float = 3600.0,
    skip_prod_pause: bool = False,
    dry_run: bool = False,
    skip_checks: bool = False,
) -> OrchestrationResultV2:
    """Top-level v2 orchestration loop."""
    run_id = _build_run_id()
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    if corpus_sqlite is None:
        corpus_sqlite = corpus_v2_sqlite_path()
    if manifest_path is None:
        manifest_path = corpus_v2_manifest_path()

    corpus_content_hash = _read_manifest_field(
        manifest_path, "corpus_content_hash", "sha256:unknown"
    )
    corpus_schema_version = _read_manifest_field(
        manifest_path, "schema_version", EXPECTED_SCHEMA_VERSION
    )

    try:
        load_1m, load_5m, _ = os.getloadavg()
    except OSError, AttributeError:
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
            manifest_record = RunManifestRecordV2(
                run_id=run_id,
                started_at_iso=started_at_iso,
                finished_at_iso=None,
                host=_host_info(),
                preflight_checks=[],
                candidate_models=list(candidate_models),
                bench_version="0.2.0",
                corpus_version=corpus_version,
                corpus_content_hash=corpus_content_hash,
                corpus_schema_version=corpus_schema_version,
                host_baseline=host_baseline,
                prod_state_at_start=prod_state_at_start,
            )
            writer._write(manifest_record.to_dict())
            writer._write(
                RunEndRecordV2(
                    run_id=run_id,
                    finished_at_iso=_dt.datetime.now(tz=_dt.UTC).isoformat(),
                    models_completed=[],
                    models_errored=[],
                    reason="dry_run",
                ).to_dict()
            )
            return OrchestrationResultV2(
                run_id=run_id,
                out_path=out_path,
            )

        if not skip_checks:
            preflight_checks, aborted = run_all_preflight_v2(
                brain_url=brain_url,
                corpus_sqlite=corpus_sqlite,
                manifest=manifest_path,
                lmstudio_url=lmstudio_url,
                skip_prod_pause=skip_prod_pause,
            )
        else:
            preflight_checks, aborted = [], False

        manifest_record = RunManifestRecordV2(
            run_id=run_id,
            started_at_iso=started_at_iso,
            finished_at_iso=None,
            host=_host_info(),
            preflight_checks=[c.to_dict() for c in preflight_checks],
            candidate_models=list(candidate_models),
            bench_version="0.2.0",
            corpus_version=corpus_version,
            corpus_content_hash=corpus_content_hash,
            corpus_schema_version=corpus_schema_version,
            embedding_model=embedding_model,
            host_baseline=host_baseline,
            prod_state_at_start=prod_state_at_start,
            lmstudio_version=_lms_version(),
        )
        writer._write(manifest_record.to_dict())

        if aborted or not candidate_models:
            writer._write(
                RunEndRecordV2(
                    run_id=run_id,
                    finished_at_iso=_dt.datetime.now(tz=_dt.UTC).isoformat(),
                    models_completed=[],
                    models_errored=[],
                    reason="preflight_aborted" if aborted else "no_models",
                ).to_dict()
            )
            return OrchestrationResultV2(
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
                result = run_one_model_v2(
                    model=model,
                    run_id=run_id,
                    corpus_sqlite=corpus_sqlite,
                    lmstudio_url=lmstudio_url
                    if lmstudio_url.endswith("/v1")
                    else f"{lmstudio_url.rstrip('/')}/v1",
                    embedding_model=embedding_model,
                    drain_timeout_sec=drain_timeout_sec,
                    prod_brain_url=brain_url,
                    skip_prod_pause=skip_prod_pause,
                )
            except Exception as e:  # noqa: BLE001 — per-model isolation
                writer._write(
                    ModelSummaryRecordV2(
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
                            "notes": [f"run_one_model_v2 raised: {type(e).__name__}: {e}"],
                        },
                    ).to_dict()
                )
                errored.append(model)
                continue

            for attempt in result.attempts:
                writer._write(attempt.to_dict())

            if result.prod_brain_restarted_during_bench:
                models_with_prod_restart_event.append(model)

            writer._write(
                ModelSummaryRecordV2(
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
                ).to_dict()
            )
            completed.append(model)

        prod_brain_resumed_ok = True
        try:
            pause_client.resume()
        except Exception:  # noqa: BLE001
            prod_brain_resumed_ok = False

        writer._write(
            RunEndRecordV2(
                run_id=run_id,
                finished_at_iso=_dt.datetime.now(tz=_dt.UTC).isoformat(),
                models_completed=list(completed),
                models_errored=list(errored),
                prod_brain_resumed_ok=prod_brain_resumed_ok,
                models_with_prod_restart_event=models_with_prod_restart_event,
            ).to_dict()
        )

        return OrchestrationResultV2(
            run_id=run_id,
            out_path=out_path,
            models_completed=completed,
            models_errored=errored,
            prod_brain_resumed_ok=prod_brain_resumed_ok,
        )
    finally:
        writer.close()
