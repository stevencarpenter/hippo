# Bench Results Datastore + Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Durably accumulate per-run, per-corpus-node bench scoring in a dedicated all-local SQLite datastore, auto-ingested at run-end, and render a self-contained HTML leaderboard/dashboard from it.

**Architecture:** A new `results_store` module owns `~/.local/share/hippo-bench/bench-results.db` (separate from `hippo.db`); it parses a run's JSONL into four tables and exposes query helpers. A new `dashboard_export` module renders those queries into one self-contained HTML file. `orchestrate_run` calls `ingest_run` at run-end (wrapped so it never fails the run); a CLI `ingest`/`export-dashboard` pair handles backfill and rendering. A one-line producer change makes each retrieval `per_item` record carry its `golden_event_id`.

**Tech Stack:** Python 3.14, stdlib `sqlite3`, `argparse` (existing bench CLI), pytest, uv.

**Spec:** `docs/superpowers/specs/2026-05-31-bench-results-datastore-design.md`

**Test command (use throughout):**
```bash
uv run --project brain pytest brain/tests/test_bench_results_store.py -v
```
Lint/format before each commit:
```bash
uv run --project brain ruff check brain/ && uv run --project brain ruff format --check brain/
```

---

## File Structure

- **Create** `brain/src/hippo_brain/bench/results_store.py` — owns `bench-results.db`: schema, `ingest_run`, query helpers. No HTML.
- **Create** `brain/src/hippo_brain/bench/dashboard_export.py` — owns HTML rendering from `results_store` queries. No SQLite internals.
- **Create** `brain/tests/test_bench_results_store.py` — ingest + query tests.
- **Create** `brain/tests/test_bench_dashboard_export.py` — HTML render tests.
- **Modify** `brain/src/hippo_brain/bench/paths.py` — add `bench_results_db_path()`.
- **Modify** `brain/src/hippo_brain/bench/downstream_proxy.py:158` — stamp `golden_event_id` onto each `per_item` score.
- **Modify** `brain/src/hippo_brain/bench/orchestrate.py` — auto-ingest in the `finally` block after `writer.close()`.
- **Modify** `brain/src/hippo_brain/bench/cli.py` — add `ingest` and `export-dashboard` subcommands.
- **Modify** `brain/tests/test_bench_downstream_proxy.py` — assert the new `golden_event_id` field.
- **Modify** `brain/src/hippo_brain/bench/README.md` and root `CLAUDE.md` — document the datastore + commands.

---

## Task 1: Producer change — `per_item` carries `golden_event_id`

**Files:**
- Modify: `brain/src/hippo_brain/bench/downstream_proxy.py:158`
- Test: `brain/tests/test_bench_downstream_proxy.py`

- [ ] **Step 1: Write the failing test**

Add to `brain/tests/test_bench_downstream_proxy.py`:

```python
def test_per_item_carries_golden_event_id():
    from hippo_brain.bench.downstream_proxy import run_downstream_proxy_pass

    qa_items = [{"qa_id": "qa-001", "question": "q?", "golden_event_id": "claude-7"}]

    def fake_search(conn, query, vec, *, mode, limit):
        # Return a single result whose source id matches the golden.
        return [{"event_id": "claude-7"}]

    out = run_downstream_proxy_pass(
        conn=None,
        qa_items=qa_items,
        embedding_fn=lambda q: [0.0, 1.0],
        modes=("hybrid",),
        search_fn=fake_search,
    )
    assert out["per_item"][0]["golden_event_id"] == "claude-7"
    assert out["per_item"][0]["qa_id"] == "qa-001"
    assert out["per_item"][0]["mode"] == "hybrid"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project brain pytest brain/tests/test_bench_downstream_proxy.py::test_per_item_carries_golden_event_id -v`
Expected: FAIL with `KeyError: 'golden_event_id'`.

- [ ] **Step 3: Add the one-line producer change**

In `run_downstream_proxy_pass`, immediately after the existing `score["qa_id"] = item.get("qa_id")` line (currently `downstream_proxy.py:158`), add:

```python
            score["golden_event_id"] = item["golden_event_id"]
```

So the block reads:
```python
            score = score_single_retrieval(results, item["golden_event_id"])
            score["qa_id"] = item.get("qa_id")
            score["golden_event_id"] = item["golden_event_id"]
            score["mode"] = mode
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project brain pytest brain/tests/test_bench_downstream_proxy.py -v`
Expected: PASS (all existing tests still green).

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/bench/downstream_proxy.py brain/tests/test_bench_downstream_proxy.py
git commit -m "feat(bench): stamp golden_event_id onto downstream_proxy per_item scores"
```

---

## Task 2: `bench_results_db_path()` in paths.py

**Files:**
- Modify: `brain/src/hippo_brain/bench/paths.py`
- Test: `brain/tests/test_bench_results_store.py` (new file)

- [ ] **Step 1: Write the failing test**

Create `brain/tests/test_bench_results_store.py`:

```python
import os

from hippo_brain.bench.paths import bench_results_db_path


