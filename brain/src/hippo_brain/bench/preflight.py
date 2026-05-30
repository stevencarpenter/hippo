"""Pre-flight checks for hippo-bench.

Verifies prod brain reachability + pauseability, corpus artifact integrity,
inference-backend reachability, bench-root disk space, and shadow-brain
port availability before per-model spawn.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from hippo_brain.bench.corpus import verify_corpus
from hippo_brain.bench.paths import bench_qa_path, hippo_bench_root
from hippo_brain.bench.qa import validate_qa_fixture
from hippo_brain.schema_version import EXPECTED_SCHEMA_VERSION

Status = Literal["pass", "warn", "fail"]


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str = ""

    def to_dict(self) -> dict:
        return {"check": self.name, "status": self.status, "detail": self.detail}


def check_inference_reachable(url: str) -> CheckResult:
    try:
        resp = httpx.get(url, timeout=5.0)
    except httpx.HTTPError as e:
        return CheckResult(
            name="inference_reachable",
            status="fail",
            detail=f"HTTP error contacting {url}: {e}",
        )
    if resp.status_code >= 400:
        return CheckResult(
            name="inference_reachable",
            status="fail",
            detail=f"got HTTP {resp.status_code}",
        )
    return CheckResult(name="inference_reachable", status="pass", detail=f"HTTP {resp.status_code}")


def check_disk_space(path: Path, min_gb: float = 2.0) -> CheckResult:
    disk_usage_path = path
    while not disk_usage_path.exists() and disk_usage_path.parent != disk_usage_path:
        disk_usage_path = disk_usage_path.parent
    usage = shutil.disk_usage(disk_usage_path)
    free_gb = usage.free / (1024**3)
    if free_gb < min_gb:
        return CheckResult(
            name="disk_free",
            status="fail",
            detail=f"only {free_gb:.2f} GB free (need {min_gb})",
        )
    return CheckResult(name="disk_free", status="pass", detail=f"{free_gb:.2f} GB free")


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


def check_corpus_present(corpus_sqlite: Path, manifest: Path) -> CheckResult:
    """Verify corpus artifacts exist and have the correct schema_version."""
    jsonl_path = corpus_sqlite.with_suffix(".jsonl")
    ok, reason = verify_corpus(corpus_sqlite, jsonl_path, manifest)
    if not ok:
        return CheckResult(name="corpus_present", status="fail", detail=reason)

    try:
        conn = sqlite3.connect(f"file:{corpus_sqlite}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT schema_version FROM corpus_meta").fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        return CheckResult(
            name="corpus_present", status="fail", detail=f"cannot read corpus_meta: {e}"
        )

    if row is None:
        return CheckResult(
            name="corpus_present",
            status="fail",
            detail="corpus_meta table missing or empty — rebuild corpus",
        )
    stored = row[0]
    if stored != EXPECTED_SCHEMA_VERSION:
        return CheckResult(
            name="corpus_present",
            status="fail",
            detail=(
                f"corpus schema version mismatch: corpus has schema_version={stored}, "
                f"live hippo has schema_version={EXPECTED_SCHEMA_VERSION}. "
                "Rebuild corpus with: hippo-bench corpus init --bump-version"
            ),
        )
    return CheckResult(name="corpus_present", status="pass", detail=f"schema_version={stored}")


def check_disk_free_bench(bench_root: Path, min_gb: float = 2.0) -> CheckResult:
    """Check that bench root has at least min_gb free. Delegates to check_disk_space."""
    result = check_disk_space(bench_root, min_gb=min_gb)
    return CheckResult(name="disk_free_bench", status=result.status, detail=result.detail)


def check_brain_port_free(port: int = 18923) -> CheckResult:
    """BT-07: refuse to start a shadow brain if its port is already listening.

    A port that's already in use almost always means the previous bench run
    leaked its shadow process group (BT-03 fixed the common path; this catches
    the residual cases — SIGKILL'd loops, manual ctrl-C escapes, dev-rebuild
    races). Without this guard the next spawn either binds elsewhere silently
    or hangs in wait_for_brain_ready until timeout.
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
    except OSError as e:
        # Try lsof for a friendlier error; tolerate its absence.
        offender = ""
        try:
            import subprocess

            r = subprocess.run(
                ["lsof", "-i", f":{port}"], capture_output=True, text=True, timeout=2.0
            )
            if r.stdout.strip():
                offender = f" — listener:\n{r.stdout.strip()}"
        except Exception:
            pass
        return CheckResult(
            name="brain_port_free",
            status="fail",
            detail=f"port {port} already in use ({e}){offender}",
        )
    finally:
        s.close()
    return CheckResult(name="brain_port_free", status="pass", detail=f"port {port} free")


def check_qa_scoreable(corpus_sqlite: Path, min_scoreable: int = 1) -> CheckResult:
    qa_path = bench_qa_path()
    if not qa_path.exists():
        return CheckResult(
            name="qa_scoreable",
            status="fail",
            detail=f"Q/A fixture missing: {qa_path}",
        )
    if not corpus_sqlite.exists():
        return CheckResult(
            name="qa_scoreable",
            status="warn",
            detail=f"corpus missing ({corpus_sqlite}); skipping QA scoreable check",
        )
    report = validate_qa_fixture(qa_path, corpus_sqlite, min_scoreable=min_scoreable)
    return CheckResult(
        name="qa_scoreable",
        status="pass" if report.passes else "fail",
        detail=report.detail,
    )


def run_all_preflight(
    brain_url: str,
    corpus_sqlite: Path,
    manifest: Path,
    inference_url: str,
    skip_prod_pause: bool,
    brain_port: int = 18923,
    min_scoreable_qa: int = 1,
) -> tuple[list[CheckResult], bool]:
    """Run all preflight checks. Returns (checks, aborted).

    aborted=True if any hard-fail condition fires:
    - corpus schema mismatch or missing
    - inference backend unreachable
    - disk < 2 GB under bench root
    - shadow brain port already in use (BT-07)
    - prod brain reachable AND not pauseable AND skip_prod_pause not set
    """
    reachable = check_prod_brain_reachable(brain_url)
    pauseable = check_prod_brain_pauseable(brain_url, skip=skip_prod_pause)
    corpus_check = check_corpus_present(corpus_sqlite, manifest)
    inference_check = check_inference_reachable(inference_url)
    bench_disk = check_disk_free_bench(hippo_bench_root())
    port_check = check_brain_port_free(brain_port)
    qa_check = check_qa_scoreable(corpus_sqlite, min_scoreable=min_scoreable_qa)

    checks = [
        reachable,
        pauseable,
        corpus_check,
        inference_check,
        bench_disk,
        port_check,
        qa_check,
    ]

    aborted = (
        corpus_check.status == "fail"
        or inference_check.status == "fail"
        or bench_disk.status == "fail"
        or port_check.status == "fail"
        or qa_check.status == "fail"
        or (reachable.status == "pass" and pauseable.status == "fail" and not skip_prod_pause)
    )

    return checks, aborted
