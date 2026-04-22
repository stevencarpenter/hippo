"""Subprocess wrapper around LM Studio's `lms` CLI.

`lms` is a hard dependency; absence aborts the bench at pre-flight time.
"""

from __future__ import annotations

import json
import shutil
import subprocess


class LmsUnavailable(RuntimeError):
    """Raised when the `lms` CLI is not on PATH."""


class LmsError(RuntimeError):
    """Raised when an `lms` subprocess call exits non-zero."""


_DEFAULT_TIMEOUT_SEC = 300


def ensure_available() -> None:
    if shutil.which("lms") is None:
        raise LmsUnavailable(
            "`lms` CLI not found on PATH. Install LM Studio "
            "(https://lmstudio.ai) and run `lms --version` to verify."
        )


def _run(args: list[str], timeout: int = _DEFAULT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def list_loaded() -> list[dict]:
    proc = _run(["lms", "ls", "--json"])
    if proc.returncode != 0:
        raise LmsError(f"lms ls failed: {proc.stderr.strip()}")
    stdout = proc.stdout.strip()
    if not stdout:
        return []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise LmsError(f"could not parse lms ls output: {e}") from e
    if not isinstance(data, list):
        raise LmsError(f"lms ls returned non-list: {type(data).__name__}")
    return data


def load(identifier: str, timeout: int = _DEFAULT_TIMEOUT_SEC) -> None:
    proc = _run(["lms", "load", identifier], timeout=timeout)
    if proc.returncode != 0:
        raise LmsError(f"lms load {identifier!r} failed: {proc.stderr.strip()}")


def unload(identifier: str) -> None:
    proc = _run(["lms", "unload", identifier])
    if proc.returncode != 0:
        raise LmsError(f"lms unload {identifier!r} failed: {proc.stderr.strip()}")


def unload_all() -> None:
    proc = _run(["lms", "unload", "--all"])
    if proc.returncode != 0:
        raise LmsError(f"lms unload --all failed: {proc.stderr.strip()}")
