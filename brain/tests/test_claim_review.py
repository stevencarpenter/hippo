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
        row = annotate(queue, member["packet_hash"], reviewer="human-test", decision=decision)
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
        annotate(queue, "unknown", reviewer="human-test", decision="yes")
    labels = tmp_path / "labels"
    labels.mkdir()
    row = annotate(queue, queue["rows"][0]["packet_hash"], reviewer="model", decision="yes")
    row["annotator_type"] = "model"
    write_artifact(labels / "row.json", row)
    with pytest.raises(ValueError, match="human annotation"):
        report(corpus, run, tmp_path / "q.json", labels)
