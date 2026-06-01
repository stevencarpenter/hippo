import json

from hippo_brain.bench.paths import bench_results_db_path
from hippo_brain.bench.results_store import SCHEMA_VERSION, connect

from tests._bench_fixtures import (
    _manifest,
    _model_summary,
    _model_summary_with_proxy,
    _run_end,
    _write_jsonl,
)


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


def test_connect_refuses_newer_schema(tmp_path):
    """connect() must not silently downgrade a DB written by a newer binary:
    a user_version above SCHEMA_VERSION raises rather than restamping to v1."""
    import sqlite3

    import pytest

    from hippo_brain.bench.results_store import connect

    # An EMPTY DB (no tables) stamped to a future version: connect() must refuse
    # BEFORE running any DDL, so no bench_* tables get created on the way out.
    db = tmp_path / "bench-results.db"
    raw = sqlite3.connect(db)
    raw.execute(f"PRAGMA user_version={SCHEMA_VERSION + 5}")
    raw.commit()
    raw.close()

    with pytest.raises(RuntimeError, match="newer than this binary"):
        connect(db)

    raw = sqlite3.connect(db)
    # On-disk version untouched, and the schema check ran before any CREATE.
    assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 5
    tables = {
        r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "bench_runs" not in tables, "DDL must not run when a newer DB is refused"
    raw.close()


def test_connect_reopen_preserves_version(tmp_path):
    """Re-opening an existing same-version DB is a no-op on user_version (no
    spurious rewrite) and stays usable."""
    from hippo_brain.bench.results_store import connect

    db = tmp_path / "bench-results.db"
    connect(db).close()
    conn = connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
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


def test_query_helpers(tmp_path):
    from hippo_brain.bench.results_store import (
        connect,
        ingest_run,
        leaderboard_latest,
        node_detail,
        run_history,
    )

    # run-1 (older) then run-2 (newer) — leaderboard headline must use run-2.
    # Each run carries a main attempt on claude-7 so node_detail has enrichment rows.
    r1 = [
        _manifest("run-1"),
        _model_summary_with_proxy("run-1"),
        _attempt("run-1", event_id="claude-7"),
        _run_end("run-1"),
    ]
    ms2 = _model_summary_with_proxy("run-2")
    ms2["downstream_proxy"]["per_item"][0]["mrr"] = 0.5  # different score in newer run
    m2 = _manifest("run-2")
    m2["started_at_iso"] = "2026-05-31T05:00:00+00:00"
    r2 = [m2, ms2, _attempt("run-2", event_id="claude-7"), _run_end("run-2")]

    conn = connect(tmp_path / "bench-results.db")
    try:
        ingest_run(_write_jsonl(tmp_path / "r1.jsonl", r1), conn=conn)
        ingest_run(_write_jsonl(tmp_path / "r2.jsonl", r2), conn=conn)

        lb = leaderboard_latest(conn, mode="hybrid")
        # headline = newest run only
        assert lb[0]["run_id"] == "run-2"
        assert abs(lb[0]["avg_mrr"] - 0.5) < 1e-9

        detail = node_detail(conn, "claude-7", mode="hybrid")
        assert {d["run_id"] for d in detail["retrieval"]} == {"run-1", "run-2"}
        assert detail["enrichment"]  # enrichment rows present

        hist = run_history(conn)
        assert [h["run_id"] for h in hist] == ["run-2", "run-1"]  # newest first
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


def test_leaderboard_uses_latest_run_with_retrieval(tmp_path):
    """I1: when the newest run has no retrieval rows for the mode, the
    leaderboard falls back to the latest run that DOES — not a blank table."""
    from hippo_brain.bench.results_store import connect, ingest_run, leaderboard_latest

    older = [_manifest("run-old"), _model_summary_with_proxy("run-old"), _run_end("run-old")]
    # Newer run: later timestamp, plain model_summary (empty downstream_proxy) →
    # produces NO retrieval rows for any mode.
    m_new = _manifest("run-new")
    m_new["started_at_iso"] = "2026-05-31T09:00:00+00:00"
    newer = [m_new, _model_summary("run-new"), _run_end("run-new")]

    conn = connect(tmp_path / "bench-results.db")
    try:
        ingest_run(_write_jsonl(tmp_path / "old.jsonl", older), conn=conn)
        ingest_run(_write_jsonl(tmp_path / "new.jsonl", newer), conn=conn)
        lb = leaderboard_latest(conn, mode="hybrid")
        assert lb, "leaderboard must not blank when an older run has retrieval data"
        assert lb[0]["run_id"] == "run-old"
    finally:
        conn.close()


def test_aborted_run_not_ingested(tmp_path):
    """I2: a preflight-aborted run (run_end carries a reason) writes no rows."""
    from hippo_brain.bench.results_store import connect, ingest_run

    aborted_end = {
        "record_type": "run_end",
        "run_id": "run-abort",
        "finished_at_iso": "2026-05-31T00:05:00+00:00",
        "models_completed": [],
        "models_errored": [],
        "reason": "preflight_aborted",
    }
    jsonl = _write_jsonl(tmp_path / "abort.jsonl", [_manifest("run-abort"), aborted_end])
    conn = connect(tmp_path / "bench-results.db")
    try:
        res = ingest_run(jsonl, conn=conn)
        assert res.skipped_aborted is True
        assert res.inserted is False
        assert conn.execute("SELECT COUNT(*) FROM bench_runs").fetchone()[0] == 0
    finally:
        conn.close()


def test_incomplete_run_removed_when_later_aborted(tmp_path):
    """An in-flight run can be ingested before run_end exists; if the final JSONL
    later says preflight_aborted, that stale partial row must be removed."""
    from hippo_brain.bench.results_store import connect, ingest_run

    aborted_end = {
        "record_type": "run_end",
        "run_id": "run-abort",
        "finished_at_iso": "2026-05-31T00:05:00+00:00",
        "models_completed": [],
        "models_errored": [],
        "reason": "preflight_aborted",
    }
    conn = connect(tmp_path / "bench-results.db")
    try:
        first = ingest_run(
            _write_jsonl(
                tmp_path / "partial.jsonl", [_manifest("run-abort"), _attempt("run-abort")]
            ),
            conn=conn,
        )
        assert first.inserted
        assert conn.execute("SELECT COUNT(*) FROM bench_runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM bench_node_enrichment").fetchone()[0] == 1

        second = ingest_run(
            _write_jsonl(tmp_path / "aborted.jsonl", [_manifest("run-abort"), aborted_end]),
            conn=conn,
        )

        assert second.skipped_aborted is True
        assert second.inserted is False
        assert conn.execute("SELECT COUNT(*) FROM bench_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM bench_node_enrichment").fetchone()[0] == 0
    finally:
        conn.close()


def test_incomplete_run_removed_when_later_no_models(tmp_path):
    """A completed run with no model rows is intentionally skipped and should
    clear a previously ingested partial row for the same run_id."""
    from hippo_brain.bench.results_store import connect, ingest_run

    no_models_end = {
        "record_type": "run_end",
        "run_id": "run-empty",
        "finished_at_iso": "2026-05-31T00:05:00+00:00",
        "models_completed": [],
        "models_errored": [],
        "reason": "no_models",
    }
    conn = connect(tmp_path / "bench-results.db")
    try:
        ingest_run(
            _write_jsonl(
                tmp_path / "partial.jsonl", [_manifest("run-empty"), _attempt("run-empty")]
            ),
            conn=conn,
        )

        second = ingest_run(
            _write_jsonl(tmp_path / "no-models.jsonl", [_manifest("run-empty"), no_models_end]),
            conn=conn,
        )

        assert second.skipped_aborted is True
        assert conn.execute("SELECT COUNT(*) FROM bench_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM bench_node_enrichment").fetchone()[0] == 0
    finally:
        conn.close()


def test_ingest_manifest_without_run_id_returns_none_gracefully(tmp_path):
    """B: a run_manifest lacking run_id must be handled like a missing manifest
    (no KeyError crash) — the CLI ingest path has no try/except around it."""
    from hippo_brain.bench.results_store import connect, ingest_run

    m = _manifest()
    del m["run_id"]
    jsonl = _write_jsonl(tmp_path / "norunid.jsonl", [m, _run_end()])
    conn = connect(tmp_path / "bench-results.db")
    try:
        res = ingest_run(jsonl, conn=conn)  # must NOT raise
        assert res.run_id is None
        assert res.inserted is False
        assert conn.execute("SELECT COUNT(*) FROM bench_runs").fetchone()[0] == 0
    finally:
        conn.close()


def test_ingest_models_replaces_duplicate_model_id(tmp_path):
    """F: two model_summary records with the same model_id in one run (e.g.
    `--models m1,m1`) must not raise IntegrityError on the (run_id, model_id) PK."""
    from hippo_brain.bench.results_store import connect, ingest_run

    records = [
        _manifest("run-x"),
        _model_summary("run-x", "m1"),
        _model_summary("run-x", "m1"),
        _run_end("run-x"),
    ]
    jsonl = _write_jsonl(tmp_path / "dup.jsonl", records)
    conn = connect(tmp_path / "bench-results.db")
    try:
        res = ingest_run(jsonl, conn=conn)  # must NOT raise IntegrityError
        assert res.inserted
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM bench_models WHERE run_id='run-x' AND model_id='m1'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_incomplete_run_reingested_when_completed(tmp_path):
    """G: a partial run ingested while in-flight (no run_end) must be re-ingestable
    once it completes, WITHOUT --force — otherwise its finish + retrieval rows are
    lost behind the skipped_existing guard."""
    from hippo_brain.bench.results_store import connect, ingest_run

    partial = [_manifest("run-x")]  # no run_end, no model_summary → incomplete
    complete = [_manifest("run-x"), _model_summary_with_proxy("run-x"), _run_end("run-x")]

    conn = connect(tmp_path / "bench-results.db")
    try:
        r1 = ingest_run(_write_jsonl(tmp_path / "p.jsonl", partial), conn=conn)
        assert r1.inserted
        assert (
            conn.execute("SELECT finished_at_iso FROM bench_runs WHERE run_id='run-x'").fetchone()[
                0
            ]
            is None
        )

        r2 = ingest_run(_write_jsonl(tmp_path / "c.jsonl", complete), conn=conn)  # no force
        assert r2.inserted, "an incomplete run must re-ingest when it completes, without --force"
        assert (
            conn.execute("SELECT finished_at_iso FROM bench_runs WHERE run_id='run-x'").fetchone()[
                0
            ]
            is not None
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM bench_node_retrieval WHERE run_id='run-x'"
            ).fetchone()[0]
            == 2
        )
    finally:
        conn.close()


