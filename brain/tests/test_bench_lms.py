from unittest.mock import patch, MagicMock

import pytest

from hippo_brain.bench.lms import (
    LmsError,
    LmsUnavailable,
    ensure_available,
    list_loaded,
    load,
    unload,
    unload_all,
)


def test_ensure_available_ok_when_binary_present():
    with patch("shutil.which", return_value="/usr/local/bin/lms"):
        # Should not raise.
        ensure_available()


def test_ensure_available_raises_when_binary_missing():
    with patch("shutil.which", return_value=None):
        with pytest.raises(LmsUnavailable):
            ensure_available()


def test_list_loaded_parses_json_output():
    fake_proc = MagicMock(returncode=0, stdout='[{"identifier":"qwen-35b","state":"loaded"}]')
    with patch("subprocess.run", return_value=fake_proc):
        result = list_loaded()
    assert result == [{"identifier": "qwen-35b", "state": "loaded"}]


def test_list_loaded_raises_on_nonzero_exit():
    fake_proc = MagicMock(returncode=1, stdout="", stderr="boom")
    with patch("subprocess.run", return_value=fake_proc):
        with pytest.raises(LmsError):
            list_loaded()


def test_load_invokes_lms_load_with_identifier():
    fake_proc = MagicMock(returncode=0, stdout="")
    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        load("qwen-35b")
    args = mock_run.call_args.args[0]
    assert args[:2] == ["lms", "load"]
    assert "qwen-35b" in args


def test_unload_invokes_lms_unload():
    fake_proc = MagicMock(returncode=0, stdout="")
    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        unload("qwen-35b")
    args = mock_run.call_args.args[0]
    assert args[:2] == ["lms", "unload"]


def test_unload_all_invokes_unload_all():
    fake_proc = MagicMock(returncode=0, stdout="")
    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        unload_all()
    args = mock_run.call_args.args[0]
    assert args[:3] == ["lms", "unload", "--all"]
