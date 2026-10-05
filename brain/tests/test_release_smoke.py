"""Exercise the smoke's isolated interpreter selection and SQLite runtime."""

import importlib.util
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

import pytest
import sqlite_vec


SCRIPT = Path(__file__).parents[2] / "scripts/smoke_release_bundle.py"
spec = importlib.util.spec_from_file_location("smoke_release_bundle", SCRIPT)
assert spec is not None and spec.loader is not None
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


@pytest.mark.skipif(os.name != "posix", reason="release process groups require POSIX")
@pytest.mark.parametrize("denied_signal", [signal.SIGTERM, 0])
def test_process_cleanup_retries_transient_eperm(tmp_path, monkeypatch, denied_signal):
    killpg = os.killpg
    denied = []

    def transient_eperm(pid, sig):
        if sig == denied_signal and not denied:
            denied.append(sig)
            raise PermissionError(1, "Operation not permitted")
        return killpg(pid, sig)

    monkeypatch.setattr(smoke.os, "killpg", transient_eperm)
    results = {}
    with smoke.process(
        [sys.executable, "-c", "import time; time.sleep(0.2)"],
        dict(os.environ),
        tmp_path / "child.log",
        tmp_path,
        results,
    ) as child:
        pass
    assert denied == [denied_signal]
    assert child.returncode is not None
    assert results["process_exits"]["child.log"] == child.returncode
    assert "forced_cleanup" not in results
    with pytest.raises(ProcessLookupError):
        killpg(child.pid, 0)


@pytest.mark.skipif(os.name != "posix", reason="release process groups require POSIX")
def test_process_cleanup_does_not_accept_persistent_eperm(tmp_path, monkeypatch):
    killpg = os.killpg
    clock = iter([0, 16])
    monkeypatch.setattr(smoke.time, "monotonic", lambda: next(clock))

    def permission_error(pid, sig):
        if sig == signal.SIGKILL:
            killpg(pid, sig)
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(smoke.os, "killpg", permission_error)
    results = {}
    with pytest.raises(RuntimeError, match="required SIGKILL"):
        with smoke.process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            dict(os.environ),
            tmp_path / "child.log",
            tmp_path,
            results,
        ) as child:
            pass
    assert results["forced_cleanup"] == [child.pid]
    assert results["process_exits"]["child.log"] == -signal.SIGKILL


def test_isolated_installer_uses_provisioned_python(tmp_path, monkeypatch):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required for the release installer")
    provisioned = subprocess.run(
        [uv, "python", "find", "--managed-python", "--no-python-downloads", "3.14"],
        capture_output=True,
        text=True,
    )
    if provisioned.returncode:
        pytest.skip("requires a provisioned uv Python 3.14, as in release CI")
    install_dir = subprocess.check_output([uv, "python", "dir"], text=True).strip()
    monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", install_dir)
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "uv").symlink_to(uv)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.defpath)
    env = smoke.environment(tmp_path)
    venv = tmp_path / "installed-venv"
    subprocess.run(
        [uv, "venv", "--relocatable", "--python", "3.14", str(venv)],
        env=env,
        check=True,
    )
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    probe = """import pathlib, sqlite3, sys
assert pathlib.Path(sys.base_prefix).resolve().is_relative_to(pathlib.Path(sys.argv[1]).resolve())
with sqlite3.connect(':memory:') as conn:
    conn.enable_load_extension(True)
    conn.load_extension(sys.argv[2])
    conn.enable_load_extension(False)
    assert conn.execute('SELECT vec_version()').fetchone()[0]
"""
    subprocess.run(
        [str(python), "-c", probe, install_dir, sqlite_vec.loadable_path()],
        env=env,
        check=True,
    )