def test_enrichment_missing_parsed_output_is_sql_null(tmp_path):
    """E: an attempt with no parsed_output must store SQL NULL, not the 4-char
    TEXT string 'null' that json.dumps(None) produces."""
    from hippo_brain.bench.results_store import connect, ingest_run

    attempt = {
        "record_type": "attempt",
        "run_id": "run-x",
        "model": {"id": "m1"},
        "event": {"event_id": "claude-7", "source": "claude", "content_hash": "h"},
        "attempt_idx": 0,
        "purpose": "main",
        "timestamps": {"total_ms": 10},
        "raw_output": "",
        "parsed_output": None,
        "gates": {
            "schema_valid": False,
            "refusal_detected": False,
            "echo_similarity": 0.0,
            "entity_type_sanity": {},
        },
        "system_snapshot": {},
        "timeout": True,
    }
    jsonl = _write_jsonl(tmp_path / "np.jsonl", [_manifest("run-x"), attempt, _run_end("run-x")])
    conn = connect(tmp_path / "bench-results.db")
    try:
        ingest_run(jsonl, conn=conn)
        val = conn.execute(
            "SELECT parsed_output_json FROM bench_node_enrichment WHERE event_id='claude-7'"
        ).fetchone()[0]
        assert val is None, "missing parsed_output must store SQL NULL, not the string 'null'"
    finally:
        conn.close()


