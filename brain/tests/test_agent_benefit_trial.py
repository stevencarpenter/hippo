import json
import sys
from pathlib import Path

import pytest

from hippo_brain.bench import agent_benefit_trial as trial
from hippo_brain.bench.agent_benefit_study import load_contract


def test_guided_query_uses_only_frozen_original_request():
    contract = load_contract(Path(__file__).parents[2] / "config/agent-benefit-v2.json")
    request = "fix CI"
    expected = trial.query_arguments(request, "/project")
    prompt = trial.guided_prompt(contract, request, "/project")
    assert json.loads(prompt.splitlines()[-1]) == expected
    assert expected["query"] == request[:1000]
    assert trial.query_arguments("é" * 1100, "/project")["query"] == "é" * 1000
    call = {"server": "hippo", "tool": "agent_query", "arguments": expected}
    assert trial.query_fidelity([call], expected)
    assert not trial.query_fidelity([], expected)
    assert not trial.query_fidelity([call, call], expected)
    for arguments in ({**expected, "query": prompt[:1000]}, {**expected, "project": "/other"}):
        assert not trial.query_fidelity([{**call, "arguments": arguments}], expected)


def test_related_nodes_alone_cannot_qualify_a_usefulness_diagnostic():
    proof = {"supporting_node_ids": ["source"]}
    with pytest.raises(ValueError, match="source-bound history opportunity"):
        trial.verify_history_opportunity(proof)
    opportunity = {
        "source_node_id": "source",
        "fact": "A prior attempt failed because the installed version differed from the lockfile.",
        "expected_application": "Check the installed version before repeating that attempt.",
        "scored_check": "The independent trace checker detects a repeated failed attempt.",
        "advantage_over_starting_context": "The installed state is absent from the checkout.",
    }
    trial.verify_history_opportunity({**proof, "history_opportunity": opportunity})
    for field in opportunity:
        with pytest.raises(ValueError):
            trial.verify_history_opportunity(
                {**proof, "history_opportunity": {**opportunity, field: ""}}
            )


@pytest.mark.parametrize("control_checker_exit", [1, 2])
def test_control_task_failure_runs_treatment_but_checker_error_stops(
    tmp_path, monkeypatch, control_checker_exit
):
    contract = load_contract(Path(__file__).parents[2] / "config/agent-benefit-v2.json")
    monkeypatch.setattr(trial, "verify", lambda *args: None)
    monkeypatch.setattr(trial, "hippo_bench_root", lambda: tmp_path / "bench")
    called = []
    expected = trial.query_arguments("task", "/project")

    def run(spec, contract, arm, output, seconds, tokens):
        called.append(arm)
        return {
            "arm": arm,
            "terminal": "completed",
            "elapsed_seconds": 0.01,
            "usage": {"totalTokens": 1},
            "hippo_calls": []
            if arm == "control"
            else [
                {
                    "server": "hippo",
                    "tool": "agent_query",
                    "status": "completed",
                    "arguments": expected,
                    "result": {"content": []},
                }
            ],
        }

    monkeypatch.setattr(trial, "run_arm", run)
    for arm in ("control", "treatment"):
        (tmp_path / arm).mkdir()
        (tmp_path / arm / "original-task.txt").write_text("task")
    checker = tmp_path / "check.py"
    checker.write_text(
        f"import sys\nsys.exit({control_checker_exit} if sys.argv[1].endswith('/control/repo') else 0)\n"
    )
    spec = {
        "arm_order": ["control", "treatment"],
        "original_project": "/project",
        "arms": {arm: {"root": str(tmp_path / arm)} for arm in ("control", "treatment")},
        "checker": [sys.executable, str(checker), "{repo}"],
        "study_id": "diagnostic",
    }
    report = trial.run_pair(spec, contract, tmp_path / "out")
    assert called == (["control", "treatment"] if control_checker_exit == 1 else ["control"])
    assert not report["rows"][0]["checker_passed"]
    if control_checker_exit == 1:
        assert report["rows"][1]["checker_passed"]
        assert report["model_thread_path_verified"] and report["query_policy_verified"]
    else:
        assert not report["model_thread_path_verified"]
        assert report["unstarted_arms"] == ["treatment"]
    assert not report["canary_verified"]


