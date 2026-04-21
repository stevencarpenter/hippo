"""Tier 0 gate functions. Each returns a typed result struct; never raises."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from hippo_brain.bench.schemas import validate_against_schema

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


@dataclass
class SchemaCheckResult:
    passed: bool
    parsed: dict | None
    errors: list[str] = field(default_factory=list)


def _strip_code_fence(text: str) -> str:
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text


def check_schema_validity(raw_output: str, source: str) -> SchemaCheckResult:
    """Parse raw LLM output and validate it against the source's schema."""
    text = _strip_code_fence(raw_output)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return SchemaCheckResult(passed=False, parsed=None, errors=[f"json parse error: {e.msg}"])

    if not isinstance(parsed, dict):
        return SchemaCheckResult(
            passed=False,
            parsed=None,
            errors=[f"expected top-level object, got {type(parsed).__name__}"],
        )

    ok, errors = validate_against_schema(parsed, source)
    return SchemaCheckResult(passed=ok, parsed=parsed, errors=errors)
