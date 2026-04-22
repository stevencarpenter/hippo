"""Individual pre-flight checks for bench hygiene."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

Status = Literal["pass", "warn", "fail"]


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str = ""

    def to_dict(self) -> dict:
        return {"check": self.name, "status": self.status, "detail": self.detail}


def check_lms_cli() -> CheckResult:
    path = shutil.which("lms")
    if path is None:
        return CheckResult(
            name="lms_cli",
            status="fail",
            detail="lms CLI missing. Install LM Studio and run `lms --version`.",
        )
    return CheckResult(name="lms_cli", status="pass", detail=path)


def check_lmstudio_reachable(url: str) -> CheckResult:
    try:
        resp = httpx.get(url, timeout=5.0)
    except httpx.HTTPError as e:
        return CheckResult(
            name="lmstudio_reachable",
            status="fail",
            detail=f"HTTP error contacting {url}: {e}",
        )
    if resp.status_code >= 400:
        return CheckResult(
            name="lmstudio_reachable",
            status="fail",
            detail=f"got HTTP {resp.status_code}",
        )
    return CheckResult(name="lmstudio_reachable", status="pass", detail=f"HTTP {resp.status_code}")


def check_power_plugged() -> CheckResult:
    try:
        proc = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, check=False)
    except OSError as e:
        return CheckResult(name="power_plugged", status="warn", detail=f"pmset unavailable: {e}")
    if proc.returncode != 0:
        return CheckResult(name="power_plugged", status="warn", detail="pmset failed")
    out = proc.stdout
    if "AC Power" in out:
        return CheckResult(name="power_plugged", status="pass", detail="AC")
    if "Battery Power" in out or "discharging" in out:
        return CheckResult(
            name="power_plugged",
            status="warn",
            detail="on battery — results not cross-comparable",
        )
    return CheckResult(name="power_plugged", status="warn", detail="unknown state")


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


def check_hippo_services() -> CheckResult:
    try:
        proc = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=False)
    except OSError as e:
        return CheckResult(
            name="hippo_services",
            status="warn",
            detail=f"launchctl unavailable: {e}",
        )
    if proc.returncode != 0:
        return CheckResult(name="hippo_services", status="warn", detail="launchctl list failed")
    running = [line for line in proc.stdout.splitlines() if "com.sjcarpenter.hippo" in line]
    if running:
        return CheckResult(
            name="hippo_services",
            status="warn",
            detail=f"{len(running)} hippo launchd agent(s) running; bench will not pause them in MVP",
        )
    return CheckResult(name="hippo_services", status="pass", detail="no hippo agents running")


def check_spotlight_idle() -> CheckResult:
    try:
        proc = subprocess.run(["mdutil", "-s", "/"], capture_output=True, text=True, check=False)
    except OSError as e:
        return CheckResult(name="spotlight", status="warn", detail=f"mdutil unavailable: {e}")
    if proc.returncode != 0:
        return CheckResult(name="spotlight", status="warn", detail="mdutil failed")
    if "Indexing enabled" in proc.stdout:
        return CheckResult(name="spotlight", status="warn", detail="indexing enabled")
    return CheckResult(name="spotlight", status="pass", detail="idle")


def run_all_preflight(path: Path, lmstudio_url: str) -> list[CheckResult]:
    return [
        check_lms_cli(),
        check_lmstudio_reachable(lmstudio_url),
        check_power_plugged(),
        check_disk_space(path),
        check_hippo_services(),
        check_spotlight_idle(),
    ]
