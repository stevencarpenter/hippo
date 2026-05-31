"""Vendor-neutral model-lifecycle abstraction for the bench coordinator.

The coordinator must "unload everything else, then load the target model" before
each per-model run. Historically this shelled out to LM Studio's `lms` CLI
(`lms.unload_all()` + `lms.load(model)`). After the migration to oMLX (an
OpenAI-compatible local inference server with NO `lms` CLI), that path hangs
until the 300s subprocess timeout and the whole bench run fails.

This module introduces a small `ModelLifecycle` abstraction so the coordinator
no longer imports a vendor-specific CLI wrapper directly:

- `OmlxLifecycle` drives oMLX's HTTP model-lifecycle API (the current backend).
- `LmsLifecycle` delegates to the existing `hippo_brain.bench.lms` CLI wrapper
  so the LM Studio path still works unchanged.
- `get_model_lifecycle(base_url)` selects an implementation. It defaults to
  oMLX for HTTP base_urls, matching the current inference backend.

There is NO silent fallback: HTTP errors, connection failures, and unknown
models all raise `ModelLifecycleError` so the bench fails loudly instead of
benchmarking a model that never loaded.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Protocol, runtime_checkable

import httpx

from hippo_brain.bench import lms

logger = logging.getLogger(__name__)

_LOAD_TIMEOUT_SEC = 300.0
_HTTP_TIMEOUT_SEC = 30.0


class ModelLifecycleError(RuntimeError):
    """Raised when a model-lifecycle operation fails (no silent fallback)."""


@runtime_checkable
class ModelLifecycle(Protocol):
    """Manages which model is resident on the inference server.

    The coordinator only needs `prepare`: make `model_id` the loaded model and
    return how long the load took in wall-clock milliseconds.
    """

    def prepare(self, model_id: str) -> int:
        """Unload other models, load `model_id`, return load wall-clock ms."""
        ...


class OmlxLifecycle:
    """Drives oMLX's HTTP model-lifecycle API.

    Endpoints (relative to the `/v1` base_url):
      - GET  /models/status            -> {"models": [{"id", "loaded", ...}]}
      - POST /models/{id}/unload       -> {"status": "ok", ...}
      - POST /models/{id}/load         -> synchronous + idempotent; returns only
                                          once the model is resident. 404 if the
                                          model id is unknown.

    Auth: a `Bearer` header is sent only when OPENAI_API_KEY is set in the
    environment (oMLX may run without auth, in which case the header is omitted).
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        api_key = os.environ.get("OPENAI_API_KEY")
        if api_key:
            return {"Authorization": f"Bearer {api_key}"}
        return {}

    def _loaded_model_ids(self) -> list[str]:
        """Return the ids of models currently `loaded:true` per /models/status."""
        url = f"{self.base_url}/models/status"
        try:
            resp = httpx.get(url, headers=self._headers(), timeout=_HTTP_TIMEOUT_SEC)
        except httpx.HTTPError as e:
            raise ModelLifecycleError(f"GET {url} failed: {e}") from e
        if resp.status_code >= 400:
            raise ModelLifecycleError(f"GET {url} returned HTTP {resp.status_code}: {resp.text}")
        data = resp.json()
        models = data.get("models", []) if isinstance(data, dict) else []
        return [m["id"] for m in models if isinstance(m, dict) and m.get("loaded")]

    def _unload(self, model_id: str) -> None:
        url = f"{self.base_url}/models/{model_id}/unload"
        try:
            resp = httpx.post(url, headers=self._headers(), timeout=_HTTP_TIMEOUT_SEC)
        except httpx.HTTPError as e:
            raise ModelLifecycleError(f"POST {url} failed: {e}") from e
        if resp.status_code == 404:
            # Model is already not loaded — the desired end state. Treat as success.
            return
        if resp.status_code >= 400:
            raise ModelLifecycleError(f"POST {url} returned HTTP {resp.status_code}: {resp.text}")

    def _load(self, model_id: str) -> None:
        url = f"{self.base_url}/models/{model_id}/load"
        try:
            resp = httpx.post(url, headers=self._headers(), timeout=_LOAD_TIMEOUT_SEC)
        except httpx.HTTPError as e:
            raise ModelLifecycleError(f"POST {url} failed: {e}") from e
        if resp.status_code == 404:
            # Surface the server's "model not found" message as a fatal error.
            # Best-effort JSON extraction only — we ALWAYS raise below, so this
            # never masks a failure. Catch only what parsing can raise:
            # ValueError (malformed JSON, incl. json.JSONDecodeError) and
            # AttributeError (body or `error` is not a dict, e.g. a bare list).
            message = resp.text
            try:
                body = resp.json()
                message = body.get("error", {}).get("message", message)
            except ValueError, AttributeError:
                pass
            raise ModelLifecycleError(f"model not found: {message}")
        if resp.status_code >= 400:
            raise ModelLifecycleError(f"POST {url} returned HTTP {resp.status_code}: {resp.text}")

    def prepare(self, model_id: str) -> int:
        """Unload every OTHER loaded model, then load `model_id`.

        Returns the load wall-clock in milliseconds. The oMLX load endpoint is
        synchronous and idempotent, so a re-load of an already-resident model
        returns promptly.
        """
        start = time.monotonic()
        for loaded_id in self._loaded_model_ids():
            if loaded_id != model_id:
                logger.info("unloading other model %s before loading %s", loaded_id, model_id)
                self._unload(loaded_id)
        self._load(model_id)
        return int((time.monotonic() - start) * 1000)


class LmsLifecycle:
    """Delegates to the LM Studio `lms` CLI wrapper (legacy backend)."""

    def prepare(self, model_id: str) -> int:
        """Unload all, settle, load target — preserving the old CLI behavior."""
        start = time.monotonic()
        lms.unload_all()
        time.sleep(1)
        lms.load(model_id)
        return int((time.monotonic() - start) * 1000)


def get_model_lifecycle(base_url: str) -> ModelLifecycle:
    """Select a `ModelLifecycle` implementation for `base_url`.

    The current inference backend is oMLX, an OpenAI-compatible HTTP server, so
    any HTTP(S) base_url maps to `OmlxLifecycle`. The LM Studio CLI path
    (`LmsLifecycle`) is selected only when explicitly requested via the
    `HIPPO_BENCH_MODEL_LIFECYCLE=lms` environment variable, keeping the legacy
    backend available without coupling the default to a vendor CLI.
    """
    backend = os.environ.get("HIPPO_BENCH_MODEL_LIFECYCLE", "").strip().lower()
    if backend == "lms":
        return LmsLifecycle()
    if backend in ("", "omlx"):
        return OmlxLifecycle(base_url)
    raise ModelLifecycleError(
        f"unknown HIPPO_BENCH_MODEL_LIFECYCLE={backend!r} (expected 'omlx' or 'lms')"
    )
