import json

from hippo_brain.bench.pretty import render_summary_text


def test_render_summary_handles_manifest_only(tmp_path):
    f = tmp_path / "run.jsonl"
    f.write_text(
        json.dumps({"record_type": "run_manifest", "run_id": "r", "candidate_models": []}) + "\n"
    )
    text = render_summary_text(f)
    assert "run_id" in text.lower()
    assert "no model summaries" in text.lower()


def test_render_summary_includes_per_model_rows(tmp_path):
    f = tmp_path / "run.jsonl"
    lines = [
        json.dumps(
            {"record_type": "run_manifest", "run_id": "r", "candidate_models": ["m1", "m2"]}
        ),
        json.dumps(
            {
                "record_type": "model_summary",
                "run_id": "r",
                "model": {"id": "m1"},
                "events_attempted": 40,
                "attempts_total": 65,
                "gates": {
                    "schema_validity_rate": 0.95,
                    "refusal_rate": 0.0,
                    "latency_p95_ms": 12_000,
                    "self_consistency_mean": 0.9,
                    "entity_sanity_mean": 0.95,
                },
                "system_peak": {"rss_max_mb": 20_000, "wall_clock_sec": 1200},
                "tier0_verdict": {"passed": True, "failed_gates": [], "notes": []},
            }
        ),
        json.dumps(
            {
                "record_type": "model_summary",
                "run_id": "r",
                "model": {"id": "m2"},
                "events_attempted": 40,
                "attempts_total": 65,
                "gates": {
                    "schema_validity_rate": 0.80,
                    "refusal_rate": 0.05,
                    "latency_p95_ms": 8_000,
                    "self_consistency_mean": 0.7,
                    "entity_sanity_mean": 0.85,
                },
                "system_peak": {"rss_max_mb": 18_000, "wall_clock_sec": 900},
                "tier0_verdict": {
                    "passed": False,
                    "failed_gates": ["schema_validity_rate", "refusal_rate", "entity_sanity_mean"],
                    "notes": [],
                },
            }
        ),
    ]
    f.write_text("\n".join(lines) + "\n")

    text = render_summary_text(f)
    assert "m1" in text
    assert "m2" in text
    assert "pass" in text.lower()
    assert "fail" in text.lower()
    assert "0.95" in text  # schema validity


def test_render_summary_handles_missing_numeric_fields(tmp_path):
    f = tmp_path / "run.jsonl"
    lines = [
        json.dumps({"record_type": "run_manifest", "run_id": "r", "candidate_models": ["m1"]}),
        json.dumps(
            {
                "record_type": "model_summary",
                "run_id": "r",
                "model": {"id": "m1"},
                "events_attempted": 1,
                "attempts_total": 1,
                "gates": {
                    "schema_validity_rate": None,
                    "refusal_rate": None,
                    "latency_p95_ms": None,
                    "self_consistency_mean": None,
                    "entity_sanity_mean": None,
                },
                "system_peak": {"wall_clock_sec": None},
                "tier0_verdict": {"passed": False, "failed_gates": [], "notes": []},
            }
        ),
    ]
    f.write_text("\n".join(lines) + "\n")

    text = render_summary_text(f)
    assert "n/a" in text
