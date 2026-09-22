"""Checks for source-recovery metrics and external experiment storage."""

from pathlib import Path
import json

import pytest

from hippo_brain.bench.decision_capture import decision_root
from hippo_brain.bench.knowledge_probe import golden_nodes, open_readonly, recovery


def test_readonly_source_and_recovery_keep_misses_in_denominator(tmp_path):
    import sqlite3

    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as conn:
        conn.executescript(
            "CREATE TABLE knowledge_nodes(id INTEGER PRIMARY KEY, uuid TEXT);"
            "CREATE TABLE knowledge_node_events(knowledge_node_id INTEGER, event_id INTEGER);"
            "INSERT INTO knowledge_nodes VALUES(1,'target');"
            "INSERT INTO knowledge_node_events VALUES(1,42);"
        )
    with open_readonly(source) as conn:
        assert golden_nodes(conn, "shell-42") == ["target"]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM knowledge_nodes")
    targets = [
        {"qa_id": "a", "candidate_uuids": ["other", "target"], "golden_nodes": ["target"]},
        {"qa_id": "b", "candidate_uuids": ["other"], "golden_nodes": ["absent"]},
    ]
    before = recovery(targets, {"a": [0, 1], "b": [0]})
    after = recovery(targets, {"a": [1, 0], "b": [0]})
    assert before["mrr"] == 0.25 and after["mrr"] == 0.5
    assert after["hit_at_1"] == after["hit_at_pool"] == 0.5
    assert recovery(targets, {})["mrr"] == 0


def test_artifacts_cannot_be_written_in_checkout(monkeypatch):
    checkout = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("XDG_DATA_HOME", str(checkout))
    with pytest.raises(ValueError, match="outside the repository"):
        decision_root()


def test_report_rejects_corruption_and_failed_comparator(tmp_path):
    from hippo_brain.bench import decision_sidecar as sidecar
    from hippo_brain.bench.knowledge_probe import report

    root, run = tmp_path / "corpus", tmp_path / "run"
    root.mkdir()
    run.mkdir()
    case = {"id": "a", "task": "ranking", "query": "answer", "candidates": ["other", "answer"]}
    normalized, _ = sidecar.normalize_case(case)
    targets = [{"qa_id": "a", "candidate_uuids": ["other", "answer"], "golden_nodes": ["answer"]}]
    config = {"llm_model": "local", "jev_model": "jev", "llm_url": "http://local/v1"}
    protocol = {
        **config,
        "label_semantics": "known-source",
        "gates": {"jev_p95_ms_max": 2000, "errors_max": 0},
    }
    for name, value in (("targets", targets), ("cases", [case]), ("protocol", protocol)):
        sidecar.write_json(root / (name + ".json"), value)
    sidecar.write_json(
        root / "freeze.json",
        {
            name + "_hash": sidecar.digest(value)
            for name, value in (("targets", targets), ("cases", [case]), ("protocol", protocol))
        },
    )
    sidecar.write_json(
        run / "manifest.json",
        {
            **config,
            "repeats": 1,
            "arms": ["llm", "jev"],
            "run_id": "test",
            "corpus_hash": sidecar.digest([normalized]),
        },
    )
    sidecar.write_json(run / "completion.json", {"exit_codes": {"llm": 0, "jev": 0}})
    requests = sidecar.requests_for(normalized, "local", "jev")
    rows = {}
    for arm, request in zip(("llm", "jev"), requests, strict=True):
        response = (
            {"choices": [{"message": {"content": "[2, 1]"}}]}
            if arm == "llm"
            else {
                "answers": {
                    f"candidate_{i}": {
                        "type": "score",
                        "score": i * 3,
                        "confidence": 1,
                        "probabilities": {str(j): int(j == i * 3) for j in range(4)},
                    }
                    for i in range(2)
                }
            }
        )
        row = {
            "arm": arm,
            "case_id": "a",
            "repeat": 0,
            "status": "ok",
            "backend": arm,
            "input_hash": normalized["input_hash"],
            "request": request,
            "request_hash": sidecar.digest(request),
            "response": response,
            "canonical_order": [1, 0],
            "elapsed_ms": 1,
        }
        rows[arm] = row
        (run / (arm + ".jsonl")).write_text(sidecar.canonical(row) + "\n")
    assert report(root, run)["screen_passed"]
    rows["llm"]["status"] = "error"
    (run / "llm.jsonl").write_text(sidecar.canonical(rows["llm"]) + "\n")
    assert not report(root, run)["screen_passed"]
    rows["jev"]["canonical_order"] = [0, 1]
    (run / "jev.jsonl").write_text(json.dumps(rows["jev"]) + "\n")
    with pytest.raises(ValueError, match="raw response"):
        report(root, run)
