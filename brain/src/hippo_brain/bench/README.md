# hippo-bench

Local enrichment-model shakeout benchmark for hippo. Compares candidate models
loaded in **LM Studio** on a frozen corpus of real (redacted) hippo events, and
emits one JSONL file per run that downstream tooling can rank.

> **Status:** MVP. Tier 0 ("automated shakeout") only. Labeled-eval (Tier 1) and
> spot-check TUI (Tier 2) are roadmap. See
> [docs/superpowers/specs/2026-04-21-hippo-bench-design.md](../../../../docs/superpowers/specs/2026-04-21-hippo-bench-design.md)
> for the full design and the
> [veracity report in the audit commit](../../../../docs/superpowers/specs/2026-04-21-hippo-bench-design.md)
> for honest caveats.

---

## Why this exists

Hippo's enrichment pipeline calls a local LLM via LM Studio for every eligible
shell event, Claude session, browser visit, and workflow run. New local models
drop frequently and "should I switch?" had no principled answer. `hippo-bench`
gives a reproducible, local-only signal so the answer is grounded in numbers
on **your** hardware and **your** workload — not someone else's leaderboard.

## Hard requirements

- Python 3.14, `uv` (project tooling)
- LM Studio running locally with at least one model loaded
- The `lms` CLI on PATH (LM Studio CLI) — bench will refuse to run without it
- macOS / Apple Silicon target (other platforms work but `pmset`/`sysctl` checks degrade)

---

## 60-second onboarding

```bash
# 1. Make sure LM Studio is running and you have at least one model.
lms ls

# 2. Initialize a corpus from your live hippo DB.
uv run --project brain hippo-bench corpus init

# 3. Verify the corpus is intact.
uv run --project brain hippo-bench corpus verify

# 4. Run bench against a model that's already loaded.
uv run --project brain hippo-bench run --models qwen3.5-35b-a3b

# 5. Read the scorecard.
uv run --project brain hippo-bench summary ~/.local/share/hippo/bench/runs/run-*.jsonl
```

That's the loop. Add a new model to LM Studio, repeat step 4 with the new
identifier, see how it ranks against the prior baseline.

---

## What it measures (Tier 0 gates)

For each (model × event) the bench computes 5 deterministic signals:

| Gate | What | Default threshold | Rationale |
|---|---|---|---|
| **Schema validity** | Output parses + matches per-source schema | ≥ 95% | Can the model emit JSON-shaped enrichment under the contract? |
| **Refusal / pathology** | Detects refusal phrases, trivial summaries, echo-of-input | 0 refusals; echo sim < 0.5 | Filters models that decline or copy input |
| **Latency** | Wall time per call | p95 ≤ 60s | Pareto axis 1: speed |
| **Self-consistency** | Mean pairwise cosine of N runs of same input, in embedding space | ≥ 0.7 | Distinguishes converging models from flailing ones |
| **Entity-type sanity** | Heuristic checks on entity categories (files look like paths, etc.) | ≥ 90% | Catches models that dump prose into entity fields |

Headline metrics are computed over the **main pass only** (1 attempt per event).
Self-consistency uses a separate, marked pass that does NOT contaminate per-event
rates. See [`summary.py`](summary.py) for the math.

System metrics (RSS / CPU / load) are sampled every 250ms during a model's
active window and reported as peaks per model.

## What it does NOT measure (be honest)

- **Quality vs. ground truth.** Schema validity and entity sanity are structural;
  they can't tell you if the summary is *accurate*. Tier 1 (labeled eval) is the
  roadmap answer.
- **TTFT (time to first token).** Requires a streaming HTTP endpoint; we use
  non-streaming. The `ttft_ms` field is always `null`.
- **GPU utilization.** Apple Silicon GPU sampling needs `sudo powermetrics` —
  incompatible with headless runs.
- **Thermal isolation.** The cooldown loop waits for `load_avg_1m < 2.0`, which
  measures scheduler contention, not SoC temperature. Long back-to-back runs
  may see later models throttled.

---

## CLI reference

