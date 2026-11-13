"""The durable decision log: one content-free line per rerank decision."""

import json
import stat

import pytest

from hippo_brain import retrieval
from hippo_brain.decision_capture import capture_decision

TRACE = {
    "backend": "jev",
    "recipe": "multi-v1",
    "stop_reason": "single_pass",
    "candidates": 3,
    "limit": 2,
    "effect": "set_and_top_changed",
    "best_relevance": 1.0,
    "elapsed_ms": 512.4,
    "deadline_ms": 2000,
    "input_hash": "f" * 64,
    "original_order": ["a", "b", "c"],
    "final_order": ["b", "c", "a"],
    "scores": [0.0, 1.0, 0.66667],
    "passes": [
        {
            "transport": {"http_ms": 440.0, "status_code": 200, "payload_bytes": 9000},
            "usage": {"input_tokens": 2200, "output_tokens": 30},
            "judgments": {"candidate_0": {"score": 0, "note": "PRIVATE TEXT"}},
        }
    ],
}


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("HIPPO_DECISION_CAPTURE", raising=False)
    yield tmp_path / "data/hippo-bench/decisions/decision-log"
    retrieval.configure({})


def test_enabled_log_appends_one_content_free_line_per_decision(log_dir):
    retrieval.configure({"decision_log": True})
    capture_decision(TRACE, origin="mcp")
    capture_decision(TRACE, origin="mcp")

    (path,) = log_dir.glob("*.jsonl")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2
    row = rows[0]
    assert (row["origin"], row["outcome"], row["effect"]) == (
        "mcp",
        "success",
        "set_and_top_changed",
    )
    assert row["original_order"] == ["a", "b", "c"] and row["final_order"] == [1, 2, 0]
    assert row["scores"] == [0.0, 1.0, 0.667] and row["input_tokens"] == 2200
    assert "PRIVATE" not in path.read_text()


def test_log_is_off_by_default(log_dir):
    capture_decision(TRACE, origin="mcp")
    assert not log_dir.exists()


@pytest.mark.parametrize("origin", ["probe", "benchmark"])
def test_synthetic_origins_are_never_logged(log_dir, origin):
    retrieval.configure({"decision_log": True})
    capture_decision(TRACE, origin=origin)
    assert not log_dir.exists()


def test_a_log_failure_cannot_fail_a_query(log_dir, monkeypatch):
    retrieval.configure({"decision_log": True})
    log_dir.parent.mkdir(parents=True)
    log_dir.write_text("a file where the directory should be")
    capture_decision(TRACE, origin="mcp")
