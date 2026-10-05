"""Exercise the smoke's isolated interpreter selection and SQLite runtime."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import sqlite_vec


SCRIPT = Path(__file__).parents[2] / "scripts/smoke_release_bundle.py"
spec = importlib.util.spec_from_file_location("smoke_release_bundle", SCRIPT)
assert spec is not None and spec.loader is not None
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


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
