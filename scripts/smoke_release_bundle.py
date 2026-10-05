"""Install and exercise local macOS release artifacts in a private fixture.

Requires uv, Python 3.14, and zsh. Dependency installation uses the packaged
lockfile and may download wheels. Runtime checks use no model service and never
register LaunchAgents. Evidence is retained after both success and failure.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, closing, contextmanager, suppress
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Iterator, TypedDict
from types import FrameType
import urllib.error
import urllib.request


class Response(TypedDict):
    status: int
    body: dict[str, Any]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


@contextmanager
def process(
    command: list[str],
    env: dict[str, str],
    log: Path,
    cwd: Path,
    results: dict[str, Any] | None = None,
) -> Iterator[subprocess.Popen[bytes]]:
    with log.open("wb") as output:
        child = subprocess.Popen(
            command,
            env=env,
            cwd=cwd,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            yield child
        finally:
            # Terminate the process group, including installer grandchildren.
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)
            try:
                deadline = time.monotonic() + 15
                while True:
                    child.poll()  # Reap the leader before checking its descendants.
                    try:
                        os.killpg(child.pid, 0)
                    except ProcessLookupError:
                        break
                    if time.monotonic() >= deadline:
                        with suppress(ProcessLookupError):
                            os.killpg(child.pid, signal.SIGKILL)
                        if results is not None:
                            results.setdefault("forced_cleanup", []).append(child.pid)
                        raise RuntimeError(
                            f"process group {child.pid} required SIGKILL; see {log}"
                        )
                    time.sleep(0.1)
            finally:
                child.wait(timeout=15)
                if results is not None:
                    results.setdefault("process_exits", {})[log.name] = child.returncode


def run(command: list[str], env: dict[str, str], log: Path, timeout: int = 30) -> str:
    with process(command, env, log, log.parent) as child:
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"{command[0]} timed out after {timeout}s; see {log}"
            ) from exc
        require(code == 0, f"{command[0]} exited {code}; see {log}")
    return log.read_text()


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def release_directory(bundle: Path, output: Path) -> Path:
    if bundle.is_dir():
        return bundle
    with tarfile.open(bundle) as archive:
        archive.extractall(output / "bundle", filter="data")
    release = output / "bundle" / "release-files"
    require(release.is_dir(), "archive must contain release-files/")
    return release


def environment(output: Path) -> dict[str, str]:
    home = output / "home"
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", os.defpath),
        "XDG_CONFIG_HOME": str(output / "config"),
        "XDG_DATA_HOME": str(output / "data"),
        "XDG_CACHE_HOME": str(output / "cache"),
        "XDG_STATE_HOME": str(output / "state"),
        "TMPDIR": str(output / "tmp"),
        "UV_CACHE_DIR": str(output / "uv-cache"),
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_PYTHON_PREFERENCE": "only-system",
        "UV_NO_PROGRESS": "1",
        "HIPPO_OTEL_ENABLED": "0",
        "OTEL_SDK_DISABLED": "true",
        "NO_PROXY": "127.0.0.1,localhost",
        "PYTHONUNBUFFERED": "1",
    }
    for key in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "TMPDIR",
        "UV_CACHE_DIR",
    ):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv")
    require(uv is not None, "uv is required")
    interpreter = Path(
        run(
            [str(uv), "python", "find", "--no-python-downloads", "3.14"],
            env,
            output / "python-find.log",
        ).strip()
    ).resolve()
    require(
        interpreter.is_file(), "uv did not locate an existing Python 3.14 interpreter"
    )
    env["PATH"] = os.pathsep.join(
        (
            str(home / ".local" / "bin"),
            str(interpreter.parent),
            str(Path(str(uv)).parent),
            env["PATH"],
        )
    )
    return env


def install(
    release: Path, output: Path, env: dict[str, str], results: dict[str, Any]
) -> None:
    require(platform.system() == "Darwin", "release smoke requires macOS")
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64"}.get(
        platform.machine()
    )
    require(arch is not None, f"unsupported architecture: {platform.machine()}")
    packages = list(release.glob("hippo-brain-*.tar.gz"))
    require(len(packages) == 1, "bundle must contain exactly one hippo-brain-*.tar.gz")
    version = packages[0].name.removeprefix("hippo-brain-").removesuffix(".tar.gz")
    require(
        re.fullmatch(r"\d+\.\d+\.\d+", version) is not None,
        f"invalid version: {version}",
    )
    checksums: dict[str, str] = {}
    for line in (release / "SHA256SUMS.txt").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        name = name.lstrip("*")
        require(name not in checksums, f"duplicate checksum: {name}")
        checksums[name] = digest
    daemon_name = f"hippo-darwin-{arch}"
    for name in (daemon_name, packages[0].name):
        require(name in checksums, f"missing checksum: {name}")
        require(sha256(release / name) == checksums[name], f"checksum mismatch: {name}")

    installer = (release / "install.sh").read_text()
    require(
        installer.rstrip().endswith('main "$@"'), 'installer must end with main "$@"'
    )
    (output / "install-helpers.sh").write_text(
        installer.rstrip().removesuffix('main "$@"')
    )
    wrapper = output / "install-candidate.sh"
    wrapper.write_text("""#!/bin/bash
