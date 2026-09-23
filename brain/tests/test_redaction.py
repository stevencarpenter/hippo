"""Tests for the Python-side secret redaction module.

These tests intentionally use synthetic credentials of obviously-fake values
(``AKIA...EXAMPLEKEY``, ``ghp_...``) to exercise the patterns. None of the
strings in this file are real secrets.
"""

from dataclasses import dataclass, field
import json

import pytest

from hippo_brain.redaction import REPLACEMENT, redact, redact_segment_secrets


def test_aws_access_key_redacted():
    out = redact("aws_access_key=AKIAIOSFODNN7EXAMPLE end")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert REPLACEMENT in out


def test_github_pat_redacted():
    pat = "ghp_" + "a" * 36
    out = redact(f"git config token {pat} commit")
    assert pat not in out
    assert REPLACEMENT in out


def test_generic_secret_assignment_redacted():
    out = redact("API_KEY = supersecretvalue123")
    assert "supersecretvalue123" not in out


@pytest.mark.parametrize(
    "key",
    ["password", "API_KEY", "api-token", "access_token", "auth-token", "secret_key", "private_key"],
)
@pytest.mark.parametrize("secret", ["short", "two word secret", 'escaped " quote', "line\nbreak"])
def test_json_credential_assignments_redacted(key, secret):
    text = json.dumps({key: secret, "public": "keep"})
    assert redact(text) == '{[REDACTED], "public": "keep"}'


@pytest.mark.parametrize(
    "text",
    [
        "password='two word secret'",
        "'password': 'two word secret'",
        'password="unterminated secret',
        '"password": "unterminated secret',
        'password="line\nbreak"',
    ],
)
def test_quoted_credentials_redacted(text):
    assert redact(text) == REPLACEMENT


def test_jwt_redacted():
    jwt = "eyJabcdefghij.eyJklmnopqrst.signaturepart"
    out = redact(jwt)
    assert "signaturepart" not in out
    assert REPLACEMENT in out


def test_bearer_header_redacted():
    out = redact("Authorization: Bearer abcDEF123token")
    assert "abcDEF123token" not in out


def test_private_key_pem_redacted():
    out = redact("-----BEGIN RSA PRIVATE KEY-----\nbody\n-----END...")
    assert out == REPLACEMENT


@pytest.mark.parametrize("kind", ["", "RSA ", "EC ", "OPENSSH ", "ENCRYPTED "])
@pytest.mark.parametrize("legacy", [False, True])
def test_private_key_body_redacted_with_surrounding_text(kind, legacy):
    begin = REPLACEMENT if legacy else f"-----BEGIN {kind}PRIVATE KEY-----"
    text = f"before\n{begin}\r\nZmFrZXNlY3JldA==\r\n-----END {kind}PRIVATE KEY-----\nafter"
    assert redact(text) == f"before\n{REPLACEMENT}\nafter"


def test_redacted_normal_text_and_public_keys_preserved():
    text = "[REDACTED]\nordinary text\n-----BEGIN PUBLIC KEY-----\nYWJj\n-----END PUBLIC KEY-----"
    assert redact(text) == text


@pytest.mark.parametrize("kind", ["", "RSA ", "EC ", "OPENSSH ", "ENCRYPTED "])
@pytest.mark.parametrize("legacy", [False, True])
def test_assignment_wrapped_private_key_body_redacted(kind, legacy):
    opening = (
        f"[REDACTED] {kind}PRIVATE KEY-----"
        if legacy
        else f"private_key=-----BEGIN {kind}PRIVATE KEY-----"
    )
    text = f"{opening}\nZmFrZXNlY3JldA==\n-----END {kind}PRIVATE KEY-----\nafter"
    assert redact(text) == f"{REPLACEMENT}\nafter"


def test_legacy_encrypted_private_key_metadata_redacted():
    text = (
        "[REDACTED]\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-256-CBC,0123456789ABCDEF\n"
        "\nZmFrZXNlY3JldA==\n-----END RSA PRIVATE KEY-----\nafter"
    )
    assert redact(text) == f"{REPLACEMENT}\nafter"


def test_empty_input_returns_empty():
    assert redact("") == ""


def test_clean_input_unchanged():
    src = "no secrets here, just normal text and code"
    assert redact(src) == src


@dataclass
class _FakeSegment:
    user_prompts: list[str] = field(default_factory=list)
    assistant_texts: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)


def test_redact_segment_secrets_handles_all_fields():
    seg = _FakeSegment(
        user_prompts=["please call API_KEY=abcdefghij1234567890 for me"],
        assistant_texts=["I'll use Authorization: Bearer secret_xyz"],
        tool_calls=[
            {"name": "shell", "summary": "curl -H 'Authorization: Bearer t0kenABC' api"},
            {"name": "shell", "summary": "ls -la"},
        ],
    )

    redact_segment_secrets(seg)

    assert "abcdefghij1234567890" not in seg.user_prompts[0]
    assert "secret_xyz" not in seg.assistant_texts[0]
    assert "t0kenABC" not in seg.tool_calls[0]["summary"]
    # Untouched non-secret tool call passes through verbatim.
    assert seg.tool_calls[1]["summary"] == "ls -la"
    # Tool call name preserved (only summary is rewritten).
    assert seg.tool_calls[0]["name"] == "shell"


def test_redact_segment_secrets_preserves_extra_tool_call_fields():
    seg = _FakeSegment(tool_calls=[{"name": "edit", "summary": "no secret", "extra": "keep me"}])

    redact_segment_secrets(seg)

    assert seg.tool_calls[0]["extra"] == "keep me"


def test_redact_segment_secrets_handles_missing_summary():
    seg = _FakeSegment(tool_calls=[{"name": "noop"}])

    redact_segment_secrets(seg)

    # Should default to redacted("") which is "".
    assert seg.tool_calls[0]["summary"] == ""
