"""Tests for the Python-side secret redaction module.

These tests intentionally use synthetic credentials of obviously-fake values
(``AKIA...EXAMPLEKEY``, ``ghp_...``) to exercise the patterns. None of the
strings in this file are real secrets.
"""

from dataclasses import dataclass, field

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
    assert "BEGIN RSA PRIVATE KEY" not in out


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
