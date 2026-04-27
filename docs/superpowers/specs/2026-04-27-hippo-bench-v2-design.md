# Hippo-Bench v2 — End-to-End Isolated Enrichment Benchmark

**Status:** Design (locked, ready for plan)
**Author:** steven + Claude (brainstorm session 2026-04-27)
**Supersedes:** Tier 0 portions of [`2026-04-21-hippo-bench-design.md`](2026-04-21-hippo-bench-design.md) (v1 remains the authority for the Tier 0 gate definitions; v2 reuses them unchanged and adds a downstream-proxy gate).
**Target branch:** TBD post-approval

## Motivation

`hippo-bench` v1 ranks candidate enrichment models against a 40-event redacted corpus, but it talks directly to LM Studio — it bypasses the daemon, the queue, embeddings, knowledge-node insertion, and retrieval. The metrics it emits answer "does this model produce well-shaped JSON quickly?" not "would this model make my hippo more useful?"

**v2 expands the bench in three dimensions:**

1. **End-to-end pipeline.** The bench runs hippo as close to production as practical — fresh ephemeral daemon + brain processes, real queue draining, real embedding pass, real sqlite-vec writes, real RRF + MMR retrieval scoring against a labeled Q/A set.
2. **Larger, more representative corpus.** ~1,000 events sampled with time-bucketed proportional stratification, plus a small adversarial overlay that grows organically.
3. **Strict isolation.** Bench enrichment never touches the user's personal hippo DB. Telemetry never commingles with prod. A bench run pauses prod's enrichment loop while it has LM Studio, and resumes prod cleanly at the end.

The user-facing question this answers: *"What does the speed × quality × system-load Pareto frontier look like for the models I can actually run on my Mac, on my real workload?"*

## Design Constraints (load-bearing — do not relax)

These were the user's explicit directives during the brainstorm session. Every decision below is downstream of these.

