"""Pinned typed decisions shared by runtime and offline experiments.

The HTTP client makes one attempt. Its timeout includes the concurrency queue;
callers own retry policy and the overall decision budget.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from typing import Any, NotRequired, TypedDict

import httpx

from hippo_brain.redaction import redact

MODEL = "1.13.0"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MAX_PAYLOAD_BYTES = 128_000


class Question(TypedDict):
    type: str
    instructions: Any
    criteria: NotRequired[Any]


class JevResponse(TypedDict):
    model: str
    answers: dict[str, dict[str, Any]]
    usage: NotRequired[dict[str, int] | None]
    _transport: NotRequired[dict[str, float | int | None]]


def canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def redact_state(value: Any) -> Any:
    """Redact string values before either transmission or external capture."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_state(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_state(item) for item in value]
    return value


def number(value: object, low: float, high: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("invalid numeric answer")
    return float(value)


def validate_questions(
    questions: dict[str, Question], *, require_instructions: bool = True
) -> None:
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 100:
        raise ValueError("expected 1 to 100 questions")
    for key, question in questions.items():
        if not isinstance(key, str) or not key or not isinstance(question, dict):
            raise ValueError("questions require nonempty string IDs and object values")
        if require_instructions and not question.get("instructions"):
            raise ValueError("question instructions are required")
        kind, criteria = question.get("type"), question.get("criteria")
        if kind == "score":
            if not isinstance(criteria, list) or not 2 <= len(criteria) <= 10:
                raise ValueError("score requires 2 to 10 ordered criteria")
        elif kind == "choice":
            if not isinstance(criteria, dict) or len(criteria) < 2:
                raise ValueError("choice requires at least two criteria")
        elif kind == "noul":
            if criteria is not None and (
                not isinstance(criteria, dict) or set(criteria) != {"true", "false"}
            ):
                raise ValueError("Noul criteria require true and false")
        else:
            raise ValueError("unsupported question type")


def validate_response(
    data: dict, questions: dict[str, Question], *, model: str | None = None
) -> JevResponse:
    """Validate complete answer coverage and the API's rounded distributions.

    ``model=None`` supports historical offline records without identity fields.
    Live callers must supply their pinned model. A ``jev-`` prefix is cosmetic;
    a different version or an alias never satisfies a pin.
    """
    validate_questions(questions, require_instructions=False)
    if not isinstance(data, dict):
        raise ValueError("response must be an object")
    canonical(data)
    if model is not None and (
        not isinstance(data.get("model"), str)
        or data["model"].removeprefix("jev-") != model.removeprefix("jev-")
    ):
        raise ValueError("Jev model identity changed")
    answers = data.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise ValueError("missing or unexpected Jev answer")
    for key, question in questions.items():
        answer = answers[key]
        if not isinstance(answer, dict) or answer.get("type") != question["type"]:
            raise ValueError("answer type mismatch")
        if question["type"] == "noul":
            number(answer.get("noul"), 0, 1)
            continue
        number(answer.get("confidence"), 0, 1)
        criteria = question["criteria"]
        options = (
            set(criteria) if question["type"] == "choice" else set(map(str, range(len(criteria))))
        )
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != options:
            raise ValueError("probability options mismatch")
        values = {key: number(value, 0, 1) for key, value in probabilities.items()}
        if not math.isclose(sum(values.values()), 1, abs_tol=0.005 * len(options) + 1e-9):
            raise ValueError("probabilities do not sum to one")
        if question["type"] == "choice":
            choice = answer.get("choice")
            if not isinstance(choice, str) or choice not in options:
                raise ValueError("invalid choice")
            if values[choice] < max(values.values()) - 0.001:
                raise ValueError("choice is not a highest-probability option")
        else:
            score = number(answer.get("score"), 0, len(criteria) - 1)
            weighted = sum(int(key) * value for key, value in values.items())
            rounding = 0.005 * (1 + sum(range(len(criteria)))) + 1e-9
            if not math.isclose(score, weighted, abs_tol=rounding):
                raise ValueError("score does not match probabilities")
            # Older captured responses omit legend. When present it must agree.
            if (
                "legend" in answer
                and answer["legend"] != dict(enumerate(criteria))
                and (
                    answer["legend"] != {str(index): level for index, level in enumerate(criteria)}
                )
            ):
                raise ValueError("score legend differs from requested criteria")
    usage = data.get("usage")
    if usage is not None and (
        not isinstance(usage, dict)
        or any(
            type(usage.get(key)) is not int or usage[key] < 0
            for key in ("input_tokens", "output_tokens")
        )
    ):
        raise ValueError("invalid token usage")
    return data


def parse_nouls(data: dict, questions: dict[str, Question]) -> dict[str, float]:
    validate_response(data, questions)
    if any(question["type"] != "noul" for question in questions.values()):
        raise ValueError("expected only Noul questions")
    return {key: float(answer["noul"]) for key, answer in data["answers"].items()}


class JevUnavailable(RuntimeError):
    """Credentials are absent or endpoint failures activated cooldown."""


class JevClient:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = MODEL,
        concurrency: int = 4,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise JevUnavailable("TYPESAFE_API_KEY is required")
        if not 1 <= concurrency <= 8:
            raise ValueError("concurrency must be between 1 and 8")
        self.model = model.removeprefix("jev-")
        self.request_model = f"jev-{self.model}"
        self._api_key = api_key
        self._slots = asyncio.Semaphore(concurrency)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        self._failures = 0
        self._cooldown_until = 0.0
        self._last_failure: str | None = None

    @classmethod
    def from_env(cls) -> JevClient:
        return cls(os.environ.get("TYPESAFE_API_KEY", ""))

    def diagnostics(self) -> dict[str, object]:
        return {
            "model": self.model,
            "consecutive_failures": self._failures,
            "cooldown_seconds": max(0.0, self._cooldown_until - time.monotonic()),
            "last_failure": self._last_failure,
        }

    async def assess(
        self, state: dict, questions: dict[str, Question], *, timeout_seconds: float | None = None
    ) -> JevResponse:
        timeout = 10.0 if timeout_seconds is None else number(timeout_seconds, 0, 3600)
        if timeout == 0:
            raise TimeoutError("decision budget exhausted")
        validate_questions(questions)
        payload = {
            "model": self.request_model,
            "state": redact_state(state),
            "questions": questions,
        }
        encoded = canonical(payload).encode()
        if len(encoded) > MAX_PAYLOAD_BYTES:
            raise ValueError("Jev request exceeds 128KB payload budget")
        started = time.monotonic()
        dispatched = False
        http_start = None
        status_code = None
        try:
            async with asyncio.timeout(timeout):
                async with self._slots:
                    if time.monotonic() < self._cooldown_until:
                        raise JevUnavailable("Jev endpoint is cooling down")
                    queued_ms = (time.monotonic() - started) * 1000
                    dispatched = True
                    http_start = time.monotonic()
                    response = await self._client.post(
                        ENDPOINT,
                        content=encoded,
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                        timeout=max(0.001, timeout - (http_start - started)),
                    )
                    status_code = response.status_code
                    response.raise_for_status()
                    data = validate_response(response.json(), questions, model=self.model)
                    data["_transport"] = {
                        "queue_ms": queued_ms,
                        "http_ms": (time.monotonic() - http_start) * 1000,
                        "payload_bytes": len(encoded),
                        "network_calls": 1,
                        "status_code": status_code,
                    }
                    self._failures = 0
                    self._last_failure = None
                    return data
        except (
            asyncio.CancelledError,
            httpx.HTTPError,
            TimeoutError,
            ValueError,
            JevUnavailable,
        ) as exc:
            exc.jev_transport = {
                "network_calls": int(dispatched),
                "payload_bytes": len(encoded),
                "queue_ms": ((http_start or time.monotonic()) - started) * 1000,
                "http_ms": None if http_start is None else (time.monotonic() - http_start) * 1000,
                "status_code": status_code,
            }
            if dispatched and not isinstance(exc, asyncio.CancelledError):
                self._failures += 1
                self._last_failure = type(exc).__name__
                if self._failures >= 3:
                    self._cooldown_until = time.monotonic() + 30.0
            raise

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> JevClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
