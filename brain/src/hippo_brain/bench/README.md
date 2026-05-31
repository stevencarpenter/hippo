# hippo-bench

Local enrichment-model shakeout benchmark for hippo. Compares candidate models
loaded in **LM Studio** against a frozen corpus of real (redacted) hippo events,
spawning a shadow stack (daemon + brain) that drains an enrichment queue
identical in shape to production. Emits one JSONL file per run that downstream
tooling can rank.

> **Status:** Tier 0 ("automated shakeout") + downstream-proxy retrieval
> scoring. Tier 1 (labeled-eval ground truth) is roadmap — see
> [issue #133](https://github.com/stevencarpenter/hippo/issues/133) for what
> blocks BT-29 metrics from being end-to-end producible.

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
- A live hippo DB at `~/.local/share/hippo/hippo.db` to seed the corpus from
- macOS / Apple Silicon target (other platforms work but `pmset`/`sysctl`
  checks degrade)

---

## 60-second onboarding

```bash
# 1. Generate a corpus (time-bucketed SQLite + JSONL sidecar) from your live hippo DB.
uv run --project brain hippo-bench corpus init

# 2. Seed the Q/A evaluation fixture (copies committed template into fixtures dir).
uv run --project brain python3 brain/src/hippo_brain/bench/qa_seed.py

# 3. Verify both artifacts.
uv run --project brain hippo-bench corpus verify

# 4. Run bench against a model that's already loaded.
uv run --project brain hippo-bench run --models qwen3.5-35b-a3b

# 5. Read the scorecard.
uv run --project brain hippo-bench summary ~/.local/share/hippo-bench/runs/run-*.jsonl
```

That's the loop. Add a new model to LM Studio, repeat step 4 with the new
identifier, see how it ranks against the prior baseline.

Corpus artifacts land in `~/.local/share/hippo-bench/fixtures/` — a sibling of
the prod data directory, never a child. This is enforced by `bench/paths.py`.

---

## What it measures

For each (model × event) the bench computes deterministic per-attempt signals
(see `gates.py`), plus per-mode retrieval metrics through a downstream-proxy
pass:

| Signal | What | Default threshold | Rationale |
|---|---|---|---|
| **Schema validity** | Output parses + matches per-source schema | ≥ 95% | Can the model emit JSON-shaped enrichment under the contract? |
| **Refusal / pathology** | Detects refusal phrases, trivial summaries, echo-of-input | 0 refusals; echo sim < 0.5 | Filters models that decline or copy input |
| **Latency** | Wall time per call | p95 ≤ 60s | Pareto axis 1: speed |
| **Self-consistency** | Mean pairwise cosine of N runs of same input, in embedding space | ≥ 0.7 | Distinguishes converging models from flailing ones |
| **Entity-type sanity** | Heuristic checks on entity categories (files look like paths, etc.) | ≥ 90% | Catches models that dump prose into entity fields |
| **Downstream retrieval** | Hit@K, MRR, NDCG@10 per mode (hybrid / semantic / lexical) | per BT-29 budget | Does enrichment improve `ask` quality vs. raw events? |

System metrics (RSS / CPU / load) are sampled every 250ms during a model's
active window and reported as peaks per model.

## What it does NOT measure (be honest)

- **Quality vs. ground truth.** Schema validity and entity sanity are structural;
  they can't tell you if the summary is *accurate*. Tier 1 (labeled eval) is the
  roadmap answer; see [issue #133](https://github.com/stevencarpenter/hippo/issues/133).
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
hippo-bench run --models m1,m2,...
                [--corpus-version corpus-v2]
                [--base-url http://localhost:1234/v1]
                [--brain-url http://127.0.0.1:9175]
                [--embedding-model text-embedding-nomic-embed-text-v2-moe]
                [--skip-checks] [--dry-run] [--skip-prod-pause]
                [--out path]

hippo-bench corpus init [--corpus-version corpus-v2] [--seed 42] [--db-path path]
                        [--corpus-days 90] [--corpus-buckets 9]
                        [--shell-min N] [--claude-min N] [--browser-min N] [--workflow-min N]
                        [--bump-version VERSION]

hippo-bench corpus verify [--corpus-version corpus-v2]

hippo-bench corpus add-adversarial <event_id> --reason "..." [--source shell|claude|browser|workflow]

hippo-bench summary <run-file>

hippo-bench determinism <run-file> <run-file> [...] [--mrr-budget 0.02]
                                                    [--hit-at-1-budget 0.02]
                                                    [--mode hybrid]

hippo-bench recover [--brain-url http://127.0.0.1:9175]
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Run completed successfully (or summary printed, or determinism passed) |
| 1 | Generic failure (e.g., `corpus verify` mismatch, determinism budget exceeded) |
| 2 | Pre-flight aborted the run (e.g., `lms` missing, disk full, corpus stale, or a present-but-unscoreable Q/A fixture) |
| 3 | Run executed but every candidate model errored |

### Important flags

- `--skip-checks`: bypasses pre-flight (lms availability, prod brain
  reachability, corpus integrity, disk, brain port). Debugging only.
- `--dry-run`: resolves config, writes a `run_manifest` + `run_end` record, and
  exits without making any LM Studio calls. Useful for validating config.
- `--skip-prod-pause`: skip pausing the production `hippo-brain` enrichment
  loop. Use this when the prod brain is not running or you're doing a dry-run.
- `--bump-version` (on `corpus init`): force-overwrite an existing corpus and
  tag the manifest with a new corpus_version label.

### Pre-flight & Q/A scoring

Pre-flight runs before any model is loaded and classifies each check as pass,
warn, or fail. A **fail** aborts the run (exit 2); a **warn** lets the run
proceed and is printed in a `[WW]` banner at the end of the run output so it is
not lost in scrollback.

The Q/A-scoreable check follows this split:

- **Fixture missing** (`eval-qa-v1.jsonl` not yet seeded) → **warn**. The run
  proceeds enrichment-only; retrieval/Q/A metrics are skipped. Seed the fixture
  (`qa_seed.py`) to enable scoring.
- **Fixture present but unscoreable** against the corpus (mislabeled or stale
  goldens) → **fail**, aborting the run (exit 2). You asked for Q/A scoring, so a
  fixture that can't score is a real error, not a silent skip.

---

## Prod brain coordination

The bench automatically pauses the production `hippo-brain` enrichment loop
before spawning the shadow stack, then resumes it after the run completes. This
prevents the prod brain from consuming LM Studio capacity during the bench
window.

- **Automatic** — no flags needed if the prod brain is running
- **Resume is best-effort** — registered via `atexit` so it fires even on crash
- **Override** — `--skip-prod-pause` bypasses pause/resume entirely

If the prod brain restarts during a bench run (e.g., due to launchd keepalive),
the `model_summary` record will include `"prod_brain_restarted_during_bench": true`.

If a prior bench was SIGKILL'd and left a stale pause lockfile, run
`hippo-bench recover` to clear it. The `run` subcommand also auto-recovers at
startup so this is rarely needed.

---

## Output format

Each run writes a JSONL file under `~/.local/share/hippo-bench/runs/`. Four
record types, discriminated by `record_type`:

### 1. `run_manifest` (first line, exactly one per file)

```json
{
  "record_type": "run_manifest",
  "run_id": "run-20260505T120000-mac-studio-01",
  "started_at_iso": "2026-05-05T12:00:00Z",
  "finished_at_iso": null,
  "bench_version": "0.2.0",
  "inference_backend_version": "lms CLI commit: 0b2a176",
  "host": {
    "hostname": "...", "os": "darwin 25.4.0", "arch": "arm64",
    "cpu_brand": "Apple M5 Max", "total_mem_gb": 128.0
  },
  "preflight_checks": [
    {"check": "prod_brain_reachable", "status": "pass", "detail": "pid=12345"},
    {"check": "corpus_present", "status": "pass", "detail": "schema_version=4"}
  ],
  "corpus_version": "corpus-v2",
  "corpus_content_hash": "sha256:...",
  "corpus_schema_version": 4,
  "eval_qa_version": "eval-qa-v1",
  "embedding_model": "text-embedding-nomic-embed-text-v2-moe",
  "candidate_models": ["qwen3.5-35b-a3b", "gpt-oss-120b-mlx-crack"],
  "host_baseline": {"load_avg_1m_at_start": 0.42},
  "prod_state_at_start": {"brain_pid": 12345, "brain_paused": false}
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
  "purpose": "self_consistency",
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
    "inference_rss_mb": 18432, "inference_cpu_pct": 87.2,
    "load_avg_1m": 4.12, "mem_free_mb": 5821
  },
  "timeout": false
}
```

### 3. `model_summary` (one per model, after that model's attempts)

```json
{
  "record_type": "model_summary",
  "run_id": "...",
  "model": {"id": "qwen3.5-35b-a3b"},
  "events_attempted": 40,
  "attempts_total": 25,
  "gates": {},
  "system_peak": {
    "inference_rss_mb": 21453, "inference_cpu_pct": 98.1,
    "load_avg_1m": 5.4, "mem_free_mb": 1200,
    "wall_clock_sec": 1854
  },
  "tier0_verdict": {
    "passed": true, "failed_gates": [], "skipped_gates": [], "notes": []
  },
  "process_ready_ms": 1850,
  "queue_drain_wall_clock_sec": 412,
  "downstream_proxy": {
    "modes": {"hybrid": {"mrr": 0.42, "hit_at_1": 0.50}, ...}
  },
  "prod_brain_restarted_during_bench": false,
  "timeout_during_drain": false,
  "errors": []
}
```

The `errors` list captures structured BT-04 failures inside the per-model
lifecycle (warmup / load_corpus / downstream_proxy / self_consistency steps)
that previously got swallowed by `except Exception: pass`. An empty list means
the run was clean.

### 4. `run_end` (last line, exactly one per file)

```json
{
  "record_type": "run_end",
  "run_id": "...",
  "finished_at_iso": "2026-05-05T13:42:11Z",
  "models_completed": ["qwen3.5-35b-a3b"],
  "models_errored": [],
  "prod_brain_resumed_ok": true,
  "models_with_prod_restart_event": []
}
```

For dry-runs and aborted runs, `reason` is also present (`"dry_run"`,
`"preflight_aborted"`, `"no_models"`).

---

## Reading a run with `jq`

```bash
RUN=~/.local/share/hippo-bench/runs/run-20260505T120000-host.jsonl

# What models passed?
jq -r 'select(.record_type=="model_summary") | "\(.model.id)\t\(.tier0_verdict.passed)"' "$RUN"

# Hybrid-mode MRR per model
jq -r 'select(.record_type=="model_summary")
       | "\(.model.id)\t\(.downstream_proxy.modes.hybrid.mrr)"' "$RUN"

# Every refusal (rare, worth investigating)
jq 'select(.record_type=="attempt" and .gates.refusal_detected)' "$RUN"

# Worst-latency event for the slowest model
jq 'select(.record_type=="attempt" and .model.id=="my-slow-model")
    | {event:.event.event_id, ms:.timestamps.total_ms}' "$RUN" \
    | jq -s 'sort_by(-.ms) | .[0:5]'

# Captured BT-04 errors per model (empty if clean)
jq 'select(.record_type=="model_summary") | {model:.model.id, errors:.errors}' "$RUN"
```

---

## Module map

| Module | Responsibility |
|---|---|
| [`cli.py`](cli.py) | argparse entrypoint, exit-code semantics |
| [`config.py`](config.py) | `BenchConfig` + `DEFAULT_THRESHOLDS` |
| [`paths.py`](paths.py) | XDG path resolution for fixtures and runs |
| [`schemas.py`](schemas.py) | Per-source enrichment JSON schemas (shell/claude/browser/workflow) |
| [`gates.py`](gates.py) | Per-attempt gate functions; pure, deterministic, never raise |
| [`enrich_call.py`](enrich_call.py) | LM Studio HTTP client; classifies errors instead of raising |
| [`lms.py`](lms.py) | `lms` CLI wrapper (load/unload/list) |
| [`metrics.py`](metrics.py) | Background `MetricsSampler` thread (RSS, CPU, load, memory) |
| [`preflight.py`](preflight.py) | Pre-flight checks (prod brain reachable / pauseable, corpus, disk, brain port, Q/A scoreable) |
| [`corpus.py`](corpus.py) | Time-bucketed sampling, shadow SQLite snapshot, JSONL sidecar, manifest |
| [`shadow_stack.py`](shadow_stack.py) | Process-group spawn (daemon + brain), env injection, SIGTERM/SIGKILL teardown |
| [`downstream_proxy.py`](downstream_proxy.py) | Q/A loading, Hit@K, MRR, NDCG, ask-synthesis sampling |
| [`coordinator.py`](coordinator.py) | Per-model lifecycle (shadow stack, queue drain, downstream proxy) |
| [`runner.py`](runner.py) | Self-consistency pass; per-attempt gate composition |
| [`output.py`](output.py) | JSONL record dataclasses + append-only writer |
| [`summary.py`](summary.py) | Aggregate per-model gates from attempts; derive verdict |
| [`pretty.py`](pretty.py) | Text-table renderer for `hippo-bench summary` |
| [`orchestrate.py`](orchestrate.py) | Top-level: preflight + pause/resume + per-model loop + JSONL writes |
| [`pause_rpc.py`](pause_rpc.py) | Thin HTTP client for `/control/pause` and `/control/resume` |
| [`prod_config.py`](prod_config.py) | Reads prod brain port from `~/.config/hippo/config.toml` |
| [`determinism.py`](determinism.py) | BT-29: compares run JSONLs against MRR / Hit@1 budget |
| [`qa_seed.py`](qa_seed.py) | Seeds `eval-qa-v1.jsonl` from committed template into fixtures dir |

## Architecture in one diagram

```
                 hippo-bench run
                        │
                        ▼
              orchestrate.orchestrate_run
                        │
       ┌────────────────┼────────────────┐
       ▼                ▼                ▼
  preflight.run   pause_rpc.pause     For each candidate model:
  _all_preflight  (prod brain)               │
  (prod brain,                               ▼
   corpus, lms,                       coordinator.run_one_model
   disk, port)                               │
                            ┌─────────┬──────┼──────┬─────────────────────┐
                            ▼         ▼      ▼      ▼                     ▼
                       lms.unload  metrics  shadow_stack.spawn    runner.run_self_consistency
                       lms.load    Sampler  → daemon + brain      enrich_call.call_enrichment
                                            → wait_for_brain_ready enrich_call.call_embedding
                                            → drain queue                  │
                                            → downstream_proxy             ▼
                                              .run_downstream       gates.check_*  → AttemptRecord
                                              _proxy_pass                   │
                                            → teardown                      ▼
                                                  │              system_snapshot
                                                  ▼                         │
                                          ModelRunResult ◄──────────────────┘
                                                  │
                                                  ▼
                                  output.RunWriter (JSONL append)
                                                  │
                                                  ▼
                                  pause_rpc.resume (atexit)
```

---

## Adding a new model

1. Pull the model into LM Studio (UI or `lms get <id>`).
2. Run: `hippo-bench run --models <new-model-id>`.
3. Wait. Each model takes roughly `(events × per_call_latency) + drain_wall_clock`
   seconds.
4. Inspect: `hippo-bench summary ~/.local/share/hippo-bench/runs/run-*.jsonl`.

To compare a new model to a previous baseline, run with both:
`--models qwen3.5-35b-a3b,new-model-id`. Bench loads/unloads each one cleanly.

---

## Adding a new source

If hippo gains a new source type (e.g., `gmail`):

1. Add a `SourceSchema` entry in [`schemas.py`](schemas.py) with required fields
   and entity categories.
2. Add a prompt template entry in [`enrich_call.py`](enrich_call.py).
3. Add a `_SourceSpec` entry in [`corpus.py`](corpus.py) `_SOURCE_SPECS` with
   the SELECT, payload-shape lambda, eligibility-dict lambda, and destination
   table/queue mapping.
4. Add a `--gmail-min` CLI flag in [`cli.py`](cli.py)'s `corpus init`.
5. Bump corpus content via `corpus init --bump-version corpus-v3` so prior
   runs aren't conflated.

The gates work source-agnostically; no gate code changes needed.

---

## Veracity caveats — read before publishing any leaderboard number

These are documented in code comments and surface in the JSONL, but worth
calling out:

1. **Self-consistency at low temperature** is meaningless. Coordinator default
   is 0.7. Set lower (e.g., 0.1) only when measuring raw determinism.
2. **Cooldown is load-driven, not thermal** — back-to-back large models may show
   spurious latency regression on the second model.
3. **Schema validity excludes prose-wrapped JSON** less rigidly than spec
   suggests: we recover fenced blocks AND first balanced `{...}`. A model that
   emits *only* a refusal (no JSON) correctly fails.
4. **Entity-sanity heuristics encode taste.** Hand-review a sample before trusting.
5. **`reference_enrichment` is always null** (spec promised baseline capture; not
   implemented — needs hippo `knowledge_nodes` join logic).
6. **Retrieval metrics require scoreable Q/A labels.** The Q/A fixture's
   `golden_event_id`s are corpus-grounded and bound to a specific
   `corpus_content_hash` (see `qa_template.provenance.json`). Run
   `hippo-bench qa validate --min-scoreable 100` before publishing any
   `downstream_proxy` MRR / Hit@1 number — if the corpus is rebuilt from a
   different DB, the goldens stop resolving and must be re-annotated. The
   `claude-<id>` goldens resolve against `agentic_sessions`
   (`harness='claude-code'`), not the frozen `claude_sessions` table — the
   corpus builder, the proxy gate, retrieval's `linked_source_ids`, and the
   validator all read the agentic family (schema v18). A real all-source smoke
   currently scores hybrid MRR ≈ 0.4 / Hit@1 ≈ 0.35 over 100 items; pure
   `lexical` mode scores ~0 by design (the anti-leakage rubric strips verbatim
   tokens, so only semantic/hybrid retrieval can find the golden).
7. **Trust still requires BT-29.** A single run now produces real metrics, but
   model-ranking claims require the three-run determinism procedure in
   [`docs/capture/bench-runbook.md`](../../../../docs/capture/bench-runbook.md).
   N=100 corpus-grounded items distinguishes a broken model from a working one;
   fine-grained ranking of similar models wants a larger fixture (tracked in
   [issue #133](https://github.com/stevencarpenter/hippo/issues/133)).
8. **Inference-server health is a precondition, not a measurement.** A degraded
   local inference server (e.g. a long-running oMLX process whose batched chat
   engine has wedged) drops requests with `RemoteProtocolError: peer closed
   connection` or returns `HTTP 507`. The `InferenceClient` retries transient
   transport errors with backoff, but a *persistently* sick server will still
   fail the run — that is an infra signal, NOT a model verdict. If a run shows
   widespread drops or a model lands in `errored`, probe the server directly
   (`POST /v1/chat/completions` a few times) and restart it before trusting any
   numbers. Also **fully quiesce the prod brain** for the run window: the soft
   `/control/pause` stops new claims but not an in-flight batch, and a
   prod brain enriching this session's own captured activity will contend for
   the inference server. Hard-stop it (`launchctl bootout gui/$UID/com.hippo.brain`,
   leaving `com.hippo.daemon` up so capture keeps queueing) for a clean run.

Use the `tier0_verdict.skipped_gates` field to surface "didn't measure this" vs.
"failed this" in any leaderboard you publish.
