"""Runtime contracts for typed decisions, deadlines, and provenance refinement."""

import asyncio
import copy
import json
import time

import httpx
import pytest

from hippo_brain.decision_rules import evaluate, ranking_rules
from hippo_brain.jev import JevClient, JevUnavailable, parse_nouls, validate_response
from hippo_brain.rerank import build_jev_questions, compose_scores, rerank_results
from hippo_brain.retrieval import Filters, SearchResult


def result(uuid, summary="some evidence"):
    return SearchResult(
        uuid=uuid,
        score=0.71,
        summary=summary,
        embed_text="detail",
        outcome=None,
        tags=[],
        cwd="hippo",
        git_branch="jev",
        captured_at=1,
    )


def response(questions, grades, *, confidence=0.4):
    answers = {}
    for key, question in questions.items():
        index = int(key.split("_")[1])
        score = grades[index]
        answers[key] = {
            "type": "score",
            "score": score,
            "confidence": confidence,
            "probabilities": {str(i): float(i == score) for i in range(4)},
        }
    return {
        "model": "1.13.0",
        "answers": answers,
        "usage": {"input_tokens": 123, "output_tokens": 45},
    }


class FakeJev:
    model = "1.13.0"

    def __init__(self, actions):
        self.actions = actions
        self.calls = []

    async def assess(self, state, questions, *, timeout_seconds=None):
        self.calls.append((copy.deepcopy(state), copy.deepcopy(questions)))
        action = self.actions[len(self.calls) - 1]
        if isinstance(action, Exception):
            raise action
        if callable(action):
            return await action(questions)
        return response(questions, action)


async def test_multi_signal_keeps_exact_objects_and_scores_and_ties():
    results = [result("a"), result("b"), result("c")]
    client = FakeJev([[1, 3, 3]])
    trace = {}
    ranked = await rerank_results(
        None, "ignored", "q", results, 3, backend="jev", jev_client=client, diagnostics=trace
    )
    assert ranked == [results[1], results[2], results[0]]
    assert ranked[0] is results[1] and ranked[0].score == 0.71
    assert len(client.calls[0][1]) == 9
    assert trace["attempts"] == 1 and trace["passes"][0]["usage"]["input_tokens"] == 123
    assert trace["final_order"] == ["b", "c", "a"]
    assert trace["stop_reason"] == "single_pass"


async def test_single_signal_is_same_recipe_as_benchmark():
    from hippo_brain.bench.decision_sidecar import requests_for, parse_jev

    results = [result("a"), result("b")]
    client = FakeJev([[1, 3]])
    output = await rerank_results(
        None, "local", "q", results, 2, backend="jev", recipe="single-v1", jev_client=client
    )
    state, questions = client.calls[0]
    _, payload = requests_for({"state": state, "task": "ranking"}, "local", "1.13.0")
    assert questions == payload["questions"]
    order = parse_jev(response(questions, [1, 3]), payload, "ranking")["order"]
    assert output == [results[i] for i in order]


@pytest.mark.parametrize(
    "results,question,budget",
    [
        ([], "q", 4),
        ([result("a")], "q", 4),
        ([result("a"), result("a")], "q", 4),
        ([result(str(i)) for i in range(31)], "q", 4),
        ([result("a"), result("b")], "", 4),
        ([result("a"), result("b")], "q", 0),
    ],
)
async def test_native_no_call_guards(results, question, budget):
    client = FakeJev([])
    assert (
        await rerank_results(
            None,
            "local",
            question,
            results,
            30,
            backend="jev",
            jev_client=client,
            request_budget=budget,
        )
        == results[:30]
    )
    assert client.calls == []


async def test_rules_backend_reports_trace_without_jev():
    results = [result("a", "unrelated"), result("b", "cargo test failure")]
    trace = {}
    ranked = await rerank_results(
        None, "local", "cargo test", results, 2, backend="rules", diagnostics=trace
    )
    assert ranked == results[::-1]
    assert trace["attempts"] == 0
    assert trace["rule_trace"][2]["winning_rules"] == ["literal-query"]


def test_rules_abstain_on_missing_or_wrong_fact_types():
    rules = ranking_rules()
    for facts in ({}, {"token_overlap": True, "query_tokens": 1, "exact_query": False}):
        outcome = evaluate(rules, "ranking", facts)
        assert outcome["output"] is None and outcome["invalid_facts"]


