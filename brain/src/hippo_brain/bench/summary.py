"""Model-summary aggregation across all attempts for a single model.

Rate-based gates (schema validity, refusal, entity sanity) and latency
percentiles are computed over the *main* pass only. The self-consistency
pass re-runs the same N events N times to measure output stability and
its attempts would skew per-event rates if mixed in.

Self-consistency mean/min are passed in separately. `None` is a first-class
"not tested" signal — downstream `compute_verdict` skips the SC check when
the value is None, instead of treating it as a failure.
"""

from __future__ import annotations

from hippo_brain.bench.output import AttemptRecord


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = int(round(pct * (len(s) - 1)))
    return s[k]


def _entity_sanity_attempt_mean(per_cat: dict) -> float | None:
    """Mean of per-category rates for a single attempt, or None if no categories present."""
    if not isinstance(per_cat, dict) or not per_cat:
        return None
    return sum(per_cat.values()) / len(per_cat)


def aggregate_model_summary(
    attempts: list[AttemptRecord],
    self_consistency_mean: float | None,
    self_consistency_min: float | None,
) -> dict:
    """Compute headline model gates.

    Validity / refusal / latency / entity-sanity rates are computed over
    the main pass only (`purpose == "main"`). The self-consistency pass
    still appears in the JSONL attempt stream but does not distort the
    per-event rates.

    Self-consistency values are passed in from the caller (computed from
    embedding cosines). `None` propagates through as "not tested"; we
    emit `None` in the returned dict rather than a misleading 0.0.
    """
    main_attempts = [a for a in attempts if a.purpose == "main"]
    total_main = len(main_attempts)

    if total_main == 0:
        return {
            "schema_validity_rate": 0.0,
            "refusal_rate": 0.0,
            "echo_similarity_max": 0.0,
            "latency_p50_ms": 0,
            "latency_p95_ms": 0,
            "latency_p99_ms": 0,
            "self_consistency_mean": self_consistency_mean,
            "self_consistency_min": self_consistency_min,
            "entity_sanity_mean": 1.0,
            "main_attempts_count": 0,
            "sc_attempts_count": len(attempts) - total_main,
        }

    valid = sum(1 for a in main_attempts if a.gates.get("schema_valid"))
    refusals = sum(1 for a in main_attempts if a.gates.get("refusal_detected"))
    echo_similarity_max = max(float(a.gates.get("echo_similarity", 0.0)) for a in main_attempts)
    latencies = [a.timestamps.get("total_ms", 0) for a in main_attempts]

    # Per-attempt mean first (avoids over-weighting attempts with more
    # populated categories); then mean across attempts that had any
    # entities at all. Attempts with no categories populated are vacuous
    # passes and excluded from the denominator.
    per_attempt_means: list[float] = []
    for a in main_attempts:
        m = _entity_sanity_attempt_mean(a.gates.get("entity_type_sanity"))
        if m is not None:
            per_attempt_means.append(m)

    return {
        "schema_validity_rate": valid / total_main,
        "refusal_rate": refusals / total_main,
        "echo_similarity_max": echo_similarity_max,
        "latency_p50_ms": int(_percentile(latencies, 0.50)),
        "latency_p95_ms": int(_percentile(latencies, 0.95)),
        "latency_p99_ms": int(_percentile(latencies, 0.99)),
        "self_consistency_mean": self_consistency_mean,
        "self_consistency_min": self_consistency_min,
        "entity_sanity_mean": (
            sum(per_attempt_means) / len(per_attempt_means) if per_attempt_means else 1.0
        ),
        "main_attempts_count": total_main,
        "sc_attempts_count": len(attempts) - total_main,
    }


def compute_verdict(gates: dict, thresholds: dict) -> dict:
    """Derive pass/fail from gate values + thresholds.

    A gate reported as `None` is "not tested" and does NOT fail the
    verdict. Only concretely-below-threshold numbers count as failures.
    """
    failed: list[str] = []
    skipped: list[str] = []

    def _check_min(gate_key: str, threshold_key: str) -> None:
        v = gates.get(gate_key)
        if v is None:
            skipped.append(gate_key)
            return
        if v < thresholds[threshold_key]:
            failed.append(gate_key)

    def _check_max(gate_key: str, threshold_key: str) -> None:
        v = gates.get(gate_key)
        if v is None:
            skipped.append(gate_key)
            return
        if v > thresholds[threshold_key]:
            failed.append(gate_key)

    _check_min("schema_validity_rate", "schema_validity_min")
    _check_max("refusal_rate", "refusal_max")
    _check_max("echo_similarity_max", "echo_similarity_max")
    _check_max("latency_p95_ms", "latency_p95_max_ms")
    _check_min("self_consistency_mean", "self_consistency_min")
    _check_min("entity_sanity_mean", "entity_sanity_min")

    return {"passed": not failed, "failed_gates": failed, "skipped_gates": skipped, "notes": []}
