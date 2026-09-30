"""One bounded, isolated model-thread diagnostic pair. No bulk fallback."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import subprocess
import time
import tomllib
from pathlib import Path

from hippo_brain.bench.agent_benefit_packets import file_hash, git
from hippo_brain.bench.agent_benefit_study import load_contract, require, study_identity
from hippo_brain.bench.claim_packets import create_directory, write_artifact
from hippo_brain.bench.paths import hippo_bench_root
from hippo_brain.decision_capture import external_path
from hippo_brain.jev import digest

TOOLS = {
    "agent_query",
    "ask",
    "get_lessons",
    "list_projects",
    "search_hybrid",
    "get_entities",
    "search_events",
    "search_knowledge",
    "get_context",
    "query_memory",
    "get_ci_status",
    "query_memory_history",
}


def verify_history_boundaries(permissions: dict, host_home: Path) -> None:
    """Known host capture stores must be denied independently of the submitted spec."""
    for relative in (
        ".pi/agent/sessions",
        ".claude/projects",
        ".codex",
        ".local/share/hippo",
        ".zsh_history",
        ".bash_history",
    ):
        path = host_home / relative
        require(permissions.get(str(path)) == "deny", f"missing host history denial: {relative}")


def verify_inventory(result: dict, arm: str) -> None:
    require(result.get("nextCursor") is None, "incomplete runtime inventory")
    servers = result["data"]
    require(
        {s["name"] for s in servers} == ({"hippo"} if arm == "treatment" else set()),
        "unexpected or missing runtime server",
    )
    for server in servers:
        require(
            not server.get("toolsError") and server.get("runtimeStatus") == "connected",
            "runtime MCP not connected",
        )
        require(set(server["tools"]) == TOOLS, "unexpected runtime Hippo tools")


def verify(spec: dict, contract: dict) -> None:
    require(spec["contract_hash"] == digest(contract), "changed study contract")
    require(
        spec["study_id"] == study_identity(contract, "guided", "diagnostic"),
        "runner requires guided diagnostic identity",
    )
    require(
        spec["role"] == "diagnostic_only" and spec["phase"] == "diagnostic",
        "no cohort launch before verified canary",
    )
    version = subprocess.check_output([spec["codex"], "--version"], text=True).strip()
    require(version == "codex-cli " + contract["codex_version"], "changed Codex build")
    require(set(spec["arms"]) == {"control", "treatment"}, "two arms required")
    require(
        set(spec["arm_order"]) == set(spec["arms"]) and len(spec["arm_order"]) == 2,
        "invalid arm order",
    )
    common = []
    settings = []
    for arm, definition in spec["arms"].items():
        root = external_path(definition["root"])
        require(root.is_dir(), "arm unavailable")
        require(
            {"prompt.txt", "codex-home/config.toml"} <= set(definition["hashes"]),
            "prompt and config must be frozen",
        )
        for name, expected in definition["hashes"].items():
            path = root / name
            require(
                path.resolve().is_relative_to(root) and file_hash(path) == expected,
                "changed frozen arm input",
            )
        repo = root / "repo"
        require(
            git(repo, "rev-parse", "HEAD").decode().strip() == definition["head"],
            "changed task commit",
        )
        require(
            not git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
            "dirty starting state",
        )
        for remote in git(repo, "remote").decode().splitlines():
            target = Path(git(repo, "remote", "get-url", remote).decode().strip())
            require(
                target.is_absolute() and target.resolve().is_relative_to(root),
                "remote boundary escape",
            )
        config = tomllib.loads((root / "codex-home/config.toml").read_text())
        require(
            config["model"] == contract["model"]
            and config["model_reasoning_effort"] == contract["effort"],
            "changed model/effort",
        )
        require(
            config["approval_policy"] == "never" and config["default_permissions"] == "trial",
            "unexpected permission profile",
        )
        require(
            config.get("features", {}).get("apps") is False
            and config["features"].get("multi_agent") is False,
            "apps/delegation must be disabled",
        )
        require(
            set(config.get("mcp_servers", {})) == ({"hippo"} if arm == "treatment" else set()),
            "inherited or missing server",
        )
        permissions = config["permissions"]["trial"]["filesystem"]
        verify_history_boundaries(permissions, Path.home())
        for denied in definition["required_denials"]:
            require(permissions.get(denied) == "deny", "missing filesystem boundary")
        if arm == "treatment":
            require(
                config["mcp_servers"]["hippo"].get("required") is True,
                "Hippo startup must fail closed",
            )
        common.append(file_hash(root / "prompt.txt"))
        settings.append(
            {k: v for k, v in config.items() if k not in {"mcp_servers", "permissions", "projects"}}
        )
        for path in definition["read_probes"]:
            require(Path(path).is_file(), "boundary read probe must exist")
            require(
                any(
                    Path(path).is_relative_to(Path(denied))
                    for denied in definition["required_denials"]
                ),
                "probe is outside denied roots",
            )
        for path in definition["write_probes"]:
            require(not Path(path).exists(), "write probe must be a new disposable marker")
            external_path(path)
            require(
                any(
                    Path(path).is_relative_to(Path(denied))
                    for denied in definition["required_denials"]
                ),
                "probe is outside denied roots",
            )
    require(common[0] == common[1], "unequal task/lookup instructions")
    require(settings[0] == settings[1], "unequal ordinary model/tool settings")
    for path, expected in spec["external_hashes"].items():
        require(file_hash(Path(path)) == expected, "changed checker or source proof")
    require(
        spec["source_proof"]["verified"] is True
        and spec["source_proof"]["memory_predates_task"] is True,
        "independent pre-task source audit required",
    )


def run_arm(
    spec: dict,
    contract: dict,
    arm: str,
    output: Path,
    seconds: float,
    tokens: int,
    *,
    preflight_only: bool = False,
) -> dict:
    root = Path(spec["arms"][arm]["root"])
    env = {
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(root / "home"),
        "CODEX_HOME": str(root / "codex-home"),
        "TMPDIR": str(root / "codex-home/tmp"),
        "XDG_CONFIG_HOME": str(root / "home/.config"),
        "XDG_DATA_HOME": str(root / "home/.local/share"),
        "XDG_CACHE_HOME": str(root / "home/.cache"),
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    command = [
        spec["codex"],
        "app-server",
        "--strict-config",
        "--disable",
        "apps",
        "--disable",
        "multi_agent",
        "--disable",
        "plugins",
        "--disable",
        "remote_plugin",
    ]
    started = time.monotonic()
    record = {
        "arm": arm,
        "thread_id": None,
        "turn_started": False,
        "terminal": "pre_thread_failure",
        "usage": None,
        "hippo_calls": [],
        "command_results": [],
        "inventory_verified": False,
        "boundaries_verified": False,
        "launch_command": command,
    }
    event_path = output / (arm + ".rpc.jsonl")
    with event_path.open("x") as events, (output / (arm + ".stderr.log")).open("x") as stderr:
        os.chmod(events.name, 0o600)
        os.chmod(stderr.name, 0o600)
        process = subprocess.Popen(
            command,
            cwd=root / "repo",
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            start_new_session=True,
        )
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        pending, buffer, request_id = [], b"", 0

        def send(method: str, params: dict, *, request: bool = True) -> int | None:
            nonlocal request_id
            message = {"method": method, "params": params}
            if request:
                request_id += 1
                message["id"] = request_id
            events.write(json.dumps({"sent": message}) + "\n")
            events.flush()
            process.stdin.write((json.dumps(message) + "\n").encode())
            process.stdin.flush()
            return message.get("id")

        def receive() -> dict:
            nonlocal buffer
            while not pending:
                if time.monotonic() - started >= seconds:
                    raise TimeoutError("arm wall budget exhausted")
                if not selector.select(timeout=0.2):
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise ConnectionError("app-server transport closed")
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line:
                        pending.append(json.loads(line))
            message = pending.pop(0)
            events.write(json.dumps({"received": message}) + "\n")
            events.flush()
            params = message.get("params", {})
            if message.get("method") == "thread/tokenUsage/updated":
                record["usage"] = params["tokenUsage"]["total"]
                if record["usage"]["totalTokens"] >= tokens:
                    raise OverflowError("reported token budget reached")
            if message.get("method") == "model/rerouted":
                raise ValueError("model recipe changed during turn")
            if message.get("method") == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "mcpToolCall":
                    record["hippo_calls"].append(item)
                if item.get("type") == "commandExecution":
                    record["command_results"].append(
                        {k: item.get(k) for k in ("command", "exitCode", "status", "durationMs")}
                    )
            if "id" in message and "method" in message:
                process.stdin.write(
                    (
                        json.dumps(
                            {
                                "id": message["id"],
                                "error": {
                                    "code": -32601,
                                    "message": "interactive requests disabled in frozen trial",
                                },
                            }
                        )
                        + "\n"
                    ).encode()
                )
                process.stdin.flush()
            return message

        def rpc(method: str, params: dict) -> dict:
            expected = send(method, params)
            while True:
                message = receive()
                if message.get("id") == expected:
                    require("error" not in message, f"RPC failed: {method}")
                    return message["result"]

        try:
            rpc(
                "initialize",
                {
                    "clientInfo": {"name": "hippo_benefit_trial", "version": "2"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            send("initialized", {}, request=False)
            result = rpc(
                "thread/start",
                {
                    "model": contract["model"],
                    "cwd": str(root / "repo"),
                    "approvalPolicy": "never",
                    "permissions": "trial",
                    "ephemeral": True,
                },
            )
            thread = result["thread"]["id"]
            record["thread_id"] = thread
            inventory = rpc(
                "mcpServerStatus/list",
                {"threadId": thread, "detail": "toolsAndAuthOnly", "limit": 100},
            )
            verify_inventory(inventory, arm)
            record["inventory_verified"] = True
            record["inventory_hash"] = digest(inventory)
            probes = spec["arms"][arm]
            code = """import os, errno, sys
