"""Shadow process-group spawn and teardown for hippo-bench v2.

Spawns hippo-daemon + hippo-brain in a shared process group with:
  XDG_DATA_HOME=<run_tree>
  HOME=<run_tree>  (so both Rust dirs::home_dir and Python Path.home resolve to run_tree)
  TMPDIR=<per-run dir>  (isolates daemon socket-fallback from prod's)
  OTEL_RESOURCE_ATTRIBUTES=service.namespace=hippo-bench,...

A minimal config.toml is written to <run_tree>/.config/hippo/config.toml before
spawning so the brain uses the shadow port and data directory.

The caller must copy corpus-v2.sqlite to <run_tree>/hippo.db before calling
spawn_shadow_stack().
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import httpx


@dataclasses.dataclass
class ShadowStack:
    daemon_proc: subprocess.Popen
    brain_proc: subprocess.Popen
    run_tree: pathlib.Path
    process_group_id: int
    brain_base_url: str
    # Per-run system tmpdir. The macOS sun_path limit (104 bytes) is shorter
    # than `<run_tree>/daemon.sock`, so the daemon falls back to
    # `$TMPDIR/hippo-daemon.sock` — without per-run TMPDIR isolation that
    # collides with prod's socket. Cleaned by teardown_shadow_stack.
    tmpdir: pathlib.Path | None = None


def _build_env(
    *,
    run_tree: pathlib.Path,
    run_id: str,
    model_id: str,
    corpus_version: str,
    embedding_model: str,
    otel_enabled: bool,
    tmpdir: pathlib.Path | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(run_tree)
    env["XDG_DATA_HOME"] = str(run_tree)
    # XDG_CONFIG_HOME intentionally not overridden — HOME override means both
    # Rust (dirs::home_dir) and Python (Path.home) resolve config to
    # <run_tree>/.config/hippo/config.toml without a separate env var.
    env.pop("XDG_CONFIG_HOME", None)
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    env["OTEL_RESOURCE_ATTRIBUTES"] = (
        f"service.namespace=hippo-bench,"
        f"bench.run_id={run_id},"
        f"bench.model_id={model_id},"
        f"bench.corpus_version={corpus_version},"
        f"bench.embedding_model={embedding_model}"
    )
    if otel_enabled:
        env["HIPPO_OTEL_ENABLED"] = "1"
    else:
        env.pop("HIPPO_OTEL_ENABLED", None)
    return env


def _write_shadow_config(run_tree: pathlib.Path, brain_port: int) -> None:
    """Write a minimal config.toml into the shadow HOME so both the daemon and
    brain read from it.  The storage.data_dir points at run_tree so the daemon
    opens run_tree/hippo.db — the same file the coordinator copied corpus into."""
    config_dir = run_tree / ".config" / "hippo"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.toml"
    config_path.write_text(
        f'[storage]\ndata_dir = "{run_tree}"\n\n[brain]\nport = {brain_port}\n',
        encoding="utf-8",
    )


def _spawn_pgrp_pair(
    *,
    daemon_cmd: list[str],
    brain_cmd: list[str],
    env: dict[str, str],
    daemon_log: pathlib.Path,
    brain_log: pathlib.Path,
) -> tuple[subprocess.Popen, subprocess.Popen, int]:
    """Spawn two children in a shared process group inside the parent's session.

    Daemon becomes pgrp leader (pgid == pid) via preexec_fn; brain joins it.
    Returns (daemon_proc, brain_proc, pgid). Raises RuntimeError if the
    daemon dies before becoming leader (with a pointer to daemon_log). If the
    brain spawn raises, kills the daemon's pgrp before re-raising — never
    returns a half-initialized pair.

    POSIX: setpgid into a target pgrp requires the target to be in the same
    session as the caller. start_new_session=True on the daemon would put it
    in a new session, breaking brain's join with EPERM.
    """
    with open(daemon_log, "ab") as daemon_log_f:
        daemon_proc = subprocess.Popen(
            daemon_cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=daemon_log_f,
            preexec_fn=lambda: os.setpgid(0, 0),
        )

    # Belt-and-suspenders: parent races child's preexec to install the pgrp.
    # PermissionError = child exec'd first (its preexec already did setpgid).
    # ProcessLookupError = daemon already died (next check raises with log path).
    try:
        os.setpgid(daemon_proc.pid, daemon_proc.pid)
    except ProcessLookupError, PermissionError:
        pass

    # Daemon liveness check (bounded). Without this, a daemon that crashes
    # on exec (broken binary, sandbox assertion failure, missing flag, port
    # already bound) leaves the brain to time out on /health 60s later with
    # a misleading "brain not ready" error — and the real diagnostic stays
    # buried in daemon_log with no breadcrumb. We poll for up to 200ms to
    # give a fast-failing child time to exit before we declare it alive;
    # slower failures are caught by wait_for_brain_ready's parallel daemon
    # poll. If you raise this window, adjust the spawn budget tests too.
    liveness_deadline = time.monotonic() + 0.2
    while time.monotonic() < liveness_deadline:
        if daemon_proc.poll() is not None:
            raise RuntimeError(
                f"daemon exited with code {daemon_proc.returncode} before "
                f"becoming pgrp leader; see {daemon_log} for stderr"
            )
        time.sleep(0.02)
    daemon_pgid = daemon_proc.pid

    try:
        with open(brain_log, "ab") as brain_log_f:
            brain_proc = subprocess.Popen(
                brain_cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=brain_log_f,
                preexec_fn=lambda: os.setpgid(0, daemon_pgid),
            )
    except BaseException:
        # Brain spawn failed — kill the daemon (and its pgrp) so we don't leak
        # a process holding the shadow brain port. Re-raise the original.
        try:
            os.killpg(daemon_pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        raise

    return daemon_proc, brain_proc, daemon_pgid


def spawn_shadow_stack(
    *,
    run_tree: pathlib.Path,
    run_id: str,
    model_id: str,
    corpus_version: str,
    embedding_model: str,
    brain_port: int = 18923,
    otel_enabled: bool = False,
) -> ShadowStack:
    run_tree = pathlib.Path(run_tree)
    logs_dir = run_tree / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    _write_shadow_config(run_tree, brain_port)

    # Per-run tmpdir for the daemon's socket-fallback (`$TMPDIR/hippo-daemon.sock`).
    # Two constraints fight here:
    #   1. Path must fit Unix socket sun_path (104 bytes on macOS).
    #   2. Must be unique per bench run, so we don't collide with prod's
    #      $TMPDIR/hippo-daemon.sock.
    # macOS `$TMPDIR` resolves to `/var/folders/<HASH>/<HASH>/T/` (~51 chars),
    # leaving only ~36 chars for prefix+suffix+socket name. Our run_id-prefixed
    # mkdtemp blew that and the daemon failed bind() with "path must be shorter
    # than SUN_LEN" (BT-29 validation run, 2026-05-04). We use /tmp directly:
    # POSIX-guaranteed and short, leaving ~85 chars of headroom. Run identity
    # comes from the daemon log path, not the socket path.
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="hb-", dir="/tmp"))

    try:
        env = _build_env(
            run_tree=run_tree,
            run_id=run_id,
            model_id=model_id,
            corpus_version=corpus_version,
            embedding_model=embedding_model,
            otel_enabled=otel_enabled,
            tmpdir=tmpdir,
        )

        hippo_bin = shutil.which("hippo") or "hippo"
        uv_bin = shutil.which("uv") or "uv"

        daemon_proc, brain_proc, daemon_pgid = _spawn_pgrp_pair(
            daemon_cmd=[hippo_bin, "serve", "--bench"],
            brain_cmd=[uv_bin, "run", "--project", "brain", "hippo-brain", "serve"],
            env=env,
            daemon_log=logs_dir / "daemon.log",
            brain_log=logs_dir / "brain.log",
        )
    except BaseException:
        # Spawn failed — clean tmpdir before propagating so we don't leak
        # per-run sockets/files across attempts.
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    return ShadowStack(
        daemon_proc=daemon_proc,
        brain_proc=brain_proc,
        run_tree=run_tree,
        process_group_id=daemon_pgid,
        brain_base_url=f"http://127.0.0.1:{brain_port}",
        tmpdir=tmpdir,
    )


def wait_for_brain_ready(stack: ShadowStack, timeout_sec: float = 60.0) -> float:
    """Poll /health until 200. Returns elapsed seconds.

    Raises TimeoutError if the brain never responds, OR RuntimeError if the
    daemon dies during the wait — second-line check beyond _spawn_pgrp_pair's
    bounded liveness window. The brain doesn't talk to the daemon during
    startup, so brain `/health` could return 200 while the daemon is dead;
    this catcher prevents us from declaring readiness in that state.
    """
    start = time.monotonic()
    deadline = start + timeout_sec
    url = f"{stack.brain_base_url}/health"
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if stack.daemon_proc.poll() is not None:
            raise RuntimeError(
                f"daemon exited with code {stack.daemon_proc.returncode} "
                f"during brain readiness wait; see {stack.run_tree}/logs/daemon.log"
            )
        try:
            resp = httpx.get(url, timeout=2.0)
            if resp.status_code == 200:
                return time.monotonic() - start
        except Exception as e:
            last_err = e
        time.sleep(0.25)
    raise TimeoutError(f"brain not ready at {url} within {timeout_sec}s (last_err={last_err!r})")


def teardown_shadow_stack(stack: ShadowStack, sigkill_timeout_sec: float = 10.0) -> None:
    """SIGTERM the process group, wait, SIGKILL if still alive, then clean tmpdir."""
    pgid = stack.process_group_id
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        _cleanup_tmpdir(stack)
        return

    deadline = time.monotonic() + sigkill_timeout_sec
    while time.monotonic() < deadline:
        daemon_done = stack.daemon_proc.poll() is not None
        brain_done = stack.brain_proc.poll() is not None
        if daemon_done and brain_done:
            _cleanup_tmpdir(stack)
            return
        time.sleep(0.1)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        # pgrp gone between SIGTERM and SIGKILL — usually a benign race, but
        # could indicate a process escaped via setsid. Log so future leak
        # triage has a breadcrumb.
        print(
            f"shadow_stack: pgrp {pgid} disappeared before SIGKILL "
            f"(likely benign race; investigate {stack.run_tree}/logs if leaks suspected)",
            file=sys.stderr,
        )
    _cleanup_tmpdir(stack)


def _cleanup_tmpdir(stack: ShadowStack) -> None:
    if stack.tmpdir is None:
        return

    def _on_rmtree_exc(_func, path, exc):
        # Log instead of swallow — accumulating $TMPDIR/hippo-bench-*/ leaks
        # are otherwise invisible to the operator.
        print(
            f"shadow_stack: tmpdir cleanup leaked {path}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

    shutil.rmtree(stack.tmpdir, onexc=_on_rmtree_exc)