async def test_missing_key_and_model_drift_fail_to_original(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    results = [result("a"), result("b")]
    assert await rerank_results(None, "local", "q", results, 2, backend="jev") == results

    async def drift(questions):
        data = response(questions, [1, 3])
        data["model"] = "1.14.0"
        return data

    trace = {}
    assert (
        await rerank_results(
            None,
            "local",
            "q",
            results,
            2,
            backend="jev",
            jev_client=FakeJev([drift]),
            diagnostics=trace,
        )
        == results
    )
    assert trace["fallback_reason"] == "ValueError"


def browser_sources(conn, results, *, excluded=False):
    for index, item in enumerate(results, 1):
        conn.execute(
            "INSERT INTO browser_events(id, timestamp, url, domain, dwell_ms, extracted_text, probe_tag) VALUES (?, 1, 'https://example.test', 'example.test', 1000, ?, ?)",
            (index, f"source {index} " + "content " * 500, "probe" if excluded else None),
        )
        conn.execute(
            "INSERT INTO knowledge_nodes(id, uuid, content, embed_text) VALUES (?, ?, '{}', '')",
            (index, item.uuid),
        )
        conn.execute(
            "INSERT INTO knowledge_node_browser_events(knowledge_node_id, browser_event_id) VALUES (?, ?)",
            (index, index),
        )
        item.evidence = [{"ref": f"browser-{index}", "source_kind": "browser"}]
    conn.commit()


async def test_refinement_appends_new_source_spans_and_stops_on_stable_order(tmp_db):
    conn, _ = tmp_db
    results = [result("a"), result("b")]
    browser_sources(conn, results)
    client = FakeJev([[1, 3], [1, 3]])
    trace = {}
    output = await rerank_results(
        None,
        "local",
        "q",
        results,
        2,
        backend="jev",
        adaptive=True,
        conn=conn,
        jev_client=client,
        diagnostics=trace,
    )
    assert output == results[::-1] and len(client.calls) == 2
    initial, refined = [call[0] for call in client.calls]
    assert initial["candidates"][0]["source_evidence"] == []
    span = refined["candidates"][0]["source_evidence"][0]
    assert span["ref"] == "browser-1" and span["start"] == 0 and span["end"] == 1200
    assert trace["stop_reason"] == "stable_order" and not conn.in_transaction
    assert all(row["status"] == "complete" for row in trace["passes"])


@pytest.mark.parametrize("failure", [RuntimeError("provider offline"), ValueError("bad pass")])
async def test_failed_refinement_preserves_last_complete_order(tmp_db, failure):
    conn, _ = tmp_db
    results = [result("a"), result("b")]
    browser_sources(conn, results)
    trace = {}
    output = await rerank_results(
        None,
        "local",
        "q",
        results,
        2,
        backend="jev",
        adaptive=True,
        conn=conn,
        jev_client=FakeJev([[1, 3], failure]),
        diagnostics=trace,
    )
    assert output == results[::-1] and trace["attempts"] == 2


async def test_refinement_never_uses_excluded_sources(tmp_db, monkeypatch):
    conn, _ = tmp_db
    results = [result("a"), result("b")]
    browser_sources(conn, results, excluded=True)
    monkeypatch.setenv("HIPPO_RETRIEVAL_INCLUDE_EXCLUDED", "1")
    client = FakeJev([[1, 3]])
    trace = {}
    await rerank_results(
        None,
        "local",
        "q",
        results,
        2,
        backend="jev",
        adaptive=True,
        conn=conn,
        jev_client=client,
        diagnostics=trace,
    )
    assert len(client.calls) == 1 and trace["stop_reason"] == "no_new_evidence"


async def test_pass_and_request_limits_and_no_repeated_evidence(tmp_db):
    conn, _ = tmp_db
    results = [result("a"), result("b")]
    browser_sources(conn, results)
    client = FakeJev([[1, 3], [3, 1], [1, 3]])
    trace = {}
    await rerank_results(
        None,
        "local",
        "q",
        results,
        2,
        backend="jev",
        adaptive=True,
        conn=conn,
        jev_client=client,
        diagnostics=trace,
    )
    assert len(client.calls) == 3 and trace["stop_reason"] == "request_budget"
    spans = client.calls[-1][0]["candidates"][0]["source_evidence"]
    assert [span["start"] for span in spans] == [0, 1200]
    client = FakeJev([[1, 3]])
    await rerank_results(
        None,
        "local",
        "q",
        results,
        2,
        backend="jev",
        adaptive=True,
        conn=conn,
        jev_client=client,
        request_budget=1,
    )
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "filters",
    [
        Filters(project="allowed"),
        Filters(branch="main"),
        Filters(since_ms=1000),
        Filters(source="browser"),
        Filters(entity="not-linked"),
        Filters(memory_category="project"),
    ],
)
async def test_refinement_filters_each_source_of_mixed_node(tmp_db, filters):
    conn, _ = tmp_db
    results = [result("a"), result("b")]
    browser_sources(conn, results)
    conn.execute(
        "INSERT INTO sessions(id, start_time, shell, hostname, username) VALUES (1, 1, 'zsh', 'host', 'user')"
    )
    for eid, cwd, branch, timestamp, command in [
        (1, "/other", "other", 1, "OFF_SCOPE_MARKER"),
        (2, "/allowed", "main", 2000, "ALLOWED_MARKER"),
    ]:
        conn.execute(
            "INSERT INTO events(id, session_id, timestamp, command, duration_ms, cwd, hostname, shell, git_branch) VALUES (?, 1, ?, ?, 0, ?, 'host', 'zsh', ?)",
            (eid, timestamp, command, cwd, branch),
        )
        for node_id in (1, 2):
            conn.execute(
                "INSERT INTO knowledge_node_events(knowledge_node_id, event_id) VALUES (?, ?)",
                (node_id, eid),
            )
    conn.commit()
    for index, item in enumerate(results, 1):
        item.evidence = [{"ref": "shell-1"}, {"ref": "shell-2"}, {"ref": f"browser-{index}"}]
    client = FakeJev([[1, 3], [1, 3]])
    await rerank_results(
        None,
        "local",
        "q",
        results,
        2,
        backend="jev",
        adaptive=True,
        conn=conn,
        jev_client=client,
        effective_filters=filters,
    )
    assert "OFF_SCOPE_MARKER" not in json.dumps(client.calls)
    if filters.source == "browser":
        assert "ALLOWED_MARKER" not in json.dumps(client.calls)
    if filters.project or filters.branch or filters.since_ms:
        assert "ALLOWED_MARKER" in json.dumps(client.calls)


