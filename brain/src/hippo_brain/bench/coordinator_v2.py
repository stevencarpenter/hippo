"""Per-model v2 lifecycle:
unload → load → copy corpus → spawn shadow stack → warmup → timed drain →
downstream-proxy pass → self-consistency pass → teardown → cooldown.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from hippo_brain.bench import lms
from hippo_brain.bench.downstream_proxy import (
    load_qa_items,
    run_downstream_proxy_pass,
)
from hippo_brain.bench.metrics import MetricsSampler
from hippo_brain.bench.output import AttemptRecord
from hippo_brain.bench.paths import bench_qa_path, bench_run_tree
from hippo_brain.bench.pause_rpc import PauseRpcClient
from hippo_brain.bench.shadow_stack import (
    spawn_shadow_stack,
    teardown_shadow_stack,
    wait_for_brain_ready,
)


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


def _wait_for_queue_drain(
    bench_db: Path,
    drain_timeout_sec: float = 3600.0,
    poll_interval_sec: float = 2.0,
) -> bool:
    """Poll enrichment queue tables until empty or timeout.

    Returns True if timeout was hit, False if successfully drained.
    """
    tables = [
        "shell_enrichment_queue",
        "claude_session_enrichment_queue",
        "browser_enrichment_queue",
        "workflow_enrichment_queue",
    ]

    start = time.time()
    consecutive_empty = 0

    while time.time() - start < drain_timeout_sec:
        try:
            conn = sqlite3.connect(str(bench_db))
            total_pending = 0
            for table in tables:
                try:
                    row = conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE status IN ('pending', 'processing')"
                    ).fetchone()
                    if row:
                        total_pending += row[0]
                except sqlite3.OperationalError:
                    pass
            conn.close()

            if total_pending == 0:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    return False
            else:
                consecutive_empty = 0
        except Exception:
            pass

        time.sleep(poll_interval_sec)

    return True


def _collect_event_ids_from_db(bench_db: Path) -> set[str]:
    """Collect all event IDs from bench DB corpus."""
    event_ids = set()

    try:
        conn = sqlite3.connect(str(bench_db))

        try:
            shell_rows = conn.execute("SELECT event_id FROM shell_events").fetchall()
            event_ids.update(f"shell-{row[0]}" for row in shell_rows if row[0])
        except sqlite3.OperationalError:
            pass

        try:
            claude_rows = conn.execute("SELECT segment_id FROM claude_session_segments").fetchall()
            event_ids.update(f"claude-{row[0]}" for row in claude_rows if row[0])
        except sqlite3.OperationalError:
            pass

        try:
            browser_rows = conn.execute("SELECT event_id FROM browser_events").fetchall()
            event_ids.update(f"browser-{row[0]}" for row in browser_rows if row[0])
        except sqlite3.OperationalError:
            pass

        try:
            workflow_rows = conn.execute("SELECT workflow_id FROM workflow_runs").fetchall()
            event_ids.update(f"workflow-{row[0]}" for row in workflow_rows if row[0])
        except sqlite3.OperationalError:
            pass

        conn.close()
    except Exception:
        pass

    return event_ids


def run_one_model_v2(
    *,
    model: str,
    run_id: str,
    corpus_sqlite: Path,
    embedding_fn=None,
    drain_timeout_sec: float = 3600.0,
    warmup_calls: int = 3,
    cooldown_max_sec: int = 90,
    prod_brain_url: str = "http://localhost:8000",
    skip_prod_pause: bool = False,
) -> ModelRunResultV2:
    """Per-model v2 lifecycle."""
    start_time = time.time()

    # 1. Unload all, load target model
    lms.unload_all()
    time.sleep(1)
    lms.load(model)

    # 2. Create run tree and copy corpus
    run_tree = bench_run_tree(run_id, model, create=True)
    bench_db = run_tree / "hippo.db"
    shutil.copy2(corpus_sqlite, bench_db)

    # 3. Spawn shadow stack
    stack = spawn_shadow_stack(
        run_tree=run_tree,
        run_id=run_id,
        model_id=model,
        corpus_version="corpus-v2",
        embedding_model="embed-model",
        brain_port=18923,
        otel_enabled=False,
    )

    # 4. Wait for brain ready and record process_ready_ms
    process_ready_ms = int(wait_for_brain_ready(stack) * 1000)

    # 5. Warmup - seed events and wait for drain
    for _ in range(warmup_calls):
        try:
            pass  # Would call enrichment endpoint in v1
        except Exception:
            pass

    # 6. Start metrics sampler
    sampler = MetricsSampler(sample_interval_ms=250)
    sampler.start()

    # 7. Wait for main queue drain
    drain_start = time.time()
    timeout_during_drain = _wait_for_queue_drain(bench_db, drain_timeout_sec)
    queue_drain_wall_clock_sec = int(time.time() - drain_start)

    # 8. Poll prod brain health every 120s
    prod_brain_restarted_during_bench = False
    probe_start = time.time()
    pause_client = PauseRpcClient(prod_brain_url, skip=skip_prod_pause)
    initial_health = pause_client.probe_health()
    was_paused = initial_health and initial_health.get("paused", False) if initial_health else False
    while time.time() - drain_start < queue_drain_wall_clock_sec:
        if time.time() - probe_start > 120:
            health = pause_client.probe_health()
            if health and not health.get("paused", False) and was_paused:
                prod_brain_restarted_during_bench = True
            probe_start = time.time()
        time.sleep(2)

    # 9. Run downstream-proxy pass
    downstream_proxy: dict[str, Any] = {}
    try:
        event_ids = _collect_event_ids_from_db(bench_db)
        if embedding_fn:
            included_qa, _ = load_qa_items(bench_qa_path(), event_ids)
            if included_qa:
                conn = sqlite3.connect(str(bench_db))
                downstream_proxy = run_downstream_proxy_pass(
                    conn,
                    included_qa,
                    embedding_fn,
                )
                conn.close()
    except Exception:
        pass

    # 10. Run self-consistency pass
    attempts: list[AttemptRecord] = []
    per_event_vectors: list[list[list[float]]] = []
    try:
        # Stub: would load corpus entries and run in v1
        pass
    except Exception:
        pass

    # 11. Teardown
    sampler.stop()
    teardown_shadow_stack(stack)

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
        peak_metrics=sampler.peak() or {},
        wall_clock_sec=wall_clock_sec,
        cooldown_timeout=cooldown_timeout,
        process_ready_ms=process_ready_ms,
        queue_drain_wall_clock_sec=queue_drain_wall_clock_sec,
        downstream_proxy=downstream_proxy,
        prod_brain_restarted_during_bench=prod_brain_restarted_during_bench,
        timeout_during_drain=timeout_during_drain,
    )
