"""Tests for hippo_brain.bench.shadow_stack — env injection, pgrp spawn,
teardown, readiness probe.

Most tests mock subprocess.Popen and httpx (no real hippo binaries spawn).
The real-subprocess test (test_spawn_pgrp_pair_with_real_subprocesses)
exercises the actual _spawn_pgrp_pair helper used by spawn_shadow_stack —
this is what would have caught the cross-session setpgid bug discovered
during the first BT-29 operator run on 2026-05-04.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from hippo_brain.bench import shadow_stack
from hippo_brain.bench.shadow_stack import (
    ShadowStack,
    _spawn_pgrp_pair,
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
    """Patch subprocess.Popen, tempfile.mkdtemp, and pgrp syscalls for tests
    that only inspect kwargs/env passed to Popen.

    Each fake Popen returns a MagicMock with `poll()` returning None (alive),
    so the daemon liveness check in _spawn_pgrp_pair doesn't raise."""
    calls: list[tuple[tuple, dict]] = []

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        proc = MagicMock()
        proc.pid = 99999
        proc.poll.return_value = None  # alive — important for liveness check
        proc.returncode = None
        return proc

    def fake_mkdtemp(prefix: str = "tmp", **_kwargs) -> str:
        d = tmp_path / f"{prefix}fake-mkdtemp"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    monkeypatch.setattr(shadow_stack.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(shadow_stack.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(shadow_stack.os, "getpgid", lambda _pid: 88888)
    # Belt-and-suspenders parent-side setpgid is benign in tests; swallow.
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
    """TMPDIR override uses a per-run path under /tmp (not parent's $TMPDIR
    where prod's socket lives, and not under run_tree where path-length blows
    sun_path). Verifies the mkdtemp call shape; sun_path budget is verified
    end-to-end by test_tmpdir_socket_path_fits_macos_sun_path."""
    mkdtemp_calls: list[dict] = []

    def fake_mkdtemp(**kwargs):
        mkdtemp_calls.append(kwargs)
        d = tmp_path / "fake-mkdtemp"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def fake_popen(*_args, **_kwargs):
        proc = MagicMock()
        proc.pid = 99999
        proc.poll.return_value = None
        return proc

    monkeypatch.setattr(shadow_stack.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(shadow_stack.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(shadow_stack.os, "setpgid", lambda _pid, _pgid: None)

    with patch.dict(os.environ, {"TMPDIR": "/var/folders/should-not-leak"}, clear=True):
        spawn_shadow_stack(**_spawn_kwargs(tmp_path))

    assert len(mkdtemp_calls) == 1
    # /tmp is short and POSIX-guaranteed — leaves room under sun_path for the
    # mkdtemp suffix + "hippo-daemon.sock". macOS $TMPDIR (~51 chars) does NOT.
    assert mkdtemp_calls[0].get("dir") == "/tmp"
    # Short prefix preserves headroom for the random suffix.
    assert mkdtemp_calls[0].get("prefix") == "hb-"


def test_tmpdir_socket_path_fits_macos_sun_path():
    """REGRESSION (BT-29 validation, 2026-05-04): the daemon's socket-fallback
    is `$TMPDIR/hippo-daemon.sock`, and bind() enforces sun_path (104 bytes
    on macOS — the tightest constraint we support). The original mkdtemp
    call used a long run_id-prefixed path under `$TMPDIR`, producing ~133
    char socket paths that failed bind() with `path must be shorter than
    SUN_LEN`. Pin the call shape so a future change reverting to a longer
    prefix or to the system $TMPDIR breaks here, not in production."""
    macos_sun_path_max = 104
    socket_name = "hippo-daemon.sock"

    # Same call shape as shadow_stack.spawn_shadow_stack uses in production.
    tmpdir = tempfile.mkdtemp(prefix="hb-", dir="/tmp")
    try:
        socket_path = f"{tmpdir}/{socket_name}"
        assert len(socket_path) < macos_sun_path_max, (
            f"socket path is {len(socket_path)} bytes, exceeds macOS sun_path "
            f"limit of {macos_sun_path_max}: {socket_path}"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_pgrp_setup_uses_preexec_fn_in_same_session(tmp_path, monkeypatch):
    """REGRESSION: daemon must NOT use start_new_session=True. That puts it in
    a new POSIX session, and brain's setpgid(0, daemon_pgid) then fails with
    EPERM (cross-session setpgid is forbidden). Daemon must use preexec_fn
    setpgid(0, 0) so the new pgrp lives inside the parent's session.

    NB: this is the kwargs-level catcher — it asserts spawn_shadow_stack
    USES the right Popen pattern. The companion
    test_spawn_pgrp_pair_with_real_subprocesses verifies the kernel actually
    accepts that pattern. Both are needed; they catch different regressions."""
    calls = _capture_popen_calls(monkeypatch, tmp_path)
    with patch.dict(os.environ, {}, clear=True):
        spawn_shadow_stack(**_spawn_kwargs(tmp_path))

    assert len(calls) == 2
    daemon_kwargs = calls[0][1]
    brain_kwargs = calls[1][1]
    assert daemon_kwargs.get("start_new_session") is not True
    assert callable(daemon_kwargs.get("preexec_fn"))
    assert brain_kwargs.get("start_new_session") is not True
    assert callable(brain_kwargs.get("preexec_fn"))


def test_otel_disabled_by_default(tmp_path, monkeypatch):
    calls = _capture_popen_calls(monkeypatch, tmp_path)
    with patch.dict(os.environ, {"HIPPO_OTEL_ENABLED": "1"}, clear=True):
        spawn_shadow_stack(**_spawn_kwargs(tmp_path))

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


def test_daemon_dead_after_spawn_raises_with_log_path(tmp_path, monkeypatch):
    """If the daemon crashes on exec, spawn_shadow_stack must raise with the
    daemon log path — NOT silently let the brain spawn and time out 60s
    later on /health with a misleading 'brain not ready' error."""
    popen_calls: list = []

    def fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        proc = MagicMock()
        proc.pid = 99999
        # First call (daemon): poll() returns 1, simulating immediate exit.
        # If the brain is ever spawned, return None to keep test honest.
        proc.poll.return_value = 1 if len(popen_calls) == 1 else None
        proc.returncode = 1 if len(popen_calls) == 1 else None
        return proc

    def fake_mkdtemp(prefix: str = "tmp", **_kwargs) -> str:
        d = tmp_path / f"{prefix}mkdtemp"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    monkeypatch.setattr(shadow_stack.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(shadow_stack.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(shadow_stack.os, "setpgid", lambda _pid, _pgid: None)

    with patch.dict(os.environ, {}, clear=True), pytest.raises(RuntimeError) as exc_info:
        spawn_shadow_stack(**_spawn_kwargs(tmp_path))

    msg = str(exc_info.value)
    assert "daemon exited with code 1" in msg
    assert "daemon.log" in msg
    # Brain spawn must NOT have been attempted after the daemon was dead.
    assert len(popen_calls) == 1


def test_brain_spawn_failure_kills_daemon_pgrp(tmp_path, monkeypatch):
    """If the brain Popen raises, the daemon must be SIGKILL'd — otherwise
    a leaked daemon holds shadow brain port 18923 and breaks the next run."""
    killpg_calls: list[tuple[int, int]] = []
    popen_calls: list = []

    def fake_popen(*_args, **_kwargs):
        if len(popen_calls) == 0:
            # Daemon: succeeds, alive.
            popen_calls.append("daemon")
            proc = MagicMock()
            proc.pid = 99999
            proc.poll.return_value = None
            return proc
        # Brain: spawn raises.
        popen_calls.append("brain")
        raise OSError("ENFILE: too many open files")

    def fake_mkdtemp(prefix: str = "tmp", **_kwargs) -> str:
        d = tmp_path / f"{prefix}mkdtemp"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    monkeypatch.setattr(shadow_stack.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(shadow_stack.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(shadow_stack.os, "setpgid", lambda _pid, _pgid: None)
    monkeypatch.setattr(
        shadow_stack.os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig))
    )

    with patch.dict(os.environ, {}, clear=True), pytest.raises(OSError, match="ENFILE"):
        spawn_shadow_stack(**_spawn_kwargs(tmp_path))

    # Daemon's pgrp must have been SIGKILL'd before the OSError propagated.
    assert killpg_calls == [(99999, signal.SIGKILL)]
    assert popen_calls == ["daemon", "brain"]


def test_spawn_failure_cleans_tmpdir(tmp_path, monkeypatch):
    """tmpdir must be removed when spawn fails — otherwise repeated failed
    bench attempts accumulate $TMPDIR/hippo-bench-*/ leaks."""
    captured_tmpdir: list[str] = []

    def fake_mkdtemp(prefix: str = "tmp", **_kwargs) -> str:
        d = tmp_path / f"{prefix}mkdtemp"
        d.mkdir(parents=True, exist_ok=True)
        captured_tmpdir.append(str(d))
        return str(d)

    def fake_popen(*_args, **_kwargs):
        # Daemon spawn raises immediately, before the brain is touched.
        raise OSError("simulated spawn failure")

    monkeypatch.setattr(shadow_stack.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(shadow_stack.tempfile, "mkdtemp", fake_mkdtemp)

    with patch.dict(os.environ, {}, clear=True), pytest.raises(OSError):
        spawn_shadow_stack(**_spawn_kwargs(tmp_path))

    assert len(captured_tmpdir) == 1
    assert not pathlib.Path(captured_tmpdir[0]).exists(), "tmpdir must be cleaned on spawn failure"


def test_teardown_sigterm_then_sigkill(monkeypatch, tmp_path):
    """When processes don't exit after SIGTERM, teardown escalates to SIGKILL."""
    daemon_proc = MagicMock()
    daemon_proc.poll.return_value = None
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

    teardown_shadow_stack(stack, sigkill_timeout_sec=0.05)

    assert len(killpg_calls) == 2
    assert killpg_calls[0] == (12345, signal.SIGTERM)
    assert killpg_calls[1] == (12345, signal.SIGKILL)
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
    assert not (tmp_path / "early-exit-tmpdir").exists()


def test_teardown_sigkill_pgrp_disappeared_logs_to_stderr(monkeypatch, tmp_path, capsys):
    """If the pgrp dies between SIGTERM and SIGKILL, teardown must log the
    race so future leak triage has a breadcrumb. Today this is silent."""
    daemon_proc = MagicMock()
    daemon_proc.poll.return_value = None  # never exits before SIGKILL
    brain_proc = MagicMock()
    brain_proc.poll.return_value = None

    stack = ShadowStack(
        daemon_proc=daemon_proc,
        brain_proc=brain_proc,
        run_tree=tmp_path / "fake-run-tree",
        process_group_id=99999,
        brain_base_url="http://127.0.0.1:18923",
        tmpdir=tmp_path / "tmpdir",
    )
    (tmp_path / "tmpdir").mkdir()

    killpg_calls: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))
        if sig == signal.SIGKILL:
            raise ProcessLookupError("pgrp gone between SIGTERM and SIGKILL")

    monkeypatch.setattr(shadow_stack.os, "killpg", fake_killpg)

    teardown_shadow_stack(stack, sigkill_timeout_sec=0.05)

    assert killpg_calls == [(99999, signal.SIGTERM), (99999, signal.SIGKILL)]
    err = capsys.readouterr().err
    assert "pgrp 99999 disappeared before SIGKILL" in err


def test_cleanup_tmpdir_logs_leaks_via_onexc(monkeypatch, tmp_path, capsys):
    """When rmtree can't fully clean tmpdir, the failure must be logged —
    not silently swallowed via ignore_errors=True."""
    daemon_proc = MagicMock()
    daemon_proc.poll.return_value = 0
    brain_proc = MagicMock()
    brain_proc.poll.return_value = 0

    stuck_tmpdir = tmp_path / "stuck-tmpdir"
    stuck_tmpdir.mkdir()

    stack = ShadowStack(
        daemon_proc=daemon_proc,
        brain_proc=brain_proc,
        run_tree=tmp_path / "fake-run-tree",
        process_group_id=12345,
        brain_base_url="http://127.0.0.1:18923",
        tmpdir=stuck_tmpdir,
    )

    monkeypatch.setattr(shadow_stack.os, "killpg", lambda _pgid, _sig: None)

    def fake_rmtree(_path, **kwargs):
        # Simulate a child file failing to unlink. shadow_stack's onexc
        # callback should log the failure to stderr.
        cb = kwargs.get("onexc")
        assert cb is not None, "_cleanup_tmpdir must pass onexc, not ignore_errors"
        cb(os.unlink, str(stuck_tmpdir / "leaked-socket"), PermissionError("EPERM"))

    monkeypatch.setattr(shadow_stack.shutil, "rmtree", fake_rmtree)

    teardown_shadow_stack(stack, sigkill_timeout_sec=0.05)

    err = capsys.readouterr().err
    assert "tmpdir cleanup leaked" in err
    assert "leaked-socket" in err
    assert "PermissionError" in err


def test_wait_for_brain_ready_timeout(monkeypatch):
    """When /health never responds and daemon stays alive, raises TimeoutError."""
    daemon_proc = MagicMock()
    daemon_proc.poll.return_value = None  # alive — must NOT trigger RuntimeError
    stack = ShadowStack(
        daemon_proc=daemon_proc,
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


def test_wait_for_brain_ready_raises_when_daemon_dies_during_wait(monkeypatch):
    """If the daemon dies WHILE brain is starting up, wait_for_brain_ready
    must surface the daemon's exit (not silently wait the full 60s and then
    blame the brain). The brain may legitimately come up healthy without the
    daemon, so brain /health=200 is not sufficient evidence of readiness."""
    daemon_proc = MagicMock()
    daemon_proc.poll.return_value = 42  # dead
    daemon_proc.returncode = 42
    stack = ShadowStack(
        daemon_proc=daemon_proc,
        brain_proc=MagicMock(),
        run_tree=pathlib.Path("/tmp/x"),
        process_group_id=12345,
        brain_base_url="http://127.0.0.1:18923",
    )

    def fake_get(*_args, **_kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(shadow_stack.httpx, "get", fake_get)
    monkeypatch.setattr(shadow_stack.time, "sleep", lambda _s: None)

    with pytest.raises(RuntimeError, match="daemon exited with code 42"):
        wait_for_brain_ready(stack, timeout_sec=10.0)


@pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "setpgid"),
    reason="POSIX setpgid required",
)
def test_spawn_pgrp_pair_with_real_subprocesses(tmp_path):
    """REGRESSION (BT-29 first operator run, 2026-05-04): exercises the actual
    `_spawn_pgrp_pair` helper used by spawn_shadow_stack, with `/bin/sh` sleep
    stubs standing in for the daemon and brain. Verifies that the kernel
    actually accepts the setpgid pattern (no EPERM cross-session error) and
    that both processes end up in the same pgrp.

    Mocked tests cannot catch this bug because subprocess.Popen is stubbed
    and the kernel never enforces the cross-session setpgid restriction.
    Calling the real helper (not duplicating the Popen pattern inline) means
    a future revert to start_new_session=True inside _spawn_pgrp_pair would
    break this test."""
    daemon_proc, brain_proc, daemon_pgid = _spawn_pgrp_pair(
        daemon_cmd=["/bin/sh", "-c", "sleep 30"],
        brain_cmd=["/bin/sh", "-c", "sleep 30"],
        env=os.environ.copy(),
        daemon_log=tmp_path / "daemon.log",
        brain_log=tmp_path / "brain.log",
    )
    try:
        # Wait briefly for both children's preexec_fns to install pgrp.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                if (
                    os.getpgid(daemon_proc.pid) == daemon_pgid
                    and os.getpgid(brain_proc.pid) == daemon_pgid
                ):
                    break
            except ProcessLookupError:
                pytest.fail("a child exited before joining pgrp — likely EPERM")
            time.sleep(0.005)
        else:
            pytest.fail(
                f"children never converged on pgrp {daemon_pgid}: "
                f"daemon={os.getpgid(daemon_proc.pid)}, "
                f"brain={os.getpgid(brain_proc.pid)}"
            )

        assert os.getpgid(daemon_proc.pid) == daemon_pgid
        assert os.getpgid(brain_proc.pid) == daemon_pgid
        assert daemon_pgid == daemon_proc.pid
    finally:
        for proc in (brain_proc, daemon_proc):
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)


@pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "setpgid"),
    reason="POSIX setpgid required",
)
def test_spawn_pgrp_pair_raises_with_log_path_when_daemon_dies(tmp_path):
    """Real-subprocess version of test_daemon_dead_after_spawn_raises — uses a
    `/bin/sh -c 'exit 17'` daemon that exits immediately, and asserts the
    helper raises with the daemon log path before attempting brain spawn."""
    daemon_log = tmp_path / "daemon.log"
    brain_log = tmp_path / "brain.log"

    with pytest.raises(RuntimeError) as exc_info:
        _spawn_pgrp_pair(
            daemon_cmd=["/bin/sh", "-c", "exit 17"],
            brain_cmd=["/bin/sh", "-c", "sleep 30"],  # never reached
            env=os.environ.copy(),
            daemon_log=daemon_log,
            brain_log=brain_log,
        )

    msg = str(exc_info.value)
    assert "daemon exited with code 17" in msg
    assert str(daemon_log) in msg
    # Brain log must NOT have been created — we never reached the brain spawn.
    assert not brain_log.exists()