def test_all_node_details_groups_by_node(tmp_path):
    """I: bulk per-node fetch (2 queries total) groups retrieval + enrichment by
    node, replacing the per-node node_detail N+1 in the dashboard exporter."""
    from hippo_brain.bench.results_store import all_node_details, connect, ingest_run

    ms = _model_summary_with_proxy("run-1")  # has a hybrid per_item for claude-7
    ms["downstream_proxy"]["per_item"].append(
        {
            "hit_at_k": {1: False, 10: True},
            "rank": 4,
            "mrr": 0.25,
            "ndcg_at_10": 0.5,
            "qa_id": "qa-002",
            "golden_event_id": "shell-9",
            "mode": "hybrid",
        }
    )
    jsonl = _write_jsonl(tmp_path / "r.jsonl", [_manifest("run-1"), ms, _run_end("run-1")])
    conn = connect(tmp_path / "bench-results.db")
    try:
        ingest_run(jsonl, conn=conn)
        nodes = all_node_details(conn, mode="hybrid")
        assert set(nodes) == {"claude-7", "shell-9"}
        assert nodes["claude-7"]["retrieval"][0]["model_id"] == "model-a"
        assert nodes["shell-9"]["retrieval"][0]["mrr"] == 0.25
        # shape matches node_detail: each node has retrieval + enrichment lists
        assert nodes["claude-7"]["enrichment"] == []
    finally:
        conn.close()


