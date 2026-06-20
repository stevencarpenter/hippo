"""Tests for the structured hippo-brain API CLI."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from hippo_brain import api_cli


class _Response:
    def __init__(
        self,
        payload: dict,
        status_code: int = 200,
        *,
        text: str | None = None,
        raise_json: bool = False,
    ):
        self._payload = payload
        self.status_code = status_code
        self._raise_json = raise_json
        # Mirror httpx.Response.text: the raw JSON body, not a Python dict repr.
        self.text = text if text is not None else json.dumps(payload)

    def json(self) -> dict:
        if self._raise_json:
            raise ValueError("response body is not valid JSON")
        return self._payload


def test_pause_posts_to_control_pause(capsys: pytest.CaptureFixture):
    with patch(
        "hippo_brain.api_cli.httpx.request", return_value=_Response({"paused_at": "now"})
    ) as req:
        rc = api_cli.main(["--url", "http://brain.local", "pause"])

    assert rc == 0
    req.assert_called_once_with(
        "POST",
        "http://brain.local/control/pause",
        params=None,
        json=None,
        timeout=10.0,
    )
    assert '"paused_at": "now"' in capsys.readouterr().out


def test_query_posts_structured_body(capsys: pytest.CaptureFixture):
    with patch(
        "hippo_brain.api_cli.httpx.request", return_value=_Response({"mode": "lexical"})
    ) as req:
        rc = api_cli.main(
            [
                "--url",
                "http://brain.local",
                "query",
                "cargo test",
                "--mode",
                "lexical",
                "--limit",
                "3",
            ]
        )

    assert rc == 0
    req.assert_called_once_with(
        "POST",
        "http://brain.local/query",
        params=None,
        json={"text": "cargo test", "mode": "lexical", "limit": 3},
        timeout=10.0,
    )
    assert '"mode": "lexical"' in capsys.readouterr().out


def test_events_gets_with_query_params():
    with patch("hippo_brain.api_cli.httpx.request", return_value=_Response({"events": []})) as req:
        rc = api_cli.main(
            [
                "--url",
                "http://brain.local",
                "events",
                "--limit",
                "2",
                "--offset",
                "4",
                "--session-id",
                "10",
                "--since-ms",
                "123",
                "--project",
                "hippo",
            ]
        )

    assert rc == 0
    req.assert_called_once_with(
        "GET",
        "http://brain.local/events",
        params={
            "limit": 2,
            "offset": 4,
            "session_id": 10,
            "since_ms": 123,
            "project": "hippo",
        },
        json=None,
        timeout=10.0,
    )


def test_openapi_offline_prints_contract_without_http(capsys: pytest.CaptureFixture):
    with patch("hippo_brain.api_cli.httpx.request") as req:
        rc = api_cli.main(["openapi", "--offline"])

    assert rc == 0
    req.assert_not_called()
    assert '"openapi": "3.1.0"' in capsys.readouterr().out


def test_http_errors_return_nonzero(capsys: pytest.CaptureFixture):
    with patch("hippo_brain.api_cli.httpx.request", return_value=_Response({"error": "bad"}, 503)):
        rc = api_cli.main(["--url", "http://brain.local", "health"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "HTTP 503" in err
    assert "bad" in err


def test_sessions_gets_with_pagination():
    with patch(
        "hippo_brain.api_cli.httpx.request", return_value=_Response({"sessions": []})
    ) as req:
        rc = api_cli.main(
            [
                "--url",
                "http://brain.local",
                "sessions",
                "--limit",
                "5",
                "--offset",
                "1",
                "--since-ms",
                "99",
            ]
        )

    assert rc == 0
    req.assert_called_once_with(
        "GET",
        "http://brain.local/sessions",
        params={"limit": 5, "offset": 1, "since_ms": 99},
        json=None,
        timeout=10.0,
    )


def test_knowledge_gets_with_filters():
    with patch(
        "hippo_brain.api_cli.httpx.request", return_value=_Response({"nodes": [], "total": 0})
    ) as req:
        rc = api_cli.main(
            [
                "--url",
                "http://brain.local",
                "knowledge",
                "--limit",
                "2",
                "--node-type",
                "lesson",
                "--since-ms",
                "5",
            ]
        )

    assert rc == 0
    # --offset omitted -> dropped by _optional_params, not sent as None.
    req.assert_called_once_with(
        "GET",
        "http://brain.local/knowledge",
        params={"limit": 2, "node_type": "lesson", "since_ms": 5},
        json=None,
        timeout=10.0,
    )


def test_knowledge_get_targets_id_path():
    with patch("hippo_brain.api_cli.httpx.request", return_value=_Response({"id": 7})) as req:
        rc = api_cli.main(["--url", "http://brain.local", "knowledge-get", "7"])

    assert rc == 0
    req.assert_called_once_with(
        "GET",
        "http://brain.local/knowledge/7",
        params=None,
        json=None,
        timeout=10.0,
    )


def test_ask_posts_question_body():
    with patch(
        "hippo_brain.api_cli.httpx.request", return_value=_Response({"answer": "hi"})
    ) as req:
        rc = api_cli.main(["--url", "http://brain.local", "ask", "why is CI red?", "--limit", "4"])

    assert rc == 0
    req.assert_called_once_with(
        "POST",
        "http://brain.local/ask",
        params=None,
        json={"question": "why is CI red?", "limit": 4},
        timeout=10.0,
    )


def test_resume_posts_to_control_resume():
    with patch(
        "hippo_brain.api_cli.httpx.request", return_value=_Response({"resumed_at": "now"})
    ) as req:
        rc = api_cli.main(["--url", "http://brain.local", "resume"])

    assert rc == 0
    req.assert_called_once_with(
        "POST",
        "http://brain.local/control/resume",
        params=None,
        json=None,
        timeout=10.0,
    )


def test_non_json_success_body_is_printed_raw(capsys: pytest.CaptureFixture):
    # A 2xx whose body isn't JSON: _request falls back to printing response.text.
    with patch(
        "hippo_brain.api_cli.httpx.request",
        return_value=_Response({}, 200, text="plain text body", raise_json=True),
    ):
        rc = api_cli.main(["--url", "http://brain.local", "health"])

    assert rc == 0
    assert "plain text body" in capsys.readouterr().out