def test_bench_results_db_path_under_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    p = bench_results_db_path()
    assert p == tmp_path / "hippo-bench" / "bench-results.db"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project brain pytest brain/tests/test_bench_results_store.py -v`
Expected: FAIL with `ImportError: cannot import name 'bench_results_db_path'`.

- [ ] **Step 3: Add the path helper**

Append to `brain/src/hippo_brain/bench/paths.py`:

```python
def bench_results_db_path() -> Path:
    """Durable, all-local bench results datastore. Separate file from hippo.db."""
    return hippo_bench_root() / "bench-results.db"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project brain pytest brain/tests/test_bench_results_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/bench/paths.py brain/tests/test_bench_results_store.py
git commit -m "feat(bench): add bench_results_db_path()"
```

---

## Task 3: `results_store.connect()` — schema creation

**Files:**
- Create: `brain/src/hippo_brain/bench/results_store.py`
- Test: `brain/tests/test_bench_results_store.py`

- [ ] **Step 1: Write the failing test**

Add to `brain/tests/test_bench_results_store.py`:

```python
from hippo_brain.bench.results_store import SCHEMA_VERSION, connect


def test_connect_creates_schema(tmp_path):
    db = tmp_path / "bench-results.db"
    conn = connect(db)
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project brain pytest brain/tests/test_bench_results_store.py::test_connect_creates_schema -v`
Expected: FAIL with `ModuleNotFoundError: ... results_store`.

- [ ] **Step 3: Create the module with schema**

Create `brain/src/hippo_brain/bench/results_store.py`:

```python
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
    started_at_ms            INTEGER,
    finished_at_ms           INTEGER,
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
CREATE INDEX IF NOT EXISTS idx_runs_started ON bench_runs(started_at_ms);
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project brain pytest brain/tests/test_bench_results_store.py::test_connect_creates_schema -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/bench/results_store.py brain/tests/test_bench_results_store.py
git commit -m "feat(bench): results_store schema + connect()"
```

---

## Task 4: `ingest_run` — `run_manifest` + `run_end` → `bench_runs`

**Files:**
- Modify: `brain/src/hippo_brain/bench/results_store.py`
- Test: `brain/tests/test_bench_results_store.py`

- [ ] **Step 1: Write the failing test**

Add a shared JSONL fixture helper and a test:

```python
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
        "started_at_ms": 1780185600000,
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
        "finished_at_ms": None,
    }


def _run_end(run_id="run-1"):
    return {
        "record_type": "run_end",
        "run_id": run_id,
        "finished_at_ms": 1780189200000,
        "models_completed": ["model-a"],
        "models_errored": [],
        "reason": None,
    }


