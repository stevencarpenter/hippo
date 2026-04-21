"""Bench-owned enrichment + embedding HTTP calls to LM Studio.

Intentionally independent from hippo_brain.client so bench can call
arbitrary candidate models without disturbing production telemetry.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx


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
    raw_output: str
    ttft_ms: int | None
    total_ms: int
    timeout: bool


def build_prompt(payload: str, source: str) -> str:
    template = _PROMPT_TEMPLATES.get(source)
    if template is None:
        raise ValueError(f"unknown source {source!r}")
    return template.format(payload=payload)


def call_enrichment(
    base_url: str, model: str, payload: str, source: str, timeout_sec: int
) -> CallResult:
    prompt = build_prompt(payload, source)
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You emit strict JSON. No prose, no code fences."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }
    start = time.monotonic()
    try:
        resp = httpx.post(url, json=body, timeout=timeout_sec)
    except httpx.TimeoutException:
        total_ms = int((time.monotonic() - start) * 1000)
        return CallResult(raw_output="", ttft_ms=None, total_ms=total_ms, timeout=True)
    total_ms = int((time.monotonic() - start) * 1000)
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return CallResult(raw_output=content, ttft_ms=None, total_ms=total_ms, timeout=False)


def call_embedding(base_url: str, model: str, text: str, timeout_sec: int = 60) -> list[float]:
    url = f"{base_url.rstrip('/')}/embeddings"
    resp = httpx.post(url, json={"model": model, "input": text}, timeout=timeout_sec)
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]
