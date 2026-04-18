"""Tests for hippo_brain.__init__.main() command dispatch."""

import sys
from unittest.mock import MagicMock, patch

import pytest

import hippo_brain
from hippo_brain import main


def test_main_no_args_prints_usage_and_exits(capsys):
    """No arguments -> prints usage and exits 1."""
    with patch.object(sys, "argv", ["hippo-brain"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Usage:" in captured.out


def test_main_unknown_command_prints_error_and_exits(capsys):
    """Unknown command -> prints error and exits 1."""
    with patch.object(sys, "argv", ["hippo-brain", "bogus"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Unknown command: bogus" in captured.out


def test_main_serve_dispatches(monkeypatch):
    """'serve' subcommand imports create_app and calls uvicorn.run."""
    monkeypatch.setattr(sys, "argv", ["hippo-brain", "serve"])

    mock_create_app = MagicMock(return_value="fake-app")
    mock_uvicorn = MagicMock()

    with patch.dict("sys.modules", {"uvicorn": mock_uvicorn}):
        with patch("hippo_brain.server.create_app", mock_create_app):
            with patch(
                "hippo_brain._load_runtime_settings",
                return_value={
                    "db_path": "",
                    "data_dir": "",
                    "lmstudio_base_url": "http://localhost:1234/v1",
                    "lmstudio_timeout_secs": 300.0,
                    "enrichment_model": "",
                    "embedding_model": "",
                    "query_model": "",
                    "poll_interval_secs": 5,
                    "enrichment_batch_size": 10,
                    "max_events_per_chunk": 10,
                    "session_stale_secs": 120,
                    "port": 9175,
                    "telemetry_endpoint": "http://localhost:4318",
                },
            ):
                hippo_brain.main()

    mock_create_app.assert_called_once_with(
        db_path="",
        data_dir="",
        lmstudio_base_url="http://localhost:1234/v1",
        lmstudio_timeout_secs=300.0,
        enrichment_model="",
        embedding_model="",
        query_model="",
        poll_interval_secs=5,
        enrichment_batch_size=10,
        session_stale_secs=120,
    )
    mock_uvicorn.run.assert_called_once_with("fake-app", host="127.0.0.1", port=9175)


def test_main_serve_uses_config_runtime_settings(monkeypatch):
    """'serve' should pass config-derived settings to create_app and uvicorn.run."""
    monkeypatch.setattr(sys, "argv", ["hippo-brain", "serve"])

    mock_create_app = MagicMock(return_value="fake-app")
    mock_uvicorn = MagicMock()
    runtime_settings = {
        "db_path": "/tmp/hippo.db",
        "data_dir": "/tmp",
        "lmstudio_base_url": "http://localhost:2222/v1",
        "lmstudio_timeout_secs": 300.0,
        "enrichment_model": "local-model",
        "embedding_model": "local-embed",
        "query_model": "local-query",
        "poll_interval_secs": 9,
        "enrichment_batch_size": 3,
        "max_events_per_chunk": 3,
        "session_stale_secs": 60,
        "port": 9444,
        "telemetry_endpoint": "http://localhost:4318",
    }

    with patch.dict("sys.modules", {"uvicorn": mock_uvicorn}):
        with patch("hippo_brain.server.create_app", mock_create_app):
            with patch("hippo_brain._load_runtime_settings", return_value=runtime_settings):
                hippo_brain.main()

    mock_create_app.assert_called_once_with(
        db_path="/tmp/hippo.db",
        data_dir="/tmp",
        lmstudio_base_url="http://localhost:2222/v1",
        lmstudio_timeout_secs=300.0,
        enrichment_model="local-model",
        embedding_model="local-embed",
        query_model="local-query",
        poll_interval_secs=9,
        enrichment_batch_size=3,
        session_stale_secs=60,
    )
    mock_uvicorn.run.assert_called_once_with("fake-app", host="127.0.0.1", port=9444)


def test_main_enrich_prints_message(capsys, monkeypatch):
    """'enrich' subcommand prints the not-yet-implemented message."""
    monkeypatch.setattr(sys, "argv", ["hippo-brain", "enrich"])
    # Should NOT raise SystemExit
    main()
    captured = capsys.readouterr()
    assert "not yet implemented" in captured.out.lower()
