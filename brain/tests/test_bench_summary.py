from hippo_brain.bench.output import AttemptRecord
from hippo_brain.bench.summary import aggregate_model_summary, compute_verdict


def _attempt(
    schema_valid=True,
    refusal=False,
    echo_similarity=0.1,
    total_ms=1000,
    purpose="main",
    event_id="e1",
    entity_cats=None,
):
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
            "echo_similarity": echo_similarity,
            "entity_type_sanity": (
                entity_cats if entity_cats is not None else {"files": 1.0, "tools": 1.0}
            ),
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
    assert gates["echo_similarity_max"] == 0.1
    assert gates["self_consistency_mean"] == 0.85
    assert gates["main_attempts_count"] == 3
    assert gates["sc_attempts_count"] == 0


def test_aggregate_latency_percentiles():
    attempts = [_attempt(total_ms=v) for v in [100, 200, 300, 400, 500, 10_000]]
    gates = aggregate_model_summary(
        attempts=attempts,
        self_consistency_mean=0.9,
        self_consistency_min=0.8,
    )
    # p95 on 6 samples: round(0.95 * 5) = 5 -> index 5 = 10_000
    assert gates["latency_p95_ms"] == 10_000


def test_aggregate_excludes_sc_attempts_from_rates():
    """Self-consistency attempts must not distort main-pass rates."""
    attempts = [
        _attempt(schema_valid=True, purpose="main", event_id="e1"),
        _attempt(schema_valid=True, purpose="main", event_id="e2", echo_similarity=0.2),
        # SC attempts on the same event — all fail schema. Must NOT pull down rate.
        _attempt(
            schema_valid=False, purpose="self_consistency", event_id="e1", echo_similarity=0.99
        ),
        _attempt(
            schema_valid=False, purpose="self_consistency", event_id="e1", echo_similarity=0.99
        ),
        _attempt(
            schema_valid=False, purpose="self_consistency", event_id="e1", echo_similarity=0.99
        ),
    ]
    gates = aggregate_model_summary(
        attempts=attempts,
        self_consistency_mean=0.9,
        self_consistency_min=0.8,
    )
    assert gates["schema_validity_rate"] == 1.0  # main pass clean
    assert gates["echo_similarity_max"] == 0.2
    assert gates["main_attempts_count"] == 2
    assert gates["sc_attempts_count"] == 3


def test_aggregate_entity_sanity_per_attempt_mean():
    """Entity sanity means per-attempt first, then mean across attempts.

    Attempt 1 has 3 populated categories averaging 0.9; attempt 2 has 1
    populated category at 0.6. Correct headline is (0.9 + 0.6) / 2 = 0.75,
    NOT (0.8 + 1.0 + 0.9 + 0.6) / 4 = 0.825 (over-weighting attempt 1).
    """
    attempts = [
        _attempt(entity_cats={"files": 0.8, "tools": 1.0, "projects": 0.9}, event_id="e1"),
        _attempt(entity_cats={"files": 0.6}, event_id="e2"),
    ]
    gates = aggregate_model_summary(
        attempts=attempts, self_consistency_mean=None, self_consistency_min=None
    )
    assert gates["entity_sanity_mean"] == 0.75


def test_aggregate_self_consistency_none_propagates():
    """None self-consistency means 'not tested', not 'zero'."""
    attempts = [_attempt()]
    gates = aggregate_model_summary(
        attempts=attempts, self_consistency_mean=None, self_consistency_min=None
    )
    assert gates["self_consistency_mean"] is None
    assert gates["self_consistency_min"] is None


def test_aggregate_empty_main_pass():
    """All SC attempts, no main — returns 0-ish but still marks sc count."""
    attempts = [_attempt(purpose="self_consistency")] * 3
    gates = aggregate_model_summary(
        attempts=attempts, self_consistency_mean=0.9, self_consistency_min=0.85
    )
    assert gates["main_attempts_count"] == 0
    assert gates["sc_attempts_count"] == 3
    assert gates["schema_validity_rate"] == 0.0


def test_verdict_pass_when_all_gates_pass():
    thresholds = {
        "schema_validity_min": 0.95,
        "refusal_max": 0.0,
        "echo_similarity_max": 0.5,
        "latency_p95_max_ms": 60_000,
        "self_consistency_min": 0.7,
        "entity_sanity_min": 0.9,
    }
    gates = {
        "schema_validity_rate": 1.0,
        "refusal_rate": 0.0,
        "echo_similarity_max": 0.1,
        "latency_p95_ms": 30_000,
        "self_consistency_mean": 0.9,
        "entity_sanity_mean": 0.95,
    }
    v = compute_verdict(gates, thresholds)
    assert v["passed"] is True
    assert v["failed_gates"] == []
    assert v["skipped_gates"] == []


def test_verdict_fail_lists_offending_gates():
    thresholds = {
        "schema_validity_min": 0.95,
        "refusal_max": 0.0,
        "echo_similarity_max": 0.5,
        "latency_p95_max_ms": 60_000,
        "self_consistency_min": 0.7,
        "entity_sanity_min": 0.9,
    }
    gates = {
        "schema_validity_rate": 0.90,
        "refusal_rate": 0.1,
        "echo_similarity_max": 0.75,
        "latency_p95_ms": 30_000,
        "self_consistency_mean": 0.8,
        "entity_sanity_mean": 0.95,
    }
    v = compute_verdict(gates, thresholds)
    assert v["passed"] is False
    assert "schema_validity_rate" in v["failed_gates"]
    assert "refusal_rate" in v["failed_gates"]
    assert "echo_similarity_max" in v["failed_gates"]


def test_verdict_none_gate_is_skipped_not_failed():
    """A missing (None) gate should not fail the verdict — just skip it."""
    thresholds = {
        "schema_validity_min": 0.95,
        "refusal_max": 0.0,
        "echo_similarity_max": 0.5,
        "latency_p95_max_ms": 60_000,
        "self_consistency_min": 0.7,
        "entity_sanity_min": 0.9,
    }
    gates = {
        "schema_validity_rate": 1.0,
        "refusal_rate": 0.0,
        "echo_similarity_max": 0.1,
        "latency_p95_ms": 30_000,
        "self_consistency_mean": None,  # not tested
        "entity_sanity_mean": 0.95,
    }
    v = compute_verdict(gates, thresholds)
    assert v["passed"] is True
    assert v["failed_gates"] == []
    assert "self_consistency_mean" in v["skipped_gates"]