```
hippo-bench run --models m1,m2,... [--corpus-version v]
                [--base-url http://localhost:1234/v1]
                [--embedding-model text-embedding-nomic-embed-text-v2-moe]
                [--temperature 0.7]
                [--latency-ceiling-sec 60]
                [--self-consistency-events 5] [--self-consistency-runs 5]
                [--skip-checks] [--dry-run] [--out path]

hippo-bench corpus init [--corpus-version v] [--seed 42] [--db-path path]
                        [--shell N] [--claude N] [--browser N] [--workflow N]
                        [--no-filter-trivial]

hippo-bench corpus verify [--corpus-version v]

hippo-bench summary <run-file>
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Run completed successfully (or summary printed) |
| 1 | Generic failure (e.g., `corpus verify` mismatch) |
| 2 | Pre-flight aborted the run (e.g., `lms` missing, disk full) |
| 3 | Run executed but every candidate model errored |

### Important flags

- `--temperature` (default **0.7**): sampling temperature. At T<0.3 every model
  produces near-deterministic output, making self-consistency a vacuous signal.
  Set lower (e.g., 0.1) only when measuring raw determinism.
- `--no-filter-trivial`: by default `corpus init` excludes events that the
  production enrichment pipeline would skip (`hippo_brain.enrichment.is_enrichment_eligible`).
  Disable to test models on the noisy tail.
- `--skip-checks`: bypasses pre-flight (lms availability, power state, disk).
  Debugging only.
- `--dry-run`: resolves config, writes a `run_manifest` + `run_end` record, and
  exits without making any LM Studio calls. Useful for validating config.

---

## Output format

Each run writes a JSONL file under `~/.local/share/hippo/bench/runs/`. Four
record types, discriminated by `record_type`:

### 1. `run_manifest` (first line, exactly one per file)

```json
{
  "record_type": "run_manifest",
  "run_id": "run-20260421T120000-mac-studio-01",
  "started_at_iso": "2026-04-21T12:00:00Z",
  "finished_at_iso": null,
  "bench_version": "0.1.0",
  "lmstudio_version": "CLI commit: 0b2a176",
  "host": {
    "hostname": "...", "os": "darwin 25.4.0", "arch": "arm64",
    "cpu_brand": "Apple M5 Max", "total_mem_gb": 128.0
  },
  "preflight_checks": [
    {"check": "lms_cli", "status": "pass", "detail": "/usr/local/bin/lms"}
  ],
  "corpus_version": "corpus-v1",
  "corpus_content_hash": "sha256:...",
  "candidate_models": ["qwen3.5-35b-a3b", "gpt-oss-120b-mlx-crack"],
  "gate_thresholds": { "schema_validity_min": 0.95, ... },
  "self_consistency_spec": {
    "events": 5, "runs_per_event": 5, "temperature": 0.7
  }
}
```

### 2. `attempt` (many; one per model × event × attempt)

```json
{
  "record_type": "attempt",
  "run_id": "...",
  "model": {"id": "qwen3.5-35b-a3b"},
  "event": {"event_id": "shell-42", "source": "shell", "content_hash": "..."},
  "attempt_idx": 0,
  "purpose": "main",
  "timestamps": {"start_iso": "...", "ttft_ms": null, "total_ms": 8432},
  "raw_output": "...",
  "parsed_output": {"summary": "...", "entities": {...}},
  "gates": {
    "schema_valid": true, "schema_errors": [],
    "refusal_detected": false, "refusal_patterns_matched": [],
    "trivial_summary": false, "echo_similarity": 0.12,
    "entity_type_sanity": {"files": 0.95, "tools": 1.0},
    "call_error": null
  },
  "system_snapshot": {
    "lmstudio_rss_mb": 18432, "lmstudio_cpu_pct": 87.2,
    "load_avg_1m": 4.12, "mem_free_mb": 5821
  },
  "timeout": false
}
```

`purpose` is `"main"` (one attempt per event, used for headline metrics) or
`"self_consistency"` (multiple attempts of the same event, used only for the
SC cosine score).

### 3. `model_summary` (one per model, after that model's attempts)

```json
{
  "record_type": "model_summary",
  "run_id": "...",
  "model": {"id": "qwen3.5-35b-a3b"},
  "events_attempted": 40,
  "attempts_total": 65,
  "gates": {
    "schema_validity_rate": 0.975,
    "refusal_rate": 0.0,
    "echo_similarity_max": 0.18,
    "latency_p50_ms": 8432, "latency_p95_ms": 14201, "latency_p99_ms": 18000,
    "self_consistency_mean": 0.82, "self_consistency_min": 0.71,
    "entity_sanity_mean": 0.94,
    "main_attempts_count": 40,
    "sc_attempts_count": 25
  },
  "system_peak": {
    "lmstudio_rss_mb": 21453, "lmstudio_cpu_pct": 98.1,
    "load_avg_1m": 5.4, "mem_free_mb": 1200,
    "wall_clock_sec": 1854
  },
  "tier0_verdict": {
    "passed": true,
    "failed_gates": [],
    "skipped_gates": [],
    "notes": []
  },
  "cooldown_timeout": false
}
```

`self_consistency_mean` and `self_consistency_min` are `null` (not 0.0) when
SC was not run for that model. `compute_verdict` correctly skips null gates
rather than failing them.

### 4. `run_end` (last line, exactly one per file)

```json
{
  "record_type": "run_end",
  "run_id": "...",
  "finished_at_iso": "2026-04-21T13:42:11Z",
  "models_completed": ["qwen3.5-35b-a3b"],
  "models_errored": []
}
```

For dry-runs and aborted runs, `reason` is also present (`"dry_run"`,
`"preflight_aborted"`, `"no_models"`).

---

## Reading a run with `jq`

```bash
RUN=~/.local/share/hippo/bench/runs/run-20260421T120000-host.jsonl

