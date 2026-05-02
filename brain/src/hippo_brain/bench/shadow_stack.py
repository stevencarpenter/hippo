"""Shadow process-group spawn and teardown for hippo-bench v2.

Spawns hippo-daemon + hippo-brain in their own process group with:
  XDG_DATA_HOME=<run_tree>
  XDG_CONFIG_HOME=<run_tree>/config
  OTEL_RESOURCE_ATTRIBUTES=service.namespace=hippo-bench,...

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
    env["XDG_DATA_HOME"] = str(run_tree)
    env["XDG_CONFIG_HOME"] = str(run_tree / "config")
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
    (run_tree / "config").mkdir(parents=True, exist_ok=True)

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

    daemon_log = open(logs_dir / "daemon.log", "ab")
    brain_log = open(logs_dir / "brain.log", "ab")

    daemon_proc = subprocess.Popen(
        [hippo_bin, "serve"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=daemon_log,
        start_new_session=True,
    )

    brain_proc = subprocess.Popen(
        [uv_bin, "run", "--project", "brain", "hippo-brain", "--port", str(brain_port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=brain_log,
        start_new_session=True,
    )

    process_group_id = os.getpgid(daemon_proc.pid)

    return ShadowStack(
        daemon_proc=daemon_proc,
        brain_proc=brain_proc,
        run_tree=run_tree,
        process_group_id=process_group_id,
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
