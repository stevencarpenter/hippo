import json

from hippo_brain.bench.paths import bench_results_db_path
from hippo_brain.bench.results_store import SCHEMA_VERSION, connect


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


def _attempt(
    run_id="run-1",
    model="model-a",
    event_id="claude-7",
    purpose="main",
    entity_rates=None,
    parsed=None,
):
    return {
        "record_type": "attempt",
        "run_id": run_id,
        "model": {"id": model},
        "event": {"event_id": event_id, "source": event_id.split("-")[0], "content_hash": "h"},
        "attempt_idx": 0,
        "purpose": purpose,
        "timestamps": {"total_ms": 150},
        "raw_output": "{}",
        "parsed_output": parsed if parsed is not None else {"summary": "s"},
        "gates": {
            "schema_valid": True,
            "refusal_detected": False,
            "echo_similarity": 0.2,
            "entity_type_sanity": entity_rates
            if entity_rates is not None
            else {"tool": 1.0, "file": 0.5},
        },
        "system_snapshot": {},
        "timeout": False,
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


def test_ingest_retrieval(tmp_path):
    from hippo_brain.bench.results_store import connect, ingest_run

    jsonl = _write_jsonl(
        tmp_path / "run-1.jsonl", [_manifest(), _model_summary_with_proxy(), _run_end()]
    )
    conn = connect(tmp_path / "bench-results.db")
    try:
        ingest_run(jsonl, conn=conn)
        hybrid = conn.execute("SELECT * FROM bench_node_retrieval WHERE mode='hybrid'").fetchone()
        assert hybrid["golden_event_id"] == "claude-7"
        assert hybrid["qa_id"] == "qa-001"
        assert hybrid["rank"] == 1
        assert hybrid["hit_at_1"] == 1
        assert hybrid["hit_at_10"] == 1
        lexical = conn.execute("SELECT * FROM bench_node_retrieval WHERE mode='lexical'").fetchone()
        assert lexical["rank"] is None
        assert lexical["hit_at_1"] == 0
    finally:
        conn.close()


def test_ingest_enrichment_main_only(tmp_path):
    from hippo_brain.bench.results_store import connect, ingest_run

    records = [
        _manifest(),
        _attempt(event_id="claude-7"),
        _attempt(event_id="shell-9", purpose="self_consistency"),  # excluded
        _run_end(),
    ]
    jsonl = _write_jsonl(tmp_path / "run-1.jsonl", records)
    conn = connect(tmp_path / "bench-results.db")
    try:
        ingest_run(jsonl, conn=conn)
        rows = conn.execute("SELECT * FROM bench_node_enrichment WHERE run_id='run-1'").fetchall()
        assert len(rows) == 1  # self_consistency attempt excluded
        row = rows[0]
        assert row["event_id"] == "claude-7"
        assert row["source"] == "claude"
        assert row["schema_valid"] == 1
        assert abs(row["entity_sanity"] - 0.75) < 1e-9  # mean(1.0, 0.5)
        assert row["latency_ms"] == 150
        assert json.loads(row["parsed_output_json"]) == {"summary": "s"}
    finally:
        conn.close()


def test_ingest_enrichment_empty_entity_rates_is_null(tmp_path):
    from hippo_brain.bench.results_store import connect, ingest_run

    jsonl = _write_jsonl(
        tmp_path / "run-1.jsonl",
        [_manifest(), _attempt(entity_rates={}), _run_end()],
    )
    conn = connect(tmp_path / "bench-results.db")
    try:
        ingest_run(jsonl, conn=conn)
        row = conn.execute("SELECT entity_sanity FROM bench_node_enrichment").fetchone()
        assert row["entity_sanity"] is None
    finally:
        conn.close()


def test_ingest_models(tmp_path):
    from hippo_brain.bench.results_store import connect, ingest_run

    jsonl = _write_jsonl(tmp_path / "run-1.jsonl", [_manifest(), _model_summary(), _run_end()])
    conn = connect(tmp_path / "bench-results.db")
    try:
        ingest_run(jsonl, conn=conn)
        row = conn.execute(
            "SELECT * FROM bench_models WHERE run_id='run-1' AND model_id='model-a'"
        ).fetchone()
        assert row["schema_validity_rate"] == 1.0
        assert row["latency_p95_ms"] == 200
        assert row["verdict_passed"] == 1
        assert row["self_consistency_mean"] is None
    finally:
        conn.close()


def test_ingest_run_writes_bench_runs(tmp_path):
    from hippo_brain.bench.results_store import connect, ingest_run

    jsonl = _write_jsonl(tmp_path / "run-1.jsonl", [_manifest(), _run_end()])
    conn = connect(tmp_path / "bench-results.db")
    try:
        ingest_run(jsonl, conn=conn, now_ms=123)
        row = conn.execute("SELECT * FROM bench_runs WHERE run_id='run-1'").fetchone()
        assert row["corpus_content_hash"] == "sha256:abc"
        assert row["finished_at_iso"] == "2026-05-31T01:00:00+00:00"
        assert json.loads(row["models_completed_json"]) == ["model-a"]
        assert row["ingested_at_ms"] == 123
    finally:
        conn.close()


def test_bench_results_db_path_under_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    p = bench_results_db_path()
    assert p == tmp_path / "hippo-bench" / "bench-results.db"


def test_connect_creates_schema(tmp_path):
    db = tmp_path / "bench-results.db"
    conn = connect(db)
    try:
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {
            "bench_runs",
            "bench_models",
            "bench_node_enrichment",
            "bench_node_retrieval",
        } <= names
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_reingest_same_run_is_skipped(tmp_path):
    from hippo_brain.bench.results_store import connect, ingest_run

    jsonl = _write_jsonl(
        tmp_path / "run-1.jsonl", [_manifest(), _model_summary_with_proxy(), _run_end()]
    )
    conn = connect(tmp_path / "bench-results.db")
    try:
        first = ingest_run(jsonl, conn=conn)
        assert first.inserted and not first.skipped_existing
        second = ingest_run(jsonl, conn=conn)
        assert second.skipped_existing and not second.inserted
        assert conn.execute("SELECT COUNT(*) FROM bench_node_retrieval").fetchone()[0] == 2
    finally:
        conn.close()


def test_force_replaces_run(tmp_path):
    from hippo_brain.bench.results_store import connect, ingest_run

    jsonl = _write_jsonl(
        tmp_path / "run-1.jsonl", [_manifest(), _model_summary_with_proxy(), _run_end()]
    )
    conn = connect(tmp_path / "bench-results.db")
    try:
        ingest_run(jsonl, conn=conn)
        out = ingest_run(jsonl, conn=conn, force=True)
        assert out.inserted
        # cascade delete + reinsert leaves exactly one run, no duplicate child rows
        assert conn.execute("SELECT COUNT(*) FROM bench_runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM bench_node_retrieval").fetchone()[0] == 2
    finally:
        conn.close()


def test_partial_jsonl_no_run_end(tmp_path):
    from hippo_brain.bench.results_store import connect, ingest_run

    jsonl = _write_jsonl(tmp_path / "run-1.jsonl", [_manifest(), _attempt()])
    conn = connect(tmp_path / "bench-results.db")
    try:
        out = ingest_run(jsonl, conn=conn)
        assert out.inserted
        row = conn.execute("SELECT finished_at_iso FROM bench_runs").fetchone()
        assert row["finished_at_iso"] is None  # incomplete run
        assert conn.execute("SELECT COUNT(*) FROM bench_node_enrichment").fetchone()[0] == 1
    finally:
        conn.close()


def test_malformed_line_tolerated(tmp_path):
    from hippo_brain.bench.results_store import connect, ingest_run

    path = tmp_path / "run-1.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_manifest(), sort_keys=True) + "\n")
        f.write("{not json\n")
        f.write(json.dumps(_run_end(), sort_keys=True) + "\n")
    conn = connect(tmp_path / "bench-results.db")
    try:
        out = ingest_run(path, conn=conn)
        assert out.inserted
        assert out.malformed_lines == 1
    finally:
        conn.close()