# What models passed?
jq -r 'select(.record_type=="model_summary") | "\(.model.id)\t\(.tier0_verdict.passed)"' "$RUN"

# Schema validity per model
jq -r 'select(.record_type=="model_summary") | "\(.model.id)\t\(.gates.schema_validity_rate)"' "$RUN"

# Every refusal (rare, worth investigating)
jq 'select(.record_type=="attempt" and .gates.refusal_detected)' "$RUN"

# Worst-latency event for the slowest model
jq 'select(.record_type=="attempt" and .model.id=="my-slow-model")
    | {event:.event.event_id, ms:.timestamps.total_ms}' "$RUN" \
    | jq -s 'sort_by(-.ms) | .[0:5]'
```

---

## Module map

| Module | Responsibility |
|---|---|
| [`cli.py`](cli.py) | argparse entrypoint, exit-code semantics |
| [`config.py`](config.py) | `BenchConfig` + `DEFAULT_THRESHOLDS` |
| [`paths.py`](paths.py) | XDG path resolution for fixtures and runs |
| [`schemas.py`](schemas.py) | Per-source enrichment JSON schemas (shell/claude/browser/workflow) |
| [`gates.py`](gates.py) | Tier 0 gate functions; pure, deterministic, never raise |
| [`enrich_call.py`](enrich_call.py) | LM Studio HTTP client; classifies errors instead of raising |
| [`lms.py`](lms.py) | `lms` CLI wrapper (load/unload/list) |
| [`metrics.py`](metrics.py) | Background `MetricsSampler` thread (RSS, CPU, load, memory) |
| [`preflight.py`](preflight.py) | Individual pre-flight checks (lms, power, disk, hippo, spotlight) |
| [`corpus.py`](corpus.py) | Sampling, content-hashing, manifest, schema-mismatch tolerance |
| [`runner.py`](runner.py) | Main pass + self-consistency pass; gate composition |
| [`coordinator.py`](coordinator.py) | Per-model lifecycle (unload-all → load → warmup → run → unload → cooldown) |
| [`output.py`](output.py) | JSONL record dataclasses + append-only writer |
| [`summary.py`](summary.py) | Aggregate per-model gates from attempts; derive verdict |
| [`pretty.py`](pretty.py) | Text-table renderer for `hippo-bench summary` |
| [`orchestrate.py`](orchestrate.py) | Top-level: preflight + per-model loop + JSONL writes |

## Architecture in one diagram

```
                 hippo-bench run
                        │
                        ▼
              orchestrate.orchestrate_run
                        │
       ┌────────────────┼────────────────┐
       ▼                ▼                ▼
  preflight.run   corpus.load_corpus    For each candidate:
  _all_preflight                              │
  (lms, power,                                ▼
  disk, ...)                          coordinator.run_one_model
                                              │
                            ┌─────────┬──────┼──────┬──────────┐
                            ▼         ▼      ▼      ▼          ▼
                       lms.unload  metrics  runner.run_main_pass
                       lms.load    Sampler  runner.run_self_consistency
                                     │             │
                                     │             ▼
                                     │     enrich_call.call_enrichment
                                     │     enrich_call.call_embedding
                                     │             │
                                     │             ▼
                                     │     gates.check_*  → AttemptRecord
                                     ▼
                              system_snapshot
                                              │
                                              ▼
                              summary.aggregate_model_summary
                              summary.compute_verdict
                                              │
                                              ▼
                              output.RunWriter (JSONL append)