async def test_slow_sql_is_off_loop_and_cannot_overrun_decision_deadline(tmp_db, monkeypatch):
    import hippo_brain.rerank as module

    conn, _ = tmp_db
    results = [result("a"), result("b")]
    browser_sources(conn, results)
    original = module._new_source_evidence
    connections = []

    def slow(owned_conn, *args):
        connections.append(owned_conn)
        time.sleep(0.15)
        return original(owned_conn, *args)

    monkeypatch.setattr(module, "_new_source_evidence", slow)
    trace = {}
    before = time.monotonic()
    output = await rerank_results(
        None,
        "local",
        "q",
        results,
        2,
        backend="jev",
        adaptive=True,
        conn=conn,
        jev_client=FakeJev([[1, 3]]),
        deadline_ms=30,
        diagnostics=trace,
    )
    assert time.monotonic() - before < 0.1 and output == results[::-1]
    assert connections and connections[0] is not conn and trace["stop_reason"] == "deadline"
    await asyncio.sleep(0.18)


async def test_redaction_precedes_candidate_truncation():
    secret = "abcdefghijklmno123456789"
    client = FakeJev([[1, 3]])
    results = [result("a", "x" * 290 + " password=" + secret), result("b")]
    await rerank_results(None, "local", "q", results, 2, backend="jev", jev_client=client)
    text = client.calls[0][0]["candidates"][0]["summary"]
    assert "password=" not in text and secret not in text


async def test_wall_deadline_and_caller_cancellation():
    cancelled = asyncio.Event()

    async def slow(questions):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    results = [result("a"), result("b")]
    trace = {}
    before = time.monotonic()
    output = await rerank_results(
        None,
        "local",
        "q",
        results,
        2,
        backend="jev",
        jev_client=FakeJev([slow]),
        deadline_ms=20,
        diagnostics=trace,
    )
    assert time.monotonic() - before < 0.5 and output == results and cancelled.is_set()
    assert trace["stop_reason"] == "deadline"
    cancelled.clear()
    client = FakeJev([slow])
    task = asyncio.create_task(
        rerank_results(None, "local", "q", results, 2, backend="jev", jev_client=client)
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


async def test_prior_stage_consumes_same_deadline():
    client = FakeJev([])
    results = [result("a"), result("b")]
    assert (
        await rerank_results(
            None,
            "local",
            "q",
            results,
            2,
            backend="jev",
            jev_client=client,
            started_at=time.monotonic() - 3,
        )
        == results
    )
    assert not client.calls


def test_weight_replay_rejects_nonfinite_and_preserves_ties():
    signals = [
        {"relevance": 1.0, "evidence": 0.0, "scope_fit": 0.0},
        {"relevance": 0.0, "evidence": 1.0, "scope_fit": 1.0},
    ]
    assert compose_scores(signals)[0] == [0, 1]
    assert compose_scores(signals, {"relevance": 0.0, "evidence": 0.5, "scope_fit": 0.5})[0] == [
        1,
        0,
    ]
    signals[0]["relevance"] = float("nan")
    with pytest.raises(ValueError):
        compose_scores(signals)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["answers"].pop("candidate_1"),
        lambda data: data["answers"].update(unknown=data["answers"]["candidate_0"]),
        lambda data: data["answers"]["candidate_0"].update(score=float("nan")),
        lambda data: data["answers"]["candidate_0"].update(score=2),
        lambda data: data["answers"]["candidate_0"]["probabilities"].update({"0": 1}),
        lambda data: data["answers"]["candidate_0"].update(confidence=True),
    ],
)
def test_invalid_typed_answers_rejected(mutation):
    questions = build_jev_questions(2, "single-v1")
    data = response(questions, [1, 3])
    mutation(data)
    with pytest.raises(ValueError):
        validate_response(data, questions, model="1.13.0")


