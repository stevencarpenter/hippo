# Hippo-Bench — Local Enrichment Model Benchmark

**Status:** Draft (brainstorm → spec, MVP scoped)
**Author:** steven + Claude (brainstorm session 2026-04-21)
**Target branch:** TBD (post-approval)

## Motivation

Hippo's enrichment pipeline calls a local LLM (currently `qwen3.5-35b-a3b`
via LM Studio) for every eligible shell event, claude session, browser
visit, and workflow run. New local models drop frequently (Qwen, Llama,
Mistral, GLM, MLX variants), and "should I switch?" currently has no
principled answer.

`hippo-bench` is a local, reproducible benchmark that ranks candidate
enrichment models on a frozen corpus, emitting raw data so a living
leaderboard can track the Pareto frontier (quality × speed × load) over
time. It is complementary to `hippo-eval` — that harness measures
*retrieval* quality on an enriched corpus, this one measures the
*enrichment* step upstream of it.

## Design Constraints

1. **Local only.** The bench talks to LM Studio's local HTTP API exclusively.
   No cloud backends, no remote judges. Hippo is local by design; the
   bench is too.
2. **Corpus never leaves the machine.** Fixture events (real, redacted)
   live in XDG data dir and are `.gitignore`d. Only the leaderboard and
   per-run scores (aggregated, no raw content) can be committed.
3. **Comparability is the product.** Runs across time must be ordinally
   comparable, so corpus versioning and machine-state hygiene are
   first-class.
4. **MVP scope: Tier 0 only.** The MVP implements the automated
   "shakeout" tier and emits JSONL. Labeled eval (Tier 1), judge (Tier 2),
   TUI curation, Pareto plotting, and leaderboard publishing are roadmap.

## Outcome Target

The design supports two converging goals:

- **Living leaderboard.** Re-run as new models appear, accumulate JSONL
  over time, surface the frontier.
- **Pareto trade-off curve.** Speed vs. quality vs. memory footprint —
  make the trade-off explicit so the user can pick a model depending on
  whether they're backfilling 10k events or doing ambient enrichment.

## Architecture

### Tiered funnel (whole system; MVP builds only Tier 0)

| Tier | Purpose | Cost | Builds in MVP? |
|---|---|---|---|
| **Tier 0 — Automated shakeout** | Eliminate obviously-bad models cheaply, no labels | Minutes/model | ✅ yes |
| **Tier 1 — Labeled evaluation** | Entity F1 + keyword hit against ~150 hand-labeled events | Labels once (~3hr), replayed forever | ❌ roadmap |
| **Tier 2 — Spot check** | User eyeballs top ~5 survivors on ~5 events (hotkey-driven TUI) | ~15 min/model | ❌ roadmap |

Explicit non-choice: **no LLM judge.** A local peer judge adds drift
without adding signal. Hand-labels + deterministic gates are the ground
truth.

### MVP runtime flow

```
1. Pre-flight (coordinator)
   - LM Studio reachable, `lms` CLI present
   - Power plugged, not thermal-throttled, hippo services paused
   - Disk space, Spotlight idle, Docker idle
   - All checks recorded into run_manifest

2. Load corpus fixture
   - ~/.local/share/hippo/bench/fixtures/corpus-v1.jsonl
   - Verify corpus content hash matches manifest

3. For each candidate model:
   a. lms unload --all
   b. Wait for idle baseline
   c. lms load <model>, poll /v1/models until ready
   d. 3 warmup calls (discarded)
   e. Start 250ms metric-sampling thread
   f. Run fixture: 40 events × 1 attempt
   g. Self-consistency: 5 events × 5 runs
   h. Stop sampler, flush records
   i. lms unload <model>
   j. Cooldown until load_avg_1m settles (cap 90s)

4. Emit JSONL: one run_manifest + many attempt + one model_summary per model
```

### Placement

- `brain/src/hippo_brain/bench/` — Python module
- `brain/pyproject.toml` adds `hippo-bench` console script
- Reuses: `client.py` (LM Studio client), `models.py` (schema validators),
  `redaction.py` (sample-time redaction), `embeddings.py` (self-consistency)