def test_ingest_run_writes_bench_runs(tmp_path):
    from hippo_brain.bench.results_store import connect, ingest_run

    jsonl = _write_jsonl(tmp_path / "run-1.jsonl", [_manifest(), _run_end()])
    conn = connect(tmp_path / "bench-results.db")
    try:
        ingest_run(jsonl, conn=conn, now_ms=123)
        row = conn.execute("SELECT * FROM bench_runs WHERE run_id='run-1'").fetchone()
        assert row["corpus_content_hash"] == "sha256:abc"
        assert row["finished_at_ms"] == 1780189200000
        assert json.loads(row["models_completed_json"]) == ["model-a"]
        assert row["ingested_at_ms"] == 123
    finally:
        conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project brain pytest brain/tests/test_bench_results_store.py::test_ingest_run_writes_bench_runs -v`
Expected: FAIL with `ImportError: cannot import name 'ingest_run'`.

- [ ] **Step 3: Implement the parse loop + bench_runs upsert**

Add to `results_store.py` (imports at top: `import json`, `import time`, `from dataclasses import dataclass`):

```python
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
            return IngestResult(run_id=None, inserted=False, skipped_existing=False,
                                malformed_lines=malformed)
        run_id = manifest["run_id"]

        existing = conn.execute(
            "SELECT 1 FROM bench_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if existing and not force:
            return IngestResult(run_id=run_id, inserted=False, skipped_existing=True,
                                malformed_lines=malformed)

        end = next((r for r in records if r.get("record_type") == "run_end"), None)

        with conn:  # one transaction; FK cascade clears child rows on replace
            conn.execute("DELETE FROM bench_runs WHERE run_id=?", (run_id,))
            conn.execute(
                """INSERT INTO bench_runs (
                    run_id, started_at_ms, finished_at_ms, host_json, bench_version,
                    corpus_version, corpus_content_hash, corpus_schema_version,
                    eval_qa_version, embedding_model, inference_backend_version,
                    gate_thresholds_json, candidate_models_json, models_completed_json,
                    models_errored_json, reason, ingested_at_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    manifest.get("started_at_ms"),
                    (end or {}).get("finished_at_ms") or manifest.get("finished_at_ms"),
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
```

Add temporary no-op helpers so the module imports (real bodies come in Tasks 5–7):

```python
def _ingest_models(conn, run_id, records):  # noqa: ANN001
    pass


def _ingest_enrichment(conn, run_id, records):  # noqa: ANN001
    pass


def _ingest_retrieval(conn, run_id, records):  # noqa: ANN001
    pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project brain pytest brain/tests/test_bench_results_store.py::test_ingest_run_writes_bench_runs -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/bench/results_store.py brain/tests/test_bench_results_store.py
git commit -m "feat(bench): ingest run_manifest/run_end into bench_runs"
```

---

## Task 5: `_ingest_models` — `model_summary` → `bench_models`

**Files:**
- Modify: `brain/src/hippo_brain/bench/results_store.py`
- Test: `brain/tests/test_bench_results_store.py`

- [ ] **Step 1: Write the failing test**

```python
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


def test_ingest_models(tmp_path):
    from hippo_brain.bench.results_store import connect, ingest_run

    jsonl = _write_jsonl(
        tmp_path / "run-1.jsonl", [_manifest(), _model_summary(), _run_end()]
    )
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project brain pytest brain/tests/test_bench_results_store.py::test_ingest_models -v`
Expected: FAIL (no `bench_models` row; assertion error on `NoneType` subscript).

- [ ] **Step 3: Implement `_ingest_models`**

Replace the `_ingest_models` stub:

```python
def _ingest_models(conn: sqlite3.Connection, run_id: str, records: list[dict]) -> int:
    n = 0
    for r in records:
        if r.get("record_type") != "model_summary":
            continue
        g = r.get("gates", {})
        verdict = r.get("tier0_verdict", {})
        conn.execute(
            """INSERT INTO bench_models (
                run_id, model_id, schema_validity_rate, refusal_rate, echo_similarity_max,
                latency_p50_ms, latency_p95_ms, latency_p99_ms, self_consistency_mean,
                self_consistency_min, entity_sanity_mean, main_attempts_count,
                verdict_passed, failed_gates_json, errors_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                r.get("model", {}).get("id"),
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project brain pytest brain/tests/test_bench_results_store.py::test_ingest_models -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/bench/results_store.py brain/tests/test_bench_results_store.py
git commit -m "feat(bench): ingest model_summary into bench_models"
```

---

## Task 6: `_ingest_enrichment` — `attempt` (main) → `bench_node_enrichment`

**Files:**
- Modify: `brain/src/hippo_brain/bench/results_store.py`
- Test: `brain/tests/test_bench_results_store.py`

- [ ] **Step 1: Write the failing test**

```python
def _attempt(run_id="run-1", model="model-a", event_id="claude-7", purpose="main",
             entity_rates=None, parsed=None):
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
            "entity_type_sanity": entity_rates if entity_rates is not None else {"tool": 1.0, "file": 0.5},
        },
        "system_snapshot": {},
        "timeout": False,
    }


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
        rows = conn.execute(
            "SELECT * FROM bench_node_enrichment WHERE run_id='run-1'"
        ).fetchall()
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project brain pytest brain/tests/test_bench_results_store.py -k ingest_enrichment -v`
Expected: FAIL (no enrichment rows).

- [ ] **Step 3: Implement `_ingest_enrichment`**

Replace the `_ingest_enrichment` stub. The `entity_sanity` mean mirrors
`summary._entity_sanity_attempt_mean` (mean of per-category rates, or None if empty):

```python
def _entity_sanity_mean(per_cat: dict | None) -> float | None:
    if not isinstance(per_cat, dict) or not per_cat:
        return None
    return sum(per_cat.values()) / len(per_cat)


def _ingest_enrichment(conn: sqlite3.Connection, run_id: str, records: list[dict]) -> int:
    n = 0
    for r in records:
        if r.get("record_type") != "attempt" or r.get("purpose") != "main":
            continue
        ev = r.get("event", {})
        g = r.get("gates", {})
        conn.execute(
            """INSERT OR REPLACE INTO bench_node_enrichment (
                run_id, model_id, event_id, source, schema_valid, refusal_detected,
                echo_similarity, entity_sanity, latency_ms, timeout, parsed_output_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                r.get("model", {}).get("id"),
                ev.get("event_id"),
                ev.get("source"),
                1 if g.get("schema_valid") else 0,
                1 if g.get("refusal_detected") else 0,
                g.get("echo_similarity"),
                _entity_sanity_mean(g.get("entity_type_sanity")),
                r.get("timestamps", {}).get("total_ms"),
                1 if r.get("timeout") else 0,
                json.dumps(r.get("parsed_output"), sort_keys=True),
            ),
        )
        n += 1
    return n
```

> Note: `INSERT OR REPLACE` tolerates the rare case of multiple main attempts
> for the same event (last one wins) without violating the PK.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project brain pytest brain/tests/test_bench_results_store.py -k ingest_enrichment -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/bench/results_store.py brain/tests/test_bench_results_store.py
git commit -m "feat(bench): ingest main attempts into bench_node_enrichment"
```

---

## Task 7: `_ingest_retrieval` — `downstream_proxy.per_item` → `bench_node_retrieval`

**Files:**
- Modify: `brain/src/hippo_brain/bench/results_store.py`
- Test: `brain/tests/test_bench_results_store.py`

- [ ] **Step 1: Write the failing test**

```python
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
        hybrid = conn.execute(
            "SELECT * FROM bench_node_retrieval WHERE mode='hybrid'"
        ).fetchone()
        assert hybrid["golden_event_id"] == "claude-7"
        assert hybrid["qa_id"] == "qa-001"
        assert hybrid["rank"] == 1
        assert hybrid["hit_at_1"] == 1
        assert hybrid["hit_at_10"] == 1
        lexical = conn.execute(
            "SELECT * FROM bench_node_retrieval WHERE mode='lexical'"
        ).fetchone()
        assert lexical["rank"] is None
        assert lexical["hit_at_1"] == 0
    finally:
        conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project brain pytest brain/tests/test_bench_results_store.py::test_ingest_retrieval -v`
Expected: FAIL (no retrieval rows).

- [ ] **Step 3: Implement `_ingest_retrieval`**

`per_item.hit_at_k` keys may be ints (in-process) or strings (after a JSON
round-trip). Look up both:

```python
def _hit(hit_at_k: dict, k: int) -> int:
    v = hit_at_k.get(k, hit_at_k.get(str(k), False))
    return 1 if v else 0


def _ingest_retrieval(conn: sqlite3.Connection, run_id: str, records: list[dict]) -> int:
    n = 0
    for r in records:
        if r.get("record_type") != "model_summary":
            continue
        model_id = r.get("model", {}).get("id")
        per_item = r.get("downstream_proxy", {}).get("per_item", [])
        for item in per_item:
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project brain pytest brain/tests/test_bench_results_store.py::test_ingest_retrieval -v`
Expected: PASS.

- [ ] **Step 5: Wire the real return counts**

In `ingest_run`, replace the three bare helper calls with count capture and
return them:

```python
            n_models = _ingest_models(conn, run_id, records)
            n_enrich = _ingest_enrichment(conn, run_id, records)
            n_retr = _ingest_retrieval(conn, run_id, records)

        return IngestResult(
            run_id=run_id, inserted=True, skipped_existing=False,
            models=n_models, enrichment_rows=n_enrich, retrieval_rows=n_retr,
            malformed_lines=malformed,
        )
```

- [ ] **Step 6: Run full file to verify nothing regressed**

Run: `uv run --project brain pytest brain/tests/test_bench_results_store.py -v`
Expected: PASS (all tests).

- [ ] **Step 7: Commit**

```bash
git add brain/src/hippo_brain/bench/results_store.py brain/tests/test_bench_results_store.py
git commit -m "feat(bench): ingest downstream_proxy per_item into bench_node_retrieval"
```

---

## Task 8: Idempotency, `--force`, partial + malformed JSONL

**Files:**
- Modify: `brain/src/hippo_brain/bench/results_store.py` (already supports these — tests lock behavior)
- Test: `brain/tests/test_bench_results_store.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_reingest_same_run_is_skipped(tmp_path):
    from hippo_brain.bench.results_store import connect, ingest_run

    jsonl = _write_jsonl(tmp_path / "run-1.jsonl",
                         [_manifest(), _model_summary_with_proxy(), _run_end()])
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

    jsonl = _write_jsonl(tmp_path / "run-1.jsonl",
                         [_manifest(), _model_summary_with_proxy(), _run_end()])
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
        row = conn.execute("SELECT finished_at_ms FROM bench_runs").fetchone()
        assert row["finished_at_ms"] is None  # incomplete run
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
```

- [ ] **Step 2: Run tests**

Run: `uv run --project brain pytest brain/tests/test_bench_results_store.py -k "reingest or force or partial or malformed" -v`
Expected: PASS (logic already implemented in Tasks 4–7). If any fail, fix `ingest_run` until green.

- [ ] **Step 3: Commit**

```bash
git add brain/tests/test_bench_results_store.py
git commit -m "test(bench): lock idempotency, force-replace, partial + malformed ingest"
```

---

## Task 9: Query helpers — leaderboard (latest run), per-node, history

**Files:**
- Modify: `brain/src/hippo_brain/bench/results_store.py`
- Test: `brain/tests/test_bench_results_store.py`

- [ ] **Step 1: Write the failing test**

```python
def test_query_helpers(tmp_path):
    from hippo_brain.bench.results_store import (
        connect,
        ingest_run,
        leaderboard_latest,
        node_detail,
        run_history,
    )

    # run-1 (older) then run-2 (newer) — leaderboard headline must use run-2.
    r1 = [_manifest("run-1"), _model_summary_with_proxy("run-1"), _run_end("run-1")]
    ms2 = _model_summary_with_proxy("run-2")
    ms2["downstream_proxy"]["per_item"][0]["mrr"] = 0.5  # different score in newer run
    m2 = _manifest("run-2")
    m2["started_at_ms"] = 1780203600000
    r2 = [m2, ms2, _run_end("run-2")]

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project brain pytest brain/tests/test_bench_results_store.py::test_query_helpers -v`
Expected: FAIL with `ImportError` on the helper names.

- [ ] **Step 3: Implement the query helpers**

Append to `results_store.py`:

```python
def leaderboard_latest(conn: sqlite3.Connection, *, mode: str = "hybrid") -> list[dict]:
    """Per-model aggregate retrieval for the single most-recent run (headline)."""
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT run_id FROM bench_runs ORDER BY started_at_ms DESC LIMIT 1
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
        (mode,),
    ).fetchall()
    return [dict(r) for r in rows]


def node_detail(conn: sqlite3.Connection, event_id: str, *, mode: str = "hybrid") -> dict:
    """All historical retrieval + enrichment rows for one corpus node."""
    retrieval = conn.execute(
        """SELECT nr.run_id, nr.model_id, nr.mrr, nr.rank, nr.hit_at_1, r.started_at_ms
           FROM bench_node_retrieval nr JOIN bench_runs r USING (run_id)
           WHERE nr.golden_event_id = ? AND nr.mode = ?
           ORDER BY r.started_at_ms DESC""",
        (event_id, mode),
    ).fetchall()
    enrichment = conn.execute(
        """SELECT ne.run_id, ne.model_id, ne.schema_valid, ne.refusal_detected,
                  ne.echo_similarity, ne.entity_sanity, ne.parsed_output_json,
                  r.started_at_ms
           FROM bench_node_enrichment ne JOIN bench_runs r USING (run_id)
           WHERE ne.event_id = ?
           ORDER BY r.started_at_ms DESC""",
        (event_id,),
    ).fetchall()
    return {
        "event_id": event_id,
        "retrieval": [dict(r) for r in retrieval],
        "enrichment": [dict(r) for r in enrichment],
    }


def run_history(conn: sqlite3.Connection) -> list[dict]:
    """All runs, newest first, for the history/trend view."""
    rows = conn.execute(
        """SELECT run_id, started_at_ms, finished_at_ms, corpus_version,
                  corpus_content_hash, models_completed_json
           FROM bench_runs ORDER BY started_at_ms DESC"""
    ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project brain pytest brain/tests/test_bench_results_store.py::test_query_helpers -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/bench/results_store.py brain/tests/test_bench_results_store.py
git commit -m "feat(bench): leaderboard/node-detail/history query helpers"
```

---

## Task 10: CLI `ingest` subcommand (+ `--all`, `--force`)

**Files:**
- Modify: `brain/src/hippo_brain/bench/cli.py`
- Test: `brain/tests/test_bench_cli.py`

- [ ] **Step 1: Write the failing test**

Add to `brain/tests/test_bench_cli.py`:

```python
def test_cli_ingest_single_and_all(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from hippo_brain.bench import cli
    from hippo_brain.bench.paths import bench_runs_dir
    from hippo_brain.bench.results_store import connect

    # build a minimal valid run JSONL inside the runs dir
    runs = bench_runs_dir(create=True)
    jsonl = runs / "run-x.jsonl"
    import json
    with jsonl.open("w") as f:
        f.write(json.dumps({
            "record_type": "run_manifest", "run_id": "run-x",
            "started_at_ms": 1780185600000, "host": {},
            "candidate_models": [], "corpus_content_hash": "h",
        }, sort_keys=True) + "\n")
        f.write(json.dumps({
            "record_type": "run_end", "run_id": "run-x",
            "finished_at_ms": 1780189200000,
            "models_completed": [], "models_errored": [],
        }, sort_keys=True) + "\n")

    assert cli.main(["ingest", str(jsonl)]) == 0
    # --all is idempotent: run-x already present → skipped, still exit 0
    assert cli.main(["ingest", "--all"]) == 0

    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM bench_runs").fetchone()[0] == 1
    finally:
        conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project brain pytest brain/tests/test_bench_cli.py::test_cli_ingest_single_and_all -v`
Expected: FAIL (argparse: invalid choice `ingest`).

- [ ] **Step 3: Implement `_cmd_ingest` and register the subparser**

Add the command handler (near the other `_cmd_*` functions in `cli.py`):

```python
def _cmd_ingest(args: argparse.Namespace) -> int:
    from hippo_brain.bench.results_store import connect, ingest_run

    if args.all:
        targets = sorted(bench_runs_dir().glob("*.jsonl"))
    else:
        targets = [Path(args.run_file)]

    conn = connect()
    try:
        total_new = 0
        for t in targets:
            res = ingest_run(t, conn=conn, force=args.force)
            status = (
                "skipped (already ingested)" if res.skipped_existing
                else f"ingested run_id={res.run_id} "
                     f"(models={res.models}, enrichment={res.enrichment_rows}, "
                     f"retrieval={res.retrieval_rows}, malformed={res.malformed_lines})"
            )
            print(f"{t.name}: {status}")
            total_new += 1 if res.inserted else 0
        print(f"done: {total_new} run(s) ingested, {len(targets)} file(s) seen")
        return 0
    finally:
        conn.close()
```

Register it inside `_build_parser` (after the `summary` subparser):

```python
    ingest = sub.add_parser("ingest", help="Ingest run JSONL into the bench results datastore")
    ingest.add_argument("run_file", nargs="?", help="Path to a run JSONL (omit with --all)")
    ingest.add_argument("--all", action="store_true", help="Ingest every *.jsonl under the runs dir")
    ingest.add_argument("--force", action="store_true", help="Re-ingest runs already present")
    ingest.set_defaults(func=_cmd_ingest)
```

The `bench_runs_dir` import already exists in `cli.py` (it is used by `_cmd_run`).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project brain pytest brain/tests/test_bench_cli.py::test_cli_ingest_single_and_all -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/bench/cli.py brain/tests/test_bench_cli.py
git commit -m "feat(bench): hippo-bench ingest CLI (single + --all + --force)"
```

---

## Task 11: Auto-ingest at run-end in `orchestrate_run`

**Files:**
- Modify: `brain/src/hippo_brain/bench/orchestrate.py`
- Test: `brain/tests/test_bench_orchestrate.py`

The hook is a tiny `_safe_ingest(out_path, dry_run)` wrapper in `orchestrate.py`
so the "never fail the run" guarantee is unit-testable without running the full
non-dry orchestration (which needs model-server + prod-pause infra).

- [ ] **Step 1: Write the failing tests**

Add to `brain/tests/test_bench_orchestrate.py`:

```python
def test_safe_ingest_skips_dry_run(tmp_path, monkeypatch):
    import hippo_brain.bench.orchestrate as orch

    called = []
    monkeypatch.setattr(orch, "ingest_run", lambda p: called.append(p), raising=False)
    orch._safe_ingest(tmp_path / "run.jsonl", dry_run=True)
    assert called == []  # dry runs are not ingested


def test_safe_ingest_calls_ingest_for_real_run(tmp_path, monkeypatch):
    import hippo_brain.bench.orchestrate as orch

    called = []
    monkeypatch.setattr(orch, "ingest_run", lambda p: called.append(p), raising=False)
    out = tmp_path / "run.jsonl"
    orch._safe_ingest(out, dry_run=False)
    assert called == [out]


def test_safe_ingest_swallows_errors(tmp_path, monkeypatch):
    import hippo_brain.bench.orchestrate as orch

    def boom(_p):
        raise RuntimeError("datastore exploded")

    monkeypatch.setattr(orch, "ingest_run", boom, raising=False)
    # Must not raise even though ingest blows up — a reporting concern never
    # fails the run (AP-1).
    orch._safe_ingest(tmp_path / "run.jsonl", dry_run=False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project brain pytest brain/tests/test_bench_orchestrate.py -k safe_ingest -v`
Expected: FAIL with `AttributeError: module ... has no attribute '_safe_ingest'`.

- [ ] **Step 3: Implement the wrapper + wire it into the finally block**

Add the import near the other bench imports in `orchestrate.py`:

```python
from hippo_brain.bench.results_store import ingest_run
```

Add a module-level logger near the top if not present:

```python
import logging

_log = logging.getLogger(__name__)
```

Add the wrapper at module scope (above `orchestrate_run`):

```python
def _safe_ingest(out_path: Path, *, dry_run: bool) -> None:
    """Ingest the just-written JSONL into the results datastore.

    A reporting concern must never fail the run (AP-1): the JSONL remains the
    fallback if this raises. Dry runs are not ingested.
    """
    if dry_run:
        return
    try:
        ingest_run(out_path)
    except Exception:  # noqa: BLE001 — never fail the run over reporting
        _log.exception("results_store ingest failed for %s", out_path)
```

In the `finally:` block at the end of `orchestrate_run` (currently
`orchestrate.py:419-420`), after `writer.close()`, call it:

```python
    finally:
        writer.close()
        _safe_ingest(out_path, dry_run=dry_run)
```

> `ingest_run` is imported at module top so the monkeypatch tests can replace
> the `orch.ingest_run` attribute.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project brain pytest brain/tests/test_bench_orchestrate.py -k safe_ingest -v`
Expected: PASS.

- [ ] **Step 5: Run the full orchestrate suite to verify no regression**

Run: `uv run --project brain pytest brain/tests/test_bench_orchestrate.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add brain/src/hippo_brain/bench/orchestrate.py brain/tests/test_bench_orchestrate.py
git commit -m "feat(bench): auto-ingest run JSONL into datastore at run-end"
```

---

## Task 12: `dashboard_export` — self-contained HTML

**Files:**
- Create: `brain/src/hippo_brain/bench/dashboard_export.py`
- Test: `brain/tests/test_bench_dashboard_export.py`

- [ ] **Step 1: Write the failing test**

Create `brain/tests/test_bench_dashboard_export.py`:

```python
import json

from hippo_brain.bench.dashboard_export import build_dashboard_html, export_dashboard
from hippo_brain.bench.results_store import connect, ingest_run

# reuse the JSONL builders from the results-store test module
from test_bench_results_store import (  # type: ignore
    _manifest,
    _model_summary_with_proxy,
    _run_end,
    _write_jsonl,
)


def _seed(tmp_path):
    conn = connect(tmp_path / "bench-results.db")
    ingest_run(
        _write_jsonl(tmp_path / "r.jsonl",
                     [_manifest(), _model_summary_with_proxy(), _run_end()]),
        conn=conn,
    )
    return conn


def test_build_dashboard_html_embeds_data(tmp_path):
    conn = _seed(tmp_path)
    try:
        html = build_dashboard_html(conn)
    finally:
        conn.close()
    assert "<html" in html.lower()
    assert 'id="hippo-bench-data"' in html
    # the embedded JSON blob is parseable and carries the three views
    blob = html.split('id="hippo-bench-data">', 1)[1].split("</script>", 1)[0]
    data = json.loads(blob)
    assert {"leaderboard", "history", "nodes"} <= set(data)
    assert data["leaderboard"][0]["model_id"] == "model-a"
    # per-node view carries the scored corpus node with its retrieval rows
    assert "claude-7" in data["nodes"]
    assert data["nodes"]["claude-7"]["retrieval"][0]["model_id"] == "model-a"


def test_export_dashboard_writes_file(tmp_path):
    conn = _seed(tmp_path)
    conn.close()
    out = tmp_path / "dashboard.html"
    written = export_dashboard(out, db_path=tmp_path / "bench-results.db")
    assert written == out
    assert out.read_text(encoding="utf-8").lower().startswith("<!doctype html")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project brain pytest brain/tests/test_bench_dashboard_export.py -v`
Expected: FAIL (`ModuleNotFoundError: ... dashboard_export`).

- [ ] **Step 3: Implement the exporter**

Create `brain/src/hippo_brain/bench/dashboard_export.py`:

```python
"""Render the bench results datastore into one self-contained HTML file.

No server, no network: the data is embedded as a JSON blob and a small
vanilla-JS view renders the leaderboard, per-node lookup, and run history.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hippo_brain.bench.paths import bench_results_db_path
from hippo_brain.bench.results_store import (
    connect,
    leaderboard_latest,
    node_detail,
    run_history,
)


def _gather(conn: sqlite3.Connection) -> dict:
    # Every node scored by either pipeline, for the per-node ("best model per
    # corpus member") view. node_detail returns all historical rows per node.
    node_ids = [
        r[0]
        for r in conn.execute(
            "SELECT event_id FROM bench_node_enrichment "
            "UNION SELECT golden_event_id FROM bench_node_retrieval "
            "ORDER BY 1"
        ).fetchall()
        if r[0] is not None
    ]
    nodes = {eid: node_detail(conn, eid, mode="hybrid") for eid in node_ids}
    return {
        "leaderboard": leaderboard_latest(conn, mode="hybrid"),
        "history": run_history(conn),
        "nodes": nodes,
    }


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hippo-bench dashboard</title>
<style>
 body {{ font-family: ui-monospace, monospace; margin: 2rem; background:#0f1115; color:#e6e6e6; }}
 h1, h2 {{ color:#7aa2ff; }}
 table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem; }}
 th, td {{ border: 1px solid #2a2f3a; padding: 6px 10px; text-align: left; }}
 th {{ background:#1a1e27; }}
 tr:nth-child(even) {{ background:#161a22; }}
</style>
</head>
<body>
<h1>hippo-bench dashboard</h1>
<h2>Leaderboard — latest run (hybrid retrieval)</h2>
<div id="leaderboard"></div>
<h2>Per-node — best model per corpus member</h2>
<select id="node-select"></select>
<h3>Retrieval (all runs, hybrid)</h3>
<div id="node-retrieval"></div>
<h3>Enrichment (all runs)</h3>
<div id="node-enrichment"></div>
<h2>Run history</h2>
<div id="history"></div>
<script type="application/json" id="hippo-bench-data">{data_json}</script>
<script>
 const data = JSON.parse(document.getElementById("hippo-bench-data").textContent);
 // Build the DOM with textContent so cell values (incl. LLM-generated
 // parsed_output) can never inject markup — no innerHTML with data.
 function renderTable(target, rows, cols) {{
   const el = document.getElementById(target);
   if (!rows || !rows.length) {{ el.textContent = "(no data)"; return; }}
   const tbl = document.createElement("table");
   const thead = tbl.insertRow();
   for (const c of cols) {{
     const th = document.createElement("th");
     th.textContent = c;
     thead.appendChild(th);
   }}
   for (const r of rows) {{
     const tr = tbl.insertRow();
     for (const c of cols) {{
       const td = tr.insertCell();
       td.textContent = (r[c] === null || r[c] === undefined) ? "" : String(r[c]);
     }}
   }}
   el.replaceChildren(tbl);
 }}
 renderTable("leaderboard", data.leaderboard, ["model_id", "avg_mrr", "hit_at_1", "scored_nodes", "run_id"]);
 renderTable("history", data.history, ["started_at_ms", "run_id", "corpus_version", "finished_at_ms"]);

 // Per-node view: a <select> of every scored corpus node; on change, render
 // that node's retrieval ranking and enrichment rows across all runs.
 const sel = document.getElementById("node-select");
 const nodeIds = Object.keys(data.nodes).sort();
 for (const id of nodeIds) {{
   const opt = document.createElement("option");
   opt.value = id; opt.textContent = id;
   sel.appendChild(opt);
 }}
 function renderNode(id) {{
   const detail = data.nodes[id] || {{retrieval: [], enrichment: []}};
   renderTable("node-retrieval", detail.retrieval,
     ["model_id", "mrr", "rank", "hit_at_1", "run_id", "started_at_ms"]);
   renderTable("node-enrichment", detail.enrichment,
     ["model_id", "schema_valid", "refusal_detected", "echo_similarity",
      "entity_sanity", "parsed_output_json", "run_id"]);
 }}
 sel.addEventListener("change", () => renderNode(sel.value));
 if (nodeIds.length) renderNode(nodeIds[0]);
</script>
</body>
</html>
"""


def build_dashboard_html(conn: sqlite3.Connection) -> str:
    payload = _gather(conn)
    # Escape "</script>" defensively so embedded data can't break out of the tag.
    blob = json.dumps(payload, sort_keys=True).replace("</", "<\\/")
    return _TEMPLATE.format(data_json=blob)


def export_dashboard(out_path: Path | None = None, *, db_path: Path | None = None) -> Path:
    out = out_path or (bench_results_db_path().parent / "dashboard.html")
    conn = connect(db_path)
    try:
        html_text = build_dashboard_html(conn)
    finally:
        conn.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_text, encoding="utf-8")
    return out
```

> Note: the `<style>` block and the JS use doubled braces (`{{ }}`) because the
> template is rendered with `str.format`; only `{data_json}` is substituted.
> The table is built with `textContent`/`replaceChildren` (never `innerHTML`
> with data) so stored LLM output cannot inject markup.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project brain pytest brain/tests/test_bench_dashboard_export.py -v`
Expected: PASS. (If the cross-module import of `test_bench_results_store` fails,
confirm `brain/tests` has no `__init__.py` and pytest `rootdir` puts `brain/tests`
on `sys.path`; otherwise move the four JSONL builders into a shared
`brain/tests/_bench_fixtures.py` and import from there in both test files.)

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/bench/dashboard_export.py brain/tests/test_bench_dashboard_export.py
git commit -m "feat(bench): self-contained HTML dashboard export"
```

---

## Task 13: CLI `export-dashboard` subcommand

**Files:**
- Modify: `brain/src/hippo_brain/bench/cli.py`
- Test: `brain/tests/test_bench_cli.py`

- [ ] **Step 1: Write the failing test**

```python
def test_cli_export_dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from hippo_brain.bench import cli
    from hippo_brain.bench.results_store import connect

    connect().close()  # create an empty datastore
    out = tmp_path / "dash.html"
    assert cli.main(["export-dashboard", "--out", str(out)]) == 0
    assert out.exists()
    assert "hippo-bench dashboard" in out.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project brain pytest brain/tests/test_bench_cli.py::test_cli_export_dashboard -v`
Expected: FAIL (argparse: invalid choice `export-dashboard`).

- [ ] **Step 3: Implement the handler + subparser**

Add the handler in `cli.py`:

```python
def _cmd_export_dashboard(args: argparse.Namespace) -> int:
    from hippo_brain.bench.dashboard_export import export_dashboard

    out = export_dashboard(Path(args.out) if args.out else None)
    print(f"wrote dashboard: {out}")
    return 0
```

Register in `_build_parser` (after the `ingest` subparser):

```python
    export = sub.add_parser("export-dashboard", help="Render the results datastore to one HTML file")
    export.add_argument("--out", help="Output HTML path (default: <hippo-bench>/dashboard.html)")
    export.set_defaults(func=_cmd_export_dashboard)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project brain pytest brain/tests/test_bench_cli.py::test_cli_export_dashboard -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/bench/cli.py brain/tests/test_bench_cli.py
git commit -m "feat(bench): hippo-bench export-dashboard CLI"
```

---

## Task 14: Documentation

**Files:**
- Modify: `brain/src/hippo_brain/bench/README.md`
- Modify: `CLAUDE.md` (root)

- [ ] **Step 1: Update the bench README**

In `brain/src/hippo_brain/bench/README.md`, add a "Results datastore" section
documenting: the DB location (`~/.local/share/hippo-bench/bench-results.db`,
separate from `hippo.db`); the four tables and what each captures; that
retrieval (MRR/Hit@1) is the headline and enrichment gates are full-coverage;
auto-ingest at run-end plus `hippo-bench ingest <jsonl> [--all] [--force]` for
backfill; and `hippo-bench export-dashboard [--out]` → one self-contained HTML
file with leaderboard + run history. Note the JSONL is now a disposable working
file and the datastore is the durable keeper.

- [ ] **Step 2: Update root CLAUDE.md**

Under the hippo-bench area of `CLAUDE.md`, add a short subsection:
"Bench results datastore — `bench-results.db` (separate from `hippo.db`),
auto-ingested at run-end via `results_store.ingest_run`; per-(model, corpus-node)
scoring across all runs; `hippo-bench ingest`/`export-dashboard` CLIs; spec at
`docs/superpowers/specs/2026-05-31-bench-results-datastore-design.md`."

- [ ] **Step 3: Verify the whole bench suite is green + lint clean**

Run:
```bash
uv run --project brain pytest brain/tests -k bench -v
uv run --project brain ruff check brain/ && uv run --project brain ruff format --check brain/
```
Expected: all PASS, no lint errors.

- [ ] **Step 4: Commit**

```bash
git add brain/src/hippo_brain/bench/README.md CLAUDE.md
git commit -m "docs(bench): document results datastore + dashboard commands"
```

---

## Final verification

- [ ] Whole-DB rebuild is deterministic: `hippo-bench ingest --all --force` over a
  folder of run JSONLs reproduces the same row counts. (Covered by Task 8 force
  test at unit level; spot-check manually if real run JSONLs exist.)
- [ ] Run the full brain suite once: `uv run --project brain pytest brain/tests -q`.
- [ ] Confirm `bench-results.db` is **not** committed (it lives under
  `~/.local/share/hippo-bench/`, outside the repo — no `.gitignore` change needed).
