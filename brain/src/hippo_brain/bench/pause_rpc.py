"""Thin client for the hippo-brain pause/resume control RPC.

BT-06: pause/resume now goes through a lockfile so a SIGKILL'd bench
leaves a marker the next bench-start can detect and clean up. Without
this, atexit/finally never run on SIGKILL → prod brain stays paused
indefinitely → enrichment queue grows silently.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

PAUSE_LOCKFILE: Path = Path("~/.local/share/hippo-bench/pause.lock").expanduser()


def _write_lockfile_atomic(brain_url: str) -> None:
    """Atomic write via tmp-file + `os.replace`.

    `os.replace` is POSIX-atomic — the lockfile either exists with the old
    content or with the new content, never with a partial payload. That's
    enough for the single-host design (no concurrent-bench-process race to
    defend against; see `feedback_single_host` memory + tracking-doc "What
    We Are Not Doing").

    Post-review C-4: an earlier docstring claimed `O_EXCL` semantics that the
    code never implemented (it uses `O_TRUNC` so the same process can re-pause
    after a transient failure). Documented honestly now.
    """
    PAUSE_LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "started_iso": _dt.datetime.now(_dt.UTC).isoformat(),
            "brain_url": brain_url,
            "pid": os.getpid(),
        }
    )
    tmp = PAUSE_LOCKFILE.with_suffix(".lock.tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, PAUSE_LOCKFILE)


def recover_stale_pause(default_brain_url: str) -> bool:
    """If a pause lockfile exists, read its brain_url and POST resume.

    Returns True after confirmed recovery, False if no marker is present.
    Failed recovery raises and retains the marker for another attempt.
    """
    if not PAUSE_LOCKFILE.exists():
        return False
    try:
        data = json.loads(PAUSE_LOCKFILE.read_text())
        brain_url = data.get("brain_url", default_brain_url)
        logger.warning(
            "BT-06: stale pause lockfile detected (started=%s, pid=%s) — issuing recovery resume to %s",
            data.get("started_iso"),
            data.get("pid"),
            brain_url,
        )
    except Exception as e:
        logger.warning("BT-06: lockfile read failed (%s) — using default brain_url", e)
        brain_url = default_brain_url

    if PauseRpcClient(brain_url).resume() is None:
        raise RuntimeError("production resume failed; recovery marker retained")
    return True


class PauseRpcClient:
    """Calls POST /control/pause and POST /control/resume on the prod brain."""

    def __init__(self, base_url: str, skip: bool = False):
        self.base_url = base_url.rstrip("/")
        self.skip = skip

    def probe_health(self) -> dict | None:
        """Return /health JSON or None if unreachable."""
        if self.skip:
            return None
        try:
            r = httpx.get(f"{self.base_url}/health", timeout=5.0)
            return r.json()
        except Exception:
            return None

    def pause(self, timeout_sec: float = 300.0) -> dict | None:
        """Pause and wait boundedly until all in-flight inference has finished.

        Retain the marker after dispatch failures: the server may have accepted
        the pause even if its response was lost. Only confirmed resume clears it.
        """
        if self.skip:
            return None
        if timeout_sec <= 0:
            raise ValueError("pause timeout must be positive")
        try:
            _write_lockfile_atomic(self.base_url)
        except Exception:
            for orphan in (PAUSE_LOCKFILE, PAUSE_LOCKFILE.with_suffix(".lock.tmp")):
                orphan.unlink(missing_ok=True)
            raise
        deadline = time.monotonic() + timeout_sec
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("production inference did not quiesce before pause deadline")
            response = httpx.post(f"{self.base_url}/control/pause", timeout=min(10.0, remaining))
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or type(result.get("in_flight_finished")) is not bool:
                raise ValueError("invalid production pause acknowledgement")
            if result["in_flight_finished"]:
                return result
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    def resume(self) -> dict | None:
        """Return confirmed resume or None; retain the recovery marker on failure."""
        if self.skip:
            return None
        try:
            response = httpx.post(f"{self.base_url}/control/resume", timeout=10.0)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or not isinstance(result.get("resumed_at"), str):
                raise ValueError("invalid production resume acknowledgement")
        except Exception as exc:
            logger.warning(
                "production resume failed (%s); recovery marker retained", type(exc).__name__
            )
            return None
        try:
            PAUSE_LOCKFILE.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("pause marker cleanup failed: %s", exc)
        return result
