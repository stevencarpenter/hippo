"""Durable, all-local datastore for hippo-bench run results.

Parses a run's append-only JSONL (the disposable working file) into four
queryable tables keyed on run_id, so historical runs survive JSONL cleanup
and per-(model, corpus-node) scoring is referenceable across all runs.

Separate SQLite file from the application DB (hippo.db); its own
PRAGMA user_version. Idempotent on run_id — a run's JSONL is immutable
after run_end, so re-ingest is a no-op unless force=True.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hippo_brain.bench.paths import bench_results_db_path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bench_runs (
    run_id                    TEXT PRIMARY KEY,
    started_at_iso            TEXT,
    finished_at_iso           TEXT,
    host_json                 TEXT,
    bench_version             TEXT,
    corpus_version            TEXT,
    corpus_content_hash       TEXT,
    corpus_schema_version     INTEGER,
    eval_qa_version           TEXT,
    embedding_model           TEXT,
    inference_backend_version TEXT,
    gate_thresholds_json      TEXT,
    candidate_models_json     TEXT,
    models_completed_json     TEXT,
    models_errored_json       TEXT,
    reason                    TEXT,
    ingested_at_ms            INTEGER
);

CREATE TABLE IF NOT EXISTS bench_models (
    run_id                TEXT,
    model_id              TEXT,
    schema_validity_rate  REAL,
    refusal_rate          REAL,
    echo_similarity_max   REAL,
    latency_p50_ms        INTEGER,
    latency_p95_ms        INTEGER,
    latency_p99_ms        INTEGER,
    self_consistency_mean REAL,
    self_consistency_min  REAL,
    entity_sanity_mean    REAL,
    main_attempts_count   INTEGER,
    verdict_passed        INTEGER,
    failed_gates_json     TEXT,
    errors_json           TEXT,
    PRIMARY KEY (run_id, model_id),
    FOREIGN KEY (run_id) REFERENCES bench_runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bench_node_enrichment (
    run_id             TEXT,
    model_id           TEXT,
    event_id           TEXT,
    source             TEXT,
    schema_valid       INTEGER,
    refusal_detected   INTEGER,
    echo_similarity    REAL,
    entity_sanity      REAL,
    latency_ms         INTEGER,
    timeout            INTEGER,
    parsed_output_json TEXT,
    PRIMARY KEY (run_id, model_id, event_id),
    FOREIGN KEY (run_id) REFERENCES bench_runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bench_node_retrieval (
    run_id          TEXT,
    model_id        TEXT,
    qa_id           TEXT,
    golden_event_id TEXT,
    mode            TEXT,
    rank            INTEGER,
    mrr             REAL,
    hit_at_1        INTEGER,
    hit_at_10       INTEGER,
    ndcg_at_10      REAL,
    PRIMARY KEY (run_id, model_id, qa_id, mode),
    FOREIGN KEY (run_id) REFERENCES bench_runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_retrieval_node ON bench_node_retrieval(golden_event_id, mode);
CREATE INDEX IF NOT EXISTS idx_enrichment_node ON bench_node_enrichment(event_id);
CREATE INDEX IF NOT EXISTS idx_runs_started ON bench_runs(started_at_iso);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the bench results DB with schema + pragmas."""
    path = db_path or bench_results_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
    return conn
