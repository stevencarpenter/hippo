"""Tests for hippo_brain.__init__.main() command dispatch."""

import sys
from unittest.mock import MagicMock, patch

import pytest

import hippo_brain
from hippo_brain import main


def test_main_no_args_prints_usage_and_exits(capsys):
    """No arguments -> argparse prints a usage error and exits nonzero."""
    with patch.object(sys, "argv", ["hippo-brain"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        # argparse uses exit code 2 for argument errors
        assert exc_info.value.code == 2
    captured = capsys.readouterr()
    # argparse writes the usage synopsis to stderr
    assert "usage:" in captured.err.lower()


def test_main_unknown_command_prints_error_and_exits(capsys):
    """Unknown command -> argparse prints 'invalid choice' and exits nonzero."""
    with patch.object(sys, "argv", ["hippo-brain", "bogus"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "invalid choice" in captured.err.lower()
    assert "bogus" in captured.err


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
                    "inference_base_url": "http://localhost:1234/v1",
                    "inference_timeout_secs": 300.0,
                    "enrichment_model": "",
                    "embedding_model": "",
                    "query_model": "",
                    "poll_interval_secs": 5,
                    "enrichment_batch_size": 10,
                    "max_events_per_chunk": 10,
                    "session_stale_secs": 120,
                    "port": 9175,
                    "telemetry_endpoint": "http://localhost:4318",
                    "max_claim_batch": 10,
                    "lock_timeout_secs": 600,
                    "long_dwell_bypass_ms": 120_000,
                },
            ):
                hippo_brain.main()

    mock_create_app.assert_called_once_with(
        db_path="",
        data_dir="",
        inference_base_url="http://localhost:1234/v1",
        inference_timeout_secs=300.0,
        enrichment_model="",
        embedding_model="",
        query_model="",
        poll_interval_secs=5,
        enrichment_batch_size=10,
        session_stale_secs=120,
        max_claim_batch=10,
        lock_timeout_ms=600_000,
        long_dwell_bypass_ms=120_000,
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
        "inference_base_url": "http://localhost:2222/v1",
        "inference_timeout_secs": 300.0,
        "enrichment_model": "local-model",
        "embedding_model": "local-embed",
        "query_model": "local-query",
        "poll_interval_secs": 9,
        "enrichment_batch_size": 3,
        "max_events_per_chunk": 3,
        "session_stale_secs": 60,
        "port": 9444,
        "telemetry_endpoint": "http://localhost:4318",
        "max_claim_batch": 7,
        "lock_timeout_secs": 900,
        "long_dwell_bypass_ms": 240_000,
    }

    with patch.dict("sys.modules", {"uvicorn": mock_uvicorn}):
        with patch("hippo_brain.server.create_app", mock_create_app):
            with patch("hippo_brain._load_runtime_settings", return_value=runtime_settings):
                hippo_brain.main()

    mock_create_app.assert_called_once_with(
        db_path="/tmp/hippo.db",
        data_dir="/tmp",
        inference_base_url="http://localhost:2222/v1",
        inference_timeout_secs=300.0,
        enrichment_model="local-model",
        embedding_model="local-embed",
        query_model="local-query",
        poll_interval_secs=9,
        enrichment_batch_size=3,
        session_stale_secs=60,
        max_claim_batch=7,
        lock_timeout_ms=900_000,
        long_dwell_bypass_ms=240_000,
    )
    mock_uvicorn.run.assert_called_once_with("fake-app", host="127.0.0.1", port=9444)


def test_main_enrich_prints_message(capsys, monkeypatch):
    """'enrich' subcommand prints the not-yet-implemented message."""
    monkeypatch.setattr(sys, "argv", ["hippo-brain", "enrich"])
    # Should NOT raise SystemExit
    main()
    captured = capsys.readouterr()
    assert "not yet implemented" in captured.out.lower()


def test_load_runtime_settings_rejects_legacy_lmstudio_section(tmp_path, monkeypatch):
    """Legacy [lmstudio] section must raise a migration error, not silently
    fall back to defaults. This is the symptom from the omlx-PR-completion
    incident: brain pointed at localhost:1234 forever because the loader
    couldn't see the user's [inference] section under the old name."""
    from pathlib import Path

    from hippo_brain import _load_runtime_settings

    config_dir = tmp_path / ".config" / "hippo"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text('[lmstudio]\nbase_url = "http://localhost:1234/v1"\n')
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with pytest.raises(RuntimeError, match=r"\[lmstudio\].*\[inference\]"):
        _load_runtime_settings()


def test_load_runtime_settings_reads_inference_section(tmp_path, monkeypatch):
    """[inference] section is read into inference_base_url + inference_timeout_secs."""
    from pathlib import Path

    from hippo_brain import _load_runtime_settings

    config_dir = tmp_path / ".config" / "hippo"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        '[inference]\nbase_url = "http://omlx:8000/v1"\ntimeout_secs = 120\n'
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    settings = _load_runtime_settings()
    assert settings["inference_base_url"] == "http://omlx:8000/v1"
    assert settings["inference_timeout_secs"] == 120.0
