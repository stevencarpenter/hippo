# hippo-bench Results Datastore + Dashboard — Design

**Date:** 2026-05-31
**Status:** Approved (brainstorming), pending implementation plan
**Author:** Steven Carpenter

## Problem

`hippo-bench run` is the only thing that scores enrichment models against a
frozen corpus, but its output is not being **kept**. Each run streams a single
append-only JSONL file to `~/.local/share/hippo-bench/runs/run-{ts}-{host}.jsonl`
(`cli.py:218`, written by `output.RunWriter`). That file is:

- **gitignored and local-only** (`.gitignore:433`) — never committed, never backed up;
- **not indexed** — no catalog, no `list`, no cross-run query; consumers
  (`hippo-bench summary`, `hippo-bench determinism`) take hand-fed file paths;
- **not actually surviving on disk** — at design time, `runs/` held **zero**
  `.jsonl` files, only 112 MB of ephemeral per-model shadow-stack scratch dirs
  (`runs/run-…/{model}/hippo.db`, logs). The durable results were gone; only the
  disposable scratch remained.

The goal: **all historical runs, referenceable**, with **per-corpus-node**
scoring so we can answer *"which model performs best on each member of the
corpus"*, visualized in a separate, all-local dashboard with a leaderboard.

## Goals

- A dedicated, all-local datastore separate from the application DB (`hippo.db`).
- Per-`(model, corpus-node)` scoring retained across **all** runs.
- Both scoring signals stored; **retrieval quality is the headline leaderboard
  signal**, enrichment quality is the full-coverage health layer.
- Auto-captured at run-end so history accrues without operator action.
- A separate, self-contained dashboard (no running server) with a leaderboard,
  per-node view, and run history.

## Non-Goals

- **Not** committing run data to git (explicit user constraint — stays local).
- **Not** recovering the already-lost historical runs (the leftover scratch dirs
  contain no JSONL — nothing to recover). Existing scratch dirs are left alone.
- **Not** multi-host / sync. This is a single-host deployment.
- **Not** changing the JSONL run format in a breaking way, nor touching the
  `summary`/`determinism` consumers. (One *additive*, non-breaking field is added:
  each `downstream_proxy.per_item` score now also carries its `golden_event_id`,
  so retrieval rows can be keyed to a corpus node. Existing consumers ignore it.)

## Key Design Decision: the two per-node signals

A bench run already contains two per-node scoring signals in its JSONL, with
different coverage. The datastore captures **both** because both already exist in
the file — normalizing them into queryable rows is marginal extra schema, not
extra pipeline.

| Signal | Source record | Coverage | Role |
|---|---|---|---|
| **Enrichment quality** | `attempt` (purpose=`main`) gates: `schema_valid`, `refusal_detected`, `echo_similarity`, `entity_type_sanity`, latency | **Every corpus node** | Full-coverage health layer |
| **Retrieval quality** | `model_summary.downstream_proxy.per_item`: `rank`, `mrr`, `ndcg_at_10`, `hit_at_k{1,3,5,10}`, `qa_id`, `mode` (one entry per QA item × mode) | **QA-labeled subset only** | **Headline leaderboard** |

Enrichment gates are a *floor* (did the output parse / not refuse / not echo),
so they rarely discriminate "best". Retrieval (`score_single_retrieval`) measures
whether a model's enrichment makes a node findable — the outcome hippo exists for
— so it is the headline. Its coverage grows automatically as more `eval-qa` items
are labeled; no schema change needed.

## Architecture

```
orchestrate_run ──(writes JSONL as today)──▶ run-{ts}-{host}.jsonl  (disposable working file)
       │
       └─(at run_end, reads it back)──▶ results_store.ingest_run()
                                              │
                                              ▼
                                    bench-results.db   (durable keeper, all-local)
                                              │
                          hippo-bench export-dashboard
                                              ▼
                                    dashboard.html     (self-contained, no server)
```

The JSONL stays the crash-safe streaming record during a run; at `run_end` it is
ingested into the durable datastore. After ingest the JSONL is a disposable
working file — losing it no longer loses history.

### New units (each one job, well-bounded)

- **`brain/src/hippo_brain/bench/results_store.py`** — owns `bench-results.db`:
  schema creation/migration (own `PRAGMA user_version`), `ingest_run(jsonl_path,
  force=False)` (idempotent, keyed on `run_id`), and query helpers used by the
  exporter. Knows nothing about HTML.
- **`brain/src/hippo_brain/bench/dashboard_export.py`** — owns the HTML: calls
  `results_store` query helpers, embeds the result as a JSON blob in a single
  self-contained template. Knows nothing about SQLite internals.
- **CLI** (`bench/cli.py`): `hippo-bench ingest <jsonl> [--all] [--force]` and
  `hippo-bench export-dashboard [--out <path>]`.
- **Hook** (`bench/orchestrate.py`): one `ingest_run(out_path)` call at the end of
  `orchestrate_run`, wrapped so an ingest failure **never** fails the run.

## Datastore