1. **Bench enrichment data MUST NOT end up in the user's personal hippo DB.** Separate XDG data tree, separate DB file, separate process tree.
2. **Telemetry MUST be separated from prod's** — every metric, log, and trace tagged so a Grafana filter cleanly excludes bench data from prod views and vice versa.
3. **The bench MUST run hippo as real as practical.** Real daemon, real brain, real queue, real embeddings, real retrieval. Capture path is the only path the bench may legitimately bypass (capture has its own integration tests; bench's signal is enrichment + downstream usefulness).
4. **The bench is single-tenant on this machine.** Two benches never run concurrently; a bench and prod-enrichment never run concurrently.
5. **Corpus is private.** Already-redacted at sample time, never committed, never copied off-host.
6. **Cross-run / cross-model fairness is paramount.** Comparing a fresh-boot run to a post-Crysis-load run is not acceptable. The architecture must make starting state byte-identical across runs of the same corpus version. (Active enforcement of baseline system load is a deferred follow-up; see Roadmap.)

## Architecture Overview

```
hippo-bench run --models a,b,c
        │
        ▼
  Pre-flight (LM Studio, lms CLI, disk, prod-brain reachable, snapshot present)
        │
        ▼
  Pause prod brain (RPC)        ──────► prod daemon keeps capturing
        │                                shell/claude/browser events
        ▼
  For each candidate model M:
        │
        ├─ mkdir ~/.local/share/hippo-bench/runs/<run-id>/<M>/
        ├─ cp corpus-v2.sqlite      ──► <run-tree>/hippo.db
        ├─ Spawn shadow hippo-daemon (XDG_DATA_HOME=<run-tree>)
        ├─ Spawn shadow hippo-brain  (XDG_DATA_HOME=<run-tree>)
        │                               OTEL_RESOURCE_ATTRIBUTES=
        │                               service.namespace=hippo-bench,
        │                               bench.run_id=<rid>,
        │                               bench.model_id=<M>
        ├─ Wait for brain ready (HTTP /health or socket probe)
        ├─ Trigger 3 warmup enrichments (discarded)
        ├─ Start metrics sampler thread
        ├─ Wait for queue drain (poll bench DB)        ─── timed window
        ├─ Run downstream-proxy retrieval pass         ─── labeled Q/A
        ├─ Run self-consistency pass (5 events × 5 runs)
        ├─ Stop sampler
        ├─ SIGTERM brain → SIGTERM daemon → wait → SIGKILL if stuck
        ├─ Append model_summary record to JSONL
        └─ Cooldown until load_avg_1m settles (cap 90s)
        │
        ▼
  Resume prod brain (RPC)
        │
        ▼
  Append run_end record to JSONL
```

### Why per-MODEL fresh process tree (not per-INVOCATION)

A `hippo-bench run --models a,b,c` invocation is *one* user action, but produces *three* hermetic process-tree lifecycles. Each model gets:

- A fresh copy of `corpus-v2.sqlite` at `~/.local/share/hippo-bench/runs/<rid>/<model>/hippo.db`
- A fresh `hippo-daemon` and `hippo-brain` process spawned with that XDG tree
- A clean Python heap, fresh Rust jemalloc arena, cold SQLite page cache, virgin FTS5 segment topology, virgin sqlite-vec vec0 index
- Resource attributes tagging every span/log/metric with `bench.model_id=<that-model>` for that process tree's lifetime

The `bench.run_id` is shared across all three models in a single invocation (so dashboards can group them) but `bench.model_id` is process-tree-scoped (so dashboards can pivot).

**Why this matters for measurement validity:** if model B inherited model A's enriched DB, model B's downstream-proxy precision@K would reflect the union of enrichment quality, not B's contribution alone. If model B inherited model A's warm SQLite page cache, B's tail latency would be artificially better. The ephemeral-per-model boundary makes those confounders impossible.

### Why ephemeral (no launchd) for shadow stack

Decided 4-1 by the design panel. The bench's purpose is measurement validity for enrichment quality + speed + system load. Persistent shadow processes accumulate invisible state (page cache, jemalloc arenas, FTS5 segment merges, sqlite WAL state) across runs. Hermeticity is a benchmark *primitive*, not a nice-to-have.

**Accepted tradeoff:** spawn time adds variance to the first events of each model's window. Mitigated by 3 warmup enrichments (discarded) before the timed window opens, and by recording `process_ready_ms` in the manifest as a separate observability field that is *not* included in p95 latency calculations.

**Explicit non-goals carried by this choice:**

- The bench does not exercise launchd-restart paths. Crash-recovery resilience is a daemon reliability concern with its own integration test suite.
- The bench does not test live Firefox extension native-messaging. Browser source events come from the corpus snapshot just like every other source. Live extension testing is a manual e2e checklist.

## Corpus

### Composition (Q1: hybrid)

~1,000 events total per corpus version, split:

- **~950 stratified time-bucketed proportional sample** from the user's prod hippo DB, drawn over the last 90 days in 9 weekly buckets. Within each bucket, sample proportionally to source distribution but with a per-source minimum floor of 50 events to guarantee diagnostic signal for low-volume sources (typically `workflow`).
- **Up to 50 adversarial overlay events** in a separate `corpus-v2.overlay.sqlite` snapshot. **Initially empty (0 events)** — there is no upfront curation. Grows organically as the user encounters real failures (`hippo-bench corpus add-adversarial <event-id-from-prod-db> --reason "model X marked benign shell as refused"`). Capped at 50 to prevent the overlay from overwhelming the proportional sample's signal (further additions require evicting an older overlay event). Scored *separately* from headline metrics so overlay growth never breaks corpus version comparability.

### Sampling rules

- Stratified random per (source × week) with a fixed seed (default 42, recorded in manifest).
- Re-applies `redaction.py` at sample time (raw never persisted).
- Skips: events shorter than per-source minimum, events flagged as high-severity-redacted, events with `probe_tag` set (synthetic probes are filtered out of every user-facing query and must be filtered out of the corpus too).
- Skips: events that the production enrichment pipeline would itself skip via `is_enrichment_eligible()` from `brain/src/hippo_brain/enrichment.py:32`.
- Configurable per-source minimums (`--shell-min 50 --claude-min 50 --browser-min 50 --workflow-min 50`) with `--corpus-days 90 --corpus-buckets 9` window controls. Defaults are the lock.

### Storage format (Q4: snapshot + sidecar)

Two artifacts produced atomically by `hippo-bench corpus init`:

- **`~/.local/share/hippo-bench/fixtures/corpus-v2.sqlite`** — the runtime artifact. Contains rows in `shell_events`, `claude_session_segments`, `browser_events`, `workflow_runs`, plus matching rows in the four `*_enrichment_queue` tables. Schema-version pinned in a `corpus_meta` table; bench asserts compatibility with the live hippo schema at run start.
- **`~/.local/share/hippo-bench/fixtures/corpus-v2.jsonl`** — the human-inspection sidecar. One JSON object per event with `event_id`, `source`, `redacted_content`, `content_sha256`, `bucket_index`, `sampled_at_iso`. Diffable in PRs (if the corpus is ever made public, which it currently is not), `head -743 | tail -1` works for "what was event 743?" debugging.

`corpus init` writes both atomically and asserts they encode identical event sets. `corpus verify` re-checks both against the manifest's content hashes.

### Versioning policy (Q10)

- Frozen until explicit `corpus init --bump-version v3`. Re-running `corpus init` without `--force` and without a version bump fails.
- Adversarial overlay grows without forcing a version bump (overlay scored separately).
- Schema migration in main hippo (e.g., new columns) requires a corpus rebuild before bench-v2 is run on the new schema. Asserted at run start (`assert snapshot.schema_version == hippo.schema_version`); fails fast otherwise.

### Manifest

`~/.local/share/hippo-bench/fixtures/corpus-v2.manifest.json` — records seed, filter spec, source counts, bucket spec, schema version, generated-at-iso, content hashes for both `.sqlite` and `.jsonl`. Bench `run_manifest` records `corpus_version`, `corpus_content_hash` (= sha256 of the .sqlite file), and `corpus_schema_version`. Cross-version comparison is non-comparable by construction.

## Per-Model Run Lifecycle

Per the architecture diagram. Concrete invariants the implementation MUST satisfy:

- **Pre-flight aborts before any model runs** if: LM Studio unreachable, `lms` CLI missing, prod brain pause RPC fails, disk free < 2GB on `~/.local/share/hippo-bench`, snapshot schema mismatch, snapshot file missing or hash mismatch.
- **Prod brain pause RPC is required** (see Q3 below). If prod brain isn't running at all, that's *acceptable* (user manually stopped it) — pre-flight records the state but doesn't abort. If prod brain *is* running but unreachable for pause, the bench aborts (rather than risk LM Studio contention).
- **`lms unload --all` runs before each candidate model is loaded.** Clean VRAM baseline, no inheritance of prior model's GPU state.
- **3 warmup enrichments per model are discarded.** They run after model load but before the timed window opens; their results never appear in `model_summary.gates`.
- **The metrics sampler thread starts after warmup.** It samples every 250ms (carry forward from v1) — process RSS, CPU%, system load_avg_1m, mem_free, page cache pressure. Apple Silicon GPU/power/thermal capture is deferred (see Roadmap; depends on macmon).
- **Queue drain detection:** poll `SELECT COUNT(*) FROM <source>_enrichment_queue WHERE status IN ('pending','processing')` across all four sources every 2s; window closes when total = 0 for two consecutive polls. Hard timeout: 4× expected wall time, then close window with `model_summary.timeout_during_drain=true`.
- **Downstream-proxy pass runs after main drain.** Q/A iteration is single-threaded against the bench's local retrieval API — no contention with main pass.
- **Self-consistency pass runs last.** 5 events × 5 runs against the same model; uses the existing `runner.run_self_consistency` from v1.
- **Tear-down is process-group-based.** Bench harness spawns daemon + brain in their own process group; on completion or interrupt, the harness sends SIGTERM to the group, waits 10s, sends SIGKILL. **Required to prevent orphans** if the bench process is killed mid-run (DevX panel called this out as the biggest accepted risk for ephemeral spawn).

## Production Hippo Coordination (Q3)

### Pause RPC contract

Add a `pause` / `resume` RPC to `hippo-brain`'s existing internal API surface (it already has an HTTP server for `/health` and queries — extend with `POST /control/pause` and `POST /control/resume`).

- **`POST /control/pause`** — brain finishes the in-flight enrichment, then enters a paused state where the enrichment loop sleeps. The capture daemon is NOT paused; the queue may grow during the bench window. Returns 200 with `{"paused_at": <iso>, "in_flight_finished": <bool>}`. Idempotent.
- **`POST /control/resume`** — brain exits paused state and resumes the enrichment loop. Returns 200 with `{"resumed_at": <iso>}`. Idempotent.
- **Persistence:** pause state lives in memory only. If prod brain restarts mid-bench (launchd `KeepAlive` kicks in), it comes back unpaused — the bench detects this on its next pre-resume health probe and aborts mid-run with `prod_brain_restarted_during_bench=true`. Not silently recoverable; the run is invalidated because LM Studio contention may have occurred.

### Pause/resume sequencing

```
Bench start:
  1. Probe prod brain /health → record state
  2. POST /control/pause → assert 200
  3. Probe again to confirm paused
  4. Begin model 1
       ↓ (run for hours)
Bench end (success):
  5. Probe prod brain /health → confirm still alive and paused
  6. POST /control/resume → assert 200

Bench end (interrupted):
  5'. Best-effort POST /control/resume on every exit path (atexit handler)
  6'. Operator may need to /control/resume manually if bench died hard
```

### Why not just stop prod via `launchctl bootout`

Two reasons:
1. The capture daemon must keep running so shell/Claude/browser events aren't lost during the bench window. Pausing brain only is the smaller blast radius.
2. `launchctl` operations on prod's `com.hippo.brain` label require the bench to manipulate launchd state, which is a meaningful permissions surface. The pause RPC is in-band and stays inside the brain's existing HTTP server.

### Manual override

`hippo-bench run --skip-prod-pause` skips the pause/resume RPCs. Bench refuses to run without `--skip-prod-pause` if prod brain is reachable AND the user did not explicitly pass `--auto-pause` (default). Also, if prod brain is verifiably stopped (`launchctl print com.hippo.brain` returns no PID), pre-flight records the state and proceeds without RPC calls — this is the legitimate "user already stopped prod" flow.

## Telemetry Isolation (Q5)

### Namespace strategy

Single OTel collector stack (`otel/docker-compose.yml` unchanged). Separation happens via resource attributes at producer side, queried via Grafana filters at consumer side.

### Per-process resource attributes (set via `OTEL_RESOURCE_ATTRIBUTES` env)

```
service.namespace = "hippo-bench"
service.name      = "hippo-bench-daemon" | "hippo-bench-brain"
bench.run_id      = "run-<iso-ts>-<host>"
bench.model_id    = "<model-being-tested>"
bench.corpus_version = "corpus-v2"
```

`service.namespace` is the primary filter key. The `bench.*` attributes enable per-model and per-run dashboard pivots without span-level cardinality explosions (resource attributes are de-duplicated by the collector; span attrs would balloon).

### Required code changes

**`crates/hippo-daemon/src/telemetry.rs`** currently does NOT pick up `OTEL_RESOURCE_ATTRIBUTES`:

```rust
// Current — bypasses env detection
fn resource(service_name: &str) -> Resource {
    Resource::builder()
        .with_service_name(service_name.to_string())
        .build()
}
```

**Required fix** — merge `EnvResourceDetector`:

```rust
fn resource(service_name: &str) -> Resource {
    Resource::builder()
        .with_service_name(service_name.to_string())
        .with_detectors(&[Box::new(opentelemetry_sdk::resource::EnvResourceDetector::new())])
        .build()
}
```

This is a prerequisite for namespace gating. Without it, the bench's env-injected `service.namespace` is silently ignored on the Rust side and prod telemetry pollutes bench dashboards (or vice versa).

**`brain/src/hippo_brain/telemetry.py`** — verify that `Resource.create()` (or equivalent) is used so the Python SDK's default detector chain picks up the env var. Implementation phase: read the file, fix if not.

### Per-attempt span attributes (vary per call)

```
bench.event_id          = "shell-42" | ...
bench.attempt_purpose   = "warmup" | "main" | "self_consistency" | "downstream_proxy" | "ask_synthesis"
bench.source            = "shell" | "claude" | "browser" | "workflow"
bench.attempt_idx       = 0..N
```

These are span attrs (not resource) because they vary per call. Cardinality control: `bench.event_id` is high-cardinality (1000 distinct values per run); fine for traces (Tempo handles per-span cardinality), but MUST NOT be exposed as a Prometheus metric label (would explode metric series). Bench code must enforce this — only span-level use of `event_id`.

### Collector config

**No change to `otel/otelcol-config.yml`.** No transform processor, no relabel rule, no tenant header. Separation is query-side.

### Grafana

New folder `Hippo Bench` with three new dashboards:

1. **`bench-run-overview.json`** — per-`bench.run_id` view: candidate models side-by-side, p50/p95 latency, schema validity, downstream-proxy precision@K, system peaks. Filtered `{service_namespace="hippo-bench", bench_run_id="$run_id"}`.
2. **`bench-model-drilldown.json`** — per-`bench.model_id` deep dive: per-event latency distribution, system metrics over time, traces. Filtered `{service_namespace="hippo-bench", bench_model_id="$model_id"}`.
3. **`bench-model-comparison.json`** — multi-model side-by-side panels for choosing the winner across runs.

### Prod dashboards must be updated

Existing dashboards under `otel/grafana/dashboards/` typically don't filter on `service_namespace`. During a bench run, bench data would commingle into prod views unless explicitly excluded. **Add `{service_namespace=""}` filter to every existing prod dashboard panel** as part of the bench-v2 work — not a follow-up. This is a load-bearing constraint of the design (the user's "EVERY LAST METRIC, LOG, TRACE separate"). Dashboard files affected: TBD by implementation phase grep, expected ~3-5 files.

