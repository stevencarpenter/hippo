"""Decision experiments must remain reproducible and isolated from production."""

import asyncio
import json
import os
import runpy
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from hippo_brain.bench import decision_sidecar as sidecar
from hippo_brain.bench import decision_conflicts
from hippo_brain.bench.decision_capture import capture_rerank, decision_root
from hippo_brain.bench.decision_rules import evaluate, judge, validate_rules
from hippo_brain.rerank import build_rerank_messages, rerank_results
from hippo_brain.retrieval import SearchResult


def result(summary="a"):
    return SearchResult(
        uuid=summary,
        score=0.7,
        summary=summary,
        embed_text="detail",
        outcome=None,
        tags=[],
        cwd="",
        git_branch="",
        captured_at=0,
    )


def rules():
    return json.loads((sidecar.FIXTURES / "decision_rules.json").read_text())


def test_rule_priority_conflict_and_abstention():
    table = rules()
    validate_rules(table)
    facts = {"empty_source": False, "identical_text": True}
    assert evaluate(table, "verification", facts)["output"] == "supports"
    table.append(
        {
            "id": "conflict",
            "task": "verification",
            "priority": 90,
            "when": [{"fact": "identical_text", "op": "eq", "value": True}],
            "output": "contradicts",
        }
    )
    answer = evaluate(table, "verification", facts)
    assert answer["output"] is None and answer["conflict"]
    assert set(answer["winning_rules"]) == {"conflict", "identical-claim"}
    assert judge({"source": "A", "claim": "B"}, "verification", table)["verdict"] is None
    table[0]["when"][0]["op"] = "eval"
    with pytest.raises(ValueError, match="operator"):
        validate_rules(table)


def test_payload_uses_production_prompt_and_never_labels():
    case, labels = sidecar.normalize_case(
        {
            "id": "x",
            "task": "ranking",
            "query": "why",
            "grades": [3, 0],
            "secret_label": "do not send",
            "candidates": [
                {"summary": "a" * 500, "embed_text": "detail", "label": "do not send"},
                "b",
            ],
        }
    )
    llm, jev = sidecar.requests_for(case, "local", "jev-pinned")
    candidates = [result("a" * 500), result("b")]
    candidates[1].embed_text = ""
    assert llm["messages"] == build_rerank_messages("why", candidates)
    assert len(jev["state"]["candidates"][0]["summary"]) <= 303
    assert "do not send" not in sidecar.canonical((llm, jev))
    assert "grades" not in sidecar.canonical((llm, jev))
    assert labels == {"grades": [3, 0]}


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -1, 4])
def test_numeric_validation(value):
    with pytest.raises(ValueError):
        sidecar.number(value, 0, 3)


def test_jev_contract():
    case, _ = sidecar.normalize_case(
        {"id": "v", "task": "verification", "source": "A", "claim": "A"}
    )
    _, payload = sidecar.requests_for(case, "local", "jev-pinned")
    response = {
        "answers": {
            "verdict": {
                "type": "choice",
                "choice": "supports",
                "confidence": 0.9,
                "probabilities": {"supports": 0.95, "contradicts": 0.01, "unsupported": 0.04},
            }
        }
    }
    assert sidecar.parse_jev(response, payload, "verification")["verdict"] == "supports"
    response["answers"]["verdict"]["probabilities"]["unsupported"] = -0.1
    with pytest.raises(ValueError):
        sidecar.parse_jev(response, payload, "verification")
    response["answers"] = {}
    with pytest.raises(ValueError, match="missing"):
        sidecar.parse_jev(response, payload, "verification")


def setup_worker(tmp_path, cases, **config):
    normalized, labels = [], {}
    for raw in cases:
        case, gold = sidecar.normalize_case(raw)
        normalized.append(case)
        labels[f"{case['task']}:{case['id']}"] = gold
    manifest = {
        "run_id": "test",
        "arms": ["rules_jev", "llm"],
        "llm_model": "local",
        "llm_url": "http://local/v1",
        "jev_model": "jev-pinned",
        "timeout": 1,
        "repeats": 1,
        "reverse": False,
        "rules_margin": 2,
        **config,
    }
    sidecar.write_json(tmp_path / "manifest.json", manifest)
    sidecar.write_json(tmp_path / "rules.json", rules())
    sidecar.write_json(tmp_path / "labels.json", labels)
    (tmp_path / "inputs.jsonl").write_text("".join(sidecar.canonical(c) + "\n" for c in normalized))


