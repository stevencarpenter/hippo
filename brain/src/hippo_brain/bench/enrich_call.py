"""Bench-owned enrichment + embedding HTTP calls to LM Studio.

Intentionally independent from hippo_brain.client so bench can call
arbitrary candidate models without disturbing production telemetry.

Errors are CALLER-CLASSIFIED, never raised:
- Timeout → CallResult(timeout=True, error="timeout")
- HTTP / connection / parse → CallResult(error="<class>: <msg>", raw_output="")

The runner asks each call: did it return useful data, did it time out,
or did it fail? It never has to handle a bare exception. This keeps
multi-event runs robust against transient LM Studio hiccups.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx


# Default sampling temperature. 0.7 is a deliberate choice — at near-zero
# temperatures (e.g., 0.1) most modern models produce nearly deterministic
# outputs, making self-consistency a vacuous signal. 0.7 is the OpenAI
# chat-API default and produces enough variance to distinguish a model
# that converges on the same answer from one that flails.
DEFAULT_TEMPERATURE = 0.7


_PROMPT_TEMPLATES = {
    "shell": (
        "Summarize this shell event. Return JSON only with keys: "
        "summary (string), intent (string), outcome (one of: success, partial, "
        "failure, unknown), entities (object with keys: projects, tools, files, "
        "services, errors, each a list of strings).\n\n"
        "Event: {payload}"
    ),
    "claude": (
        "Summarize this Claude session. Return JSON only with keys: "
        "summary (string), entities (object with keys: projects, topics, files, "
        "decisions, errors, each a list of strings).\n\n"
        "Session: {payload}"
    ),
    "browser": (
        "Summarize this browser visit. Return JSON only with keys: "
        "summary (string), entities (object with keys: topics, urls, projects, "
        "each a list of strings).\n\n"
        "Visit: {payload}"
    ),
    "workflow": (
        "Summarize this CI workflow run. Return JSON only with keys: "
        "summary (string), entities (object with keys: projects, jobs, errors, "
        "each a list of strings).\n\n"
        "Run: {payload}"
    ),
}


@dataclass
class CallResult:
    """Outcome of a single enrichment call.

    `raw_output` is the LLM's response (empty on timeout/error).
    `total_ms` is wall time from request to response/error.
    `ttft_ms` is None — TTFT requires a streaming endpoint we don't use.
    `timeout` is True if the HTTP timeout fired.
    `error` is None on success, otherwise a short classification ("timeout",
    "http_500", "connect_error: ...", etc.). Callers should treat any
    non-None error as "no useful data."
    """

    raw_output: str
    ttft_ms: int | None
    total_ms: int
    timeout: bool
    error: str | None = None


def build_prompt(payload: str, source: str) -> str:
    template = _PROMPT_TEMPLATES.get(source)
    if template is None:
        raise ValueError(f"unknown source {source!r}")
    return template.format(payload=payload)


def call_enrichment(
    base_url: str,
    model: str,
    payload: str,
    source: str,
    timeout_sec: int,
    temperature: float = DEFAULT_TEMPERATURE,
) -> CallResult:
    """Send one enrichment request to LM Studio. Never raises."""
    prompt = build_prompt(payload, source)
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You emit strict JSON. No prose, no code fences."},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
    }
    start = time.monotonic()
    try:
        resp = httpx.post(url, json=body, timeout=timeout_sec)
    except httpx.TimeoutException:
        return CallResult(
            raw_output="",
            ttft_ms=None,
            total_ms=int((time.monotonic() - start) * 1000),
            timeout=True,
            error="timeout",
        )
    except httpx.HTTPError as e:
        return CallResult(
            raw_output="",
            ttft_ms=None,
            total_ms=int((time.monotonic() - start) * 1000),
            timeout=False,
            error=f"http_error: {type(e).__name__}: {e}",
        )

    total_ms = int((time.monotonic() - start) * 1000)
    if resp.status_code >= 400:
        return CallResult(
            raw_output="",
            ttft_ms=None,
            total_ms=total_ms,
            timeout=False,
            error=f"http_{resp.status_code}",
        )
    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as e:
        return CallResult(
            raw_output=resp.text[:200],
            ttft_ms=None,
            total_ms=total_ms,
            timeout=False,
            error=f"response_parse_error: {type(e).__name__}: {e}",
        )
    return CallResult(raw_output=content, ttft_ms=None, total_ms=total_ms, timeout=False)


def call_embedding(base_url: str, model: str, text: str, timeout_sec: int = 60) -> list[float]:
    """Send one embedding request. Raises on error (caller catches)."""
    url = f"{base_url.rstrip('/')}/embeddings"
    resp = httpx.post(url, json={"model": model, "input": text}, timeout=timeout_sec)
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]
