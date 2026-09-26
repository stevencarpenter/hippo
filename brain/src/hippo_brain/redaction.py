"""Python-side secret redaction for parsed session content.

Mirrors the builtin patterns from `crates/hippo-core/src/config.rs::RedactConfig::builtin`.
The Rust daemon redacts shell event output before it ever reaches SQLite, but
parsed sessions (Claude, Codex) flow into the brain via Python file readers
that bypass the daemon's redaction path. This module is the chokepoint for
those flows so that secrets in tool calls, user prompts, and assistant
responses do not get persisted or sent to the LLM.

Token signatures mirror the Rust builtin set. Python additionally handles
quoted and serialized credential fields at session and external-request boundaries.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

REPLACEMENT = "[REDACTED]"
_SECRET_NAME = (
    r"api[_-]?key|api[_-]?token|access[_-]?token|auth[_-]?token|"
    r"secret[_-]?key|private[_-]?key|password|(?:proxy[_-]?)?authorization"
)
_SECRET_KEY = re.compile(rf"(?:{_SECRET_NAME})", re.IGNORECASE)


def is_secret_key(value: object) -> bool:
    """Recognize credential field names at structured-data boundaries."""
    return isinstance(value, str) and _SECRET_KEY.fullmatch(value) is not None


_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Run first: assignment rules otherwise consume part of the opening marker.
    # Also remove bodies left behind by historical marker-only redaction.
    re.compile(
        r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"
        r"|\[REDACTED\](?: [A-Z ]*PRIVATE KEY-----)?[ \t]*\r?\n"
        r"(?:(?:Proc-Type|DEK-Info):[^\r\n]*\r?\n)*(?:[ \t]*\r?\n)*(?:[A-Za-z0-9+/=]+[ \t]*\r?\n)+-----END [A-Z ]*PRIVATE KEY-----"
    ),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{82}"),
    # Quoted values may contain whitespace or escaped quotes, including JSON.
    re.compile(
        rf"""(?i)["']?(?:{_SECRET_NAME})["']?\s*[=:]\s*(?:"(?:\\.|[^"\\])*(?:"|$)|'(?:\\.|[^'\\])*(?:'|$))"""
    ),
    re.compile(rf"(?i)(?:{_SECRET_NAME})\s*[=:]\s*\S{{8,}}"),
    re.compile(r"eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]+"),
    re.compile(
        r"""(?i)(?:proxy-)?authorization(?:\\*["'])?\s*:\s*(?:\\*["'])?"""
        r"(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+"
    ),
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