def test_pending_amendment_cannot_reserve_or_start_a_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(trial, "verify", lambda *args: pytest.fail("must not prepare a launch"))
    with pytest.raises(ValueError, match="requires owner approval before reservation"):
        trial.run_pair({}, {"amendment": {"approval_status": "pending"}}, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_rejected_model_tool_call_does_not_verify_delivery(tmp_path, monkeypatch):
    contract = load_contract(Path(__file__).parents[2] / "config/agent-benefit-v2.json")
    monkeypatch.setattr(trial, "verify", lambda *args: None)
    monkeypatch.setattr(trial, "hippo_bench_root", lambda: tmp_path / "bench")

    def run(spec, contract, arm, output, seconds, tokens):
        return {
            "arm": arm,
            "terminal": "completed",
            "elapsed_seconds": 0.01,
            "usage": {"totalTokens": 1},
            "hippo_calls": []
            if arm == "control"
            else [
                {
                    "server": "hippo",
                    "tool": "agent_query",
                    "status": "failed",
                    "error": {"message": "approval denied"},
                    "result": None,
                }
            ],
        }

    monkeypatch.setattr(trial, "run_arm", run)
    spec = {
        "arm_order": ["control", "treatment"],
        "arms": {arm: {"root": str(tmp_path / arm)} for arm in ("control", "treatment")},
        "checker": ["/usr/bin/true"],
        "study_id": "diagnostic",
        "original_project": "/project",
    }
    (tmp_path / "treatment").mkdir()
    (tmp_path / "treatment/original-task.txt").write_text("task")
    report = trial.run_pair(spec, contract, tmp_path / "out")
    assert not report["model_thread_path_verified"]
    assert not report["canary_verified"]


def test_host_history_denials_cannot_be_omitted_from_the_trial_spec(tmp_path):
    paths = (
        ".pi/agent/sessions",
        ".claude/projects",
        ".codex",
        ".local/share/hippo",
        ".zsh_history",
        ".bash_history",
    )
    permissions = {str(tmp_path / path): "deny" for path in paths}
    trial.verify_history_boundaries(permissions, tmp_path)
    for path in paths:
        with pytest.raises(ValueError, match="missing host history denial"):
            trial.verify_history_boundaries(
                {key: value for key, value in permissions.items() if key != str(tmp_path / path)},
                tmp_path,
            )


def test_runtime_inventory_rejects_apps_missing_servers_and_discovery_errors():
    trial.verify_inventory({"data": []}, "control")
    server = {"name": "hippo", "tools": dict.fromkeys(trial.TOOLS), "runtimeStatus": "connected"}
    trial.verify_inventory({"data": [server]}, "treatment")
    for inventory, arm in [
        ({"data": [server]}, "control"),
        ({"data": []}, "treatment"),
        ({"data": [server, {**server, "name": "apps"}]}, "treatment"),
        ({"data": [{**server, "toolsError": "failed"}]}, "treatment"),
    ]:
        with pytest.raises(ValueError):
            trial.verify_inventory(inventory, arm)


@pytest.mark.parametrize(
    "behavior,terminal",
    [
        ("zero_call", "completed"),
        ("closed", "transport_closed"),
        ("timeout", "timeout"),
        ("token_cap", "token_budget_exhausted"),
        ("preflight", "preflight_verified"),
    ],
)
def test_real_stdio_transport_keeps_explicit_terminal_failures(tmp_path, behavior, terminal):
    root = tmp_path / "control"
    for folder in ("repo", "home", "codex-home/tmp", "data"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "prompt.txt").write_text("task")
    fake = tmp_path / "codex"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        + f"BEHAVIOR = {behavior!r}\n"
        + """import sys,json,time
def send(value): print(json.dumps(value),flush=True)
for line in sys.stdin:
    request=json.loads(line);method=request['method']; ident=request.get('id')
    if ident is None: continue
    result={}
    if method=='thread/start':result={'thread':{'id':'thread'}}
    elif method=='mcpServerStatus/list':result={'data':[]}
    elif method=='command/exec':result={'exitCode':0,'stdout':'boundaries_denied\\n'}
    send({'id':ident,'result':result})
    if method=='turn/start':
        if BEHAVIOR=='closed':break
        if BEHAVIOR=='timeout':time.sleep(2);break
        send({'method':'thread/tokenUsage/updated','params':{'tokenUsage':{'total':{'totalTokens':100 if BEHAVIOR=='token_cap' else 5,'inputTokens':4,'cachedInputTokens':0,'outputTokens':1}}}})
        send({'method':'turn/completed','params':{'turn':{'status':'completed'}}})
"""
    )
    fake.chmod(0o700)
    spec = {
        "codex": str(fake),
        "arms": {
            "control": {
                "root": str(root),
                "read_probes": [str(root / "data/x")],
                "write_probes": [str(root / "data/y")],
            }
        },
    }
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    record = trial.run_arm(
        spec,
        {"model": "fixture", "effort": "medium"},
        "control",
        out,
        0.5,
        50,
        preflight_only=behavior == "preflight",
    )
    assert record["turn_started"] is (behavior != "preflight")
    assert record["launch_command"][-4:] == ["--disable", "plugins", "--disable", "remote_plugin"]
    if behavior == "preflight":
        assert '"turn/start"' not in (out / "control.rpc.jsonl").read_text()
    assert record["terminal"] == terminal
    assert record["hippo_calls"] == []
    assert record["trace_sha256"]


def test_first_failed_canary_blocks_the_second_arm_and_repeat_launch(tmp_path, monkeypatch):
    contract = load_contract(Path(__file__).parents[2] / "config/agent-benefit-v2.json")
    monkeypatch.setattr(trial, "verify", lambda *args: None)
    monkeypatch.setattr(trial, "hippo_bench_root", lambda: tmp_path / "bench")
    called = []

    def fail(spec, contract, arm, output, seconds, tokens):
        called.append(arm)
        return {
            "arm": arm,
            "terminal": "transport_closed",
            "usage": None,
            "elapsed_seconds": 0.01,
            "hippo_calls": [],
        }

    monkeypatch.setattr(trial, "run_arm", fail)
    spec = {
        "arm_order": ["control", "treatment"],
        "arms": {arm: {"root": str(tmp_path / arm)} for arm in ("control", "treatment")},
        "checker": ["/usr/bin/true"],
        "study_id": "diagnostic",
    }
    report = trial.run_pair(spec, contract, tmp_path / "out")
    assert called == ["control"]
    assert report["canary_verified"] is False
    assert report["unstarted_arms"] == ["treatment"]
    with pytest.raises(FileExistsError):
        trial.run_pair(spec, contract, tmp_path / "second-out")
    assert (
        json.loads((tmp_path / "out/report.json").read_text())["rows"][0]["terminal"]
        == "transport_closed"
    )
