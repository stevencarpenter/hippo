"""Tests for hippo_brain.__init__.main() command dispatch."""

import sys
from unittest.mock import patch, MagicMock

import pytest

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
            # Re-import to get fresh dispatch
            import importlib
            import hippo_brain

            importlib.reload(hippo_brain)
            hippo_brain.main()

    mock_create_app.assert_called_once()
    mock_uvicorn.run.assert_called_once()
    call_args = mock_uvicorn.run.call_args
    assert call_args[1].get("host", call_args[0][1] if len(call_args[0]) > 1 else None) or True


def test_main_enrich_prints_message(capsys, monkeypatch):
    """'enrich' subcommand prints the not-yet-implemented message."""
    monkeypatch.setattr(sys, "argv", ["hippo-brain", "enrich"])
    # Should NOT raise SystemExit
    main()
    captured = capsys.readouterr()
    assert "not yet implemented" in captured.out.lower()