### Deferred (telemetry roadmap)

- Prometheus retention rules per namespace. Defer until disk pressure observed.
- Loki tenant header (`X-Scope-OrgID`) for stricter log-side isolation. Current Loki config doesn't enforce tenants; defer until needed.
- Alert relabel rules. Currently zero prod alerts; nothing to filter.

## Gates

### Tier 0 (carried forward unchanged from v1)

See [`2026-04-21-hippo-bench-design.md`](2026-04-21-hippo-bench-design.md) §"Tier 0 Gates". v2 reuses the same five gates with the same defaults:

| Gate | Threshold |
|---|---|
| Schema validity | ≥ 95% |
| Refusal/pathology (regex + echo cosine) | 0 refusals, echo sim < 0.5 |
| Latency p95 (per-event wall time, excludes warmup + `process_ready_ms`) | ≤ 60s |
| Self-consistency (5 events × 5 runs, mean pairwise cosine) | ≥ 0.7 |
| Entity-type sanity (per-source heuristics) | ≥ 90% |

### Downstream-proxy gate (NEW in v2)

This is the headline addition. Measures whether a model's enrichment makes hippo's retrieval *useful* — answering the user's "ability to use semantic search, MCP, regex" axis.

#### Q/A set

- **Storage:** `~/.local/share/hippo-bench/fixtures/eval-qa-v1.jsonl`. **Target ≥ 100 questions** — seeded from existing `brain/tests/eval_questions.json` (~40) and extended during implementation to cover all four sources (target distribution: 40 shell, 30 claude, 20 browser, 10 workflow). Questions are user-curated, not auto-generated; they reflect real "things I'd ask hippo."
- **Schema per Q/A item:**
  ```json
  {
    "qa_id": "qa-001",
    "question": "Which command did I run to migrate vec0 to a new dimension?",
    "golden_event_id": "shell-12345",
    "source_filter": "shell",
    "acceptable_answer_keywords": ["migrate-vectors", "vec_dim", "768"],
    "tags": ["lookup", "single-event"]
  }
  ```
