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

    Returns True if a stale lockfile was found and recovery attempted,
    False if no lockfile present. Best-effort on the HTTP call — even a
    failed POST removes the lockfile to avoid permanent paste-staleness
    when the brain itself has rotated. The caller's next /health probe
    will surface any genuinely-still-paused state.
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

    try:
        httpx.post(f"{brain_url.rstrip('/')}/control/resume", timeout=10.0)
    except Exception as e:
        logger.warning("BT-06: recovery resume POST failed: %s — removing lockfile anyway", e)

    try:
        PAUSE_LOCKFILE.unlink(missing_ok=True)
    except Exception as e:
        logger.warning("BT-06: lockfile unlink failed: %s", e)
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

    def pause(self) -> dict | None:
        """POST /control/pause. Returns response JSON or None on skip.

        Writes the pause lockfile BEFORE the HTTP call. If the bench is
        SIGKILL'd between this point and resume(), the next bench start
        finds the lockfile and recovers (BT-06).

        Post-review CC-1 + M2: the lockfile is the watchdog's ground truth
        of "bench has paused prod brain RIGHT NOW", so any failure that
        leaves a partial or stale lockfile would mute I-2/I-4/I-8 alarms
        for up to the 30-min C-1 staleness window even though prod was
        never paused. Both `_write_lockfile_atomic` (rare: disk full,
        permission errors mid-write — could orphan `.lock.tmp`) and the
        HTTP call (common: brain unreachable, 5xx) get rolled back here.
        """
        if self.skip:
            return None
        try:
            _write_lockfile_atomic(self.base_url)
            r = httpx.post(f"{self.base_url}/control/pause", timeout=10.0)
            r.raise_for_status()
        except Exception:
            # Roll back any partial state. Both PAUSE_LOCKFILE (the renamed
            # final file) and .lock.tmp (the pre-rename target) need cleanup
            # depending on which step raised; missing_ok=True handles the
            # case where one or both never got created.
            for orphan in (PAUSE_LOCKFILE, PAUSE_LOCKFILE.with_suffix(".lock.tmp")):
                try:
                    orphan.unlink(missing_ok=True)
                except Exception as unlink_err:
                    logger.warning(
                        "BT-06/CC-1/M2: pause failed AND %s unlink failed: %s. "
                        "Watchdog may suppress I-2/I-4/I-8 until the next bench's "
                        "recover_stale_pause runs (or the C-1 30-min mtime gate elapses).",
                        orphan,
                        unlink_err,
                    )
            raise
        return r.json()

    def resume(self) -> dict | None:
        """POST /control/resume. Best-effort — swallows errors (called in atexit).

        Removes the pause lockfile only after the HTTP call returns
        (success OR failure — a failed resume probably means the brain is
        already gone, in which case there's nothing to keep paused).
        """
        if self.skip:
            return None
        result: dict | None = None
        try:
            r = httpx.post(f"{self.base_url}/control/resume", timeout=10.0)
            result = r.json()
        except Exception:
            result = None
        try:
            PAUSE_LOCKFILE.unlink(missing_ok=True)
        except Exception as e:
            logger.warning("BT-06: lockfile unlink during resume failed: %s", e)
        return result
