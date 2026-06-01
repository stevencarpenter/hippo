"""Shared JSONL record builders for the bench results-store tests.

Both ``test_bench_results_store`` and ``test_bench_dashboard_export`` ingest the
same synthetic run; keeping the builders here gives them one source of truth
without a cross-test-module import (``brain/tests`` is a package, so a sibling
test module is not importable as a top-level name).
"""

import json


def _write_jsonl(path, records):
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True))
            f.write("\n")
    return path


def _manifest(run_id="run-1"):
    return {
        "record_type": "run_manifest",
        "run_id": run_id,
        "started_at_iso": "2026-05-31T00:00:00+00:00",
        "host": {"node": "test-host"},
        "preflight_checks": [],
        "candidate_models": ["model-a"],
        "bench_version": "0.2.0",
        "corpus_version": "corpus-v2",
        "corpus_content_hash": "sha256:abc",
        "corpus_schema_version": 18,
        "eval_qa_version": "eval-qa-v1",
        "embedding_model": "embed-x",
        "inference_backend_version": None,
        "gate_thresholds": {"schema_validity_min": 0.9},
        "host_baseline": {},
        "prod_state_at_start": {},
        "self_consistency_spec": {},
        "finished_at_iso": None,
    }


def _run_end(run_id="run-1"):
    return {
        "record_type": "run_end",
        "run_id": run_id,
        "finished_at_iso": "2026-05-31T01:00:00+00:00",
        "models_completed": ["model-a"],
        "models_errored": [],
        "reason": None,
    }


def _model_summary(run_id="run-1", model="model-a"):
    return {
        "record_type": "model_summary",
        "run_id": run_id,
        "model": {"id": model},
        "events_attempted": 2,
        "attempts_total": 2,
        "gates": {
            "schema_validity_rate": 1.0,
            "refusal_rate": 0.0,
            "echo_similarity_max": 0.1,
            "latency_p50_ms": 100,
            "latency_p95_ms": 200,
            "latency_p99_ms": 300,
            "self_consistency_mean": None,
            "self_consistency_min": None,
            "entity_sanity_mean": 0.9,
            "main_attempts_count": 2,
        },
        "system_peak": {},
        "tier0_verdict": {"passed": True, "failed_gates": [], "skipped_gates": [], "notes": []},
        "downstream_proxy": {},
        "errors": [],
    }


def _model_summary_with_proxy(run_id="run-1", model="model-a"):
    ms = _model_summary(run_id, model)
    ms["downstream_proxy"] = {
        "modes": {"hybrid": {"mrr": 1.0, "hit_at_1": 1.0}},
        "qa_count": 1,
        "k": 10,
        "per_item": [
            {
                "hit_at_k": {1: True, 3: True, 5: True, 10: True},
                "rank": 1,
                "mrr": 1.0,
                "ndcg_at_10": 1.0,
                "qa_id": "qa-001",
                "golden_event_id": "claude-7",
                "mode": "hybrid",
            },
            {
                "hit_at_k": {1: False, 3: False, 5: False, 10: False},
                "rank": None,
                "mrr": 0.0,
                "ndcg_at_10": 0.0,
                "qa_id": "qa-001",
                "golden_event_id": "claude-7",
                "mode": "lexical",
            },
        ],
    }
    return ms