- **Filter at run start:** items whose `golden_event_id` is not in the active corpus snapshot are skipped (logged as `qa_filtered_count`). Cross-corpus-version Q/A reuse is therefore safe.
- **Versioning:** `eval_qa_version` recorded in run manifest. Bumping the Q/A set bumps the version; cross-version Q/A scores are not comparable.

#### Per-model evaluation flow

After the main pass enrichment + embedding completes for model M:

1. For each Q/A item, run `retrieval.search(query=qa.question, mode=mode)` from `brain/src/hippo_brain/retrieval.py` against the bench DB. Modes: `hybrid`, `semantic`, `lexical`. Top-K results captured (K=10).
2. Score each retrieval:
   - **Hit@K** (K ∈ {1, 3, 5, 10}): is `golden_event_id` in the top-K?
   - **MRR**: 1/rank if found, else 0. Mean across all Q/A.
   - **NDCG@10**: binary relevance (golden = 1, others = 0). Mean across all Q/A.
3. Aggregate per-mode and write to `model_summary.downstream_proxy`:
   ```json
   {
     "downstream_proxy": {
       "qa_count": 87,
       "qa_filtered_count": 13,
       "modes": {
         "hybrid":   { "hit_at_1": 0.62, "hit_at_5": 0.81, "mrr": 0.71, "ndcg_at_10": 0.74 },
         "semantic": { "hit_at_1": 0.55, "hit_at_5": 0.78, "mrr": 0.66, "ndcg_at_10": 0.70 },
         "lexical":  { "hit_at_1": 0.41, "hit_at_5": 0.62, "mrr": 0.50, "ndcg_at_10": 0.55 }
       }
     }
   }
   ```