```

---

## Adding a new model

1. Pull the model into LM Studio (UI or `lms get <id>`).
2. Run: `hippo-bench run --models <new-model-id>`.
3. Wait. Each model takes roughly `(40 + 25) × per_call_latency` seconds (default
   ~30 minutes for a 5s/call model).
4. Inspect: `hippo-bench summary ~/.local/share/hippo/bench/runs/run-*.jsonl`.

To compare a new model to a previous baseline, run with both:
`--models qwen3.5-35b-a3b,new-model-id`. Bench loads/unloads each one cleanly.

---

## Adding a new source

If hippo gains a new source type (e.g., `gmail`):

1. Add a `SourceSchema` entry in [`schemas.py`](schemas.py) with required fields
   and entity categories.
2. Add a prompt template entry in [`enrich_call.py`](enrich_call.py).
3. Add a `_SOURCE_QUERIES` entry in [`corpus.py`](corpus.py) with the SELECT,
   payload-shape lambda, and eligibility-dict lambda.
4. Add a CLI flag `--gmail` in [`cli.py`](cli.py)'s `corpus init`.
5. Bump `corpus_version` to `corpus-v2` so prior runs aren't conflated.

The five gates work source-agnostically; no gate code changes needed.

---

## Veracity caveats — read before publishing any leaderboard number

These are documented in code comments and surface in the JSONL, but worth
calling out:

1. **Self-consistency at `--temperature 0.1`** is meaningless. Default 0.7.
2. **Cooldown is load-driven, not thermal** — back-to-back large models may show
   spurious latency regression on the second model.
3. **Schema validity excludes prose-wrapped JSON** less rigidly than spec
   suggests: we recover fenced blocks AND first balanced `{...}`. A model that
   emits *only* a refusal (no JSON) correctly fails.
4. **Entity-sanity heuristics encode taste.** Hand-review a sample before trusting.
5. **`reference_enrichment` is always null** (spec promised baseline capture; not
   implemented — needs hippo `knowledge_nodes` join logic).

Use the `tier0_verdict.skipped_gates` field to surface "didn't measure this" vs.
"failed this" in any leaderboard you publish.

---

## v2 Usage

hippo-bench v2 adds a shadow-stack architecture, time-bucketed corpus sampling,
downstream-proxy evaluation (Hit@K / MRR / NDCG), and automatic pause/resume of
the production brain during bench runs.

For the full design rationale see:
- v1 design: [`docs/superpowers/specs/2026-04-21-hippo-bench-design.md`](../../../../docs/superpowers/specs/2026-04-21-hippo-bench-design.md) (history-preserved)
- v2 design: [`docs/superpowers/specs/2026-04-27-hippo-bench-v2-design.md`](../../../../docs/superpowers/specs/2026-04-27-hippo-bench-v2-design.md)

### Prerequisites

```bash
# 1. Generate the v2 corpus (time-bucketed SQLite + JSONL sidecar).
#    Reads from your live hippo DB at ~/.local/share/hippo/hippo.db.
uv run --project brain hippo-bench corpus init --corpus-version corpus-v2

# 2. Seed the Q/A evaluation fixture (copies committed template to fixtures dir).
uv run --project brain python3 brain/src/hippo_brain/bench/qa_seed.py

# 3. Verify both artifacts are intact.
uv run --project brain hippo-bench corpus verify --corpus-version corpus-v2
```

Corpus artifacts land in `~/.local/share/hippo-bench/fixtures/` — a sibling of
the prod data directory, never a child. This is enforced by `bench/paths.py`.

### Running a v2 bench

```bash
# Basic: single model, v2 corpus.
uv run --project brain hippo-bench run \
  --models qwen3.5-35b-a3b \
  --corpus-version corpus-v2

# With downstream ask-synthesis sampling (keyword hit rate on Q/A items).
uv run --project brain hippo-bench run \
  --models qwen3.5-35b-a3b \
  --corpus-version corpus-v2 \
  --with-ask-synthesis

# Multiple models in one run (bench loads/unloads each cleanly).
uv run --project brain hippo-bench run \
  --models qwen3.5-35b-a3b,llama3.3-70b-mlx \
  --corpus-version corpus-v2 \
  --with-ask-synthesis
```

### Prod brain coordination

v2 automatically pauses the production `hippo-brain` enrichment loop before
spawning the shadow stack, then resumes it after the run completes. This prevents
the prod brain from consuming LM Studio capacity during the bench window.

- **Automatic** — no flags needed if the prod brain is running
- **Resume is best-effort** — registered via `atexit` so it fires even on crash
- **Override** — `--skip-prod-pause` bypasses pause/resume entirely (e.g., if
  the prod brain is not running or you're doing a dry-run):

```bash
uv run --project brain hippo-bench run \
  --models qwen3.5-35b-a3b \
  --corpus-version corpus-v2 \
  --skip-prod-pause