def test_ingest_tolerates_null_hit_at_k(tmp_path):
    """Ingest is malformed-tolerant: a per_item with an explicit null hit_at_k
    (key present, value null) must score as "no hit", not raise AttributeError
    and abort the file. `item.get("hit_at_k", {})` returns None here — the {}
    default only applies to an ABSENT key — so _hit must guard non-dicts."""
    from hippo_brain.bench.results_store import connect, ingest_run

    ms = _model_summary_with_proxy("run-1")
    ms["downstream_proxy"]["per_item"] = [
        {
            "hit_at_k": None,  # explicit null, not absent
            "rank": None,
            "mrr": 0.0,
            "ndcg_at_10": 0.0,
            "qa_id": "qa-001",
            "golden_event_id": "claude-7",
            "mode": "hybrid",
        }
    ]
    jsonl = _write_jsonl(tmp_path / "r.jsonl", [_manifest("run-1"), ms, _run_end("run-1")])
    conn = connect(tmp_path / "bench-results.db")
    try:
        out = ingest_run(jsonl, conn=conn)
        assert out.inserted
        assert out.retrieval_rows == 1
        row = conn.execute(
            "SELECT hit_at_1, hit_at_10 FROM bench_node_retrieval WHERE qa_id='qa-001'"
        ).fetchone()
        assert (row["hit_at_1"], row["hit_at_10"]) == (0, 0)
    finally:
        conn.close()


def test_node_detail_orders_best_score_first(tmp_path):
    """Per-node view is "best model per corpus member": a stronger model in an
    OLDER run must sort above a weaker model in a NEWER run — score, not recency,
    leads. All runs are kept so the regression stays visible."""
    from hippo_brain.bench.results_store import all_node_details, connect, ingest_run, node_detail

    # run-old: strong score (mrr 1.0). run-new: later timestamp, weaker (mrr 0.2).
    old = [
        _manifest("run-old"),
        _model_summary_with_proxy("run-old", model="strong"),
        _run_end("run-old"),
    ]
    ms_new = _model_summary_with_proxy("run-new", model="weak")
    ms_new["downstream_proxy"]["per_item"][0]["mrr"] = 0.2
    m_new = _manifest("run-new")
    m_new["started_at_iso"] = "2026-05-31T09:00:00+00:00"
    new = [m_new, ms_new, _run_end("run-new")]

    conn = connect(tmp_path / "bench-results.db")
    try:
        ingest_run(_write_jsonl(tmp_path / "old.jsonl", old), conn=conn)
        ingest_run(_write_jsonl(tmp_path / "new.jsonl", new), conn=conn)

        detail = node_detail(conn, "claude-7", mode="hybrid")
        assert [d["model_id"] for d in detail["retrieval"]] == ["strong", "weak"]
        # both runs retained — regression not hidden behind "latest"
        assert {d["run_id"] for d in detail["retrieval"]} == {"run-old", "run-new"}

        nodes = all_node_details(conn, mode="hybrid")
        assert [d["model_id"] for d in nodes["claude-7"]["retrieval"]] == ["strong", "weak"]
    finally:
        conn.close()


def test_non_object_jsonl_line_is_malformed_not_a_crash(tmp_path):
    """A valid-JSON line that is not an object (list/string/number) must be
    counted malformed and skipped — every consumer does record.get(), so a
    non-dict record would otherwise raise AttributeError and abort the file."""
    from hippo_brain.bench.results_store import connect, ingest_run

    path = tmp_path / "run-1.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps([1, 2, 3]) + "\n")  # JSON array, not an object
        f.write(json.dumps("a bare string") + "\n")  # JSON string
        f.write(json.dumps(_manifest("run-1")) + "\n")
        f.write(json.dumps(_model_summary_with_proxy("run-1")) + "\n")
        f.write(json.dumps(_run_end("run-1")) + "\n")
    conn = connect(tmp_path / "bench-results.db")
    try:
        out = ingest_run(path, conn=conn)
        assert out.inserted
        assert out.malformed_lines == 2  # the array + the string
        assert out.retrieval_rows == 2  # the well-formed model_summary still ingested
    finally:
        conn.close()


