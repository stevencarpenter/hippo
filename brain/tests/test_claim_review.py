"""Shadow decisions never become human labels or production approvals."""

import json

import httpx
import pytest

from hippo_brain.bench.claim_packets import load_packets, prepare, write_artifact
from hippo_brain.bench.claim_review import (
    annotate,
    assess,
    disposition,
    load_run,
    make_queue,
    report,
    review,
)
from hippo_brain.jev import JevClient
from tests.test_claim_packets import database


def cohort(tmp_path, count=4):
    source = tmp_path / "source.sqlite"
    with database(source) as conn:
        for i in range(2, count + 1):
            conn.execute(
                "INSERT INTO knowledge_nodes(id,uuid,content,embed_text,created_at) VALUES(?,?,?,'',?)",
                (i, f"node-{i}", json.dumps({"summary": f"Claim {i}"}), i),
            )
            conn.execute("INSERT INTO knowledge_node_events VALUES(?,1)", (i,))
    corpus = tmp_path / "packets"
    prepare(source, corpus)
    return corpus


def response(choice="supports", confidence=0.99):
    return {
        "model": "jev-1.13.0",
        "answers": {
            "verdict": {
                "type": "choice",
                "choice": choice,
                "confidence": confidence,
                "probabilities": {
                    v: int(v == choice) for v in ("supports", "contradicts", "unsupported")
                },
            }
        },
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }


async def evaluated(tmp_path):
    corpus = cohort(tmp_path)
    run = tmp_path / "run"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response()))
    ) as http:
        async with JevClient("synthetic-key", client=http) as client:
            await assess(corpus, run, max_requests=4, client=client)
    return corpus, run


