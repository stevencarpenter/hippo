"""Per-model passes: main (one attempt per event) + self-consistency."""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable

from hippo_brain.bench.corpus import CorpusEntry
from hippo_brain.bench.enrich_call import call_embedding, call_enrichment
from hippo_brain.bench.gates import (
    check_entity_sanity,
    check_refusal_pathology,
    check_schema_validity,
)
from hippo_brain.bench.output import AttemptRecord


def _event_dict(entry: CorpusEntry) -> dict:
    return {
        "event_id": entry.event_id,
        "source": entry.source,
        "content_hash": entry.content_sha256,
    }


def _build_attempt(
    run_id: str,
    model: dict,
    entry: CorpusEntry,
    attempt_idx: int,
    purpose: str,
    call_result,
    gates: dict,
    parsed: dict | None,
    system_snapshot: dict,
) -> AttemptRecord:
    start_iso = _dt.datetime.now(tz=_dt.UTC).isoformat()
    return AttemptRecord(
        run_id=run_id,
        model=model,
        event=_event_dict(entry),
        attempt_idx=attempt_idx,
        purpose=purpose,
        timestamps={
            "start_iso": start_iso,
            "ttft_ms": call_result.ttft_ms,
            "total_ms": call_result.total_ms,
        },
        raw_output=call_result.raw_output,
        parsed_output=parsed,
        gates=gates,
        system_snapshot=system_snapshot,
        timeout=call_result.timeout,
    )


def _compute_gates(call_result, entry: CorpusEntry) -> tuple[dict, dict | None]:
    # Treat any failure (timeout, HTTP error, parse error) the same way:
    # mark schema_invalid with the error class as the "schema error",
    # leave parsed=None, and let downstream rate computation count it.
    if call_result.timeout or call_result.error is not None:
        return (
            {
                "schema_valid": False,
                "schema_errors": [call_result.error or "timeout"],
                "refusal_detected": False,
                "refusal_patterns_matched": [],
                "echo_similarity": 0.0,
                "entity_type_sanity": {},
                "call_error": call_result.error,
            },
            None,
        )
    schema_result = check_schema_validity(call_result.raw_output, entry.source)
    refusal_result = check_refusal_pathology(
        raw_output=call_result.raw_output,
        input_text=entry.redacted_content,
        parsed=schema_result.parsed,
    )
    entity_sanity = (
        check_entity_sanity(schema_result.parsed, entry.source) if schema_result.parsed else None
    )
    return (
        {
            "schema_valid": schema_result.passed,
            "schema_errors": schema_result.errors,
            "refusal_detected": refusal_result.refusal_detected,
            "refusal_patterns_matched": refusal_result.refusal_patterns_matched,
            "trivial_summary": refusal_result.trivial_summary,
            "echo_similarity": refusal_result.echo_similarity,
            "entity_type_sanity": (
                entity_sanity.per_category_rates if entity_sanity is not None else {}
            ),
        },
        schema_result.parsed,
    )


def run_model_main_pass(
    *,
    base_url: str,
    model: str,
    entries: list[CorpusEntry],
    timeout_sec: int,
    metrics_snapshot: Callable[[], dict],
    temperature: float,
    run_id: str = "run-local",
) -> list[AttemptRecord]:
    """Single attempt per event. Headline metrics are computed over this pass."""
    model_dict = {"id": model}
    attempts: list[AttemptRecord] = []
    for entry in entries:
        cr = call_enrichment(
            base_url=base_url,
            model=model,
            payload=entry.redacted_content,
            source=entry.source,
            timeout_sec=timeout_sec,
            temperature=temperature,
        )
        gates, parsed = _compute_gates(cr, entry)
        attempts.append(
            _build_attempt(
                run_id=run_id,
                model=model_dict,
                entry=entry,
                attempt_idx=0,
                purpose="main",
                call_result=cr,
                gates=gates,
                parsed=parsed,
                system_snapshot=metrics_snapshot(),
            )
        )
    return attempts


def run_self_consistency_pass(
    *,
    base_url: str,
    model: str,
    entries: list[CorpusEntry],
    runs_per_event: int,
    embedding_model: str,
    timeout_sec: int,
    metrics_snapshot: Callable[[], dict],
    temperature: float,
    run_id: str = "run-local",
) -> tuple[list[AttemptRecord], list[list[list[float]]]]:
    """N attempts per event; embed each successful output for cosine aggregation.

    Self-consistency only makes sense at temperature > 0 — at T=0 the model
    is near-deterministic and every output is identical. Caller is
    responsible for passing a meaningful temperature.
    """
    model_dict = {"id": model}
    attempts: list[AttemptRecord] = []
    per_event_vectors: list[list[list[float]]] = []
    for entry in entries:
        event_vectors: list[list[float]] = []
        for i in range(runs_per_event):
            cr = call_enrichment(
                base_url=base_url,
                model=model,
                payload=entry.redacted_content,
                source=entry.source,
                timeout_sec=timeout_sec,
                temperature=temperature,
            )
            gates, parsed = _compute_gates(cr, entry)
            attempts.append(
                _build_attempt(
                    run_id=run_id,
                    model=model_dict,
                    entry=entry,
                    attempt_idx=i,
                    purpose="self_consistency",
                    call_result=cr,
                    gates=gates,
                    parsed=parsed,
                    system_snapshot=metrics_snapshot(),
                )
            )
            if cr.error is None and cr.raw_output:
                try:
                    vec = call_embedding(
                        base_url=base_url,
                        model=embedding_model,
                        text=cr.raw_output,
                        timeout_sec=timeout_sec,
                    )
                    event_vectors.append(vec)
                except Exception:  # noqa: BLE001 — embedding failures are informational
                    pass
        per_event_vectors.append(event_vectors)
    return attempts, per_event_vectors