set -euo pipefail
fixture_root="$1"; release_dir="$2"; arch="$3"; tag="$4"
set --
source "$fixture_root/install-helpers.sh"
download_file() { cp "$release_dir/$2" "$3"; }
install_components "$arch" "$tag" "$release_dir/SHA256SUMS.txt" "$fixture_root/tmp"
install_skills
setup_config "$BIN_DIR/hippo"
""")
    command = [
        "/bin/bash",
        str(wrapper),
        str(output),
        str(release),
        str(arch),
        f"v{version}",
    ]
    print(
        "Installing candidate components with the packaged installer...",
        file=sys.stderr,
    )
    run(command, env, output / "install.log", timeout=600)
    home = Path(env["HOME"])
    brain = home / ".local/share/hippo-brain"
    hippo = home / ".local/bin/hippo"
    receipts = Path(env["XDG_STATE_HOME"]) / "hippo/install-receipts"
    for component, name in (("daemon", daemon_name), ("brain", packages[0].name)):
        require(
            (receipts / f"{component}.sha256").read_text().strip() == checksums[name],
            f"incorrect {component} receipt",
        )
    require(
        sha256(hippo) == checksums[daemon_name], "installed daemon checksum mismatch"
    )
    for relative in (
        "scripts/hippo-ingest-claude.py",
        "shell/hippo-env.zsh",
        "shell/hippo.zsh",
        "claude-skill/using-hippo-brain/SKILL.md",
        "claude-skill/monitoring-hippo/SKILL.md",
    ):
        require((brain / relative).is_file(), f"missing packaged file: {relative}")
    for skill in ("using-hippo-brain", "monitoring-hippo"):
        require(
            (home / ".claude/skills" / skill / "SKILL.md").read_bytes()
            == (brain / "claude-skill" / skill / "SKILL.md").read_bytes(),
            f"installed skill differs: {skill}",
        )
    probe = """import importlib.metadata, json, pathlib, hippo_brain
from hippo_brain.server import create_app
print(json.dumps({"version": importlib.metadata.version("hippo-brain"),
                  "module": str(pathlib.Path(hippo_brain.__file__).resolve())}))