```

If the prod brain restarts during a bench run (e.g., due to launchd keepalive),
the `model_summary` record will include `"prod_brain_restarted_during_bench": true`.

### Results

Run output lands in `~/.local/share/hippo-bench/runs/<run_id>/<model_id>/`.
The top-level JSONL file (specified via `--out` or auto-named) contains the same
four record types as v1 (`run_manifest`, `attempt`, `model_summary`, `run_end`),
extended with v2 fields:

| New field | Where | Meaning |
|---|---|---|
| `bench_version` | `run_manifest` | `"0.2.0"` for v2 runs |
| `corpus_schema_version` | `run_manifest` | Schema version of the bench corpus SQLite |
| `eval_qa_version` | `run_manifest` | Q/A fixture version used |
| `host_baseline` | `run_manifest` | Load avg at run start |
| `prod_state_at_start` | `run_manifest` | Was prod brain running / paused? |
| `process_ready_ms` | `model_summary` | Time from shadow stack spawn to `/health` 200 |
| `queue_drain_wall_clock_sec` | `model_summary` | Wall time for enrichment queue to empty |
| `downstream_proxy` | `model_summary` | Hit@1/3/5/10, MRR, NDCG@10 per retrieval mode |
| `prod_brain_restarted_during_bench` | `model_summary` | Whether prod brain came back up during run |
| `timeout_during_drain` | `model_summary` | Whether drain hard-timeout (3600s) was hit |
| `prod_brain_resumed_ok` | `run_end` | Whether resume RPC succeeded after run |
| `models_with_prod_restart_event` | `run_end` | Models where prod restart was detected |

### Dry-run / config validation

```bash
uv run --project brain hippo-bench run \
  --dry-run \
  --models qwen3.5-35b-a3b \
  --corpus-version corpus-v2 \
  --skip-prod-pause \
  --out /tmp/test-dryrun.jsonl
```

Dry-run skips LM Studio, corpus, and shadow stack. It writes a `run_manifest`
with `bench_version="0.2.0"` and a `run_end` with `reason="dry_run"`, then exits
0. Use this to verify CLI config without a running model or corpus.

### New v2 CLI flags

| Flag | Default | Meaning |
|---|---|---|
| `--corpus-version corpus-v2` | `corpus-v2` | Select v2 corpus path and orchestrator |
| `--corpus-days N` | 90 | Days of history to sample from |
| `--corpus-buckets N` | 9 | Time buckets for even temporal coverage |
| `--shell-min / --claude-min / --browser-min / --workflow-min` | 50 | Per-source minimum event floor |
| `--skip-prod-pause` | false | Skip pause/resume of the prod brain |
| `--with-ask-synthesis` | false | Run ask-synthesis keyword hit rate pass |
| `--ask-synthesis-sample N` | 10 | Number of Q/A items to sample for synthesis |

### v2 Module map (additions)

| Module | Responsibility |
|---|---|
| [`corpus_v2.py`](corpus_v2.py) | Time-bucketed sampling, SQLite snapshot, JSONL sidecar, manifest |
| [`shadow_stack.py`](shadow_stack.py) | Process-group spawn (daemon + brain), env injection, SIGTERM/SIGKILL teardown |
| [`downstream_proxy.py`](downstream_proxy.py) | Q/A loading, Hit@K, MRR, NDCG, ask-synthesis sampling |
| [`coordinator_v2.py`](coordinator_v2.py) | Per-model v2 lifecycle (shadow stack, queue drain, downstream proxy) |
| [`preflight_v2.py`](preflight_v2.py) | v2 pre-flight checks (prod brain reachable, corpus schema, disk) |
| [`orchestrate_v2.py`](orchestrate_v2.py) | v2 top-level orchestrator (pause/resume, per-model loop, atexit resume) |
| [`output_v2.py`](output_v2.py) | v2 record dataclasses with extended fields |
| [`schemas_v2.py`](schemas_v2.py) | Corpus schema-version assertion against live hippo schema |
| [`qa_seed.py`](qa_seed.py) | Seeds `eval-qa-v1.jsonl` from committed template into fixtures dir |
| [`pause_rpc.py`](pause_rpc.py) | Thin HTTP client for `/control/pause` and `/control/resume` |