async def test_bounded_assessment_uses_pinned_redacted_state_without_labels(tmp_path):
    corpus = cohort(tmp_path)
    calls = []

    def handle(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["model"] == "jev-1.13.0"
        assert set(payload["state"]) == {"claim", "sources"}
        assert "expected" not in payload and "reviewer" not in payload
        return httpx.Response(200, json=response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        async with JevClient("synthetic-key", client=http) as client:
            completion = await assess(corpus, tmp_path / "run", max_requests=2, client=client)
    assert len(calls) == 2
    assert completion["statuses"] == {"ok": 2, "budget_exhausted": 2}
    _, _, records = load_run(corpus, tmp_path / "run")
    assert list(disposition(r, 0.9) for r in records.values()).count("approve") == 2


async def test_incomplete_sources_do_not_dispatch_and_invalid_responses_fail_closed(tmp_path):
    source = tmp_path / "source.sqlite"
    with database(source) as conn:
        conn.execute("UPDATE events SET stdout=?", ("x" * 6001,))
    corpus = tmp_path / "packets"
    prepare(source, corpus)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"answers": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        async with JevClient("synthetic-key", client=http) as client:
            assert (await assess(corpus, tmp_path / "blocked", max_requests=1, client=client))[
                "statuses"
            ] == {"blocked": 1}
            assert not calls
            with database(tmp_path / "valid.sqlite") as conn:
                pass
            prepare(tmp_path / "valid.sqlite", tmp_path / "valid")
            assert (
                await assess(
                    tmp_path / "valid", tmp_path / "invalid", max_requests=1, client=client
                )
            )["statuses"] == {"error": 1}
    _, _, records = load_run(tmp_path / "valid", tmp_path / "invalid")
    assert all(disposition(r, 0) == "review" for r in records.values())


async def test_random_audit_includes_automatic_cases_and_is_reproducible(tmp_path):
    corpus, run = await evaluated(tmp_path)
    one = make_queue(
        corpus, run, tmp_path / "q1.json", threshold=0.9, limit=3, audit=2, seed="frozen"
    )
    two = make_queue(
        corpus, run, tmp_path / "q2.json", threshold=0.9, limit=3, audit=2, seed="frozen"
    )
    assert one == two
    assert len(one["rows"]) == 2
    assert all(r["stratum"] == "audit" for r in one["rows"])


async def test_report_retains_false_approvals_unsure_and_unreviewed(tmp_path):
    corpus, run = await evaluated(tmp_path)
    path = tmp_path / "q.json"
    queue = make_queue(corpus, run, path, threshold=0.9, limit=4, audit=4, seed="frozen")
    labels = tmp_path / "labels"
    labels.mkdir()
    for member, decision in zip(queue["rows"], ("yes", "no", "unsure")):
        row = annotate(
            queue,
            next(p for p in load_packets(corpus) if p["packet_hash"] == member["packet_hash"]),
            reviewer="human-test",
            decision=decision,
        )
        write_artifact(labels / f"{member['packet_hash']}.json", row)
    result = report(corpus, run, path, labels)
    assert result["automation_coverage"] == 1
    audit = result["review"]["audit"]
    assert audit["reviewed_approvals"] == 2 and audit["false_approvals"] == 1
    assert audit["unsure"] == 1 and audit["unreviewed"] == 1
    assert result["acceptance_qualified"] is False
    assert result["reported_input_tokens"] == 400
    first = next(labels.glob("*.json"))
    write_artifact(labels / "duplicate.json", json.loads(first.read_text()))
    with pytest.raises(ValueError, match="duplicate"):
        report(corpus, run, path, labels)


async def test_review_hides_predictions_and_saves_notes_before_quit(tmp_path, monkeypatch, capsys):
    corpus, run = await evaluated(tmp_path)
    path = tmp_path / "q.json"
    make_queue(corpus, run, path, threshold=0.9, limit=3, audit=3, seed="frozen")
    answers = iter(["n", "The source only proposed the change.", "q"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    labels = tmp_path / "labels"
    assert review(corpus, run, path, labels, reviewer="human-test") == 1
    output = capsys.readouterr().out
    assert "confidence" not in output and "0.99" not in output and '"supports"' not in output
    row = json.loads(next(labels.glob("*.json")).read_text())
    assert row["notes"] == "The source only proposed the change."
    assert row["decision"] == "no"
    assert next(labels.glob("*.json")).stat().st_mode & 0o777 == 0o600
    answers = iter(["y", "", "q"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert review(corpus, run, path, labels, reviewer="human-test") == 1
    assert len(list(labels.glob("*.json"))) == 2


async def test_revisions_and_missing_records_cannot_reuse_approval(tmp_path):
    corpus, run = await evaluated(tmp_path)
    key = load_packets(corpus)[0]["packet_hash"]
    (run / f"{key}.json").unlink()
    _, _, records = load_run(corpus, run)
    assert disposition(records[key], 0) == "review"
    with database(tmp_path / "different.sqlite") as conn:
        conn.execute("UPDATE events SET stdout='Correction: FAIL'")
    prepare(tmp_path / "different.sqlite", tmp_path / "changed")
    with pytest.raises(ValueError, match="identity mismatch"):
        load_run(tmp_path / "changed", run)


async def test_unknown_model_labels_and_nonfinite_policy_are_rejected(tmp_path):
    corpus, run = await evaluated(tmp_path)
    with pytest.raises(ValueError, match="numeric"):
        make_queue(corpus, run, tmp_path / "bad.json", threshold=float("nan"), seed="frozen")
    queue = make_queue(
        corpus, run, tmp_path / "q.json", threshold=0.9, audit=1, limit=1, seed="frozen"
    )
    with pytest.raises(ValueError, match="outside"):
        annotate(queue, {"packet_hash": "unknown"}, reviewer="human-test", decision="yes")
    labels = tmp_path / "labels"
    labels.mkdir()
    row = annotate(
        queue,
        next(
            p for p in load_packets(corpus) if p["packet_hash"] == queue["rows"][0]["packet_hash"]
        ),
        reviewer="model",
        decision="yes",
    )
    row["annotator_type"] = "model"
    write_artifact(labels / "row.json", row)
    with pytest.raises(ValueError, match="human annotation"):
        report(corpus, run, tmp_path / "q.json", labels)


@pytest.mark.parametrize("summary", ["", "The test passed.".ljust(4000) + " Production shipped."])
async def test_incomplete_claim_cannot_receive_or_replay_yes(
    tmp_path, monkeypatch, capsys, summary
):
    source = tmp_path / "source.sqlite"
    with database(source) as conn:
        conn.execute("UPDATE knowledge_nodes SET content=?", (json.dumps({"summary": summary}),))
    corpus, run, queue_path, labels = [
        tmp_path / name for name in ("corpus", "run", "queue.json", "labels")
    ]
    prepare(source, corpus)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("blocked request"))
    ) as http:
        async with JevClient("synthetic", client=http) as client:
            await assess(corpus, run, max_requests=1, client=client)
    queue = make_queue(corpus, run, queue_path, threshold=0.9, limit=1, audit=1, seed="fixed")
    with pytest.raises(ValueError, match="complete summary"):
        annotate(queue, load_packets(corpus)[0], reviewer="human-test", decision="yes")
    answers = iter(["y", "u", "Need the complete summary"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert review(corpus, run, queue_path, labels, reviewer="human-test") == 1
    saved = next(labels.glob("*.json"))
    row = json.loads(saved.read_text())
    assert row["decision"] == "unsure"
    assert row["notes"] == "Need the complete summary"
    assert "yes requires a complete summary" in capsys.readouterr().out
    assert report(corpus, run, queue_path, labels)["review"]["audit"]["yes"] == 0
    row["decision"] = "yes"
    saved.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="complete summary"):
        report(corpus, run, queue_path, labels)


async def test_presentation_order_does_not_group_audits_before_exceptions(tmp_path):
    corpus, run = await evaluated(tmp_path)
    orders = []
    for seed in ("one", "two", "three", "four"):
        queue = make_queue(
            corpus, run, tmp_path / f"{seed}.json", threshold=1, limit=4, audit=1, seed=seed
        )
        repeated = make_queue(
            corpus, run, tmp_path / f"{seed}-repeat.json", threshold=1, limit=4, audit=1, seed=seed
        )
        assert queue == repeated
        assert sum(row["stratum"] == "audit" for row in queue["rows"]) == 1
        orders.append([row["stratum"] for row in queue["rows"]])
    assert any(order[0] == "exception" for order in orders)


async def test_missing_credentials_leave_output_available(tmp_path, monkeypatch):
    from hippo_brain.jev import JevUnavailable

    corpus, run = cohort(tmp_path), tmp_path / "run"
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(JevUnavailable):
        await assess(corpus, run, max_requests=1)
    assert not run.exists()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response()))
    ) as http:
        async with JevClient("synthetic", client=http) as client:
            result = await assess(corpus, run, max_requests=1, client=client)
    assert result["attempted"] == 1


@pytest.mark.parametrize("flag", ["stdout_truncated", "stderr_truncated"])
async def test_known_capture_truncation_never_dispatches(tmp_path, flag):
    source, corpus = tmp_path / "source.sqlite", tmp_path / "corpus"
    with database(source) as conn:
        conn.execute(f"ALTER TABLE events ADD COLUMN {flag} INTEGER DEFAULT 1")
    prepare(source, corpus)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("incomplete capture dispatched"))
    ) as http:
        async with JevClient("synthetic", client=http) as client:
            result = await assess(corpus, tmp_path / "run", max_requests=1, client=client)
    assert result["statuses"] == {"blocked": 1}


