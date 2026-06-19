"""Tests for the structured hippo-brain API CLI."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hippo_brain import api_cli


class _Response:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> dict:
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
