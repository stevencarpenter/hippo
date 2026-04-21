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


_REFUSAL_PATTERNS = (
    re.compile(r"\bI'?m sorry\b", re.IGNORECASE),
    re.compile(r"\bI (?:cannot|can['']?t|won['']?t)\b", re.IGNORECASE),
    re.compile(r"\bas an AI\b", re.IGNORECASE),
    re.compile(r"\bI'?m unable to\b", re.IGNORECASE),
    re.compile(r"\bI don'?t have the ability\b", re.IGNORECASE),
    re.compile(r"\bI'?m not able to\b", re.IGNORECASE),
)


@dataclass
class RefusalPathologyResult:
    refusal_detected: bool
    refusal_patterns_matched: list[str]
    trivial_summary: bool
    echo_similarity: float


def _char_ngrams(s: str, n: int = 4) -> set[str]:
    s = s.lower().strip()
    if len(s) < n:
        return {s} if s else set()
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def check_refusal_pathology(
    raw_output: str, input_text: str, parsed: dict | None
) -> RefusalPathologyResult:
    """Detect refusal phrases, trivial summaries, and echo of input."""
    patterns_matched: list[str] = []
    for pat in _REFUSAL_PATTERNS:
        m = pat.search(raw_output)
        if m:
            patterns_matched.append(m.group(0))

    trivial = False
    if parsed is not None and isinstance(parsed, dict):
        summary = parsed.get("summary")
        if summary is None or not isinstance(summary, str) or len(summary.strip()) < 4:
            trivial = True

    echo = _jaccard(_char_ngrams(raw_output), _char_ngrams(input_text))

    return RefusalPathologyResult(
        refusal_detected=bool(patterns_matched),
        refusal_patterns_matched=patterns_matched,
        trivial_summary=trivial,
        echo_similarity=echo,
    )


_PATH_LIKE = re.compile(r"[/\\]|^\.\w+$|\.\w{1,8}$")
_WHITESPACE_WORDS = re.compile(r"\s+")


@dataclass
class EntitySanityResult:
    passed: bool
    per_category_rates: dict[str, float]
    files_path_rate: float = 1.0
    tools_sanity_rate: float = 1.0
    projects_sanity_rate: float = 1.0


def _file_looks_like_path(s: str) -> bool:
    if not isinstance(s, str) or not s:
        return False
    if len(s) > 200:
        return False
    return bool(_PATH_LIKE.search(s))


def _tool_looks_sane(s: str) -> bool:
    if not isinstance(s, str) or not s:
        return False
    if len(s) > 40:
        return False
    words = _WHITESPACE_WORDS.findall(s)
    if len(words) + 1 > 3:  # more than 3 tokens
        return False
    if s.rstrip().endswith((".", "!", "?")):
        return False
    return True


def _project_looks_sane(s: str) -> bool:
    if not isinstance(s, str) or not s:
        return False
    if len(s) > 80:
        return False
    return not any(c.isspace() for c in s if c not in "-_")


_CATEGORY_CHECKERS = {
    "files": _file_looks_like_path,
    "tools": _tool_looks_sane,
    "projects": _project_looks_sane,
}


def check_entity_sanity(parsed: dict, source: str, min_rate: float = 0.9) -> EntitySanityResult:
    entities = parsed.get("entities") if isinstance(parsed, dict) else None
    per_cat: dict[str, float] = {}
    if not isinstance(entities, dict):
        return EntitySanityResult(passed=True, per_category_rates={})

    for cat, checker in _CATEGORY_CHECKERS.items():
        values = entities.get(cat)
        if not isinstance(values, list) or not values:
            continue
        hits = sum(1 for v in values if checker(v))
        per_cat[cat] = hits / len(values)

    all_pass = all(rate >= min_rate for rate in per_cat.values())
    return EntitySanityResult(
        passed=all_pass,
        per_category_rates=per_cat,
        files_path_rate=per_cat.get("files", 1.0),
        tools_sanity_rate=per_cat.get("tools", 1.0),
        projects_sanity_rate=per_cat.get("projects", 1.0),
    )
