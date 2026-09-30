import json
from pathlib import Path

import pytest

from hippo_brain.bench import agent_benefit_trial as trial
from hippo_brain.bench.agent_benefit_study import load_contract


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
    record = trial.run_arm(spec, {"model": "fixture", "effort": "medium"}, "control", out, 0.5, 50)
    assert record["turn_started"]
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
