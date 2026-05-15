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

    def __init__(self, base_url: str = "http://localhost:1234/v1", timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def chat(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float = 0.0,
        max_tokens: int = 16384,
    ) -> str:
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json={
                        "model": model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
                )
                _raise_with_body(resp)
                data = resp.json()
                result = data["choices"][0]["message"]["content"]
            if _request_duration:
                _request_duration.record((time.monotonic() - t0) * 1000, {"method": "chat"})
            if _prompt_tokens:
                total_chars = sum(len(m.get("content", "")) for m in messages)
                _prompt_tokens.record(total_chars)
            return result
        except Exception:
            if _inference_errors:
                _inference_errors.add(1, {"method": "chat"})
            raise

    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/embeddings",
                    json={"model": model, "input": texts},
                )
                _raise_with_body(resp)
                data = resp.json()
                result = _parse_embed_response(data, source=self.base_url)
            if _request_duration:
                _request_duration.record((time.monotonic() - t0) * 1000, {"method": "embed"})
            return result
        except Exception:
            if _inference_errors:
                _inference_errors.add(1, {"method": "embed"})
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