def test_model_summary_missing_model_id_is_skipped(tmp_path):
    """A model_summary without model.id would write a NULL composite-PK row;
    SQLite permits multiple NULL PKs so INSERT OR REPLACE can't dedupe them.
    Skip the malformed summary (both the model row and its retrieval rows)."""
    from hippo_brain.bench.results_store import connect, ingest_run

    ms = _model_summary_with_proxy("run-1")
    ms["model"] = {}  # no id
    jsonl = _write_jsonl(tmp_path / "r.jsonl", [_manifest("run-1"), ms, _run_end("run-1")])
    conn = connect(tmp_path / "bench-results.db")
    try:
        out = ingest_run(jsonl, conn=conn)
        assert out.inserted
        assert out.models == 0
        assert out.retrieval_rows == 0
        assert conn.execute("SELECT COUNT(*) FROM bench_models").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM bench_node_retrieval").fetchone()[0] == 0
    finally:
        conn.close()


def test_malformed_per_item_is_tolerated(tmp_path):
    """downstream_proxy.per_item that is null, a non-list, or contains non-dict
    entries must not crash ingest; well-formed entries alongside still land."""
    from hippo_brain.bench.results_store import connect, ingest_run

    # null per_item → no retrieval rows, no crash
    ms_null = _model_summary_with_proxy("run-null", model="m1")
    ms_null["downstream_proxy"]["per_item"] = None
    # mixed: one bogus non-dict entry + one valid hybrid entry
    ms_mixed = _model_summary_with_proxy("run-mixed", model="m2")
    ms_mixed["downstream_proxy"]["per_item"] = [
        "not-a-dict",
        {
            "hit_at_k": {1: True, 10: True},
            "rank": 1,
            "mrr": 1.0,
            "ndcg_at_10": 1.0,
            "qa_id": "qa-001",
            "golden_event_id": "claude-7",
            "mode": "hybrid",
        },
    ]
    conn = connect(tmp_path / "bench-results.db")
    try:
        out_null = ingest_run(
            _write_jsonl(
                tmp_path / "null.jsonl", [_manifest("run-null"), ms_null, _run_end("run-null")]
            ),
            conn=conn,
        )
        assert out_null.inserted
        assert out_null.retrieval_rows == 0

        out_mixed = ingest_run(
            _write_jsonl(
                tmp_path / "mixed.jsonl", [_manifest("run-mixed"), ms_mixed, _run_end("run-mixed")]
            ),
            conn=conn,
        )
        assert out_mixed.inserted
        assert out_mixed.retrieval_rows == 1  # only the valid entry
    finally:
        conn.close()


def test_retrieval_entry_missing_qa_id_or_mode_is_skipped(tmp_path):
    """A per_item entry missing qa_id or mode would write NULL composite-PK
    columns that can't dedupe under INSERT OR REPLACE — skip it, keep valids."""
    from hippo_brain.bench.results_store import connect, ingest_run

    ms = _model_summary_with_proxy("run-1", model="m1")
    ms["downstream_proxy"]["per_item"] = [
        {
            "hit_at_k": {1: True},
            "mrr": 1.0,
            "golden_event_id": "claude-7",
            "mode": "hybrid",
        },  # no qa_id
        {
            "hit_at_k": {1: True},
            "mrr": 1.0,
            "qa_id": "qa-001",
            "golden_event_id": "claude-7",
        },  # no mode
        {
            "hit_at_k": {1: True, 10: True},
            "rank": 1,
            "mrr": 1.0,
            "ndcg_at_10": 1.0,
            "qa_id": "qa-002",
            "golden_event_id": "shell-9",
            "mode": "hybrid",
        },
    ]
    jsonl = _write_jsonl(tmp_path / "r.jsonl", [_manifest("run-1"), ms, _run_end("run-1")])
    conn = connect(tmp_path / "bench-results.db")
    try:
        out = ingest_run(jsonl, conn=conn)
        assert out.inserted
        assert out.retrieval_rows == 1  # only the qa-002 entry with both keys
        rows = conn.execute("SELECT qa_id FROM bench_node_retrieval").fetchall()
        assert [r["qa_id"] for r in rows] == ["qa-002"]
    finally:
        conn.close()