- Fixture/output paths respect `XDG_DATA_HOME`, default
  `~/.local/share/hippo/bench/`

## Corpus

### Path

`~/.local/share/hippo/bench/fixtures/corpus-v1.jsonl` — **local, never
committed.** Added to `.gitignore`. Bench warns on run if the fixture
path is inside a git-tracked directory.

### Size and composition (MVP: shakeout-sized)

40 events total, stratified:

| Source | Count |
|---|---:|
| shell | 15 |
| claude | 12 |
| browser | 10 |
| workflow | 3 |

Rationale: small enough that one Tier 0 pass finishes in ~30 min; large
enough to catch source-specific failures. Tier 1's larger ~150-event
corpus is a separate fixture (future).

### Seeding

`hippo-bench corpus init` samples from `hippo.db` with:

- Stratified random sampling per source with fixed seed
- Re-applies `redaction.py` at sample time
- Skips: empty content, events below source-specific minimum size,
  events where redaction flagged a high-severity secret (extra cautious)
- Writes each event to the JSONL with:
  - `event_id`, `source`, `redacted_content`, `content_sha256`
  - `reference_enrichment` — whatever enrichment already exists in the
    DB at sample time (baseline curiosity; **not ground truth**)

### Manifest

`~/.local/share/hippo/bench/fixtures/corpus-v1.manifest.json`:

```json
{
  "corpus_version": "corpus-v1",
  "created_at_iso": "...",
  "created_by_bench_version": "0.1.0",
  "db_schema_version": 4,
  "seed": 42,
  "filter_spec": { "min_content_chars": 8, "skip_high_severity_redactions": true },
  "source_counts": { "shell": 15, "claude": 12, "browser": 10, "workflow": 3 },
  "event_ids_sha256": [ { "event_id": "...", "sha256": "..." } ],
  "corpus_content_hash": "sha256 of concatenated per-event hashes"
}
```

The `corpus_content_hash` is recorded into every benchmark run so
scores can be scoped "comparable under corpus-v1". Changing the corpus
→ bump version → older runs remain valid but are no longer
cross-comparable to new runs.

### Overlay (future, not MVP)

`corpus-v1.overlay.jsonl` lets the user pin specific regression cases
(events a particular model failed on). Overlay results scored
separately from the stratified sample.

## Tier 0 Gates

Per model × source × event, five gates are computed. All gates are
recorded whether they pass or fail; the `tier0_verdict` on the model
summary is a derived field the user can re-threshold offline.

| Gate | Computation | Default threshold |
|---|---|---|
| **Schema validity** | JSON parses; required top-level keys per source; entity categories match per-source schema; non-empty summary within length bounds | ≥ 95% |
| **Refusal / pathology** | Regex scan for refusal phrases; cosine similarity of output to input (echo); trivial-summary detection (≤ 3 words, whitespace only); zero-entity detection on events that should have entities | 0 refusals, echo sim < 0.5 |
| **Latency** | Wall time per call (total_ms); TTFT where LM Studio exposes it | p95 ≤ 60s (configurable) |
| **Self-consistency** | 5 events × 5 runs; embed each output with `nomic-embed-text`; mean pairwise cosine similarity across runs | mean ≥ 0.7 |
| **Entity-type sanity** | Heuristic: `files` entries look like paths, `tools` are short non-sentence strings, `projects` are short identifiers, etc. | ≥ 90% of entities per category pass |

### Entity-type heuristics (deterministic)

- **files**: contains `/` OR has a file extension OR is a bare `.rc`/`.env`-style name; ≤ 200 chars
- **tools**: ≤ 40 chars, ≤ 3 words, no sentence-final punctuation
- **projects**: ≤ 80 chars, no whitespace other than hyphens/underscores, or known-project allowlist hit
- **services**: ≤ 60 chars, lowercase-dominant
- **errors / concepts**: no strict heuristic; always passes (free-form)

## System Metrics Capture

A monitoring thread samples every **250ms** while a model is active:

- LM Studio process RSS (MB) via `psutil`, PID discovered by process-name match
- LM Studio process CPU% (single sample, non-blocking)
- System `getloadavg()` 1-minute load average (`os.getloadavg()[0]`)
- `psutil.virtual_memory()` free + swap
- Monotonic timestamp

Per-model aggregation captures peak values + percentiles.

**Apple Silicon GPU utilization explicitly deferred.** Requires
`sudo powermetrics`; not compatible with headless `hippo-bench run`.
Noted as a future opt-in (`--with-power-metrics` would require the
user to pre-auth sudo).

## Output Format

### Path

`~/.local/share/hippo/bench/runs/run-<iso_ts>-<hostname>.jsonl`

Runs accumulate; never deleted automatically. A future
`bench cleanup --older-than 90d` may prune.

### Records

Four `record_type` values in one JSONL file:

**1. `run_manifest`** — first line, exactly one per file:

```json
{
  "record_type": "run_manifest",
  "run_id": "run-20260421T120000-mac-studio-01",
  "started_at_iso": "2026-04-21T12:00:00Z",
  "finished_at_iso": "2026-04-21T13:42:11Z",
  "bench_version": "0.1.0",
  "lmstudio_version": "0.3.x",
  "host": {
    "hostname": "...",
    "os": "darwin 25.4.0",
    "arch": "arm64",
    "cpu_brand": "Apple M3 Max",
    "total_mem_gb": 64,
    "power_state": "plugged",
    "thermal_state_at_start": "nominal"
  },
  "preflight_checks": [ { "check": "lms_cli", "status": "pass" } ],
  "corpus_version": "corpus-v1",
  "corpus_content_hash": "sha256:...",
  "candidate_models": [ "qwen3.5-35b-a3b", "llama-3.3-70b-instruct" ],
  "gate_thresholds": { "schema_validity_min": 0.95, "..." : "..." },
  "self_consistency_spec": { "events": 5, "runs_per_event": 5 }
}
```

**2. `attempt`** — one per (model × event × attempt):

```json
{
  "record_type": "attempt",
  "run_id": "...",
  "model": {
    "id": "qwen3.5-35b-a3b",
    "lmstudio_resolved_id": "qwen3.5-35b-a3b-instruct-mlx",
    "quantization": "4bit-mlx"
  },
  "event": {
    "event_id": "e_abc123",
    "source": "shell",
    "content_hash": "sha256:..."
  },
  "attempt_idx": 0,
  "purpose": "main | self_consistency",
  "timestamps": {
    "start_iso": "...",
    "start_monotonic_ns": 123456789,
    "ttft_ms": 412,
    "total_ms": 12843
  },
  "raw_output": "...",
  "parsed_output": { "summary": "...", "entities": { "projects": ["hippo"] } },
  "gates": {
    "schema_valid": true,
    "schema_errors": [],
    "refusal_detected": false,
    "refusal_patterns_matched": [],
    "echo_similarity": 0.12,
    "entity_type_sanity": { "files_path_rate": 0.95, "tools_sanity": true }
  },
  "system_snapshot": {
    "lmstudio_rss_mb": 18432,
    "lmstudio_cpu_pct": 87.2,
    "load_avg_1m": 4.12,
    "mem_free_mb": 5821
  }
}
```

**3. `model_summary`** — one per model, at end of its block:

```json
{
  "record_type": "model_summary",
  "run_id": "...",
  "model": { "...": "..." },
  "events_attempted": 40,
  "attempts_total": 65,
  "gates": {
    "schema_validity_rate": 0.975,
    "refusal_rate": 0.0,
    "latency_p50_ms": 8432,
    "latency_p95_ms": 14201,
    "latency_p99_ms": 18000,
    "self_consistency_mean": 0.82,
    "self_consistency_min": 0.71,
    "entity_sanity_mean": 0.94
  },
  "system_peak": {
    "rss_max_mb": 21453,
    "cpu_pct_max": 98.1,
    "wall_clock_sec": 1854
  },
  "tier0_verdict": {
    "passed": true,
    "failed_gates": [],
    "notes": []
  }
}
```

**4. `run_end`** — terminal record, exactly one per file:

