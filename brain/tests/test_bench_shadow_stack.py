"""Tests for hippo_brain.bench.shadow_stack — env injection, teardown, readiness probe.

Mocks subprocess.Popen and httpx throughout — does NOT spawn real hippo processes.
"""

from __future__ import annotations

import os
import pathlib
import signal
from unittest.mock import MagicMock, patch

import httpx
import pytest

from hippo_brain.bench import shadow_stack
from hippo_brain.bench.shadow_stack import (
    ShadowStack,
    spawn_shadow_stack,
    teardown_shadow_stack,
    wait_for_brain_ready,
)


def _spawn_kwargs(tmp_path: pathlib.Path, **overrides):
    return {
        "run_tree": tmp_path / "run-tree",
        "run_id": "run-2026-04-27-abc",
        "model_id": "qwen3.5-35b-a3b",
        "corpus_version": "corpus-v2",
        "embedding_model": "embedding-test",
        **overrides,
    }


def _capture_popen_calls(monkeypatch):
    calls: list[tuple[tuple, dict]] = []

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        proc = MagicMock()
        proc.pid = 99999  # fake pid; we patch os.getpgid below
        return proc

    monkeypatch.setattr(shadow_stack.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(shadow_stack.os, "getpgid", lambda _pid: 88888)
    return calls


def test_env_injection_otel_resource_attributes(tmp_path, monkeypatch):
    calls = _capture_popen_calls(monkeypatch)
    with patch.dict(os.environ, {}, clear=True):
        spawn_shadow_stack(**_spawn_kwargs(tmp_path))

    assert len(calls) == 2  # daemon + brain
    for _args, kwargs in calls:
        env = kwargs["env"]
        attrs = env["OTEL_RESOURCE_ATTRIBUTES"]
        assert "service.namespace=hippo-bench" in attrs
        assert "bench.run_id=run-2026-04-27-abc" in attrs
        assert "bench.model_id=qwen3.5-35b-a3b" in attrs
        assert "bench.corpus_version=corpus-v2" in attrs


def test_env_injection_xdg_data_home(tmp_path, monkeypatch):
    calls = _capture_popen_calls(monkeypatch)
    run_tree = tmp_path / "run-tree"
    with patch.dict(os.environ, {}, clear=True):
        spawn_shadow_stack(**_spawn_kwargs(tmp_path, run_tree=run_tree))

    assert len(calls) == 2
    for _args, kwargs in calls:
        env = kwargs["env"]
        assert env["XDG_DATA_HOME"] == str(run_tree)
        assert env["XDG_CONFIG_HOME"] == str(run_tree / "config")


def test_start_new_session_flag(tmp_path, monkeypatch):
    calls = _capture_popen_calls(monkeypatch)
    with patch.dict(os.environ, {}, clear=True):
        spawn_shadow_stack(**_spawn_kwargs(tmp_path))

    assert len(calls) == 2
    for _args, kwargs in calls:
        assert kwargs.get("start_new_session") is True


def test_otel_disabled_by_default(tmp_path, monkeypatch):
    calls = _capture_popen_calls(monkeypatch)
    # Even if parent env has HIPPO_OTEL_ENABLED=1, an unsolicited otel_enabled=False
    # call must NOT propagate it to the shadow stack.
    with patch.dict(os.environ, {"HIPPO_OTEL_ENABLED": "1"}, clear=True):
        spawn_shadow_stack(**_spawn_kwargs(tmp_path))  # otel_enabled defaults to False

    assert len(calls) == 2
    for _args, kwargs in calls:
        env = kwargs["env"]
        assert env.get("HIPPO_OTEL_ENABLED", "0") in ("", "0")


def test_otel_enabled_when_requested(tmp_path, monkeypatch):
    calls = _capture_popen_calls(monkeypatch)
    with patch.dict(os.environ, {}, clear=True):
        spawn_shadow_stack(**_spawn_kwargs(tmp_path, otel_enabled=True))

    assert len(calls) == 2
    for _args, kwargs in calls:
        env = kwargs["env"]
        assert env["HIPPO_OTEL_ENABLED"] == "1"


def test_teardown_sigterm_then_sigkill(monkeypatch):
    """When processes don't exit after SIGTERM, teardown escalates to SIGKILL."""
    daemon_proc = MagicMock()
    daemon_proc.poll.return_value = None  # never exits
    brain_proc = MagicMock()
    brain_proc.poll.return_value = None

    stack = ShadowStack(
        daemon_proc=daemon_proc,
        brain_proc=brain_proc,
        run_tree=pathlib.Path("/tmp/x"),
        process_group_id=12345,
        brain_base_url="http://127.0.0.1:18923",
    )

    killpg_calls: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))

    monkeypatch.setattr(shadow_stack.os, "killpg", fake_killpg)

    # Use a tiny timeout so we don't wait the full 10 seconds.
    teardown_shadow_stack(stack, sigkill_timeout_sec=0.05)

    assert len(killpg_calls) == 2
    assert killpg_calls[0] == (12345, signal.SIGTERM)
    assert killpg_calls[1] == (12345, signal.SIGKILL)


def test_teardown_tolerates_process_lookup_error(monkeypatch):
    """If the process group is already gone, teardown should not raise."""
    daemon_proc = MagicMock()
    daemon_proc.poll.return_value = 0
    brain_proc = MagicMock()
    brain_proc.poll.return_value = 0

    stack = ShadowStack(
        daemon_proc=daemon_proc,
        brain_proc=brain_proc,
        run_tree=pathlib.Path("/tmp/x"),
        process_group_id=12345,
        brain_base_url="http://127.0.0.1:18923",
    )

    def fake_killpg(_pgid, _sig):
        raise ProcessLookupError("no such process group")

    monkeypatch.setattr(shadow_stack.os, "killpg", fake_killpg)

    teardown_shadow_stack(stack, sigkill_timeout_sec=0.05)


def test_wait_for_brain_ready_timeout(monkeypatch):
    """When /health never responds, wait_for_brain_ready raises TimeoutError."""
    stack = ShadowStack(
        daemon_proc=MagicMock(),
        brain_proc=MagicMock(),
        run_tree=pathlib.Path("/tmp/x"),
        process_group_id=12345,
        brain_base_url="http://127.0.0.1:18923",
    )

    def fake_get(*_args, **_kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(shadow_stack.httpx, "get", fake_get)
    monkeypatch.setattr(shadow_stack.time, "sleep", lambda _s: None)

    with pytest.raises(TimeoutError):
        wait_for_brain_ready(stack, timeout_sec=0.05)
