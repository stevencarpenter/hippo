import asyncio
import hashlib
import logging
import math
import time

import httpx

from hippo_brain.telemetry import get_meter

logger = logging.getLogger(__name__)

_meter = get_meter()
_request_duration = (
    _meter.create_histogram(
        "hippo.brain.inference.request_duration",
        description="Inference-backend API latency",
        unit="ms",
    )
    if _meter
    else None
)
_inference_errors = (
    _meter.create_counter(
        "hippo.brain.inference.errors", description="Failed inference-backend calls"
    )
    if _meter
    else None
)
_inference_crashes = (
    _meter.create_counter(
        "hippo.brain.inference.crashes",
        description=(
            "Model-worker crashes reported by the inference backend "
            "(LM-Studio-specific 'model has crashed' substring match for now)"
        ),
    )
    if _meter
    else None
)
_prompt_tokens = (
    _meter.create_histogram(
        "hippo.brain.inference.prompt_tokens", description="Prompt size in chars"
    )
    if _meter
    else None
)


# Transient transport failures we retry: dropped/failed connections that a
# single retry can plausibly survive. httpx.TransportError is the base class of
# RemoteProtocolError, ConnectError, ConnectTimeout, ReadError, ReadTimeout,
# WriteError, PoolTimeout, etc. — exactly the "peer closed connection without
# sending complete message body" family oMLX produces under engine swaps.
# HTTPStatusError is NOT a TransportError (it's a real server response surfaced
# by _raise_with_body), and parse errors (KeyError/JSONDecodeError/ValueError)
# are not transport errors either — both propagate immediately, no retry.
_RETRYABLE: tuple[type[Exception], ...] = (httpx.TransportError,)


async def _sleep(seconds: float) -> None:
    # Indirection point so tests can monkeypatch backoff to avoid real delay.
    await asyncio.sleep(seconds)


def _raise_with_body(resp: httpx.Response) -> None:
    # OpenAI-compatible backends (LM Studio, oMLX, ollama, vLLM, …) return a
    # JSON body on 4xx (e.g. {"error": "Context history must not be empty."})
    # that pinpoints the failure. httpx's default raise_for_status discards it,
    # so we re-raise with the body appended to keep diagnoses visible.
    # If body extraction itself fails (decode error, body unread, etc.), fall
    # back to the original raise — never let a body-extraction error mask the
    # real HTTP error.
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        try:
            body = resp.text[:500].strip()
        except Exception as text_err:
            # Catch broad: any failure to decode (UnicodeDecodeError,
            # ResponseNotRead, programming bugs in the property accessor)
            # must not mask the real HTTP error. Log at debug so the loss of
            # body context is greppable in incidents.
            logger.debug("inference response body extraction failed: %s", text_err)
            body = ""
        if not body:
            raise
        # Surface model-worker crashes as a first-class signal — independent
        # of the queue-level retries that absorb them. Substring match against
        # the LM Studio UI string as of 2026-05-07 (case-insensitive to
        # survive capitalization drift). This is LM-Studio-specific; other
        # backends (oMLX, ollama, vLLM) report crashes differently and won't
        # increment this counter. Re-check on LM Studio upgrades.
        # Crashes are a subset of _inference_errors (which counts every failed
        # call from the chat()/embed() except blocks): a single crash
        # increments BOTH.
        if _inference_crashes and "model has crashed" in body.lower():
            _inference_crashes.add(1)
        raise httpx.HTTPStatusError(
            f"{e.args[0]}\nBody: {body}",
            request=e.request,
            response=e.response,
        ) from e


