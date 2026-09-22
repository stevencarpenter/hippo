"""External-only, checkpointed replay of fixed candidate objects."""

import argparse
import json

import pytest

from hippo_brain.bench.knowledge_replay import _cached_local, load_inputs, replay
from hippo_brain.jev import digest
from hippo_brain.rerank import build_rerank_messages
from hippo_brain.retrieval import SearchResult


def arguments(tmp_path, **overrides):
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "q1",
                    "query": "cargo test",
                    "candidates": [
                        {"uuid": "a", "summary": "unrelated"},
                        {"uuid": "b", "summary": "cargo test success"},
                    ],
                }
            ]
        )
    )
    return argparse.Namespace(
        cases=path,
        out=tmp_path / "replay",
        targets=None,
        questions=None,
        db=None,
        cached_local=None,
        arms="retrieval,rules",
        repeats=1,
        deadline_ms=2000,
        max_requests=0,
        max_tokens=0,
        local_url="",
        local_model="",
        local_timeout=180,
        resume=False,
        **overrides,
    )


async def test_external_replay_and_idempotent_resume(tmp_path):
    args = arguments(tmp_path)
    summary = await replay(args)
    assert summary["complete"] and summary["observed_rows"] == 2 and summary["network_calls"] == 0
    rows = json.loads((args.out / "rankings.json").read_text())
    assert rows[0]["order"] == ["a", "b"] and rows[1]["order"] == ["b", "a"]
    assert all(row["filter_violations"] is None for row in rows)
    assert all(row["malformed_accepted"] == 0 for row in rows)
    original = (args.out / "results.jsonl").read_bytes()
    args.resume = True
    resumed = await replay(args)
    assert resumed == summary and (args.out / "results.jsonl").read_bytes() == original
    samples = [json.loads(line) for line in (args.out / "resources.jsonl").read_text().splitlines()]
    assert len(samples) == 2 and samples[1]["resumed"] is True
    assert all(row["client_cpu_ms"] >= 0 and row["client_peak_rss_bytes"] > 0 for row in samples)
    args.deadline_ms = 1900
    with pytest.raises(ValueError, match="changed"):
        await replay(args)


async def test_resume_rejects_changed_checkpoint(tmp_path):
    args = arguments(tmp_path)
    await replay(args)
    file = args.out / "results.jsonl"
    text = file.read_text().replace('"status":"ok"', '"status":"fallback"', 1)
    file.write_text(text)
    args.resume = True
    with pytest.raises(ValueError, match="hash changed"):
        await replay(args)


async def test_resume_rejects_changed_source_snapshot(tmp_path):
    args = arguments(tmp_path)
    await replay(args)
    source = args.out / "source" / "bench" / "knowledge_replay.py"
    assert source.is_file()
    source.write_text("modified snapshot")
    args.resume = True
    with pytest.raises(ValueError, match="source snapshot changed"):
        await replay(args)


async def test_budget_stops_before_dispatch(tmp_path, monkeypatch):
    from hippo_brain.bench import knowledge_replay

    args = arguments(tmp_path)
    args.arms = "retrieval,jev-single"

    class NoNetwork:
        async def aclose(self):
            pass

    monkeypatch.setattr(knowledge_replay.JevClient, "from_env", lambda: NoNetwork())
    summary = await replay(args)
    assert not summary["complete"] and summary["observed_rows"] == 1
    assert summary["stop_reason"] == "request_or_token_budget"


def test_archived_uuid_mapping_requires_hash_match(tmp_path):
    args = arguments(tmp_path)
    cases = json.loads(args.cases.read_text())
    targets = tmp_path / "targets.json"
    targets.write_text(
        json.dumps(
            [
                {
                    "qa_id": "q1",
                    "candidate_uuids": ["old-a", "old-b"],
                    "candidate_hash": digest(cases[0]),
                    "source_filter": "browser",
                }
            ]
        )
    )
    records = load_inputs(args.cases, targets=targets)
    assert [candidate["uuid"] for candidate in records[0]["candidates"]] == ["old-a", "old-b"]
    assert records[0]["filters"] == {"source": "browser"}
    cases[0]["query"] = "changed"
    args.cases.write_text(json.dumps(cases))
    with pytest.raises(ValueError, match="candidate hash"):
        load_inputs(args.cases, targets=targets)


def test_frozen_question_identity_is_distinct_from_runtime_state(tmp_path):
    args = arguments(tmp_path)
    questions = tmp_path / "questions.json"
    question = {"id": "q1", "question": "cargo test", "task_family_id": "task-a", "filters": {}}
    questions.write_text(json.dumps([question]))
    loaded = load_inputs(args.cases, questions=questions)
    assert loaded[0]["input_hash"] == digest(question)
    assert loaded[0]["input_hash"] != loaded[0]["source_case_hash"]


def test_cached_baseline_requires_identical_prompt_and_complete_order(tmp_path):
    args = arguments(tmp_path)
    cases = load_inputs(args.cases)
    results = [SearchResult(**candidate) for candidate in cases[0]["candidates"]]
    row = {
        "case_id": "q1",
        "task": "ranking",
        "repeat": 0,
        "status": "ok",
        "request": {"messages": build_rerank_messages("cargo test", results)},
        "canonical_order": [1, 0],
    }
    path = tmp_path / "local.jsonl"
    path.write_text(json.dumps(row) + "\n")
    assert _cached_local(path, cases)["q1"] == row
    row["canonical_order"] = [1, 1]
    path.write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="complete permutation"):
        _cached_local(path, cases)


