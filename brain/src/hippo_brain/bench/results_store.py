"""Durable, all-local datastore for hippo-bench run results.

Parses a run's append-only JSONL (the disposable working file) into four
queryable tables keyed on run_id, so historical runs survive JSONL cleanup
and per-(model, corpus-node) scoring is referenceable across all runs.

Separate SQLite file from the application DB (hippo.db); its own
PRAGMA user_version. Idempotent on run_id — a run's JSONL is immutable
after run_end, so re-ingest is a no-op unless force=True.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
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


@dataclass
class IngestResult:
    run_id: str | None
    inserted: bool
    skipped_existing: bool
    models: int = 0
    enrichment_rows: int = 0
    retrieval_rows: int = 0
    malformed_lines: int = 0


def _parse_records(jsonl_path: Path) -> tuple[list[dict], int]:
    records: list[dict] = []
    malformed = 0
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                malformed += 1
    return records, malformed


def ingest_run(
    jsonl_path: Path,
    conn: sqlite3.Connection | None = None,
    *,
    force: bool = False,
    now_ms: int = 0,
) -> IngestResult:
    """Parse one run's JSONL into the datastore. Idempotent on run_id."""
    now_ms = now_ms or int(time.time() * 1000)  # populate ingested_at_ms for real ingests
    owns_conn = conn is None
    conn = conn or connect()
    try:
        records, malformed = _parse_records(jsonl_path)
        manifest = next((r for r in records if r.get("record_type") == "run_manifest"), None)
        if manifest is None:
            return IngestResult(
                run_id=None, inserted=False, skipped_existing=False, malformed_lines=malformed
            )
        run_id = manifest["run_id"]

        existing = conn.execute("SELECT 1 FROM bench_runs WHERE run_id=?", (run_id,)).fetchone()
        if existing and not force:
            return IngestResult(
                run_id=run_id, inserted=False, skipped_existing=True, malformed_lines=malformed
            )

        end = next((r for r in records if r.get("record_type") == "run_end"), None)

        with conn:  # one transaction; FK cascade clears child rows on replace
            conn.execute("DELETE FROM bench_runs WHERE run_id=?", (run_id,))
            conn.execute(
                """INSERT INTO bench_runs (
                    run_id, started_at_iso, finished_at_iso, host_json, bench_version,
                    corpus_version, corpus_content_hash, corpus_schema_version,
                    eval_qa_version, embedding_model, inference_backend_version,
                    gate_thresholds_json, candidate_models_json, models_completed_json,
                    models_errored_json, reason, ingested_at_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    manifest.get("started_at_iso"),
                    (end or {}).get("finished_at_iso") or manifest.get("finished_at_iso"),
                    json.dumps(manifest.get("host", {}), sort_keys=True),
                    manifest.get("bench_version"),
                    manifest.get("corpus_version"),
                    manifest.get("corpus_content_hash"),
                    manifest.get("corpus_schema_version"),
                    manifest.get("eval_qa_version"),
                    manifest.get("embedding_model"),
                    manifest.get("inference_backend_version"),
                    json.dumps(manifest.get("gate_thresholds", {}), sort_keys=True),
                    json.dumps(manifest.get("candidate_models", []), sort_keys=True),
                    json.dumps((end or {}).get("models_completed", []), sort_keys=True),
                    json.dumps((end or {}).get("models_errored", []), sort_keys=True),
                    (end or {}).get("reason"),
                    now_ms,
                ),
            )
            _ingest_models(conn, run_id, records)
            _ingest_enrichment(conn, run_id, records)
            _ingest_retrieval(conn, run_id, records)

        return IngestResult(
            run_id=run_id, inserted=True, skipped_existing=False, malformed_lines=malformed
        )
    finally:
        if owns_conn:
            conn.close()


def _ingest_models(conn, run_id, records):  # noqa: ANN001
    pass


def _ingest_enrichment(conn, run_id, records):  # noqa: ANN001
    pass


def _ingest_retrieval(conn, run_id, records):  # noqa: ANN001
    pass