def mock_http(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(
        sidecar.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(handler), **kw)
    )


def test_cascade_calls_only_on_abstain_and_retains_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-secret")
    setup_worker(
        tmp_path,
        [
            {
                "id": "equal",
                "task": "verification",
                "source": "same",
                "claim": "same",
                "expected": "supports",
            },
            {
                "id": "semantic",
                "task": "verification",
                "source": "not A",
                "claim": "A",
                "expected": "contradicts",
            },
        ],
    )
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "jev-resolved",
                "usage": {"input_tokens": 123, "output_tokens": 10},
                "answers": {
                    "verdict": {
                        "type": "choice",
                        "choice": "contradicts",
                        "confidence": 1,
                        "probabilities": {"supports": 0, "contradicts": 1, "unsupported": 0},
                    }
                },
            },
        )

    mock_http(monkeypatch, handler)
    assert sidecar.worker(tmp_path, "rules_jev") == 0
    rows = sidecar.read_rows(tmp_path)
    assert len(calls) == 1
    assert [r["network_calls"] for r in rows] == [0, 1]
    assert rows[1]["rules_output"]["verdict"] is None
    assert rows[1]["resolved_model"] == "jev-resolved"
    assert "expected" not in sidecar.canonical(calls)
    assert "test-secret" not in (tmp_path / "rules_jev.jsonl").read_text()
    summary = sidecar.summarize(tmp_path)["groups"][0]
    assert summary["accuracy"] == 1 and summary["input_tokens"] == 123
    assert summary["usage_missing"] == 0


def test_failure_is_observed_and_does_not_stop_next_case(tmp_path, monkeypatch):
    setup_worker(
        tmp_path,
        [
            {
                "id": str(i),
                "task": "ranking",
                "query": "q",
                "candidates": ["a", "b"],
                "grades": [3, 0],
            }
            for i in range(2)
        ],
    )
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "server says secret"})
        return httpx.Response(
            200, json={"model": "local", "choices": [{"message": {"content": "[1,2]"}}]}
        )

    mock_http(monkeypatch, handler)
    sidecar.worker(tmp_path, "llm")
    rows = sidecar.read_rows(tmp_path)
    assert [row["status"] for row in rows] == ["error", "ok"]
    assert rows[0]["fallback_order"] == [0, 1]
    group = next(g for g in sidecar.summarize(tmp_path)["groups"] if g["arm"] == "llm")
    assert group["coverage"] == 0.5 and group["accuracy"] == 0.5
    assert group["effective_ranking_accuracy"] == 1 and group["effective_ndcg"] == 1
    assert group["usage_missing"] == 2 and group["input_tokens"] == 0
    assert "server says secret" not in (tmp_path / "llm.jsonl").read_text()
    assert 'service_namespace="hippo-bench"' in sidecar.prometheus(tmp_path)


def test_reversed_repeats_preserve_candidate_identity(tmp_path, monkeypatch):
    setup_worker(
        tmp_path,
        [{"id": "q", "task": "ranking", "query": "q", "candidates": ["a", "b"], "grades": [3, 0]}],
        repeats=2,
        reverse=True,
    )
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "[1,2]"}}]}),
    )
    sidecar.worker(tmp_path, "llm")
    rows = sidecar.read_rows(tmp_path)
    assert [r["canonical_order"] for r in rows] == [[0, 1], [1, 0]]
    assert rows[0]["input_hash"] != rows[1]["input_hash"]
    group = next(g for g in sidecar.summarize(tmp_path)["groups"] if g["arm"] == "llm")
    assert group["accuracy"] == 0.5