"""
    imported = json.loads(
        run(
            [str(brain / ".venv/bin/python"), "-c", probe],
            env,
            output / "brain-import.log",
        )
    )
    require(
        imported["version"] == version, "installed brain version differs from bundle"
    )
    require(
        Path(imported["module"]).is_relative_to(brain / ".venv"),
        "brain was imported from outside the relocated venv",
    )
    results["brain_import"] = imported
    results["daemon_version"] = run(
        [str(hippo), "--version"], env, output / "daemon-version.log"
    ).strip()
    require(
        re.match(rf"hippo {re.escape(version)}(?:$|[-+])", results["daemon_version"])
        is not None,
        "installed daemon version differs from bundle",
    )
    run(
        [str(brain / ".venv/bin/hippo-brain"), "--help"], env, output / "brain-help.log"
    )
    config = Path(env["XDG_CONFIG_HOME"]) / "hippo/config.toml"
    require(
        stat.S_IMODE(config.stat().st_mode) == 0o600,
        "initialized config mode must be 0600",
    )
    (output / "initialized-config.toml").write_bytes(config.read_bytes())
    with config.open("a") as stream:
        stream.write("\n# Preserve this existing configuration during reinstall.\n")
    original_config = config.read_bytes()
    original_inode = brain.stat().st_ino
    print(
        "Checking receipts, config preservation, and reinstall skipping...",
        file=sys.stderr,
    )
    rerun = run(command, env, output / "reinstall.log", timeout=600)
    require(
        f"Brain already at v{version}, skipping" in rerun,
        "brain did not skip reinstall",
    )
    require(
        f"Daemon already at v{version}, skipping" in rerun,
        "daemon did not skip reinstall",
    )
    require(
        brain.stat().st_ino == original_inode, "reinstall replaced the brain directory"
    )
    require(
        config.read_bytes() == original_config,
        "reinstall changed existing configuration",
    )
    results["installation"] = {
        "version": version,
        "checksums": checksums,
        "config_mode": "0600",
        "reinstall_preserved": True,
    }


def request(port: int, path: str, body: object = None) -> Response:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    # Ignore the caller's proxy environment as well as the child processes'.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(req, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        payload = json.load(response)
        require(isinstance(payload, dict), f"{path} returned non-object JSON")
        return {"status": response.status, "body": payload}


def runtime(output: Path, env: dict[str, str], results: dict[str, Any]) -> None:
    home = Path(env["HOME"])
    data = Path(env["XDG_DATA_HOME"]) / "hippo-smoke"
    # The daemon uses a shared /tmp fallback for long Unix socket paths.
    require(
        len(os.fsencode(data / "hippo.sock")) < 100,
        "output directory is too long for an isolated Unix socket; choose a shorter path",
    )
    config = Path(env["XDG_CONFIG_HOME"]) / "hippo/config.toml"
    hippo = home / ".local/bin/hippo"
    brain = home / ".local/share/hippo-brain"
    with ExitStack() as stack:
        # Reserve an unreachable local port so no running model can receive requests.
        inert = stack.enter_context(socket.socket())
        inert.bind(("127.0.0.1", 0))
        config.write_text(f"""[storage]
data_dir = {json.dumps(str(data))}
[inference]
base_url = "http://127.0.0.1:{inert.getsockname()[1]}/v1"
timeout_secs = 0.1
[brain]
port = 0
poll_interval_secs = 3600
[telemetry]
enabled = false
[classification]
enabled = false
[auto_memory]
enabled = false
""")
        print(
            "Starting packaged daemon and brain against an isolated database...",
            file=sys.stderr,
        )
        daemon = stack.enter_context(
            process(
                [str(hippo), "daemon", "run", "--bench"],
                env,
                output / "daemon.log",
                output,
                results,
            )
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            require(
                daemon.poll() is None, "daemon exited before readiness; see daemon.log"
            )
            with process(
                [str(hippo), "status"], env, output / "daemon-status.log", output
            ) as status:
                code = status.wait(timeout=5)
            if code == 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("daemon readiness timed out; see daemon.log")
        results["daemon_status"] = (output / "daemon-status.log").read_text()
        server = stack.enter_context(
            process(
                [str(brain / ".venv/bin/hippo-brain"), "serve"],
                env,
                output / "brain.log",
                output,
                results,
            )
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            require(
                server.poll() is None, "brain exited before readiness; see brain.log"
            )
            match = re.search(
                r"Uvicorn running on http://127\.0\.0\.1:(\d+)",
                (output / "brain.log").read_text(),
            )
            if match:
                port = int(match[1])
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("brain readiness timed out; see brain.log")
        health = results["health"] = request(port, "/health")
        require(
            health["status"] == 200 and health["body"]["db_reachable"] is True,
            "brain health failed or database is unreachable",
        )
        require(
            health["body"]["inference_reachable"] is False,
            "unexpected reachable inference server",
        )
        require(
            health["body"]["telemetry_enabled"] is False,
            "telemetry unexpectedly enabled",
        )
        query = {"text": "candidate offline smoke", "mode": "lexical", "limit": 5}
        empty = results["empty_query"] = request(port, "/query", query)
        require(
            empty["status"] == 200 and empty["body"]["nodes"] == [],
            "empty lexical query failed",
        )
        with closing(sqlite3.connect(data / "hippo.db", timeout=5)) as db, db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA busy_timeout = 5000")
            schema = db.execute("PRAGMA user_version").fetchone()[0]
            require(
                schema == health["body"]["expected_schema_version"],
                "daemon/brain schema mismatch",
            )
            results["schema_version"] = schema
            db.execute(
                "INSERT INTO knowledge_nodes(uuid,content,embed_text,outcome,tags,enrichment_model,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "candidate-smoke",
                    json.dumps({"summary": query["text"]}),
                    query["text"],
                    "success",
                    "[]",
                    "fixture",
                    1700000000000,
                    1700000000000,
                ),
            )
        seeded = results["seeded_query"] = request(port, "/query", query)
        require(
            seeded["status"] == 200
            and any(
                node["uuid"] == "candidate-smoke" for node in seeded["body"]["nodes"]
            ),
            "seeded lexical query failed",
        )
        malformed = results["non_object_query"] = request(port, "/query", [])
        require(malformed["status"] == 400, "non-object query must return HTTP 400")
        hook = """source "$1/hippo-env.zsh"