```json
{
  "record_type": "run_end",
  "run_id": "run-20260421T120000-mac-studio-01",
  "finished_at_iso": "2026-04-21T13:42:11Z",
  "models_completed": ["qwen3.5-35b-a3b"],
  "models_errored": [],
  "reason": "completed | dry_run | preflight_aborted | no_models"
}
```

`tier0_verdict.passed` is `true` iff **every** gate's threshold from
`run_manifest.gate_thresholds` is met by the corresponding field on
`model_summary.gates`. It is a derived field — the raw rates are the
source of truth, so the user can re-threshold offline without re-running.

### Why JSONL

- Append-friendly; survives mid-run crashes (partial file still parseable)
- Tails well with `tail -f` during a long run
- Cheap record-type filter via `jq 'select(.record_type == "attempt")'`
- Future leaderboard tooling reads all runs; DuckDB/Polars both ingest
  JSONL natively

## Coordinator / Pre-Flight

The coordinator is a deterministic orchestrator, not an LLM agent.
It sets up machine state so cross-day / cross-model runs are comparable.

### Pre-flight checks (abort on `fail`, warn on `warn`)

| Check | Tool | Pass / fail |
|---|---|---|
| LM Studio reachable | `GET /v1/models` | fail → abort |
| `lms` CLI present | `which lms` | **fail → abort (hard requirement)** |
| Power plugged, not low-power | `pmset -g batt` | warn |
| Not thermally throttled at start | `sysctl` throttling counter / `pmset -g thermlog` | fail → abort |
| Hippo services state | `launchctl list \| grep hippo` (labels: `com.sjcarpenter.hippo.daemon`, `com.sjcarpenter.hippo.brain`) | info only; **bench stops both** via `launchctl bootout` for the duration, re-bootstraps after (whether pass or fail) |
| Other claude processes | `pgrep claude` | warn |
| Docker heavy CPU | `docker stats --no-stream` | warn if > 50% |
| Spotlight indexing | `mdutil -s /` | warn |
| Disk free | `df -h ~/.local/share/hippo` | fail if < 2 GB |

All check results recorded into `preflight_checks` array on the
`run_manifest` line.

### Per-model sequence

Per Section 4 above. Key invariants:

