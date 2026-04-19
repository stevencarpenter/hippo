"""Python-side secret redaction for parsed session content.

Mirrors the builtin patterns from `crates/hippo-core/src/config.rs::RedactConfig::builtin`.
The Rust daemon redacts shell event output before it ever reaches SQLite, but
parsed sessions (Claude, Codex) flow into the brain via Python file readers
that bypass the daemon's redaction path. This module is the chokepoint for
those flows so that secrets in tool calls, user prompts, and assistant
responses do not get persisted or sent to the LLM.

Patterns are kept in lockstep with the Rust builtin set; if you add a pattern
in one place, add it in the other.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

REPLACEMENT = "[REDACTED]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{82}"),
    re.compile(
        r"(?i)(api[_-]?key|api[_-]?token|access[_-]?token|auth[_-]?token|"
        r"secret[_-]?key|private[_-]?key|password)\s*[=:]\s*\S{8,}"
    ),
    re.compile(r"eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]+"),
    re.compile(r"(?i)authorization:\s*bearer\s+\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def redact(text: str) -> str:
    """Apply all builtin redaction patterns to ``text``."""
    if not text:
        return text
    for pattern in _PATTERNS:
        text = pattern.sub(REPLACEMENT, text)
    return text


def redact_iterable(values: Iterable[str]) -> list[str]:
    return [redact(v) for v in values]


def redact_segment_secrets(segment: Any) -> None:
    """Redact secrets in-place across a SessionSegment's free-text fields.

    Mutates ``user_prompts``, ``assistant_texts``, and the ``summary`` of each
    entry in ``tool_calls``. Other tool-call fields (``name``, etc.) are left
    alone since they are short identifiers, not free text.
    """
    segment.user_prompts = redact_iterable(segment.user_prompts)
    segment.assistant_texts = redact_iterable(segment.assistant_texts)
    segment.tool_calls = [
        {**tc, "summary": redact(tc.get("summary", ""))} for tc in segment.tool_calls
    ]