source "$1/hippo.zsh"
_HIPPO_OUTPUT_FILE="$2/hook-output"
_hippo_preexec "echo candidate-hook-capture"
_hippo_precmd
"""
        print(
            "Verifying packaged shell capture and lexical retrieval...", file=sys.stderr
        )
        with process(
            [
                "zsh",
                "-f",
                "-c",
                hook,
                "candidate-smoke",
                str(brain / "shell"),
                str(output),
            ],
            env,
            output / "shell-hook.log",
            output,
        ) as shell:
            require(
                shell.wait(timeout=10) == 0, "shell hook failed; see shell-hook.log"
            )
            captured = None
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with closing(sqlite3.connect(data / "hippo.db", timeout=5)) as db:
                    db.execute("PRAGMA journal_mode = WAL")
                    db.execute("PRAGMA foreign_keys = ON")
                    db.execute("PRAGMA busy_timeout = 5000")
                    captured = db.execute(
                        "SELECT command,exit_code,source_kind FROM events WHERE command=?",
                        ("echo candidate-hook-capture",),
                    ).fetchone()
                if captured:
                    break
                time.sleep(0.1)
        require(
            captured == ("echo candidate-hook-capture", 0, "shell"),
            "packaged shell capture failed",
        )
        results["packaged_shell_hook_capture"] = captured
        require(
            daemon.poll() is None and server.poll() is None,
            "a runtime process exited during checks",
        )


def interrupt(signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt(f"received signal {signum}")


def main() -> int:
    signal.signal(signal.SIGTERM, interrupt)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "bundle",
        type=Path,
        help="release-files directory or tar containing release-files/",
    )
    parser.add_argument(
        "--output-dir", type=Path, help="new private directory for retained evidence"
    )
    args = parser.parse_args()
    try:
        if args.output_dir is None:
            output = Path(tempfile.mkdtemp(prefix="hippo-smoke-", dir="/tmp")).resolve()
        else:
            output = args.output_dir.resolve()
            output.mkdir(mode=0o700)
    except OSError as exc:
        print(f"Release smoke failed: {exc}", file=sys.stderr)
        return 1
    results: dict[str, Any] = {
        "ok": False,
        "bundle": str(args.bundle.resolve()),
        "output_dir": str(output),
    }
    try:
        release = release_directory(args.bundle.resolve(), output)
        env = environment(output)
        install(release, output, env, results)
        runtime(output, env, results)
        results["ok"] = True
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        tarfile.TarError,
        subprocess.SubprocessError,
        sqlite3.Error,
        KeyboardInterrupt,
    ) as exc:
        results["error"] = str(exc) or type(exc).__name__
        print(f"Release smoke failed: {results['error']}", file=sys.stderr)
    finally:
        result_path = output / "results.json"
        result_path.write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps({"ok": results["ok"], "results": str(result_path)}))
    return 0 if results["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