#### Why this controls confounds

- The **embedding model is held constant** across candidate enrichment LLMs within a single run. Hit@K differences between candidates therefore reflect enrichment quality, not embedding quality. This is the core invariant — must be enforced in code by reading the bench's embedding model from a single config value (`--embedding-model`, default pinned), not from the candidate model's metadata.
- The **DB is fresh per model** (snapshot copy + per-model spawn). Retrieval scores are not contaminated by prior models' enrichments.
- The **same Q/A set runs against every model** at the same run.

#### Cross-run comparability rule (critical)

Downstream-proxy scores are cross-run-comparable **only when the comparability tuple matches**:

```
(corpus_version, eval_qa_version, embedding_model)
```

All three are recorded in `run_manifest`. Any analyst tooling that aggregates downstream-proxy across runs MUST filter to a single tuple value. Changing any of the three (corpus bump, Q/A set extension, embedding swap) invalidates cross-run comparison for downstream-proxy specifically — the Tier 0 gates remain comparable across `corpus_version` matches because they don't depend on embeddings.

#### Optional `hippo ask` synthesis check

For a 10-Q/A sample (deterministic by `qa_id` modulo), run the full `ask` RAG pipeline and check whether `acceptable_answer_keywords` appear in the synthesis. Tagged `bench.attempt_purpose=ask_synthesis`. Slow (calls the candidate model again for synthesis), so sample-based not exhaustive. Recorded under `model_summary.downstream_proxy.ask_synthesis = { "sampled": 10, "keyword_hit_rate": 0.7 }`.