def _parse_embed_response(data: dict, *, source: str) -> list[list[float]]:
    """Parse an OpenAI-compatible `/v1/embeddings` response into vectors.

    Raises `ValueError` if any returned vector contains a `None` element.
    oMLX has a known bug where batched embedding requests with
    disparate-length inputs return all-null vectors for the shorter
    items; the brain triggers this loudly here so a backend regression
    can't silently corrupt the knowledge corpus.
    """
    result: list[list[float]] = []
    for idx, item in enumerate(data["data"]):
        vec = item["embedding"]
        none_idx = next((i for i, x in enumerate(vec) if x is None), None)
        if none_idx is not None:
            raise ValueError(
                f"embedding response from {source} contains None at "
                f"item[{idx}].embedding[{none_idx}] "
                f"(vector_len={len(vec)}); refusing to corrupt corpus"
            )
        result.append(vec)
    return result


class InferenceClient:
    """OpenAI-compatible inference client.

    Works against any backend that speaks the OpenAI chat/embed protocol:
    LM Studio, oMLX, ollama, vLLM, llama.cpp's server, or a hosted
    OpenAI-compatible proxy. The class name was previously `LMStudioClient`;
    it was renamed in the vendor-neutrality push to reflect that LM Studio
    is one of many backends. No legacy alias is provided — imports that
    still reference `LMStudioClient` will fail loudly with `ImportError`,
    which is the intended signal to update the call-site.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:42069/v1",
        timeout: float = 300.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
    ):
        if max_retries < 1:
            raise ValueError(f"max_retries must be >= 1, got {max_retries}")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Total attempts = max_retries (e.g. up to 3 tries). backoff_base seconds
        # scaled by 2**(attempt-1): 0.5s before retry 2, 1.0s before retry 3.
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    async def _post_with_retry(self, url, payload, parse):
        """POST `payload` to `url`, returning `parse(resp.json())`.

        Retries ONLY on transient transport errors (`_RETRYABLE`) with
        exponential backoff. HTTPStatusError (a real server response raised by
        `_raise_with_body`) and parse errors propagate immediately — no retry.
        On exhaustion, the last transport exception is re-raised (never
        swallowed): the enrichment queue's own retry_count handles cross-poll
        failure; our job is to survive a transient blip, then surface a real
        outage.
        """
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, json=payload)
                    _raise_with_body(resp)
                    return parse(resp.json())
            except _RETRYABLE as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    await _sleep(self.backoff_base * 2 ** (attempt - 1))
        # Exhausted: re-raise the last transient transport error.
        # Defensive typed raise instead of assert so python -O can't produce
        # a TypeError from `raise None`, and so future logic changes surface
        # clearly rather than as a cryptic AssertionError.
        if last_exc is None:
            raise RuntimeError("_post_with_retry exhausted without capturing an exception")
        raise last_exc

    async def chat(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float = 0.0,
        max_tokens: int = 16384,
    ) -> str:
        t0 = time.monotonic()
        try:
            # Retry loop sits INSIDE the telemetry try/except: a successful call
            # (even after retries) records duration/tokens and returns; only a
            # final failure falls through to the outer except, so
            # _inference_errors increments once per final failure, NOT per retry.
            result = await self._post_with_retry(
                f"{self.base_url}/chat/completions",
                {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                lambda data: data["choices"][0]["message"]["content"],
            )
            if _request_duration:
                _request_duration.record((time.monotonic() - t0) * 1000, {"method": "chat"})
            if _prompt_tokens:
                total_chars = sum(len(m.get("content", "")) for m in messages)
                _prompt_tokens.record(total_chars)
            return result
        except Exception as exc:
            if _inference_errors:
                if isinstance(exc, httpx.TransportError):
                    error_type = "transport"
                elif isinstance(exc, httpx.HTTPStatusError):
                    error_type = "status"
                else:
                    error_type = "parse"
                _inference_errors.add(1, {"method": "chat", "error_type": error_type})
            raise

    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        t0 = time.monotonic()
        try:
            result = await self._post_with_retry(
                f"{self.base_url}/embeddings",
                {"model": model, "input": texts},
                lambda data: _parse_embed_response(data, source=self.base_url),
            )
            if _request_duration:
                _request_duration.record((time.monotonic() - t0) * 1000, {"method": "embed"})
            return result
        except Exception as exc:
            if _inference_errors:
                if isinstance(exc, httpx.TransportError):
                    error_type = "transport"
                elif isinstance(exc, httpx.HTTPStatusError):
                    error_type = "status"
                else:
                    error_type = "parse"
                _inference_errors.add(1, {"method": "embed", "error_type": error_type})
            raise

    async def list_models(self) -> list[str]:
        """Return IDs of all models currently loaded on the inference backend."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/models")
            _raise_with_body(resp)
            return [m["id"] for m in resp.json().get("data", [])]

    async def is_reachable(self) -> bool:
        try:
            await self.list_models()
            return True
        except Exception:
            return False

    async def health_check(self, model: str) -> dict:
        """Probe the inference backend and verify ``model`` is loaded.

        Returns a dict:

            {"ok": bool, "reason": str | None, "loaded_models": list[str]}

        On unreachable endpoint or missing model, ``ok=False`` and ``reason``
        describes the problem (exception type + message, or model-not-loaded
        with the list of models that ARE loaded). Never raises.
        """
        try:
            models = await self.list_models()
        except Exception as e:
            return {
                "ok": False,
                "reason": (
                    f"inference backend unreachable at {self.base_url} "
                    f"[{type(e).__name__}]: {str(e) or repr(e)}"
                ),
                "loaded_models": [],
            }
        if model and model not in models:
            return {
                "ok": False,
                "reason": (
                    f"model {model!r} not loaded on inference backend at "
                    f"{self.base_url}. Loaded: {models}"
                ),
                "loaded_models": models,
            }
        return {"ok": True, "reason": None, "loaded_models": models}


