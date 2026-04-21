"""Model-summary aggregation across all attempts for a single model."""

from __future__ import annotations

from hippo_brain.bench.output import AttemptRecord


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = int(round(pct * (len(s) - 1)))
    return s[k]


def aggregate_model_summary(
    attempts: list[AttemptRecord],
    self_consistency_mean: float,
    self_consistency_min: float,
) -> dict:
    total = len(attempts)
    if total == 0:
        return {
            "schema_validity_rate": 0.0,
            "refusal_rate": 0.0,
            "latency_p50_ms": 0,
            "latency_p95_ms": 0,
            "latency_p99_ms": 0,
            "self_consistency_mean": self_consistency_mean,
            "self_consistency_min": self_consistency_min,
            "entity_sanity_mean": 0.0,
        }

    valid = sum(1 for a in attempts if a.gates.get("schema_valid"))
    refusals = sum(1 for a in attempts if a.gates.get("refusal_detected"))
    latencies = [a.timestamps.get("total_ms", 0) for a in attempts]

    entity_rates: list[float] = []
    for a in attempts:
        per_cat = a.gates.get("entity_type_sanity", {})
        if isinstance(per_cat, dict) and per_cat:
            entity_rates.extend(per_cat.values())

    return {
        "schema_validity_rate": valid / total,
        "refusal_rate": refusals / total,
        "latency_p50_ms": int(_percentile(latencies, 0.50)),
        "latency_p95_ms": int(_percentile(latencies, 0.95)),
        "latency_p99_ms": int(_percentile(latencies, 0.99)),
        "self_consistency_mean": self_consistency_mean,
        "self_consistency_min": self_consistency_min,
        "entity_sanity_mean": sum(entity_rates) / len(entity_rates) if entity_rates else 1.0,
    }


def compute_verdict(gates: dict, thresholds: dict) -> dict:
    failed: list[str] = []
    if gates.get("schema_validity_rate", 0) < thresholds["schema_validity_min"]:
        failed.append("schema_validity_rate")
    if gates.get("refusal_rate", 1) > thresholds["refusal_max"]:
        failed.append("refusal_rate")
    if gates.get("latency_p95_ms", 0) > thresholds["latency_p95_max_ms"]:
        failed.append("latency_p95_ms")
    if gates.get("self_consistency_mean", 0) < thresholds["self_consistency_min"]:
        failed.append("self_consistency_mean")
    if gates.get("entity_sanity_mean", 0) < thresholds["entity_sanity_min"]:
        failed.append("entity_sanity_mean")
    return {"passed": not failed, "failed_gates": failed, "notes": []}