def test_enrichment_attempt_missing_event_id_is_skipped(tmp_path):
    """An attempt with no event_id would write a NULL composite-PK row
    (run_id, model_id, event_id) that can't dedupe — skip the malformed attempt."""
    from hippo_brain.bench.results_store import connect, ingest_run

    bad = _attempt(event_id="claude-7")
    bad["event"] = {"source": "claude"}  # event_id missing
    good = _attempt(event_id="shell-9")
    jsonl = _write_jsonl(tmp_path / "r.jsonl", [_manifest("run-1"), bad, good, _run_end("run-1")])
    conn = connect(tmp_path / "bench-results.db")
    try:
        ingest_run(jsonl, conn=conn)
        rows = conn.execute("SELECT event_id FROM bench_node_enrichment").fetchall()
        assert [r["event_id"] for r in rows] == ["shell-9"]  # bad attempt skipped
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cycle 4: per-node dashboard view shows enrichment for every corpus node
# ---------------------------------------------------------------------------


def test_all_node_details_shows_enrichment_for_every_corpus_node(tmp_path):
    """Cycle 4: all_node_details returns enrichment rows for every node that has
    a main-pass attempt — verifying the dashboard's per-node view is populated."""
    from hippo_brain.bench.results_store import all_node_details, connect, ingest_run

    # Three corpus nodes — all with main-pass attempts.
    corpus_event_ids = ["shell-1", "shell-2", "claude-7"]
    records = [
        _manifest("run-1"),
        *[_attempt(event_id=eid) for eid in corpus_event_ids],
        _model_summary("run-1"),
        _run_end("run-1"),
    ]
    jsonl = _write_jsonl(tmp_path / "r.jsonl", records)
    conn = connect(tmp_path / "bench-results.db")
    try:
        result = ingest_run(jsonl, conn=conn)
        assert result.enrichment_rows == len(corpus_event_ids), (
            f"expected {len(corpus_event_ids)} enrichment rows, got {result.enrichment_rows}"
        )

        nodes = all_node_details(conn)
        for eid in corpus_event_ids:
            assert eid in nodes, f"corpus node {eid!r} missing from all_node_details"
            enrich = nodes[eid]["enrichment"]
            assert len(enrich) == 1, f"expected 1 enrichment row for {eid!r}, got {len(enrich)}"
            assert enrich[0]["model_id"] == "model-a"
    finally:
        conn.close()


def test_all_node_details_enrichment_coverage_equals_corpus_size(tmp_path):
    """Cycle 4: bench_node_enrichment rows == corpus size × models that ran."""
    from hippo_brain.bench.results_store import connect, ingest_run

    corpus_size = 5
    corpus_events = [f"shell-{i}" for i in range(corpus_size)]
    records = [
        _manifest("run-1"),
        *[_attempt(event_id=eid) for eid in corpus_events],
        _model_summary("run-1"),
        _run_end("run-1"),
    ]
    jsonl = _write_jsonl(tmp_path / "r.jsonl", records)
    conn = connect(tmp_path / "bench-results.db")
    try:
        result = ingest_run(jsonl, conn=conn)
        n = conn.execute("SELECT COUNT(*) FROM bench_node_enrichment").fetchone()[0]
        assert n == corpus_size, (
            f"bench_node_enrichment must have {corpus_size} rows (one per corpus event), got {n}"
        )
        assert result.enrichment_rows == corpus_size
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cycle 5: idempotency — re-ingest does not duplicate enrichment rows
# ---------------------------------------------------------------------------


def test_reingest_does_not_duplicate_enrichment_rows(tmp_path):
    """Cycle 5: re-ingesting the same run JSONL must not produce duplicate rows
    in bench_node_enrichment — the (run_id, model_id, event_id) PK must hold."""
    from hippo_brain.bench.results_store import connect, ingest_run

    records = [
        _manifest("run-1"),
        _attempt(event_id="shell-1"),
        _attempt(event_id="shell-2"),
        _model_summary("run-1"),
        _run_end("run-1"),
    ]
    jsonl = _write_jsonl(tmp_path / "r.jsonl", records)
    conn = connect(tmp_path / "bench-results.db")
    try:
        ingest_run(jsonl, conn=conn)
        ingest_run(jsonl, conn=conn, force=True)  # explicit force to re-ingest same run_id
        n = conn.execute("SELECT COUNT(*) FROM bench_node_enrichment").fetchone()[0]
        assert n == 2, f"re-ingest must not duplicate rows: expected 2 (one per event), got {n}"
    finally:
        conn.close()