class MockInferenceClient(InferenceClient):
    CANNED_RESPONSE = (
        '{"summary": "test command", "intent": "testing", "outcome": "success", '
        '"entities": {"projects": [], "tools": [], "files": [], "services": [], "errors": []}, '
        '"tags": ["test"], "embed_text": "test embed text"}'
    )

    def __init__(self, base_url: str = "http://mock:1234/v1", timeout: float = 1.0):
        super().__init__(base_url, timeout)
        self.chat_calls: list[dict] = []
        self.embed_calls: list[dict] = []

    async def chat(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float = 0.0,
        max_tokens: int = 16384,
    ) -> str:
        self.chat_calls.append(
            {
                "messages": messages,
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        return self.CANNED_RESPONSE

    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        self.embed_calls.append({"texts": texts, "model": model})
        from hippo_brain.embeddings import EMBED_DIM

        return [self._deterministic_vector(text, EMBED_DIM) for text in texts]

    async def list_models(self) -> list[str]:
        return ["mock-model", "text-embedding-mock"]

    async def is_reachable(self) -> bool:
        return True

    async def health_check(self, model: str) -> dict:
        models = await self.list_models()
        if model and model not in models:
            return {
                "ok": False,
                "reason": f"query model {model!r} not loaded. Loaded: {models}",
                "loaded_models": models,
            }
        return {"ok": True, "reason": None, "loaded_models": models}

    @staticmethod
    def _deterministic_vector(text: str, dims: int) -> list[float]:
        """Generate a deterministic, normalized vector from text using SHA256."""
        raw = []
        i = 0
        while len(raw) < dims:
            h = hashlib.sha256(f"{text}:{i}".encode()).digest()
            for j in range(0, len(h), 4):
                if len(raw) >= dims:
                    break
                # Convert 4 bytes to a float in [-1, 1]
                val = int.from_bytes(h[j : j + 4], "big", signed=True) / (2**31)
                raw.append(val)
            i += 1

        # Normalize
        magnitude = math.sqrt(sum(x * x for x in raw))
        if magnitude > 0:
            raw = [x / magnitude for x in raw]
        return raw