async def test_failed_publication_preserves_assessments_and_annotations(tmp_path, monkeypatch):
    import errno
    import os
    from hippo_brain.bench.claim_review import load_annotations

    corpus, run = cohort(tmp_path), tmp_path / "run"
    sync = os.fsync
    writes = 0

    def fail_second_result(fd):
        nonlocal writes
        writes += 1
        if writes == 3:
            raise OSError(errno.ENOSPC, "synthetic disk full")
        sync(fd)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response()))
    ) as http:
        async with JevClient("synthetic", client=http) as client:
            with monkeypatch.context() as patch:
                patch.setattr(os, "fsync", fail_second_result)
                with pytest.raises(OSError):
                    await assess(corpus, run, max_requests=4, client=client)
    packets, _, records = load_run(corpus, run)
    assert sum(r["status"] == "ok" for r in records.values()) == 1
    assert sum(r["status"] == "missing" for r in records.values()) == 3
    indexed = {p["packet_hash"]: p for p in packets}
    queue_path, labels = tmp_path / "queue.json", tmp_path / "labels"
    queue = make_queue(corpus, run, queue_path, threshold=0.9, limit=4, audit=4, seed="fixed")
    labels.mkdir(mode=0o700)
    first, second = [indexed[row["packet_hash"]] for row in queue["rows"][:2]]
    write_artifact(labels / "first.json", annotate(queue, first, reviewer="human", decision="no"))

    def fail_publication(fd):
        raise OSError(errno.ENOSPC, "synthetic disk full")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_publication)
        with pytest.raises(OSError):
            write_artifact(
                labels / "second.json", annotate(queue, second, reviewer="human", decision="no")
            )
    assert len(load_annotations(queue, labels, indexed)) == 1
    assert report(corpus, run, queue_path, labels)["review"]["audit"]["unreviewed"] == 3
    write_artifact(labels / "second.json", annotate(queue, second, reviewer="human", decision="no"))
    assert len(load_annotations(queue, labels, indexed)) == 2


def test_cli_setup_error_is_concise_and_does_not_reserve_output(tmp_path, monkeypatch, capsys):
    import sys
    from hippo_brain.bench.claim_review import main

    corpus, run = cohort(tmp_path), tmp_path / "run"
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "claim-review",
            "assess",
            "--corpus",
            str(corpus),
            "--run",
            str(run),
            "--max-requests",
            "1",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert capsys.readouterr().err == "claim-review: TYPESAFE_API_KEY is required\n"
    assert not run.exists()


async def test_owned_client_is_closed_when_output_is_rejected(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    corpus, run = cohort(tmp_path), tmp_path / "run"
    run.mkdir()
    client = AsyncMock(spec=JevClient)
    monkeypatch.setattr(JevClient, "from_env", lambda: client)
    with pytest.raises(FileExistsError):
        await assess(corpus, run, max_requests=1)
    client.aclose.assert_awaited_once()
    client.assess.assert_not_awaited()