#### Defaults & thresholds

No threshold-based pass/fail for the downstream-proxy gate in v2. The numbers are reported and surfaced in the leaderboard; the user judges acceptable trade-offs against speed/load. Threshold-based gating is a roadmap item once enough cross-model data exists to set defensible defaults.

## Output Format

JSONL records under `~/.local/share/hippo-bench/runs/run-<iso>-<host>.jsonl`. Carry forward v1's four record types (`run_manifest`, `attempt`, `model_summary`, `run_end`) with these v2 deltas:

### `run_manifest` deltas

```json
{
  "record_type": "run_manifest",
  "bench_version": "0.2.0",
  "corpus_version": "corpus-v2",
  "corpus_content_hash": "sha256:...",
  "corpus_schema_version": 8,
  "eval_qa_version": "eval-qa-v1",
  "embedding_model": "text-embedding-nomic-embed-text-v2-moe",
  "host_baseline": {
    "load_avg_1m_at_start": 0.42,
    "load_avg_5m_at_start": 0.51
    // SoC temp at start: deferred (macmon), see roadmap
  },
  "prod_state_at_start": {
    "brain_pid": 12345,
    "brain_paused": true,
    "daemon_pid": 12340,
    "daemon_running": true
  },
  // ... carry-forward fields from v1
}
```

### `attempt` deltas

Add `bench.attempt_purpose` ∈ `{"warmup", "main", "self_consistency", "downstream_proxy", "ask_synthesis"}` (warmup attempts emitted but flagged so summary code excludes them).

### `model_summary` deltas

