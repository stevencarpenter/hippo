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
    skipped_aborted: bool = False


def _as_dict(v: object) -> dict:
    """Coerce a JSONL value to a dict; a non-dict (null, list, scalar) becomes {}.

    Ingest is malformed-tolerant: every consumer reaches into nested objects with
    ``.get()``, so a partial/older record carrying ``null`` (or any non-object)
    where a dict is expected must degrade to empty fields, not raise AttributeError
    and abort the whole file.
    """
    return v if isinstance(v, dict) else {}


def _parse_records(jsonl_path: Path) -> tuple[list[dict], int]:
    records: list[dict] = []
    malformed = 0
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            # A line can be valid JSON yet not an object (list/string/number).
            # Every downstream consumer does record.get(...), so drop non-dict
            # records as malformed rather than crashing on the first .get().
            if isinstance(rec, dict):
                records.append(rec)
            else:
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
        run_id = manifest.get("run_id")
        if not run_id:
            # A manifest present but missing run_id is bad data, not a crash —
            # handle it like a missing manifest. The CLI ingest path does not
            # wrap ingest_run in try/except, so a KeyError here would abort it.
            return IngestResult(
                run_id=None, inserted=False, skipped_existing=False, malformed_lines=malformed
            )

        # Skip re-ingest only for a COMPLETE run already on file (finished_at_iso
        # set). An incomplete run (ingested while still in-flight, finished_at_iso
        # NULL) must remain re-ingestable so its later run_end + retrieval rows
        # land without --force. `--force` always re-ingests.
        existing = conn.execute(
            "SELECT finished_at_iso FROM bench_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if existing is not None and existing[0] is not None and not force:
            return IngestResult(
                run_id=run_id, inserted=False, skipped_existing=True, malformed_lines=malformed
            )

        end = next((r for r in records if r.get("record_type") == "run_end"), None)

        # Aborted / no-model runs carry no scoring rows. Skip them so they don't
        # add empty noise to run history or become the "latest" run that blanks
        # the leaderboard. (A still-running partial JSONL has no run_end / no
        # reason and is NOT skipped here — it ingests what it has.)
        if end and end.get("reason") in {"preflight_aborted", "no_models"}:
            with conn:
                conn.execute("DELETE FROM bench_runs WHERE run_id=?", (run_id,))
            return IngestResult(
                run_id=run_id,
                inserted=False,
                skipped_existing=False,
                skipped_aborted=True,
                malformed_lines=malformed,
            )

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
            n_models = _ingest_models(conn, run_id, records)
            n_enrich = _ingest_enrichment(conn, run_id, records)
            n_retr = _ingest_retrieval(conn, run_id, records)

        return IngestResult(
            run_id=run_id,
            inserted=True,
            skipped_existing=False,
            models=n_models,
            enrichment_rows=n_enrich,
            retrieval_rows=n_retr,
            malformed_lines=malformed,
        )
    finally:
        if owns_conn:
            conn.close()


def _ingest_models(conn: sqlite3.Connection, run_id: str, records: list[dict]) -> int:
    n = 0
    for r in records:
        if r.get("record_type") != "model_summary":
            continue
        model_id = _as_dict(r.get("model")).get("id")
        if not model_id:
            # No model id → a NULL composite-PK row. SQLite permits multiple NULLs
            # in a PRIMARY KEY, so INSERT OR REPLACE would NOT collapse them and a
            # malformed summary could spawn junk rows. Skip it.
            continue
        g = _as_dict(r.get("gates"))
        verdict = _as_dict(r.get("tier0_verdict"))
        # OR REPLACE (matching _ingest_enrichment / _ingest_retrieval): a run
        # with a duplicated candidate model (e.g. `--models m1,m1`) emits two
        # model_summary records for the same (run_id, model_id) PK; collapse to
        # the last rather than raising IntegrityError mid-transaction.
        conn.execute(
            """INSERT OR REPLACE INTO bench_models (
                run_id, model_id, schema_validity_rate, refusal_rate, echo_similarity_max,
                latency_p50_ms, latency_p95_ms, latency_p99_ms, self_consistency_mean,
                self_consistency_min, entity_sanity_mean, main_attempts_count,
                verdict_passed, failed_gates_json, errors_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                model_id,
                g.get("schema_validity_rate"),
                g.get("refusal_rate"),
                g.get("echo_similarity_max"),
                g.get("latency_p50_ms"),
                g.get("latency_p95_ms"),
                g.get("latency_p99_ms"),
                g.get("self_consistency_mean"),
                g.get("self_consistency_min"),
                g.get("entity_sanity_mean"),
                g.get("main_attempts_count"),
                1 if verdict.get("passed") else 0,
                json.dumps(verdict.get("failed_gates", []), sort_keys=True),
                json.dumps(r.get("errors", []), sort_keys=True),
            ),
        )
        n += 1
    return n


def _entity_sanity_mean(per_cat: dict | None) -> float | None:
    if not isinstance(per_cat, dict) or not per_cat:
        return None
    return sum(per_cat.values()) / len(per_cat)


def _ingest_enrichment(conn: sqlite3.Connection, run_id: str, records: list[dict]) -> int:
    # DORMANT on real runs: this selects `main`-purpose attempts, but the bench
    # pipeline does not yet emit any (the self-consistency pass labels its
    # attempts `self_consistency`, and the shadow brain's full-corpus enrichment
    # is discarded with the shadow stack). The ingest + schema are ready; the
    # producer is owed by https://github.com/stevencarpenter/hippo/issues/191.
    # Until then this is a no-op on real runs (tests exercise it with synthetic
    # `main` attempts). Keep the `main` filter — it is the correct contract.
    n = 0
    for r in records:
        if r.get("record_type") != "attempt" or r.get("purpose") != "main":
            continue
        ev = _as_dict(r.get("event"))
        g = _as_dict(r.get("gates"))
        conn.execute(
            """INSERT OR REPLACE INTO bench_node_enrichment (
                run_id, model_id, event_id, source, schema_valid, refusal_detected,
                echo_similarity, entity_sanity, latency_ms, timeout, parsed_output_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                _as_dict(r.get("model")).get("id"),
                ev.get("event_id"),
                ev.get("source"),
                1 if g.get("schema_valid") else 0,
                1 if g.get("refusal_detected") else 0,
                g.get("echo_similarity"),
                _entity_sanity_mean(g.get("entity_type_sanity")),
                _as_dict(r.get("timestamps")).get("total_ms"),
                1 if r.get("timeout") else 0,
                # Map a missing/None parsed_output to SQL NULL, not the literal
                # TEXT 'null' that json.dumps(None) would produce (matches the
                # None→NULL behavior of every sibling column in this INSERT).
                (
                    json.dumps(r.get("parsed_output"), sort_keys=True)
                    if r.get("parsed_output") is not None
                    else None
                ),
            ),
        )
        n += 1
    return n


def _hit(hit_at_k: object, k: int) -> int:
    # Ingest is malformed-tolerant: a record whose hit_at_k is missing, null, or
    # any non-dict value counts as "no hit" rather than aborting the whole file.
    # (`item.get("hit_at_k", {})` does NOT guard this — an explicit `"hit_at_k":
    # null` yields None, since .get only substitutes the default for an ABSENT key.)
    if not isinstance(hit_at_k, dict):
        return 0
    v = hit_at_k.get(k, hit_at_k.get(str(k), False))
    return 1 if v else 0


def _ingest_retrieval(conn: sqlite3.Connection, run_id: str, records: list[dict]) -> int:
    n = 0
    for r in records:
        if r.get("record_type") != "model_summary":
            continue
        model_id = _as_dict(r.get("model")).get("id")
        if not model_id:
            continue  # NULL composite-PK row (see _ingest_models) — skip
        per_item = _as_dict(r.get("downstream_proxy")).get("per_item")
        if not isinstance(per_item, list):
            # null / non-list per_item (older/partial/malformed run): nothing to
            # score for this model, not a crash.
            continue
        for item in per_item:
            if not isinstance(item, dict):
                continue  # skip a malformed non-dict score entry
            hk = item.get("hit_at_k", {})
            conn.execute(
                """INSERT OR REPLACE INTO bench_node_retrieval (
                    run_id, model_id, qa_id, golden_event_id, mode, rank, mrr,
                    hit_at_1, hit_at_10, ndcg_at_10
                ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    model_id,
                    item.get("qa_id"),
                    item.get("golden_event_id"),
                    item.get("mode"),
                    item.get("rank"),
                    item.get("mrr"),
                    _hit(hk, 1),
                    _hit(hk, 10),
                    item.get("ndcg_at_10"),
                ),
            )
            n += 1
    return n


def leaderboard_latest(conn: sqlite3.Connection, *, mode: str = "hybrid") -> list[dict]:
    """Per-model aggregate retrieval for the headline run.

    The headline is the most-recent run that actually has retrieval rows for
    ``mode`` — NOT simply the newest run. QA scoring is often skipped (the
    fixture can be absent), so the newest run may have no retrieval data; falling
    back to the latest run that does keeps the leaderboard populated instead of
    blanking it and hiding all history.
    """
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT r.run_id
            FROM bench_runs r
            WHERE EXISTS (
                SELECT 1 FROM bench_node_retrieval nr
                WHERE nr.run_id = r.run_id AND nr.mode = ?
            )
            ORDER BY r.started_at_iso DESC
            LIMIT 1
        )
        SELECT nr.run_id, nr.model_id,
               AVG(nr.mrr)            AS avg_mrr,
               AVG(nr.hit_at_1)       AS hit_at_1,
               COUNT(*)               AS scored_nodes
        FROM bench_node_retrieval nr
        JOIN latest ON latest.run_id = nr.run_id
        WHERE nr.mode = ?
        GROUP BY nr.run_id, nr.model_id
        ORDER BY avg_mrr DESC
        """,
        (mode, mode),
    ).fetchall()
    return [dict(r) for r in rows]


def node_detail(conn: sqlite3.Connection, event_id: str, *, mode: str = "hybrid") -> dict:
    """All historical retrieval + enrichment rows for one corpus node."""
    # "Best model per corpus member": order by score so the strongest model/run
    # surfaces first, not merely the newest. All runs are kept (not deduped to
    # latest-per-model) so a regression — a model that scored worse in a later
    # run — stays visible instead of being hidden behind "current".
    retrieval = conn.execute(
        """SELECT nr.run_id, nr.model_id, nr.mrr, nr.rank, nr.hit_at_1, r.started_at_iso
           FROM bench_node_retrieval nr JOIN bench_runs r USING (run_id)
           WHERE nr.golden_event_id = ? AND nr.mode = ?
           ORDER BY nr.mrr DESC, nr.hit_at_1 DESC, r.started_at_iso DESC""",
        (event_id, mode),
    ).fetchall()
    enrichment = conn.execute(
        """SELECT ne.run_id, ne.model_id, ne.schema_valid, ne.refusal_detected,
                  ne.echo_similarity, ne.entity_sanity, ne.parsed_output_json,
                  r.started_at_iso
           FROM bench_node_enrichment ne JOIN bench_runs r USING (run_id)
           WHERE ne.event_id = ?
           ORDER BY r.started_at_iso DESC""",
        (event_id,),
    ).fetchall()
    return {
        "event_id": event_id,
        "retrieval": [dict(r) for r in retrieval],
        "enrichment": [dict(r) for r in enrichment],
    }


def all_node_details(conn: sqlite3.Connection, *, mode: str = "hybrid") -> dict[str, dict]:
    """Per-node retrieval + enrichment for EVERY scored node, keyed by node id.

    Two queries total (one retrieval, one enrichment), vs ``node_detail``'s two
    queries *per node*. Same per-node shape as ``node_detail``; used by the
    dashboard exporter to avoid an N+1 over the corpus.
    """
    nodes: dict[str, dict] = {}

    def _node(event_id: str) -> dict:
        return nodes.setdefault(event_id, {"event_id": event_id, "retrieval": [], "enrichment": []})

    # Best-score-first within each node (see node_detail): surfaces the strongest
    # model/run per corpus member rather than just the newest, while keeping all
    # runs so regressions stay visible.
    for r in conn.execute(
        """SELECT nr.golden_event_id AS event_id, nr.run_id, nr.model_id, nr.mrr, nr.rank,
                  nr.hit_at_1, r.started_at_iso
           FROM bench_node_retrieval nr JOIN bench_runs r USING (run_id)
           WHERE nr.mode = ? AND nr.golden_event_id IS NOT NULL
           ORDER BY nr.mrr DESC, nr.hit_at_1 DESC, r.started_at_iso DESC""",
        (mode,),
    ).fetchall():
        d = dict(r)
        _node(d.pop("event_id"))["retrieval"].append(d)

    for r in conn.execute(
        """SELECT ne.event_id, ne.run_id, ne.model_id, ne.schema_valid, ne.refusal_detected,
                  ne.echo_similarity, ne.entity_sanity, ne.parsed_output_json, r.started_at_iso
           FROM bench_node_enrichment ne JOIN bench_runs r USING (run_id)
           ORDER BY r.started_at_iso DESC""",
    ).fetchall():
        d = dict(r)
        _node(d.pop("event_id"))["enrichment"].append(d)

    return nodes


def run_history(conn: sqlite3.Connection) -> list[dict]:
    """All runs, newest first, for the history/trend view."""
    rows = conn.execute(
        """SELECT run_id, started_at_iso, finished_at_iso, corpus_version,
                  corpus_content_hash, models_completed_json
           FROM bench_runs ORDER BY started_at_iso DESC"""
    ).fetchall()
    return [dict(r) for r in rows]