def test_capture_is_opt_in_bounded_private_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("HIPPO_DECISION_CAPTURE", raising=False)
    capture_rerank("q", [result("a"), result("b")])
    assert not decision_root().exists()
    monkeypatch.setenv("HIPPO_DECISION_CAPTURE", "1")
    capture_rerank("q", [result("a"), result("b")])
    capture_rerank("q", [result("a"), result("b")])
    captures = list((decision_root() / "captures").glob("*.json"))
    assert len(captures) == 1 and captures[0].stat().st_mode & 0o777 == 0o600
    cases, labels = sidecar.load_cases(captures[0].parent)
    assert cases[0]["state"]["query"] == "q"
    assert list(labels.values()) == [{}]
    assert not (tmp_path / "hippo").exists()


def test_capture_failure_cannot_change_production_result(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("HIPPO_DECISION_CAPTURE", "1")
    (tmp_path / "hippo-bench").write_text("not a directory")

    class Client:
        async def chat(self, *_args, **_kwargs):
            return "[2,1]"

    results = [result("a"), result("b")]
    answer = asyncio.run(rerank_results(Client(), "local", "q", results, 2))
    assert answer == [results[1], results[0]]
    assert [r.score for r in answer] == [0.7, 0.7]


def test_rejects_artifact_symlink_into_production(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    (tmp_path / "hippo").mkdir()
    (tmp_path / "hippo-bench").symlink_to(tmp_path / "hippo", target_is_directory=True)
    with pytest.raises(ValueError, match="outside production"):
        decision_root()


def test_actual_rules_worker_process_is_isolated(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("HIPPO_OTEL_ENABLED", "1")
    monkeypatch.setenv("HIPPO_DECISION_CAPTURE", "1")
    prod = tmp_path / "hippo"
    prod.mkdir()
    sentinel = prod / "hippo.db"
    sentinel.write_bytes(b"production sentinel")
    assert sidecar.main(["run", "--arms", "rules", "--repeats", "2", "--reverse"]) == 0
    run_dir = next(decision_root().iterdir())
    rows = sidecar.read_rows(run_dir)
    assert len(rows) == 98
    assert all(row["pid"] != os.getpid() and row["network_calls"] == 0 for row in rows)
    assert sentinel.read_bytes() == b"production sentinel"
    assert list(prod.iterdir()) == [sentinel]
    assert (run_dir / "completion.json").exists()
    assert (run_dir / "summary.json").exists()
    assert all("request" not in row for row in rows)


def test_ndcg_and_unlabeled_data(tmp_path, monkeypatch):
    assert sidecar.ndcg([0, 3, 1], [1, 2, 0]) == 1
    assert sidecar.ndcg([0, 0], [0, 1]) is None
    setup_worker(
        tmp_path,
        [{"id": "no-label", "task": "verification", "source": "a", "claim": "a"}],
        arms=["rules"],
    )
    sidecar.worker(tmp_path, "rules")
    group = sidecar.summarize(tmp_path)["groups"][0]
    assert group["labeled"] == 0 and group["accuracy"] is None


def test_missing_workers_remain_in_quality_denominators(tmp_path):
    setup_worker(
        tmp_path,
        [
            {
                "id": "a",
                "task": "verification",
                "source": "a",
                "claim": "a",
                "expected": "supports",
            },
            {
                "id": "b",
                "task": "verification",
                "source": "b",
                "claim": "b",
                "expected": "supports",
            },
        ],
        arms=["rules", "jev"],
    )
    sidecar.worker(tmp_path, "rules")
    path = tmp_path / "rules.jsonl"
    path.write_text(path.read_text().splitlines()[0] + "\n")
    summary = sidecar.summarize(tmp_path)
    assert summary["missing_records"] == 3 and not summary["complete"]
    assert summary["groups"][0]["coverage"] == 0.5
    assert summary["groups"][0]["accuracy"] == 0.5
    assert summary["groups"][1]["missing"] == 2
    assert summary["groups"][1]["coverage"] == 0


def test_nonfinite_response_is_a_case_error_not_a_worker_crash(tmp_path, monkeypatch):
    setup_worker(tmp_path, [{"id": "a", "task": "ranking", "query": "a", "candidates": ["a", "b"]}])
    mock_http(
        monkeypatch, lambda request: httpx.Response(200, content=b'{"usage":{"prompt_tokens":NaN}}')
    )
    sidecar.worker(tmp_path, "llm")
    row = sidecar.read_rows(tmp_path)[0]
    assert row["status"] == "error"
    assert "response" not in row


def test_rounded_score_probabilities_are_accepted():
    payload = {"questions": {"candidate_0": {"type": "score", "criteria": sidecar.RUBRIC}}}
    response = {
        "answers": {
            "candidate_0": {
                "type": "score",
                "score": 2.95,
                "confidence": 0.95,
                "probabilities": {"0": 0.01, "1": 0.01, "2": 0.03, "3": 0.95},
            }
        }
    }
    assert sidecar.parse_jev(response, payload, "ranking")["order"] == [0]


def test_capture_symlink_cannot_write_production(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("HIPPO_DECISION_CAPTURE", "1")
    prod = tmp_path / "hippo"
    prod.mkdir()
    root = decision_root()
    root.mkdir(parents=True)
    (root / "captures").symlink_to(prod, target_is_directory=True)
    capture_rerank("q", [result("a"), result("b")])
    assert not list(prod.iterdir())


def test_parallel_workers_against_local_fixture_server(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    fixture = tmp_path / "cases.json"
    fixture.write_text(
        json.dumps(
            [
                {
                    "id": "r",
                    "task": "ranking",
                    "query": "a",
                    "candidates": ["a", "b"],
                    "grades": [3, 0],
                },
                {
                    "id": "v",
                    "task": "verification",
                    "source": "x",
                    "claim": "x",
                    "expected": "supports",
                },
            ]
        )
    )
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            content = (
                "[1,2]"
                if request["messages"][0]["content"].startswith("You rank")
                else '{"verdict":"supports"}'
            )
            body = json.dumps(
                {
                    "model": "test",
                    "choices": [{"message": {"content": content}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert (
                sidecar.main(
                    [
                        "run",
                        "--cases",
                        str(fixture),
                        "--parallel",
                        "--arms",
                        "llm,rules,rules_llm",
                        "--llm-model",
                        "test",
                        "--llm-url",
                        f"http://127.0.0.1:{server.server_port}/v1",
                    ]
                )
                == 0
            )
        finally:
            server.shutdown()
            thread.join(timeout=2)
    run_dir = next(decision_root().iterdir())
    rows = sidecar.read_rows(run_dir)
    assert len({row["pid"] for row in rows}) == 3
    assert len(rows) == 6 and sidecar.summarize(run_dir)["complete"]
    assert len(requests) == 2  # Both rules-first cases resolved without inference.
    assert "grades" not in sidecar.canonical(requests)


def test_whitespace_optional_fields_preserve_production_prompt():
    candidate = result("a")
    candidate.embed_text = " \n "
    candidate.commands_raw = " \t "
    case, _ = sidecar.normalize_case(
        {
            "id": "r",
            "task": "ranking",
            "query": "q",
            "candidates": [
                {"summary": "a", "embed_text": " \n ", "commands_raw": " \t "},
                {"summary": "b", "embed_text": "detail"},
            ],
        }
    )
    request, _ = sidecar.requests_for(case, "local", "jev")
    assert request["messages"] == build_rerank_messages("q", [candidate, result("b")])


def test_malformed_collections_are_validation_errors(tmp_path):
    for case in (
        {"id": "x", "task": []},
        {"id": "x", "task": "verification", "source": "s", "claim": "c", "expected": []},
    ):
        with pytest.raises(ValueError):
            sidecar.normalize_case(case)
    table = rules()
    table[0]["task"] = []
    with pytest.raises(ValueError):
        validate_rules(table)
    path = tmp_path / "bad.json"
    path.write_text('{"ranking":{"bad":"x"}}')
    with pytest.raises(ValueError):
        sidecar.load_cases(path)


def test_partial_multibyte_record_is_not_read(tmp_path):
    (tmp_path / "rules.jsonl").write_bytes(b'{"status":"ok"}\n{"content":"\xc3')
    assert sidecar.read_rows(tmp_path) == [{"status": "ok"}]


def test_empty_xdg_uses_default_data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", "")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert decision_root() == tmp_path / ".local/share/hippo-bench/decisions"


def test_sigterm_reaps_owned_worker(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    previous = signal.getsignal(signal.SIGTERM)

    class Child:
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if self.returncode is None:
                os.kill(os.getpid(), signal.SIGTERM)
            return self.returncode

        def terminate(self):
            self.returncode = -signal.SIGTERM

    child = Child()
    monkeypatch.setattr(sidecar.subprocess, "Popen", lambda *args, **kwargs: child)
    assert sidecar.main(["run"]) == 130
    assert child.returncode == -signal.SIGTERM
    assert signal.getsignal(signal.SIGTERM) == previous
    run_dir = next(decision_root().iterdir())
    summary = sidecar.summarize(run_dir)
    assert not summary["complete"] and summary["workers_failed"] == 1


def test_conflict_payload_whitelists_labels_and_preserves_current_detector():
    raw = {
        "id": "scope",
        "task": "decision_conflict",
        "expected": "compatible",
        "rationale": "LABEL_ONLY",
        "hits": [
            {
                "summary": "The vector backend is sqlite-vec",
                "label": "LABEL_ONLY",
                "design_decisions": [{"chosen": "sqlite-vec", "expected": "LABEL_ONLY"}],
            },
            {"summary": "The HTTP client is httpx", "design_decisions": [{"chosen": "httpx"}]},
        ],
    }
    case, labels = sidecar.normalize_case(raw)
    llm, jev = sidecar.requests_for(case, "local", "jev-pinned")
    assert "LABEL_ONLY" not in sidecar.canonical((llm, jev))
    assert json.loads(llm["messages"][1]["content"]) == jev["state"] == case["state"]
    assert labels == {"expected": "compatible"}
    assert jev["questions"]["verdict"]["criteria"] == decision_conflicts.CRITERIA
    from hippo_brain.conflict_detection import analyze_conflicts

    output = decision_conflicts.current(case["state"], case["task"])
    assert output["report"] == analyze_conflicts(case["state"]["hits"])
    assert output["verdict"] == "conflict"  # Existing heuristic deliberately preserved.
    assert judge(case["state"], case["task"], rules())["verdict"] is None


def test_conflict_rules_skip_empty_and_identical_but_not_different_qualifiers():
    for task in decision_conflicts.KINDS:
        state = decision_conflicts.normalize_state({"hits": []})
        assert judge(state, task, rules())["verdict"] == "compatible"
        hit = {
            "summary": "Same run, same result",
            "outcome": "success",
            "design_decisions": [{"chosen": "SQLite", "reason": "WAL required"}],
        }
        state = decision_conflicts.normalize_state({"hits": [hit, {**hit, "captured_at": 10}]})
        assert judge(state, task, rules())["verdict"] == "compatible"
        state["hits"][1]["summary"] = "Same run, opposite result"
        state["hits"][1]["design_decisions"][0]["reason"] = "WAL forbidden"
        assert judge(state, task, rules())["verdict"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"hits": None},
        {"hits": [None]},
        {"hits": [{"outcome": []}]},
        {"hits": [{"captured_at": True}]},
        {"hits": [{"design_decisions": {}}]},
        {"hits": [{"design_decisions": [{"chosen": []}]}]},
    ],
)
def test_conflict_bad_input_is_rejected_before_inference(change):
    with pytest.raises(ValueError):
        sidecar.normalize_case({"id": "bad", "task": "outcome_conflict", **change})


def test_current_conflict_worker_metrics_and_source_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    fixture = tmp_path / "cases.json"
    fixture.write_text(
        json.dumps(
            [
                {
                    "id": "false-alarm",
                    "task": "decision_conflict",
                    "expected": "compatible",
                    "hits": [
                        {"design_decisions": [{"chosen": "sqlite-vec"}]},
                        {"design_decisions": [{"chosen": "httpx"}]},
                    ],
                },
                {
                    "id": "hidden-middle",
                    "task": "decision_conflict",
                    "expected": "conflict",
                    "hits": [
                        {"captured_at": i, "design_decisions": [{"chosen": choice}]}
                        for i, choice in enumerate(["A", "B", "A"])
                    ],
                },
            ]
        )
    )
    assert (
        sidecar.main(
            [
                "run",
                "--cases",
                str(fixture),
                "--arms",
                "current,rules",
                "--repeats",
                "2",
                "--reverse",
            ]
        )
        == 0
    )
    run_dir = next(decision_root().iterdir())
    rows = sidecar.read_rows(run_dir)
    assert len(rows) == 8 and len({row["pid"] for row in rows}) == 2
    assert all(row["network_calls"] == 0 for row in rows)
    assert rows[0]["input_hash"] != rows[2]["input_hash"]
    summary = sidecar.summarize(run_dir)
    current = next(group for group in summary["groups"] if group["arm"] == "current")
    assert current["false_conflicts"] == 2 and current["missed_conflicts"] == 2
    assert current["accuracy"] == 0 and summary["complete"]
    assert 'task="decision_conflict",arm="current"' in sidecar.prometheus(run_dir)
    for path in sidecar.implementation_paths():
        relative = path.relative_to(Path(sidecar.__file__).parents[1])
        assert (run_dir / "source" / relative).read_bytes() == path.read_bytes()
    assert len(sidecar.implementation_hashes()) == len(sidecar.implementation_paths())
    audit = runpy.run_path(
        str(Path(__file__).parents[2] / "docs/research/typesafe-2026-09-20-conflicts/audit.py")
    )["audit"]
    analysis = audit(run_dir)
    assert analysis["validated_records"] == 8 and analysis["missing_records"] == 0
    actual = next(group for group in analysis["groups"] if group["arm"] == "current")
    assert actual["false_conflicts"] == 2 and actual["missed_conflicts"] == 2
    assert actual["correct_in_both_orders"] == 0
    assert actual["changed_between_orders"] == 0
    with (run_dir / "current.jsonl").open("a") as stream:
        stream.write(sidecar.canonical(next(row for row in rows if row["arm"] == "current")) + "\n")
    with pytest.raises(ValueError, match="row identity"):
        audit(run_dir)


def test_conflict_cascade_contract_and_pairing(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-secret")
    setup_worker(
        tmp_path,
        [
            {"id": "empty", "task": "outcome_conflict", "hits": [], "expected": "compatible"},
            {
                "id": "recovery",
                "task": "outcome_conflict",
                "expected": "compatible",
                "hits": [
                    {"summary": "Attempt 1 failed", "outcome": "failure"},
                    {"summary": "Retry attempt 2 passed", "outcome": "success"},
                ],
            },
        ],
        arms=["current", "rules_jev", "llm"],
    )
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if "questions" in payload:
            data = {
                "answers": {
                    "verdict": {
                        "type": "choice",
                        "choice": "compatible",
                        "confidence": 1,
                        "probabilities": {"conflict": 0, "compatible": 1},
                    }
                }
            }
        else:
            data = {"choices": [{"message": {"content": '{"verdict":"compatible"}'}}]}
        return httpx.Response(200, json=data)

    mock_http(monkeypatch, handler)
    for arm in ("current", "rules_jev", "llm"):
        assert sidecar.worker(tmp_path, arm) == 0
    assert len(calls) == 3 and "expected" not in sidecar.canonical(calls)
    summary = sidecar.summarize(tmp_path)
    assert all(pair["baseline_arm"] == "current" for pair in summary["paired"])
    assert all(pair["agreement"] == 0.5 for pair in summary["paired"])
    assert next(g for g in summary["groups"] if g["arm"] == "rules_jev")["network_calls"] == 1


def test_current_arm_cannot_misrepresent_ranking(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    with pytest.raises(SystemExit) as error:
        sidecar.main(["run", "--arms", "current"])
    assert error.value.code == 2 and not decision_root().exists()