```json
{
  "record_type": "model_summary",
  // ... v1 carry-forward (gates, system_peak, tier0_verdict)
  "process_ready_ms": 4231,            // spawn time, NOT counted in p95
  "queue_drain_wall_clock_sec": 1854,
  "downstream_proxy": { ... },         // see Gates section above
  "prod_brain_restarted_during_bench": false,
  "timeout_during_drain": false
}
```

### `run_end` deltas

Add `prod_brain_resumed_ok: true|false` and `models_with_prod_restart_event: [...]`.

## CLI

Carry forward v1's CLI surface. v2 deltas:

```
hippo-bench corpus init [--corpus-version v]
                        [--corpus-days 90]
                        [--corpus-buckets 9]
                        [--shell-min 50] [--claude-min 50]
                        [--browser-min 50] [--workflow-min 50]
                        [--seed 42]
                        [--db-path path]
                        [--bump-version v3]   # required to overwrite

hippo-bench corpus add-adversarial <event-id-from-prod-db>
                                   --reason "<text>"
                                   [--source shell|claude|browser|workflow]

hippo-bench corpus verify [--corpus-version v]

hippo-bench run --models m1,m2,...
                [--skip-prod-pause]
                [--auto-pause]                # default behavior
                [--with-ask-synthesis]        # default off
                [--ask-synthesis-sample 10]
                [--corpus-version v]
                [--out path]
                [--dry-run]

hippo-bench summary <run-file>                # carry forward
```

`hippo daemon install` gains a flag to write the OTel `EnvResourceDetector` reminder into the install report (post-impl detail; not load-bearing for the design).

## Testing Plan

New tests:

- `brain/tests/test_bench_corpus_v2.py` — snapshot determinism, JSONL/SQLite-equivalence, schema-version assertion at load, time-bucketed sampling correctness
- `brain/tests/test_bench_shadow_stack.py` — process-group spawn/teardown, orphan prevention, `process_ready_ms` recorded, OTel env vars correctly inherited
- `brain/tests/test_bench_pause_rpc.py` — pause/resume RPC against a stub brain server, mid-run-restart detection, `--skip-prod-pause` path
- `brain/tests/test_bench_downstream_proxy.py` — Q/A filtering, Hit@K computation, MRR computation, mode-aware retrieval
- `brain/tests/test_bench_telemetry_isolation.py` — assert spawned process env contains `OTEL_RESOURCE_ATTRIBUTES`, assert `service.namespace` ends up on emitted spans (mock OTel collector)
- `crates/hippo-daemon/tests/telemetry_env_resource_test.rs` — assert `EnvResourceDetector` merge picks up `OTEL_RESOURCE_ATTRIBUTES`

E2E smoke: `hippo-bench run --dry-run --models qwen3.5-35b-a3b` writes a complete `run_manifest` + `run_end` without LM Studio calls.

## Non-Goals (v2 scope discipline)

Explicitly out of scope; do not creep:

- **Launchd-restart resilience testing.** Daemon resilience belongs in a separate integration suite.
- **Live Firefox extension native-messaging.** Browser source events come from the corpus snapshot. Live extension testing is a manual e2e checklist.
- **Apple Silicon GPU / power / thermal capture.** Deferred — depends on macmon. See Roadmap.
- **Baseline-system-load gate.** Deferred — see Roadmap. v2 records `load_avg_1m_at_start` for post-hoc filtering, but does not block runs on it.
- **Cross-machine comparability.** Each run is scoped to its host.
- **Pareto plotting / leaderboard.json.** v2 emits the JSONL data; plotting is a downstream `leaderboard-analyst` agent (see v1 roadmap).
- **Pause RPC persistence to disk.** In-memory only; restart of prod brain mid-bench invalidates the run.
- **Threshold-based gating on downstream-proxy metrics.** Reported only; no pass/fail until enough cross-model data exists to set defensible thresholds.
- **`hippo-bench` MCP server.** Overkill until ≥10 accumulated runs.
- **Pause/resume of bench runs themselves.** A run is a contiguous block; if interrupted, partial JSONL is parseable and you re-run.

## Roadmap (post-v2 follow-ups)

In approximate priority order. These have **already been spawned as background tasks** with self-contained prompts that wait on v2 isolation work to land first:

