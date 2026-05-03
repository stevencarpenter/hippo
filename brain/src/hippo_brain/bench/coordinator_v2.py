"""Per-model v2 lifecycle:
unload → load → copy corpus → spawn shadow stack → warmup → timed drain →
downstream-proxy pass → self-consistency pass → teardown → cooldown.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import random
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from hippo_brain.bench import lms
from hippo_brain.bench.corpus import CorpusEntry, load_corpus
from hippo_brain.bench.downstream_proxy import (
    load_qa_items,
    run_downstream_proxy_pass,
)
from hippo_brain.bench.enrich_call import call_enrichment
from hippo_brain.bench.metrics import MetricsSampler
from hippo_brain.bench.output import AttemptRecord
from hippo_brain.bench.paths import bench_qa_path, bench_run_tree
from hippo_brain.bench.pause_rpc import PauseRpcClient
from hippo_brain.bench.runner import run_self_consistency_pass
from hippo_brain.bench.shadow_stack import (
    spawn_shadow_stack,
    teardown_shadow_stack,
    wait_for_brain_ready,
)

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class ModelRunResultV2:
    model: str
    attempts: list[AttemptRecord]
    per_event_vectors: list[list[list[float]]]
    peak_metrics: dict[str, Any]
    wall_clock_sec: int
    cooldown_timeout: bool
    process_ready_ms: int
    queue_drain_wall_clock_sec: int
    downstream_proxy: dict[str, Any]
    prod_brain_restarted_during_bench: bool
    timeout_during_drain: bool
    # BT-04: structured capture of failures previously swallowed by
    # `except Exception: pass`. Plumbed into ModelSummaryRecordV2.errors.
    errors: list[dict[str, str]] = dataclasses.field(default_factory=list)


def _wait_for_queue_drain(
    bench_db: Path,
    drain_timeout_sec: float = 3600.0,
    poll_interval_sec: float = 2.0,
) -> bool:
    """Poll enrichment queue tables until empty or timeout.

    Returns True if timeout was hit, False if successfully drained.

    BT-05: Raises RuntimeError if NONE of the expected queue tables exist on
    the very first poll — a schema mismatch must fail fast, not be reported
    as "drained instantly". The previous behavior (warn once, return False)
    masked schema bumps as bench success and recorded near-zero throughput.
    """
    tables = [
        "enrichment_queue",
        "claude_enrichment_queue",
        "browser_enrichment_queue",
        "workflow_enrichment_queue",
    ]

    start = time.time()
    consecutive_empty = 0
    schema_checked = False

    while time.time() - start < drain_timeout_sec:
        total_pending = 0
        tables_found = 0
        try:
            # BT-08: contextlib.closing + busy_timeout. Previously this opened a
            # fresh connection per poll (~0.5 Hz × 1 hr = 1800 connections)
            # without a try/finally close — on long drains this exhausted the
            # default macOS 256-fd-per-process limit. WAL+busy_timeout match
            # the rest of the codebase and tolerate brief contention with the
            # shadow brain's writer.
            with contextlib.closing(sqlite3.connect(str(bench_db), timeout=5.0)) as conn:
                conn.execute("PRAGMA busy_timeout = 5000")
                for table in tables:
                    try:
                        row = conn.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE status IN ('pending', 'processing')"
                        ).fetchone()
                        if row:
                            total_pending += row[0]
                        tables_found += 1
                    except sqlite3.OperationalError:
                        pass

            # Fail fast on schema mismatch before declaring success.
            if not schema_checked:
                if tables_found == 0:
                    raise RuntimeError(
                        f"_wait_for_queue_drain: no queue tables present in {bench_db} — "
                        "schema mismatch, refusing to declare drained. Expected at least one of: "
                        f"{tables}"
                    )
                schema_checked = True

            if total_pending == 0:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    return False
            else:
                consecutive_empty = 0
        except sqlite3.OperationalError as e:
            # Transient sqlite errors (e.g. WAL contention with the brain
            # writer) are not schema mismatches — keep polling.
            logger.warning("transient sqlite error in queue drain: %s", e)

        time.sleep(poll_interval_sec)

    return True


def _collect_event_ids_from_db(bench_db: Path) -> set[str]:
    """Collect all event IDs from bench DB corpus."""
    event_ids = set()

    try:
        conn = sqlite3.connect(str(bench_db))

        try:
            shell_rows = conn.execute("SELECT id FROM events").fetchall()
            event_ids.update(f"shell-{row[0]}" for row in shell_rows if row[0])
        except sqlite3.OperationalError:
            pass

        try:
            claude_rows = conn.execute("SELECT id FROM claude_sessions").fetchall()
            event_ids.update(f"claude-{row[0]}" for row in claude_rows if row[0])
        except sqlite3.OperationalError:
            pass

        try:
            browser_rows = conn.execute("SELECT id FROM browser_events").fetchall()
            event_ids.update(f"browser-{row[0]}" for row in browser_rows if row[0])
        except sqlite3.OperationalError:
            pass

        try:
            workflow_rows = conn.execute("SELECT id FROM workflow_runs").fetchall()
            event_ids.update(f"workflow-{row[0]}" for row in workflow_rows if row[0])
        except sqlite3.OperationalError:
            pass

        conn.close()
    except Exception:
        pass

    return event_ids


def _load_corpus_entries(corpus_sqlite: Path) -> list[CorpusEntry]:
    """Load CorpusEntry objects from the JSONL sidecar next to the SQLite snapshot."""
    corpus_jsonl = corpus_sqlite.with_suffix(".jsonl")
    if not corpus_jsonl.exists():
        return []
    try:
        return list(load_corpus(corpus_jsonl))
    except Exception:
        return []


def _metrics_snapshot_fn(sampler: MetricsSampler):
    """Return a zero-arg callable that yields the current sampler snapshot as a dict."""

    def _snap() -> dict:
        s = sampler.latest()
        if s is None:
            return {}
        return {
            "lmstudio_rss_mb": s.lmstudio_rss_mb,
            "lmstudio_cpu_pct": s.lmstudio_cpu_pct,
            "load_avg_1m": s.load_avg_1m,
            "mem_free_mb": s.mem_free_mb,
        }

    return _snap


def run_one_model_v2(
    *,
    model: str,
    run_id: str,
    corpus_sqlite: Path,
    embedding_fn=None,
    lmstudio_url: str = "http://localhost:1234/v1",
    embedding_model: str = "",
    drain_timeout_sec: float = 3600.0,
    warmup_calls: int = 3,
    sc_events: int = 5,
    sc_runs: int = 5,
    temperature: float = 0.7,
    timeout_sec: int = 120,
    cooldown_max_sec: int = 90,
    prod_brain_url: str = "http://localhost:8000",
    skip_prod_pause: bool = False,
) -> ModelRunResultV2:
    """Per-model v2 lifecycle.

    BT-03: spawn → teardown is wrapped in try/finally so a raise anywhere in
    the body still calls teardown_shadow_stack. Without this, model N's
    failure leaks the shadow process group; model N+1's spawn races on the
    fixed brain port and either succeeds against stale data or hangs.
    """
    start_time = time.time()

    # 1. Unload all, load target model
    lms.unload_all()
    time.sleep(1)
    lms.load(model)

    # 2. Create run tree and copy corpus
    run_tree = bench_run_tree(run_id, model, create=True)
    bench_db = run_tree / "hippo.db"
    shutil.copy2(corpus_sqlite, bench_db)

    stack = None
    sampler: MetricsSampler | None = None
    process_ready_ms = 0
    queue_drain_wall_clock_sec = 0
    timeout_during_drain = False
    prod_brain_restarted_during_bench = False
    downstream_proxy: dict[str, Any] = {}
    attempts: list[AttemptRecord] = []
    per_event_vectors: list[list[list[float]]] = []
    # BT-04: capture failures inside the body that would otherwise be
    # swallowed by `except Exception: pass`. Plumbed through to the JSONL
    # ModelSummaryRecordV2.errors so a silently-zero downstream_proxy or
    # missing SC pass shows up in the run output instead of looking clean.
    errors: list[dict[str, str]] = []

    def _capture(step: str, exc: Exception) -> None:
        logger.exception("BT-04: %s failed: %s", step, exc)
        errors.append({"step": step, "type": type(exc).__name__, "error": str(exc)})

    try:
        # 3. Spawn shadow stack
        stack = spawn_shadow_stack(
            run_tree=run_tree,
            run_id=run_id,
            model_id=model,
            corpus_version="corpus-v2",
            embedding_model=embedding_model or "embed-model",
            brain_port=18923,
            otel_enabled=False,
        )

        # 4. Wait for brain ready and record process_ready_ms
        process_ready_ms = int(wait_for_brain_ready(stack) * 1000)

        # 5. Warmup — direct calls to LM Studio to prime the model before the timed window
        all_entries = _load_corpus_entries(corpus_sqlite)
        rng = random.Random(42)
        if all_entries and warmup_calls > 0:
            warmup_pool = all_entries[: min(20, len(all_entries))]
            warmup_entries = rng.sample(warmup_pool, min(warmup_calls, len(warmup_pool)))
            for entry in warmup_entries:
                try:
                    call_enrichment(
                        base_url=lmstudio_url,
                        model=model,
                        payload=entry.redacted_content,
                        source=entry.source,
                        timeout_sec=timeout_sec,
                        temperature=temperature,
                    )
                except Exception as e:
                    _capture(f"warmup:{entry.source}", e)

        # 6. Start metrics sampler
        sampler = MetricsSampler(sample_interval_ms=250)
        sampler.start()

        # 7. Wait for main queue drain (shadow brain drains naturally)
        drain_start = time.time()
        timeout_during_drain = _wait_for_queue_drain(bench_db, drain_timeout_sec)
        queue_drain_wall_clock_sec = int(time.time() - drain_start)

        # 8. Poll prod brain health every 120s to detect an unexpected restart
        pause_client = PauseRpcClient(prod_brain_url, skip=skip_prod_pause)
        initial_health = pause_client.probe_health()
        was_paused = bool(initial_health and initial_health.get("paused", False))
        # Re-probe only if drain took long enough to be worth checking
        if queue_drain_wall_clock_sec > 120:
            health = pause_client.probe_health()
            if health and not health.get("paused", False) and was_paused:
                prod_brain_restarted_during_bench = True

        # 9. Run downstream-proxy pass
        try:
            event_ids = _collect_event_ids_from_db(bench_db)
            if embedding_fn:
                qa_path = bench_qa_path()
                if qa_path.exists():
                    included_qa, _ = load_qa_items(qa_path, event_ids)
                    if included_qa:
                        conn = sqlite3.connect(str(bench_db))
                        downstream_proxy = run_downstream_proxy_pass(
                            conn,
                            included_qa,
                            embedding_fn,
                        )
                        conn.close()
        except Exception as e:
            _capture("downstream_proxy", e)

        # 10. Self-consistency pass — 5 events × N runs via direct LM Studio calls
        if all_entries and sc_events > 0 and sc_runs > 0:
            sc_pool = rng.sample(all_entries, min(sc_events, len(all_entries)))
            try:
                sc_attempts, sc_vecs = run_self_consistency_pass(
                    base_url=lmstudio_url,
                    model=model,
                    entries=sc_pool,
                    runs_per_event=sc_runs,
                    embedding_model=embedding_model,
                    timeout_sec=timeout_sec,
                    metrics_snapshot=_metrics_snapshot_fn(sampler),
                    temperature=temperature,
                    run_id=run_id,
                )
                attempts.extend(sc_attempts)
                per_event_vectors.extend(sc_vecs)
            except Exception as e:
                _capture("self_consistency", e)

    finally:
        # 11. Teardown — runs even if any step above raised so we never leak
        # shadow processes or leave the metrics sampler running.
        if sampler is not None:
            try:
                sampler.stop()
            except Exception:
                logger.exception("BT-03: sampler.stop failed during teardown")
        if stack is not None:
            try:
                teardown_shadow_stack(stack)
            except Exception:
                logger.exception(
                    "BT-03: teardown_shadow_stack failed — manual cleanup may be required"
                )

    # 12. Cooldown
    cooldown_start = time.time()
    cooldown_timeout = False
    while time.time() - cooldown_start < cooldown_max_sec:
        try:
            load_1m = os.getloadavg()[0]
            if load_1m < 2.0:
                break
        except Exception:
            break
        time.sleep(1)
    else:
        cooldown_timeout = True

    wall_clock_sec = int(time.time() - start_time)

    return ModelRunResultV2(
        model=model,
        attempts=attempts,
        per_event_vectors=per_event_vectors,
        peak_metrics=(sampler.peak() if sampler is not None else None) or {},
        wall_clock_sec=wall_clock_sec,
        cooldown_timeout=cooldown_timeout,
        process_ready_ms=process_ready_ms,
        queue_drain_wall_clock_sec=queue_drain_wall_clock_sec,
        downstream_proxy=downstream_proxy,
        prod_brain_restarted_during_bench=prod_brain_restarted_during_bench,
        timeout_during_drain=timeout_during_drain,
        errors=errors,
    )
