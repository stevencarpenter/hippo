"""v2 pre-flight checks for hippo-bench.

Extends the v1 check set with prod-brain pause, corpus-v2 artifact
verification, and bench-specific disk check.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx

from hippo_brain.bench.corpus_v2 import verify_corpus_v2
from hippo_brain.bench.paths import hippo_bench_root
from hippo_brain.bench.preflight import CheckResult, check_disk_space, check_lmstudio_reachable
from hippo_brain.schema_version import EXPECTED_SCHEMA_VERSION


def check_prod_brain_reachable(brain_url: str) -> CheckResult:
    """GET /health on the prod brain. 200=pass (with PID), refused=warn, error=fail."""
    try:
        resp = httpx.get(f"{brain_url.rstrip('/')}/health", timeout=5.0)
    except httpx.ConnectError:
        return CheckResult(
            name="prod_brain_reachable",
            status="warn",
            detail=f"connection refused — prod brain not running at {brain_url}",
        )
    except httpx.HTTPError as e:
        return CheckResult(
            name="prod_brain_reachable",
            status="fail",
            detail=f"HTTP error contacting {brain_url}: {e}",
        )
    if resp.status_code == 200:
        data = resp.json()
        pid = data.get("pid", "unknown")
        return CheckResult(name="prod_brain_reachable", status="pass", detail=f"pid={pid}")
    return CheckResult(
        name="prod_brain_reachable",
        status="fail",
        detail=f"HTTP {resp.status_code}",
    )


def check_prod_brain_pauseable(brain_url: str, skip: bool) -> CheckResult:
    """Probe /health to verify the brain exposes the pause capability.

    This is intentionally read-only — the actual POST /control/pause is sent
    by the orchestrator after all preflight checks pass.  Sending a real pause
    here would leave prod stuck if a later check (corpus, disk) aborts the run.
    """
    if skip:
        return CheckResult(
            name="prod_brain_pauseable",
            status="pass",
            detail="skipped (--skip-prod-pause)",
        )
    try:
        resp = httpx.get(f"{brain_url.rstrip('/')}/health", timeout=5.0)
    except httpx.ConnectError:
        return CheckResult(
            name="prod_brain_pauseable",
            status="warn",
            detail="connection refused — prod brain not running; pause support unknown",
        )
    except httpx.HTTPError as e:
        return CheckResult(name="prod_brain_pauseable", status="fail", detail=f"HTTP error: {e}")
    if resp.status_code == 200:
        data = resp.json()
        if "paused" in data:
            return CheckResult(
                name="prod_brain_pauseable",
                status="pass",
                detail="pause endpoint available (health reports paused field)",
            )
        return CheckResult(
            name="prod_brain_pauseable",
            status="warn",
            detail="health response missing 'paused' field — brain may not support pause",
        )
    return CheckResult(
        name="prod_brain_pauseable",
        status="fail",
        detail=f"health check failed: HTTP {resp.status_code}",
    )


def check_corpus_v2_present(corpus_sqlite: Path, manifest: Path) -> CheckResult:
    """Verify corpus-v2 artifacts exist and have the correct schema_version."""
    jsonl_path = corpus_sqlite.with_suffix(".jsonl")
    ok, reason = verify_corpus_v2(corpus_sqlite, jsonl_path, manifest)
    if not ok:
        return CheckResult(name="corpus_v2_present", status="fail", detail=reason)

    try:
        conn = sqlite3.connect(f"file:{corpus_sqlite}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT schema_version FROM corpus_meta").fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        return CheckResult(
            name="corpus_v2_present", status="fail", detail=f"cannot read corpus_meta: {e}"
        )

    if row is None:
        return CheckResult(
            name="corpus_v2_present",
            status="fail",
            detail="corpus_meta table missing or empty — rebuild corpus",
        )
    stored = row[0]
    if stored != EXPECTED_SCHEMA_VERSION:
        return CheckResult(
            name="corpus_v2_present",
            status="fail",
            detail=(
                f"corpus schema version mismatch: corpus has schema_version={stored}, "
                f"live hippo has schema_version={EXPECTED_SCHEMA_VERSION}. "
                "Rebuild corpus with: hippo-bench corpus init --bump-version"
            ),
        )
    return CheckResult(name="corpus_v2_present", status="pass", detail=f"schema_version={stored}")


def check_disk_free_bench(bench_root: Path, min_gb: float = 2.0) -> CheckResult:
    """Check that bench root has at least min_gb free. Delegates to check_disk_space."""
    result = check_disk_space(bench_root, min_gb=min_gb)
    return CheckResult(name="disk_free_bench", status=result.status, detail=result.detail)


def run_all_preflight_v2(
    brain_url: str,
    corpus_sqlite: Path,
    manifest: Path,
    lmstudio_url: str,
    skip_prod_pause: bool,
) -> tuple[list[CheckResult], bool]:
    """Run all v2 preflight checks. Returns (checks, aborted).

    aborted=True if any hard-fail condition fires:
    - corpus schema mismatch or missing
    - LM Studio unreachable
    - disk < 2 GB under bench root
    - prod brain reachable AND not pauseable AND skip_prod_pause not set
    """
    reachable = check_prod_brain_reachable(brain_url)
    pauseable = check_prod_brain_pauseable(brain_url, skip=skip_prod_pause)
    corpus_check = check_corpus_v2_present(corpus_sqlite, manifest)
    lms_check = check_lmstudio_reachable(lmstudio_url)
    bench_disk = check_disk_free_bench(hippo_bench_root())

    checks = [reachable, pauseable, corpus_check, lms_check, bench_disk]

    aborted = (
        corpus_check.status == "fail"
        or lms_check.status == "fail"
        or bench_disk.status == "fail"
        or (reachable.status == "pass" and pauseable.status == "fail" and not skip_prod_pause)
    )

    return checks, aborted
