"""Thin client for the hippo-brain pause/resume control RPC."""

from __future__ import annotations

import httpx


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
        """POST /control/pause. Returns response JSON or None on skip."""
        if self.skip:
            return None
        r = httpx.post(f"{self.base_url}/control/pause", timeout=10.0)
        r.raise_for_status()
        return r.json()

    def resume(self) -> dict | None:
        """POST /control/resume. Best-effort — swallows errors (called in atexit)."""
        if self.skip:
            return None
        try:
            r = httpx.post(f"{self.base_url}/control/resume", timeout=10.0)
            return r.json()
        except Exception:
            return None
