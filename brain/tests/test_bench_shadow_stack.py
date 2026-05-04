"""Tests for hippo_brain.bench.shadow_stack — env injection, teardown, readiness probe.

Most tests mock subprocess.Popen and httpx (does NOT spawn real hippo). The
real-subprocess regression test at the bottom (test_pgrp_join_with_real_subprocesses)
is the one that would have caught the cross-session setpgid bug discovered
during the first BT-29 operator run on 2026-05-04 — see comments in
shadow_stack.spawn_shadow_stack for the full incident.
"""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
import time
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


def _capture_popen_calls(monkeypatch, tmp_path: pathlib.Path):
    """Patch subprocess.Popen, tempfile.mkdtemp, and os.setpgid for tests that
    only inspect the kwargs/env passed to Popen. Returns the captured calls
    list. Real subprocess work happens in test_pgrp_join_with_real_subprocesses."""
    calls: list[tuple[tuple, dict]] = []

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        proc = MagicMock()
        proc.pid = 99999  # fake pid
        return proc

    def fake_mkdtemp(prefix: str = "tmp", **_kwargs) -> str:
        d = tmp_path / f"{prefix}fake-mkdtemp"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    monkeypatch.setattr(shadow_stack.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(shadow_stack.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(shadow_stack.os, "getpgid", lambda _pid: 88888)
    # Belt-and-suspenders parent-side setpgid is benign in tests; swallow it.
    monkeypatch.setattr(shadow_stack.os, "setpgid", lambda _pid, _pgid: None)
    return calls


def test_env_injection_otel_resource_attributes(tmp_path, monkeypatch):
    calls = _capture_popen_calls(monkeypatch, tmp_path)
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
    calls = _capture_popen_calls(monkeypatch, tmp_path)
    run_tree = tmp_path / "run-tree"
    with patch.dict(os.environ, {}, clear=True):
        spawn_shadow_stack(**_spawn_kwargs(tmp_path, run_tree=run_tree))

    assert len(calls) == 2
    for _args, kwargs in calls:
        env = kwargs["env"]
        assert env["XDG_DATA_HOME"] == str(run_tree)
        # HOME is overridden so both Rust dirs::home_dir and Python Path.home
        # resolve to run_tree; XDG_CONFIG_HOME is intentionally not set so both
        # tools fall through to <HOME>/.config/hippo/config.toml.
        assert env["HOME"] == str(run_tree)
        assert "XDG_CONFIG_HOME" not in env


def test_env_injection_isolates_tmpdir(tmp_path, monkeypatch):
    """TMPDIR is overridden to a per-run path so the daemon's socket-fallback
    path (`$TMPDIR/hippo-daemon.sock`) does not collide with the prod
    daemon's. Without this, both daemons race for one socket and the bench
    silently corrupts capture state. See follow-up #1 in PR #X."""
    calls = _capture_popen_calls(monkeypatch, tmp_path)
    with patch.dict(os.environ, {"TMPDIR": "/var/folders/should-not-leak"}, clear=True):
        spawn_shadow_stack(**_spawn_kwargs(tmp_path))

    assert len(calls) == 2
    for _args, kwargs in calls:
        env = kwargs["env"]
        # Must NOT inherit the parent's TMPDIR (that's where prod's socket lives).
        assert env["TMPDIR"] != "/var/folders/should-not-leak"
        # Must point at a per-run path containing the run_id, so concurrent
        # bench runs don't collide with each other either.
        assert "run-2026-04-27-abc" in env["TMPDIR"]


def test_pgrp_setup_uses_preexec_fn_in_same_session(tmp_path, monkeypatch):
    """REGRESSION: daemon must NOT use start_new_session=True. That puts it in
    a new POSIX session, and brain's setpgid(0, daemon_pgid) then fails with
    EPERM (cross-session setpgid is forbidden). Daemon must use preexec_fn
    setpgid(0, 0) so the new pgrp lives inside the parent's session, and
    brain's preexec setpgid into that pgrp succeeds.

    NB: this test only inspects Popen kwargs — it does NOT exercise the actual
    POSIX behavior. The test_pgrp_join_with_real_subprocesses test below is
    what verifies the kernel actually accepts the resulting setpgid pattern."""
    calls = _capture_popen_calls(monkeypatch, tmp_path)
    with patch.dict(os.environ, {}, clear=True):
        spawn_shadow_stack(**_spawn_kwargs(tmp_path))

    assert len(calls) == 2
    daemon_kwargs = calls[0][1]
    brain_kwargs = calls[1][1]
    # Daemon stays in parent's session and creates a new pgrp via preexec_fn.
    # start_new_session would put it in a NEW session, breaking brain's join.
    assert daemon_kwargs.get("start_new_session") is not True
    assert callable(daemon_kwargs.get("preexec_fn"))
    # Brain joins daemon's process group via preexec_fn (same session, OK).
    assert brain_kwargs.get("start_new_session") is not True
    assert callable(brain_kwargs.get("preexec_fn"))


def test_otel_disabled_by_default(tmp_path, monkeypatch):
    calls = _capture_popen_calls(monkeypatch, tmp_path)
    # Even if parent env has HIPPO_OTEL_ENABLED=1, an unsolicited otel_enabled=False
    # call must NOT propagate it to the shadow stack.
    with patch.dict(os.environ, {"HIPPO_OTEL_ENABLED": "1"}, clear=True):
        spawn_shadow_stack(**_spawn_kwargs(tmp_path))  # otel_enabled defaults to False

    assert len(calls) == 2
    for _args, kwargs in calls:
        env = kwargs["env"]
        assert env.get("HIPPO_OTEL_ENABLED", "0") in ("", "0")


def test_otel_enabled_when_requested(tmp_path, monkeypatch):
    calls = _capture_popen_calls(monkeypatch, tmp_path)
    with patch.dict(os.environ, {}, clear=True):
        spawn_shadow_stack(**_spawn_kwargs(tmp_path, otel_enabled=True))

    assert len(calls) == 2
    for _args, kwargs in calls:
        env = kwargs["env"]
        assert env["HIPPO_OTEL_ENABLED"] == "1"


def test_teardown_sigterm_then_sigkill(monkeypatch, tmp_path):
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
        tmpdir=tmp_path / "leftover-tmpdir",
    )
    (tmp_path / "leftover-tmpdir").mkdir()

    killpg_calls: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))

    monkeypatch.setattr(shadow_stack.os, "killpg", fake_killpg)

    # Use a tiny timeout so we don't wait the full 10 seconds.
    teardown_shadow_stack(stack, sigkill_timeout_sec=0.05)

    assert len(killpg_calls) == 2
    assert killpg_calls[0] == (12345, signal.SIGTERM)
    assert killpg_calls[1] == (12345, signal.SIGKILL)
    # tmpdir must be cleaned even on SIGKILL path (no leak across runs).
    assert not (tmp_path / "leftover-tmpdir").exists()