- `lms unload --all` before each model (clean VRAM baseline)
- 3 warmup calls discarded
- Monitoring thread stops between models (so inter-model cooldown isn't
  contaminated by the previous model's sampler)
- Max cooldown 90s; if load_avg_1m doesn't settle below threshold, proceed
  anyway but tag the model_summary with `cooldown_timeout: true`

### Abort / interrupt behavior

- Single call > `latency_ceiling × 3` → recorded as `timeout: true`, run continues
- LM Studio process dies → abort; JSONL already flushed is valid
- SIGINT → finalize current attempt, flush, exit 130
- SIGTERM → hard-flush pending record, exit 143

### Execution mode

- Single-threaded: no parallel HTTP, no batch requests. We measure each
  model alone, not throughput under contention.
- Monitoring thread uses low QoS on macOS (`os.setpriority` + idle class
  where supported).
- JSONL writes only between events (never during an active call).

## CLI

```
hippo-bench run [--models m1,m2,...]        # default: all loaded + lms ls cache
                [--fixture path]
                [--self-consistency-runs 5]
                [--self-consistency-events 5]
                [--latency-ceiling-sec 60]
                [--skip-checks]              # for debugging only
                [--out path]
                [--dry-run]                  # resolve everything, emit manifest, no calls

hippo-bench corpus init [--seed 42]
                        [--source-counts shell=15,claude=12,browser=10,workflow=3]
                        [--db-path path]
                        [--force]            # overwrite existing corpus

hippo-bench corpus verify                    # re-check content hashes

hippo-bench summary <run-file>               # pretty text table of the latest run
                                             # (full Markdown leaderboard is roadmap)
```

## Testing Plan

- `brain/tests/test_bench_gates.py` — each Tier 0 gate against synthetic fixtures
  (valid JSON, refusal phrases, echo, malformed entities, timeout, etc.)
- `brain/tests/test_bench_corpus.py` — corpus init determinism given a fixed seed,
  manifest integrity, content hash reproducibility
- `brain/tests/test_bench_output.py` — JSONL record shape, record_type discrimination,
  manifest-first ordering
- `brain/tests/test_bench_coordinator.py` — mocked `lms` subprocess + mocked
  LM Studio; pre-flight failure paths
- `brain/tests/test_bench_metrics.py` — monitoring thread lifecycle, sample cadence,
  peak aggregation
- End-to-end smoke: a `--dry-run` invocation that produces a complete
  `run_manifest` without any LM Studio calls

## Non-Goals (MVP)

- **No LLM judge.** Explicit non-choice; see Tiered Funnel section.
- **No labeling TUI.** Tier 1 needs it; roadmap item.
- **No committed leaderboard.md.** `hippo-bench summary` prints a text table;
  the polished Markdown leaderboard with Pareto plot is roadmap (agent:
  leaderboard-analyst).
- **No Pareto plotting.** JSONL carries the data; plotting is a reader
  concern, not a bench concern.
- **No cross-machine comparability.** Each run is scoped to its host.
- **No per-model prompt tuning.** All candidate models see the same
  enrichment prompts. Models that need tuning to pass Tier 0 fail Tier 0.
- **No auto-trigger on new `lms ls` models.** User invokes manually.
- **No GPU utilization capture.** Sudo-requiring; deferred.

## Roadmap (post-MVP)

Build order:

1. **corpus-curator TUI + agent.** Hotkey-driven review loop. Per-event
   decisions: add / skip / send-to-Claude-for-reference / adversarial-tag /
   reject. Agent pre-triages each event. Writes to a living queue table
   in SQLite so labeling sessions are resumable. **Unblocks Tier 1.**
2. **Tier 1 labeled runner.** Reuses MVP infrastructure; adds entity F1,
   keyword-hit, per-source-type scoring against the labeled fixture.
3. **bench-diagnostician agent.** When a run has outliers, reviews JSONL,
   cross-references prior runs, explains anomalies.
4. **leaderboard-analyst agent.** Reads accumulated JSONL runs, produces
   the committed Markdown leaderboard + Pareto plot; flags regressions;
   suggests retirement for consistently-failing models.
5. **model-triage-agent.** Short compatibility smoke test before committing
   to a full bench run on a newly-loaded model.
6. **prompt-fairness-auditor.** Checks candidate models for prompt-format
   bias; flags if a per-model variant would be needed (and thus should
   disqualify for comparability).

### Reusable skills (not MVP)

- **`hippo-bench` skill** — schema + gate semantics + coordinator invariants docs
- **`lm-studio-ops` skill** — canonical `lms` + `/v1/models` wrapper
- **`macos-benchmark-hygiene` skill** — thermal / throttling / power hygiene
- **`pareto-analysis` skill** — N-objective frontier computation + rendering

### Explicitly deferred

- **A `hippo-bench` MCP server.** Overkill until ≥10 accumulated runs.
- **Downstream-proxy evaluation** (run `hippo-eval` against each model's
  enrichments as a quality signal). Only feasible once a stable
  representative subset of the corpus is identified. Future opportunity.
- **Cross-machine comparability.** Hard; not worth solving until the bench
  is actually running on multiple hosts.

## Open Questions / Risks

- **Fixture drift.** As new source types appear (e.g., Codex sessions), the
  fixture must be expanded → corpus-v2 → older scores stay valid but
  stop accruing. Mitigated by explicit versioning.
- **LM Studio version drift.** `lms` behavior could change across
  versions; the `lmstudio_version` field in the manifest lets us filter
  later.
- **First-run sample bias.** The seed user runs `corpus init` on a DB
  that reflects *their* usage patterns at that moment. If usage patterns
  shift (new projects, different tools), the corpus becomes less
  representative. Mitigation: `corpus init --seed <new>` regenerates;
  overlay mechanism lets regression cases persist across corpus
  versions.
- **Self-consistency gaming.** A model that always produces the same
  conservative "{}" output scores 1.0 on self-consistency but 0 on
  schema validity. Gates are independent so this is caught, but it's
  worth noting: self-consistency alone is not a quality signal.