for path, flags in PROBES:
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as error:
        if error.errno not in (errno.EACCES, errno.EPERM):
            sys.exit(2)
    else:
        os.close(fd)
        sys.exit(1)
print('boundaries_denied')
"""
            pairs = [(p, os.O_RDONLY) for p in probes["read_probes"]] + [
                (p, os.O_WRONLY | os.O_CREAT | os.O_EXCL) for p in probes["write_probes"]
            ]
            require(
                bool(probes["read_probes"]) and bool(probes["write_probes"]),
                "read and write boundary probes required",
            )
            code = "PROBES = " + repr(pairs) + "\n" + code
            probe = ["/usr/bin/python3", "-c", code]
            check = rpc(
                "command/exec",
                {
                    "command": probe,
                    "cwd": str(root / "repo"),
                    "permissionProfile": "trial",
                    "timeoutMs": 15000,
                },
            )
            require(
                check["exitCode"] == 0 and check["stdout"].strip() == "boundaries_denied",
                "sandbox boundary escape or inconclusive probe",
            )
            record["boundaries_verified"] = True
            if preflight_only:
                record["terminal"] = "preflight_verified"
            else:
                rpc(
                    "turn/start",
                    {
                        "threadId": thread,
                        "effort": contract["effort"],
                        "input": [{"type": "text", "text": (root / "prompt.txt").read_text()}],
                    },
                )
                record["turn_started"] = True
                record["terminal"] = "task_failure"
                while True:
                    message = receive()
                    if message.get("method") == "turn/completed":
                        record["terminal"] = message["params"]["turn"]["status"]
                        break
        except (ValueError, TimeoutError, ConnectionError, OverflowError, OSError) as error:
            record["failure_type"] = type(error).__name__
            record["failure_reason"] = str(error)
            if record["turn_started"]:
                record["terminal"] = {
                    TimeoutError: "timeout",
                    OverflowError: "token_budget_exhausted",
                    ConnectionError: "transport_closed",
                }.get(type(error), "task_failure")
        finally:
            selector.close()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            process.stdin.close()
            process.stdout.close()
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    record["trace_sha256"] = file_hash(event_path)
    record["stderr_sha256"] = file_hash(output / (arm + ".stderr.log"))
    return record


def run_pair(spec: dict, contract: dict, output: Path) -> dict:
    require(
        contract.get("amendment", {}).get("approval_status", "approved") == "approved",
        "diagnostic amendment requires owner approval before reservation",
    )
    verify(spec, contract)
    reservation = create_directory(
        hippo_bench_root()
        / "agent-benefit"
        / ("study-" + digest(contract))
        / "diagnostic-reservation"
    )
    write_artifact(
        reservation / "reservation.json",
        {"spec_hash": digest(spec), "output": str(external_path(output)), "runs_reserved": 2},
    )
    output = create_directory(output)
    write_artifact(
        output / "reservation.json",
        {
            "spec_hash": digest(spec),
            "contract_hash": digest(contract),
            "runs_reserved": 2,
            "phase": "diagnostic",
        },
    )
    rows = []
    budget = contract["budgets"]["diagnostic"]
    for arm in spec["arm_order"]:
        seconds = min(
            budget["per_arm_seconds"],
            budget["total_seconds"] - sum(r["elapsed_seconds"] for r in rows),
        )
        try:
            row = run_arm(spec, contract, arm, output, seconds, budget["max_total_tokens"] // 2)
        except Exception as error:
            row = {
                "arm": arm,
                "terminal": "pre_thread_failure",
                "turn_started": False,
                "usage": None,
                "hippo_calls": [],
                "elapsed_seconds": seconds,
                "failure_type": type(error).__name__,
                "failure_reason": str(error),
            }
        command = [
            str(Path(spec["arms"][arm]["root"]) / "repo") if arg == "{repo}" else arg
            for arg in spec["checker"]
        ]
        try:
            checker = subprocess.run(command, capture_output=True, text=True, timeout=30)
            row["checker_passed"] = checker.returncode == 0
            checker_record = {
                "exit_code": checker.returncode,
                "stdout": checker.stdout,
                "stderr": checker.stderr,
            }
        except (OSError, subprocess.TimeoutExpired) as error:
            row["checker_passed"] = False
            checker_record = {"status": "checker_error", "failure_type": type(error).__name__}
        write_artifact(output / (arm + ".checker.json"), checker_record)
        write_artifact(output / (arm + ".result.json"), row)
        rows.append(row)
        if row["terminal"] != "completed" or not row["checker_passed"] or row["usage"] is None:
            break
    treatment = next((r for r in rows if r["arm"] == "treatment"), None)
    control = next((r for r in rows if r["arm"] == "control"), None)
    verified = bool(
        treatment
        and control
        and all(
            r["terminal"] == "completed" and r["checker_passed"] and r["usage"] is not None
            for r in rows
        )
        and any(
            call.get("server") == "hippo"
            and call.get("tool") == "agent_query"
            and call.get("status") == "completed"
            and not call.get("error")
            and call.get("result") is not None
            and not call["result"].get("isError", False)
            for call in treatment["hippo_calls"]
        )
        and not control["hippo_calls"]
    )
    report = {
        "spec_hash": digest(spec),
        "study_id": spec["study_id"],
        "role": "diagnostic_only",
        "rows": rows,
        "model_thread_path_verified": verified,
        "source_support_verified": False,
        "canary_verified": False,
        "expansion_allowed": False,
        "unstarted_arms": [
            arm for arm in spec["arm_order"] if not any(r["arm"] == arm for r in rows)
        ],
    }
    write_artifact(output / "report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="private frozen diagnostic specification")
    parser.add_argument("--contract", type=Path, default=Path("config/agent-benefit-v2.json"))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    spec, contract = json.loads(args.spec.read_text()), load_contract(args.contract)
    if args.out:
        result = run_pair(spec, contract, args.out)
        print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=2))
    else:
        verify(spec, contract)
        print(json.dumps({"spec_hash": digest(spec), "verified_inputs": True, "agent_runs": 0}))


if __name__ == "__main__":
    main()