1. **Baseline-system-load gate** (spawned). Block run start until `load_avg_1m`, CPU%, and (if macmon present) SoC temperature settle to defined baselines, or proceed-and-tag on timeout. Eliminates the "fresh-boot vs. post-Crysis" confounder the user explicitly named.
2. **macmon-based GPU/power/thermal capture** (spawned). Apple Silicon SoC power, GPU%, fan RPM, SoC temp — all sudoless via macmon's Prometheus endpoint. Adds the "doesn't peg my fans" axis to the Pareto frontier.
3. **Pareto / leaderboard agent.** Reads accumulated JSONL runs, emits the Markdown leaderboard with quality × speed × load curves; flags regressions.
4. **Tier 1 labeled evaluation.** Hand-labeled entity F1 + keyword-hit scoring against a labeled fixture (separate from the downstream-proxy Q/A set).
5. **Bench-diagnostician agent.** When a run has outliers, reviews JSONL, cross-references prior runs, explains anomalies.
6. **Threshold-based downstream-proxy gating.** Once ~5+ candidate models have run on the same corpus version, derive defensible thresholds for Hit@K / MRR.

## Open Questions / Risks

- **Pause RPC reliability.** Prod brain crashing mid-bench (launchd `KeepAlive` resurrects it) invalidates the run. Mitigated by per-2-min health-probe-and-reassert during long runs (ratchet that fact into the manifest if detected). Alternative: take a process-watcher dependency. Defer until first time it actually bites.
- **OTel collector backpressure.** A bench run produces materially more spans/metrics than prod (per-event spans × N candidate models). If the collector falls behind, the bench's measured latency might inadvertently include OTel batching backpressure. Mitigation: bench measures latency from monotonic clock pre-call to post-call, NOT from span timing. Already the v1 convention.
- **Q/A set drift.** As hippo's surface area grows, the Q/A set's coverage shifts. Cross-version comparisons of `downstream_proxy` numbers between Q/A v1 and Q/A v2 are non-comparable; bumping `eval_qa_version` is permanent.
- **Embedding model swap.** v2 holds the embedding model constant. If the user later swaps embedders (e.g., to Qwen3-Embedding-0.6B), all prior `downstream_proxy` numbers are non-comparable. Not a v2 problem; a `bench-evolution` problem.
- **Snapshot regeneration cost.** Every schema migration in main hippo forces a full corpus rebuild. Time cost: minutes (sampling + redaction). Acceptable.
- **Dashboard updates as part of v2 ship.** Updating prod dashboards to filter `{service_namespace=""}` is load-bearing and must be in the implementation plan, not a follow-up. Risk: forgotten and bench data commingles in prod views.

## Acceptance Criteria

For v2 to be declared shipped:

1. `hippo-bench corpus init` produces both `corpus-v2.sqlite` and `corpus-v2.jsonl` atomically, both passing `corpus verify`.
2. A clean `hippo-bench run --models <one-loaded-model>` invocation produces a JSONL with one `run_manifest`, ≥ 1000 `attempt` records (warmup + main + self-consistency + downstream-proxy), one `model_summary`, and one `run_end` record. Schema validates against the spec's record shapes.
3. Pre-flight aborts cleanly with informative exit code when (a) prod brain unreachable for pause, (b) snapshot schema mismatch, (c) LM Studio absent.
4. Bench DB rows never appear in `~/.local/share/hippo/hippo.db` (asserted in test by sha256-checking the prod DB before/after a bench run).
5. Telemetry isolation: a Grafana query for `{service_namespace=""}` returns zero hippo-bench data during a bench run (asserted by an integration test that reads the local Prometheus directly).
6. Prod dashboards updated; prod brain returns to enrichment loop within 10s of `run_end`.
7. Downstream-proxy metrics (`hit_at_1`, `mrr`, `ndcg_at_10`) populated for ≥ 80% of Q/A items (the rest filtered by missing-from-corpus rule).
8. Tear-down leaves no orphan `hippo-daemon` or `hippo-brain` processes after `pkill -0` check.
9. All existing v1 tests still pass; v2 tests pass.
10. Documentation: `brain/src/hippo_brain/bench/README.md` updated with v2 usage; existing v1 design doc cross-linked but not edited (history-preserving).
