"""Shadow process-group spawn and teardown for hippo-bench v2.

Spawns hippo-daemon + hippo-brain in their own process group with:
  XDG_DATA_HOME=<run_tree>
  HOME=<run_tree>  (so both Rust dirs::home_dir and Python Path.home resolve to run_tree)
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
import time

import httpx


@dataclasses.dataclass
class ShadowStack:
    daemon_proc: subprocess.Popen
    brain_proc: subprocess.Popen
    run_tree: pathlib.Path
    process_group_id: int
    brain_base_url: str


def _build_env(
    *,
    run_tree: pathlib.Path,
    run_id: str,
    model_id: str,
    corpus_version: str,
    embedding_model: str,
    otel_enabled: bool,
) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(run_tree)
    env["XDG_DATA_HOME"] = str(run_tree)
    # XDG_CONFIG_HOME intentionally not overridden — HOME override means both
    # Rust (dirs::home_dir) and Python (Path.home) resolve config to
    # <run_tree>/.config/hippo/config.toml without a separate env var.
    env.pop("XDG_CONFIG_HOME", None)
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

    env = _build_env(
        run_tree=run_tree,
        run_id=run_id,
        model_id=model_id,
        corpus_version=corpus_version,
        embedding_model=embedding_model,
        otel_enabled=otel_enabled,
    )

    hippo_bin = shutil.which("hippo") or "hippo"
    uv_bin = shutil.which("uv") or "uv"

    # Daemon gets its own session (new process group).
    # The brain joins the daemon's process group so a single os.killpg tears
    # both down without orphaning either process.
    #
    # NOTE: Use "daemon run" — there is no `hippo serve` subcommand. PR #127
    # shipped `[hippo_bin, "serve"]` which silently failed: shadow daemon
    # crashed on spawn, brain still came up against the pre-copied corpus DB
    # so JSONL output kept appearing while bench had no daemon-side
    # telemetry. Caught by panel review (BT-02).
    # BT-11: --bench tells the daemon to log bench mode and assert sandbox
    # isolation. Use `serve` (the BT-09 alias) instead of `daemon run` so the
    # flag has somewhere to land — `daemon run` doesn't accept --bench.
    with open(logs_dir / "daemon.log", "ab") as daemon_log:
        daemon_proc = subprocess.Popen(
            [hippo_bin, "serve", "--bench"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=daemon_log,
            start_new_session=True,
        )
    # With start_new_session=True the child calls setsid(), making its PID the
    # process-group leader.  pgid == pid is guaranteed.
    daemon_pgid = daemon_proc.pid

    with open(logs_dir / "brain.log", "ab") as brain_log:
        brain_proc = subprocess.Popen(
            [uv_bin, "run", "--project", "brain", "hippo-brain", "serve"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=brain_log,
            preexec_fn=lambda: os.setpgid(0, daemon_pgid),
        )

    return ShadowStack(
        daemon_proc=daemon_proc,
        brain_proc=brain_proc,
        run_tree=run_tree,
        process_group_id=daemon_pgid,
        brain_base_url=f"http://127.0.0.1:{brain_port}",
    )


def wait_for_brain_ready(stack: ShadowStack, timeout_sec: float = 60.0) -> float:
    """Poll /health until 200. Returns elapsed seconds. Raises TimeoutError."""
    start = time.monotonic()
    deadline = start + timeout_sec
    url = f"{stack.brain_base_url}/health"
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=2.0)
            if resp.status_code == 200:
                return time.monotonic() - start
        except Exception as e:
            last_err = e
        time.sleep(0.25)
    raise TimeoutError(f"brain not ready at {url} within {timeout_sec}s (last_err={last_err!r})")


def teardown_shadow_stack(stack: ShadowStack, sigkill_timeout_sec: float = 10.0) -> None:
    """SIGTERM the process group, wait, SIGKILL if still alive."""
    pgid = stack.process_group_id
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + sigkill_timeout_sec
    while time.monotonic() < deadline:
        daemon_done = stack.daemon_proc.poll() is not None
        brain_done = stack.brain_proc.poll() is not None
        if daemon_done and brain_done:
            return
        time.sleep(0.1)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
