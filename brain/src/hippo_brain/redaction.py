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

import json
import re
from collections.abc import Iterable
from typing import Any

REPLACEMENT = "[REDACTED]"
# ponytail: cap key-name prefixes to keep long nonsecret text cheap; extend for observed longer fields.
_SECRET_NAME = (
    r"(?:[a-z0-9]{1,32}[_-]){0,4}(?:token|secret)|"
    r"(?:x[_-])?api[_-]?key|aws[_-]?secret[_-]?access[_-]?key|"
    r"secret[_-]?key|private[_-]?key|password|passwd|(?:proxy[_-]?)?authorization"
)
_SECRET_KEY = re.compile(rf"(?:{_SECRET_NAME})", re.IGNORECASE)
_ESCAPED_SECRET_ASSIGNMENT = re.compile(
    rf"""(?i)\\+["'](?:{_SECRET_NAME})\\+["']\s*[=:]\s*\\+["']"""
)


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
    re.compile(
        r"""(?i)(?:proxy-)?authorization(?:\\*["'])?\s*:\s*(?:\\*["'])?"""
        r"[^\r\n\"']+"
    ),
    re.compile(r"(?i)private[_-]?key\s*[=:]\s*\[REDACTED\]"),
    # Quoted values may contain whitespace or escaped quotes, including JSON.
    re.compile(
        rf"""(?i)["']?(?:{_SECRET_NAME})["']?\s*[=:]\s*(?:"(?:\\.|[^"\\])*(?:"|$)|'(?:\\.|[^'\\])*(?:'|$))"""
    ),
    re.compile(rf"(?i)(?:{_SECRET_NAME})\s*[=:]\s*(?!\[REDACTED\])\S+"),
    re.compile(r"eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]+"),
)


def redact_state(value: Any) -> Any:
    """Redact recognized secret fields and nested string values."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            key: REPLACEMENT if is_secret_key(key) else redact_state(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_state(item) for item in value]
    return value


def redact(text: str) -> str:
    """Apply all builtin redaction patterns to ``text``."""
    if not text:
        return text
    if text.lstrip().startswith(("{", "[", '"')):
        try:
            decoded = json.loads(text)
        except ValueError, RecursionError:
            pass
        else:
            if isinstance(decoded, (dict, list, str)):
                clean = redact_state(decoded)
                if clean != decoded:
                    return json.dumps(clean, ensure_ascii=False)
                return text
    # When prose wraps serialized JSON, its inner object cannot be parsed alone.
    if _ESCAPED_SECRET_ASSIGNMENT.search(text):
        return REPLACEMENT
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
