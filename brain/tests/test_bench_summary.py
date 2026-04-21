from hippo_brain.bench.output import AttemptRecord
from hippo_brain.bench.summary import aggregate_model_summary, compute_verdict


def _attempt(schema_valid=True, refusal=False, total_ms=1000, purpose="main", event_id="e1"):
    return AttemptRecord(
        run_id="r",
        model={"id": "m"},
        event={"event_id": event_id, "source": "shell", "content_hash": "h"},
        attempt_idx=0,
        purpose=purpose,
        timestamps={"total_ms": total_ms},
        raw_output="",
        parsed_output=None,
        gates={
            "schema_valid": schema_valid,
            "refusal_detected": refusal,
            "echo_similarity": 0.1,
            "entity_type_sanity": {"files": 1.0, "tools": 1.0},
        },
        system_snapshot={},
    )


def test_aggregate_schema_validity_rate():
    attempts = [
        _attempt(schema_valid=True),
        _attempt(schema_valid=True),
        _attempt(schema_valid=False, event_id="e2"),
    ]
    gates = aggregate_model_summary(
        attempts=attempts,
        self_consistency_mean=0.85,
        self_consistency_min=0.8,
    )
    assert gates["schema_validity_rate"] == 2 / 3
    assert gates["self_consistency_mean"] == 0.85


def test_aggregate_latency_percentiles():
    attempts = [_attempt(total_ms=v) for v in [100, 200, 300, 400, 500, 10_000]]
    gates = aggregate_model_summary(
        attempts=attempts,
        self_consistency_mean=0.9,
        self_consistency_min=0.8,
    )
    # p95 on 6 samples: round(0.95 * 5) = 5 -> index 5 = 10_000
    assert gates["latency_p95_ms"] == 10_000


def test_verdict_pass_when_all_gates_pass():
    thresholds = {
        "schema_validity_min": 0.95,
        "refusal_max": 0.0,
        "latency_p95_max_ms": 60_000,
        "self_consistency_min": 0.7,
        "entity_sanity_min": 0.9,
    }
    gates = {
        "schema_validity_rate": 1.0,
        "refusal_rate": 0.0,
        "latency_p95_ms": 30_000,
        "self_consistency_mean": 0.9,
        "entity_sanity_mean": 0.95,
    }
    v = compute_verdict(gates, thresholds)
    assert v["passed"] is True
    assert v["failed_gates"] == []


def test_verdict_fail_lists_offending_gates():
    thresholds = {
        "schema_validity_min": 0.95,
        "refusal_max": 0.0,
        "latency_p95_max_ms": 60_000,
        "self_consistency_min": 0.7,
        "entity_sanity_min": 0.9,
    }
    gates = {
        "schema_validity_rate": 0.90,
        "refusal_rate": 0.1,
        "latency_p95_ms": 30_000,
        "self_consistency_mean": 0.8,
        "entity_sanity_mean": 0.95,
    }
    v = compute_verdict(gates, thresholds)
    assert v["passed"] is False
    assert "schema_validity_rate" in v["failed_gates"]
    assert "refusal_rate" in v["failed_gates"]