async def test_http_redaction_noul_validation_and_no_hidden_retries():
    requests = []
    questions = {"topic": {"type": "noul", "instructions": "Is this about databases?"}}

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json={"model": "jev-1.13.0", "answers": {"topic": {"type": "noul", "noul": 0.9}}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = JevClient("test-key", client=http)
        assert client.model == "1.13.0" and client.request_model == "jev-1.13.0"
        data = await client.assess({"text": "password=verysecretvalue"}, questions)
        assert parse_nouls(data, questions) == {"topic": 0.9}
        assert data.get("usage") is None
        assert requests[0]["state"]["text"] == "[REDACTED]"
        assert requests[0]["model"] == "jev-1.13.0"
        assert data["_transport"]["payload_bytes"] > 0
        assert data["_transport"]["status_code"] == 200
        with pytest.raises(ValueError, match="128KB"):
            await client.assess({"text": "a" * 128_000}, questions)
        assert len(requests) == 1
        data["answers"]["topic"]["noul"] = True
        with pytest.raises(ValueError):
            parse_nouls(data, questions)


@pytest.mark.parametrize(
    "opening",
    [
        "-----BEGIN PRIVATE KEY-----",
        "[REDACTED]",
        "private_key=-----BEGIN PRIVATE KEY-----",
        "[REDACTED] PRIVATE KEY-----",
    ],
)
async def test_private_key_body_never_reaches_jev_transport(opening):
    requests = []
    pem = f"{opening}\nZmFrZXNlY3JldA==\n-----END PRIVATE KEY-----"

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(200, json=response(payload["questions"], [1, 3]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = JevClient("test-key", client=http)
        await rerank_results(
            None,
            "local",
            f"What was this key used for?\n{pem}",
            [result("a", pem), result("b")],
            2,
            backend="jev",
            jev_client=client,
        )
    assert len(requests) == 1
    assert "ZmFrZXNlY3JldA==" not in json.dumps(requests)
    assert requests[0]["state"]["candidates"][0]["summary"] == "[REDACTED]"


async def test_endpoint_failures_cool_down_without_retrying():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, json={"error": "limited"})

    questions = {"topic": {"type": "noul", "instructions": "topic?"}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = JevClient("test-key", client=http)
        for _ in range(3):
            with pytest.raises(httpx.HTTPStatusError) as failure:
                await client.assess({}, questions)
            assert failure.value.jev_transport["status_code"] == 429
        with pytest.raises(JevUnavailable) as failure:
            await client.assess({}, questions)
        assert failure.value.jev_transport["status_code"] is None
        assert len(requests) == 3 and client.diagnostics()["cooldown_seconds"] > 0


async def test_deadline_includes_waiting_for_client_concurrency():
    entered, finish = asyncio.Event(), asyncio.Event()

    async def handler(request):
        entered.set()
        await finish.wait()
        return httpx.Response(
            200, json={"model": "1.13.0", "answers": {"q": {"type": "noul", "noul": 1}}}
        )

    questions = {"q": {"type": "noul", "instructions": "q?"}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = JevClient("test-key", client=http, concurrency=1)
        active = asyncio.create_task(client.assess({}, questions))
        await entered.wait()
        with pytest.raises(TimeoutError) as failure:
            await client.assess({}, questions, timeout_seconds=0.01)
        assert failure.value.jev_transport["status_code"] is None
        finish.set()
        await active
        assert client.diagnostics()["consecutive_failures"] == 0


async def test_records_metrics_once_after_final_diagnostics(monkeypatch):
    import hippo_brain.telemetry as telemetry

    observed = []
    monkeypatch.setattr(
        telemetry,
        "record_decision_metrics",
        lambda trace, *, task: observed.append((copy.deepcopy(trace), task)),
        raising=False,
    )
    results = [result("a"), result("b")]
    await rerank_results(None, "", "q", results, 2, backend="jev", jev_client=FakeJev([[1, 3]]))
    assert len(observed) == 1 and observed[0][1] == "rerank"
    assert observed[0][0]["final_order"] == ["b", "a"]
    assert observed[0][0]["elapsed_ms"] > 0