Location: `~/.local/share/hippo-bench/bench-results.db` (resolved via
`paths.hippo_bench_root()`, sibling of `runs/` and `fixtures/`). SQLite, WAL mode,
`PRAGMA foreign_keys=ON`, `busy_timeout=5000`, independent `PRAGMA user_version`.

### Schema

```sql
CREATE TABLE bench_runs (
    run_id                  TEXT PRIMARY KEY,
    started_at_iso          TEXT,
    finished_at_iso         TEXT,           -- NULL ⇒ incomplete/crashed run
    host_json               TEXT,
    bench_version           TEXT,
    corpus_version          TEXT,
    corpus_content_hash     TEXT,
    corpus_schema_version   INTEGER,
    eval_qa_version         TEXT,
    embedding_model         TEXT,
    inference_backend_version TEXT,
    gate_thresholds_json    TEXT,
    candidate_models_json   TEXT,
    models_completed_json   TEXT,
    models_errored_json     TEXT,
    reason                  TEXT,
    ingested_at_ms          INTEGER
);

CREATE TABLE bench_models (
    run_id                TEXT,
    model_id              TEXT,
    schema_validity_rate  REAL,
    refusal_rate          REAL,
    echo_similarity_max   REAL,
    latency_p50_ms        INTEGER,
    latency_p95_ms        INTEGER,
    latency_p99_ms        INTEGER,
    self_consistency_mean REAL,    -- nullable: "not tested"
    self_consistency_min  REAL,    -- nullable
    entity_sanity_mean    REAL,
    main_attempts_count   INTEGER,
    verdict_passed        INTEGER, -- 0/1
    failed_gates_json     TEXT,
    errors_json           TEXT,
    PRIMARY KEY (run_id, model_id),
    FOREIGN KEY (run_id) REFERENCES bench_runs(run_id) ON DELETE CASCADE
);

-- Full-coverage health layer: one row per corpus node per model (main pass).
CREATE TABLE bench_node_enrichment (
    run_id             TEXT,
    model_id           TEXT,
    event_id           TEXT,   -- source-prefixed corpus node id, e.g. "claude-2853"
    source             TEXT,   -- shell | claude | browser | workflow
    schema_valid       INTEGER,
    refusal_detected   INTEGER,
    echo_similarity    REAL,
    entity_sanity      REAL,   -- per-attempt entity-type-sanity mean (nullable)
    latency_ms         INTEGER,
    timeout            INTEGER,
    parsed_output_json TEXT,   -- the model's parsed enrichment output, for eyeballing
    PRIMARY KEY (run_id, model_id, event_id),
    FOREIGN KEY (run_id) REFERENCES bench_runs(run_id) ON DELETE CASCADE
);

-- Headline leaderboard layer: one row per QA item per model per mode.
-- Keyed on qa_id (not golden_event_id): two questions can target the same node.
CREATE TABLE bench_node_retrieval (
    run_id          TEXT,
    model_id        TEXT,
    qa_id           TEXT,
    golden_event_id TEXT,   -- the corpus node the question targets (see producer change)
    mode            TEXT,    -- hybrid | semantic | lexical
    rank            INTEGER, -- NULL ⇒ not retrieved within k
    mrr             REAL,
    hit_at_1        INTEGER, -- 0/1, derived from per_item.hit_at_k[1]
    hit_at_10       INTEGER, -- 0/1, derived from per_item.hit_at_k[10]
    ndcg_at_10      REAL,
    PRIMARY KEY (run_id, model_id, qa_id, mode),
    FOREIGN KEY (run_id) REFERENCES bench_runs(run_id) ON DELETE CASCADE
);
```

Indexes: `bench_node_retrieval(golden_event_id, mode)` and
`bench_node_enrichment(event_id)` for the per-node leaderboard;
`bench_runs(started_at_iso)` for "latest run" and history ordering.

### Headline leaderboard query (latest run, history retained)

The leaderboard headline uses the **latest run** that scored a given model/node;
all previous runs stay in the DB for the history/trend view.

```sql
-- "best model on node X", latest run only
WITH latest AS (
  SELECT model_id, golden_event_id, mrr, hit_at_1,
         ROW_NUMBER() OVER (
           PARTITION BY model_id, golden_event_id, mode
           ORDER BY r.started_at_iso DESC
         ) AS rn
  FROM bench_node_retrieval nr
  JOIN bench_runs r USING (run_id)
  WHERE nr.golden_event_id = ? AND nr.mode = 'hybrid'
)
SELECT model_id, mrr, hit_at_1 FROM latest WHERE rn = 1 ORDER BY mrr DESC;
```

## Ingest

`results_store.ingest_run(jsonl_path, force=False)`:

1. Parse the JSONL stream by `record_type`: `run_manifest` → `bench_runs` row;
   `model_summary` → `bench_models` rows (+ verdict via `summary.compute_verdict`);
   `attempt` (purpose=`main`) → `bench_node_enrichment` rows; the
   `model_summary.downstream_proxy.per_item` flat list →
   `bench_node_retrieval` rows (`hit_at_1`/`hit_at_10` extracted from each
   item's `hit_at_k` dict; `golden_event_id` from the producer change below);
   `run_end` → fill `finished_at_iso`, `models_completed/errored`, `reason`.
2. **Idempotency**: if `run_id` already present and not `force`, no-op. With
   `force` (or first ingest), run a single transaction: `DELETE FROM bench_runs
   WHERE run_id=?` (cascades), then insert all rows. A run's JSONL is immutable
   after `run_end`, so this is safe and makes whole-DB rebuild deterministic.
3. **Partial/crashed JSONL** (no `run_end`): ingest what is present;
   `finished_at_iso` stays NULL and the run is rendered as "incomplete".
4. **Malformed lines**: skip + count + log a warning; never abort the whole
   ingest over one bad line.

### Producer change (one line, justified)

`downstream_proxy.run_downstream_proxy_pass` currently stamps each `per_item`
score with `qa_id` and `mode` but **not** `golden_event_id`. Add
`score["golden_event_id"] = item["golden_event_id"]` next to the existing
`score["qa_id"] = ...` (`downstream_proxy.py:158`). This makes each run's JSONL
self-describing — ingest never has to re-read the `eval-qa` fixture (which can be
relabeled between runs) to resolve a node id, and historical runs stay correct
even as labels evolve. Update the downstream_proxy golden/unit tests to expect
the new field.

Trigger points:
- **Auto**: end of `orchestrate_run`, after `writer.close()`, wrapped in
  try/except — a failed ingest logs and leaves the JSONL as fallback but does
  **not** fail the bench run (AP-1 spirit: a reporting concern must not break the
  primary path).
- **Manual**: `hippo-bench ingest <jsonl>` (one file), `--all` (scan `runs/` for
  `*.jsonl`), `--force` (re-ingest existing `run_id`s).

## Dashboard

`hippo-bench export-dashboard [--out <path>]` (default
`~/.local/share/hippo-bench/dashboard.html`). Queries `bench-results.db` via
`results_store` helpers, embeds the data as a `<script type="application/json">`
blob in a single self-contained HTML file (vanilla JS, no network, no server).
Regenerate after each run (or wire into the auto-ingest hook later). Three views:

1. **Leaderboard** — models ranked by aggregate retrieval MRR / Hit@1 for the
   selected run (default: latest). Enrichment gate rates as secondary columns.
2. **Per-node** — pick a corpus node → each model's retrieval rank/MRR (if
   QA-labeled) plus enrichment gates and `parsed_output_json` (always). This is
   the "best model per member of the corpus" view.
3. **Run history** — every run with `corpus_version`, timestamp, and a trend of
   headline metrics across runs.

## Testing

pytest, reusing existing JSONL golden fixtures (`test_bench_golden.py` and a
sample run file):

- ingest idempotency: re-ingest same `run_id` is a no-op; `--force` replaces.
- both-signal extraction correctness (enrichment rows from `attempt`; retrieval
  rows from `downstream_proxy.per_item`).
- partial JSONL (no `run_end`) → run marked incomplete, rows still ingested.
- malformed-line tolerance → skipped + counted, ingest completes.
- `export-dashboard` produces a self-contained HTML with the data blob embedded;
  deterministic output for a fixed DB.
- whole-DB rebuild from a folder of JSONLs is deterministic (`--all` + `--force`).

## Error Handling Summary

- Run-end auto-ingest wrapped: never fails the bench run; JSONL is the fallback.
- Nullable signals (`self_consistency_*`, `rank`, `entity_sanity`) stored as NULL,
  preserving the existing "not tested" semantics (`summary.compute_verdict` skips
  NULLs rather than failing).
- Foreign keys with `ON DELETE CASCADE` so `--force` re-ingest cleanly replaces a
  run's full row set in one transaction.

## Implementation Note: enrichment layer deferred

During implementation, the final review found that the bench pipeline does **not**
emit `attempt` records with `purpose = 'main'` over the full corpus — every
attempt is `purpose = 'self_consistency'`, and the self-consistency pass samples
only a few nodes, while the shadow brain's full-corpus enrichment is discarded
with the shadow stack. The `bench_node_enrichment` table and its ingest are
therefore **dormant on real runs** (correct, tested against synthetic `main`
attempts, but empty on real data). The retrieval layer — the agreed headline — is
unaffected and ships fully working.

Restoring full-corpus per-node enrichment (and, by the same root cause, the
currently-vacuous per-model gate scorecard) is tracked in
[#191](https://github.com/stevencarpenter/hippo/issues/191) and is the immediate
follow-up. The schema and ingest here are intentionally left in place so that work
is purely additive (emit the records; the datastore already consumes them).

## Open Questions / Future

- Store **raw** (un-parsed) enrichment output too? Deferred — `parsed_output_json`
  covers eyeballing; raw is bulky. Revisit if parsed proves insufficient.
- Auto-run `export-dashboard` inside the run-end hook vs. on demand? Start on
  demand; promote to auto if it proves useful.
- Retention/size policy for `bench-results.db`? Not needed initially — distilled
  per-node rows are small; the heavy scratch dirs in `runs/` are the size concern
  and are out of scope here.
