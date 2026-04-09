import hashlib
import math
import time

import httpx

from hippo_brain.telemetry import get_meter

_meter = get_meter()
_request_duration = (
    _meter.create_histogram(
        "hippo.brain.lmstudio.request_duration_ms", description="LM Studio API latency", unit="ms"
    )
    if _meter
    else None
)
_lm_errors = (
    _meter.create_counter("hippo.brain.lmstudio.errors", description="Failed LM Studio calls")
    if _meter
    else None
)
_prompt_tokens = (
    _meter.create_histogram(
        "hippo.brain.lmstudio.prompt_tokens", description="Prompt size in chars"
    )
    if _meter
    else None
)


class LMStudioClient:
    def __init__(self, base_url: str = "http://localhost:1234/v1", timeout: float = 120.0):
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
                resp.raise_for_status()
                data = resp.json()
                result = data["choices"][0]["message"]["content"]
            if _request_duration:
                _request_duration.record((time.monotonic() - t0) * 1000, {"method": "chat"})
            if _prompt_tokens:
                total_chars = sum(len(m.get("content", "")) for m in messages)
                _prompt_tokens.record(total_chars)
            return result
        except Exception:
            if _lm_errors:
                _lm_errors.add(1, {"method": "chat"})
            raise

    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/embeddings",
                    json={"model": model, "input": texts},
                )
                resp.raise_for_status()
                data = resp.json()
                result = [item["embedding"] for item in data["data"]]
            if _request_duration:
                _request_duration.record((time.monotonic() - t0) * 1000, {"method": "embed"})
            return result
        except Exception:
            if _lm_errors:
                _lm_errors.add(1, {"method": "embed"})
            raise

    async def list_models(self) -> list[str]:
        """Return IDs of all models currently loaded in LM Studio."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{self.base_url}/models")
            resp.raise_for_status()
            return [m["id"] for m in resp.json().get("data", [])]

    async def is_reachable(self) -> bool:
        try:
            await self.list_models()
            return True
        except Exception:
            return False


class MockLMStudioClient(LMStudioClient):
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