def test_teardown_tolerates_process_lookup_error(monkeypatch, tmp_path):
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
        tmpdir=tmp_path / "early-exit-tmpdir",
    )
    (tmp_path / "early-exit-tmpdir").mkdir()

    def fake_killpg(_pgid, _sig):
        raise ProcessLookupError("no such process group")

    monkeypatch.setattr(shadow_stack.os, "killpg", fake_killpg)

    teardown_shadow_stack(stack, sigkill_timeout_sec=0.05)
    # Even when the pgrp is gone before SIGTERM lands, the tmpdir still cleans up.
    assert not (tmp_path / "early-exit-tmpdir").exists()


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


@pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "setpgid"),
    reason="POSIX setpgid required",
)
def test_pgrp_join_with_real_subprocesses():
    """REGRESSION (BT-29 first operator run, 2026-05-04): exercises the EXACT
    Popen pattern shadow_stack uses, with two real /bin/sh sleep stubs
    standing in for the daemon and the brain. Verifies that:

      1. The daemon ends up as its own process-group leader (pgid == pid).
      2. The brain successfully joins the daemon's pgrp without raising
         SubprocessError ('Exception occurred in preexec_fn').
      3. Daemon and brain end up in the SAME pgrp, so a single os.killpg
         tears both down.

    Mocked tests cannot catch this bug because subprocess.Popen is stubbed and
    the kernel never enforces the cross-session setpgid restriction. The
    original implementation passed start_new_session=True on the daemon, which
    silently broke this test would surface as EPERM in step 2."""
    daemon = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30"],
        preexec_fn=lambda: os.setpgid(0, 0),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Belt-and-suspenders parent-side setpgid; matches shadow_stack.
        try:
            os.setpgid(daemon.pid, daemon.pid)
        except ProcessLookupError, PermissionError:
            pass

        # Wait for daemon to actually be its own pgrp leader (preexec ran).
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                if os.getpgid(daemon.pid) == daemon.pid:
                    break
            except ProcessLookupError:
                pytest.fail(f"daemon {daemon.pid} exited before becoming pgrp leader")
            time.sleep(0.005)
        else:
            pytest.fail(f"daemon {daemon.pid} never became its own pgrp leader")

        daemon_pgid = daemon.pid

        brain = subprocess.Popen(
            ["/bin/sh", "-c", "sleep 30"],
            preexec_fn=lambda: os.setpgid(0, daemon_pgid),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Wait for brain to land in daemon's pgrp.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    if os.getpgid(brain.pid) == daemon_pgid:
                        break
                except ProcessLookupError:
                    pytest.fail(
                        f"brain {brain.pid} exited before joining pgrp "
                        f"{daemon_pgid} — likely EPERM in preexec_fn"
                    )
                time.sleep(0.005)
            else:
                pytest.fail(f"brain {brain.pid} never joined daemon pgrp {daemon_pgid}")

            assert os.getpgid(brain.pid) == os.getpgid(daemon.pid) == daemon_pgid
        finally:
            brain.terminate()
            try:
                brain.wait(timeout=2)
            except subprocess.TimeoutExpired:
                brain.kill()
                brain.wait(timeout=2)
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=2)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=2)