async def test_repo_and_symlink_output_rejected(tmp_path):
    from pathlib import Path

    args = arguments(tmp_path)
    args.out = Path(__file__).parents[1] / "must-not-create"
    with pytest.raises(ValueError, match="repository"):
        await replay(args)
    args.out = tmp_path / "replay"
    args.out.mkdir()
    (args.out / "results.jsonl").symlink_to(args.cases)
    args.resume = True
    with pytest.raises(ValueError, match="symlinks"):
        await replay(args)


async def test_resume_accounts_for_interrupted_dispatch(tmp_path, monkeypatch):
    import asyncio
    from hippo_brain.bench import knowledge_replay

    args = arguments(tmp_path)
    args.arms = "local-optimized"
    args.local_url, args.local_model = "http://unused", "pinned"
    args.max_requests, args.max_tokens = 1, 200_000

    async def interrupted(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(knowledge_replay, "_decision", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await replay(args)
    assert (args.out / "inflight.json").exists()
    args.resume = True
    summary = await replay(args)
    assert not summary["complete"] and summary["network_calls"] == 1
    assert summary["accounted_tokens"] == 160_000
    rows = json.loads((args.out / "rankings.json").read_text())
    assert rows[0]["status"] == "cancelled" and rows[0]["usage"] is None
    assert rows[0]["elapsed_ms"] is None
    assert rows[0]["filter_violations"] is None
    assert rows[0]["malformed_accepted"] is None


@pytest.mark.parametrize("source,expected", [("shell", 0), ("browser", 2)])
async def test_replay_measures_filters_from_database_not_candidate_claims(
    tmp_path, tmp_db, source, expected
):
    from hippo_brain.bench.knowledge_replay import _decision

    conn, _ = tmp_db
    conn.execute(
        "INSERT INTO sessions(id,start_time,shell,hostname,username) VALUES (1,0,'zsh','host','user')"
    )
    conn.execute(
        """INSERT INTO events(id,session_id,timestamp,command,duration_ms,cwd,hostname,shell)
        VALUES (1,1,0,'cargo test',1,'/project','host','zsh')"""
    )
    for node_id, uid in enumerate(("a", "b"), 1):
        conn.execute(
            "INSERT INTO knowledge_nodes(id,uuid,content,embed_text) VALUES (?,?, '{}','')",
            (node_id, uid),
        )
        conn.execute("INSERT INTO knowledge_node_events VALUES (?,1)", (node_id,))
    case = load_inputs(arguments(tmp_path).cases)[0]
    case["filters"] = {"source": source}
    for candidate in case["candidates"]:
        candidate["linked_source_ids"] = ["browser-1"]
    for arm in ("retrieval", "rules"):
        row = await _decision(
            case,
            arm,
            client=None,
            conn=conn,
            http=None,
            config={"implementation_hashes": {}, "deadline_ms": 2000},
            cached={},
        )
        assert row["filter_violations"] == expected
        assert row["malformed_accepted"] == 0
    conn.execute("UPDATE events SET probe_tag='synthetic'")
    case["filters"] = {"source": "shell"}
    row = await _decision(
        case,
        "retrieval",
        client=None,
        conn=conn,
        http=None,
        config={"implementation_hashes": {}, "deadline_ms": 2000},
        cached={},
    )
    assert row["filter_violations"] == 2


async def test_resume_preserves_and_discards_only_incomplete_tail(tmp_path):
    args = arguments(tmp_path)
    await replay(args)
    with (args.out / "results.jsonl").open("ab") as stream:
        stream.write(b'{"unfinished":')
    args.resume = True
    assert (await replay(args))["complete"]
    assert next(args.out.glob("interrupted-tail-*.bin")).read_bytes() == b'{"unfinished":'


@pytest.mark.parametrize("max_requests,expected,complete", [(100, 12, True), (3, 3, False)])
async def test_concurrency_four_isolates_arms_and_reserves_whole_batch(
    tmp_path, monkeypatch, max_requests, expected, complete
):
    import asyncio
    from hippo_brain.bench import knowledge_replay

    args = arguments(tmp_path)
    cases = json.loads(args.cases.read_text())
    args.cases.write_text(json.dumps([{**cases[0], "id": str(i)} for i in range(6)]))
    args.concurrency = 4
    args.arms = "jev-single,local-optimized"
    args.local_url, args.local_model = "http://unused", "test"
    args.max_requests, args.max_tokens = max_requests, 2_000_000
    active, maximum = [], 0

    class Client:
        async def aclose(self):
            pass

    async def decision(case, arm, **kwargs):
        nonlocal maximum
        active.append(arm)
        maximum = max(maximum, len(active))
        assert len(set(active)) == 1
        # Every concurrent request must already have a persisted reservation.
        reserved = json.loads((args.out / "inflight.json").read_text())
        assert any(row["query_id"] == case["id"] and row["arm"] == arm for row in reserved)
        await asyncio.sleep(0.01)
        active.remove(arm)
        return {
            "query_id": case["id"],
            "arm": arm,
            "input_hash": case["input_hash"],
            "order": [candidate["uuid"] for candidate in case["candidates"]],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "cached": False,
            "network_calls": 1,
            "status": "ok",
            "elapsed_ms": 10,
            "model": "test",
            "recipe_hash": "test",
        }

    monkeypatch.setattr(knowledge_replay, "_decision", decision)
    monkeypatch.setattr(knowledge_replay.JevClient, "from_env", lambda: Client())
    summary = await replay(args)
    assert summary["complete"] is complete
    assert summary["observed_rows"] == expected and summary["network_calls"] == expected
    assert maximum == min(4, max_requests)
    rows = json.loads((args.out / "rankings.json").read_text())
    assert len({(row["query_id"], row["arm"]) for row in rows}) == expected
    assert not (args.out / "inflight.json").exists()
