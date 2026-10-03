"""Exercise optional Grafana authentication through Docker Compose."""

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def docker() -> str:
    executable = shutil.which("docker")
    if (
        executable is None
        or subprocess.run([executable, "compose", "version"], capture_output=True).returncode
    ):
        pytest.skip("Docker Compose is required for stack configuration checks")
    return executable


def _environment(authenticated: bool, password: str | None) -> dict[str, str]:
    env = os.environ.copy()
    env["COMPOSE_FILE"] = "docker-compose.yml"
    if authenticated:
        env["COMPOSE_FILE"] += ":docker-compose.auth.yml"
    env["COMPOSE_DISABLE_ENV_FILE"] = "true"
    env.pop("HIPPO_OTEL_GRAFANA_ADMIN_PASSWORD", None)
    env.pop("HIPPO_OTEL_GRAFANA_ORG_NAME", None)
    if password is not None:
        env["HIPPO_OTEL_GRAFANA_ADMIN_PASSWORD"] = password
    return env


@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize("password", [None, "", "configured-for-test"])
def test_grafana_authentication_is_opt_in(docker, authenticated, password):
    result = subprocess.run(
        [docker, "compose", "config", "--format", "json"],
        cwd=_ROOT / "otel",
        env=_environment(authenticated, password),
        capture_output=True,
        text=True,
    )
    if authenticated and not password:
        assert result.returncode != 0
        assert "HIPPO_OTEL_GRAFANA_ADMIN_PASSWORD" in result.stderr
        return

    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    grafana = services["grafana"]["environment"]
    assert grafana["GF_AUTH_ANONYMOUS_ENABLED"] == str(not authenticated).lower()
    assert grafana["GF_AUTH_ANONYMOUS_ORG_ROLE"] == "Admin"
    assert grafana["GF_AUTH_ANONYMOUS_ORG_NAME"] == "Main Org."
    assert grafana["GF_SECURITY_ADMIN_PASSWORD"] == (password or "hippo")
    for service in services.values():
        for port in service.get("ports", []):
            assert port["host_ip"] == "127.0.0.1"


def test_anonymous_access_uses_configured_organization(docker, tmp_path):
    env_file = tmp_path / "grafana.env"
    env_file.write_text('HIPPO_OTEL_GRAFANA_ORG_NAME="Renamed organization"\n')
    result = subprocess.run(
        [docker, "compose", "--env-file", str(env_file), "config", "--format", "json"],
        cwd=_ROOT / "otel",
        env=_environment(False, None),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    grafana = json.loads(result.stdout)["services"]["grafana"]["environment"]
    assert grafana["GF_AUTH_ANONYMOUS_ORG_NAME"] == "Renamed organization"


@pytest.mark.parametrize("authenticated", [False, True])
def test_otel_startup_without_password(docker, tmp_path, authenticated):
    stub = tmp_path / "docker"
    stub.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$HIPPO_TEST_DOCKER_LOG"\n'
        'if [ "$1 $2" = "compose config" ]; then\n'
        '    exec "$HIPPO_TEST_DOCKER" "$@"\n'
        "fi\n"
        'if [ "$1" = "volume" ]; then exit 1; fi\n'
    )
    stub.chmod(0o755)
    log = tmp_path / "docker.log"
    env = _environment(authenticated, None)
    env.update(
        PATH=f"{tmp_path}:{env['PATH']}",
        XDG_DATA_HOME=str(tmp_path / "data"),
        HIPPO_TEST_DOCKER=docker,
        HIPPO_TEST_DOCKER_LOG=str(log),
    )
    task = tomllib.loads((_ROOT / "mise.toml").read_text())["tasks"]["otel:up"]
    result = subprocess.run(
        ["bash", "-c", task["run"]],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    calls = log.read_text().splitlines()
    assert calls[0] == "compose config --quiet"
    if authenticated:
        assert result.returncode != 0
        assert calls == ["compose config --quiet"]
    else:
        assert result.returncode == 0, result.stderr
        assert "compose up -d --remove-orphans" in calls


@pytest.mark.parametrize("authenticated", [False, True])
def test_otel_restart_honors_authentication_override(docker, tmp_path, authenticated):
    stub = tmp_path / "docker"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        "args = sys.argv[1:]\n"
        "before_up = args[:args.index('up')]\n"
        "sys.exit(subprocess.call([os.environ['HIPPO_TEST_DOCKER'], *before_up, "
        "'config', '--format', 'json']))\n"
    )
    stub.chmod(0o755)
    env = _environment(authenticated, "configured-for-test")
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["HIPPO_TEST_DOCKER"] = docker
    task = tomllib.loads((_ROOT / "mise.toml").read_text())["tasks"]["otel:restart"]
    result = subprocess.run(
        ["bash", "-c", task["run"]],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    grafana = json.loads(result.stdout)["services"]["grafana"]["environment"]
    assert grafana["GF_AUTH_ANONYMOUS_ENABLED"] == str(not authenticated).lower()
