# Hippo-Bench Trust Initiative — Tracking Document

**Status:** Active
**Goal:** Make `hippo-bench` the bench we trust to rank every new open-source local model on hippo's enrichment pipeline. "Trust" = if the bench says model A > model B, that's true with high confidence (paired t-test p<0.05 on quality and retrieval metrics, deterministic across reruns, with no prod blast-radius).
**Companion plan:** `docs/superpowers/plans/2026-05-03-hippo-bench-trust-ralph-plan.md` (autonomous execution)
**Origin:** Six-expert panel review of PR #127 (methodology, Rust, Python, QA, SRE, telemetry). Synthesized 2026-05-03.

## North Star (operator-stated 2026-05-03)

> "I want the benchmark for realistic hippo usage and activity embedding and enriching on a very reliable and varied corpus of test events. That is our north star! Secondary to that will be how can hippo enrichment itself feel like a daemon. In the future, I would love my phone to be able to do inference with the context of local data and leveraging on device enclaves. But right now I am building my corpus. I think at some point I will need to distill a seed corpus to make people useful in the first week, because collecting a months data sounds insane and no way I would stick around."

**Implications for this plan:**

1. **Primary metric is realistic activity quality, not just retrieval scores.** The Q/A fixture (Phase 2) must reflect actual hippo usage patterns — shell commands the user actually runs, Claude sessions about real work, browser pages from active research — not synthetic retrieval probes. A model that scores 0.85 MRR on synthetic queries but 0.40 MRR on the user's actual workflows is the wrong winner.
2. **"Feels like a daemon" is a measurable secondary axis.** Add enrichment latency / queue-drain rate / responsiveness-under-load as first-class bench metrics, not just throwaway peak_metrics. P50/P95 latency and "time-to-knowledge-node" matter as much as Hit@K.
3. **Seed corpus is a future product feature, not in current bench scope** — but the bench's Phase 2 corpus work should be designed so a curated subset can later be distilled into a shippable seed for new-user onboarding (target: Week 1 utility from Day 1).
4. **On-device inference (phone + enclaves) is explicit future scope.** Keep the bench architecture model-agnostic — no assumptions about LM Studio specifically; the LM Studio dependency should be behind a swappable client interface so future on-device runtimes can replace it.

These reshape Phase 2 (BT-23..BT-28 sketches) when they're unblocked for design review. They do not change Phase 0/1 (foundational reliability + correctness), which proceeds as planned.

---

## Definition of Done (overall)

The bench earns "trustworthy" status when **all** of the following hold:

1. Three consecutive runs of the same model against the frozen reference corpus produce identical verdicts (Hit@1 ± 0.02, MRR ± 0.02, judge-mean ± 0.1).
2. A deliberately-injected retrieval regression (rank-flip on 3 of the Q/A items) is caught by the golden-output test in CI.
3. A SIGKILL'd run leaves no orphaned shadow processes and no permanently-paused prod brain.
4. `hippo-bench compare run-A.jsonl run-B.jsonl` outputs a per-mode delta with paired-t confidence intervals.
5. An operator can answer "is this new local model better than last week's leader?" from one Grafana dashboard panel + one PromQL alert summary, without reading any JSONL.
6. Q/A fixture has ≥150 scoreable items with independent annotation provenance documented.
7. Bench emits `service.namespace=hippo-bench` consistently; prod dashboards verified clean of bench cardinality.

---

## Milestone Gates

### Phase 0 — Stop the Bleed (P0, blocking everything)

The bench in PR #127 has correctness bugs that must land before any further work. Until P0 closes, every bench result is suspect.

**Exit criteria:**
- [ ] `hippo-bench run --models <one>` completes end-to-end against real LM Studio with `pgrep -af "hippo serve"` showing 0 orphan shadow processes after teardown.
- [ ] A SIGKILL'd bench mid-run leaves no prod brain stuck paused; recovery script invoked on next bench start.
- [ ] `_wait_for_queue_drain` raises clearly on a corpus DB with renamed/missing queue tables.
- [ ] All `service_namespace` matchers in dashboards use `!~".+"` (no `=""` landmines).

| ID | Title | Status | Acceptance |
|----|-------|--------|------------|
| P0-01 | Fix `hippo serve` invocation in shadow_stack | [ ] | `shadow_stack.py:112` no longer calls a nonexistent subcommand; bench actually spawns a daemon |
| P0-02 | Wrap `run_one_model_v2` in `try/finally(teardown_shadow_stack)` | [ ] | Test injects raise after spawn, asserts teardown still called |
| P0-03 | Replace bare `except Exception: pass` with structured error capture | [ ] | All silent catches in `coordinator_v2.py` either re-raise or write a typed error field to the result record. Originally identified as 5; post-review C-2 (2026-05-03) found 6 more in `_collect_event_ids_from_db` and `_load_corpus_entries`, all addressed in commit `0b5d36a` |
| P0-04 | `_wait_for_queue_drain` hard-fails on missing queue tables | [ ] | Test with renamed `enrichment_queue` table asserts function raises, not "drained" |
| P0-05 | Pause lockfile + crash recovery | [ ] | Atomic file write before pause, removal after resume; `hippo-bench recover` resumes prod if stale lockfile |
| P0-06 | Port-conflict preflight on shadow brain port | [ ] | Refuses spawn if `lsof -i :18923` shows any listener; clear error |
| P0-07 | Connection-leak fix in queue-drain poll loop | [ ] | `contextlib.closing` + WAL+busy_timeout pragmas; test asserts no fd growth across 100 polls |

### Phase 1 — Trust Foundation

Test coverage and reliability investments that make the bench's verdicts believable.

**Exit criteria:**
- [ ] `pytest brain/tests` covers `run_one_model_v2` end-to-end with stubbed LM Studio and shadow stack.
- [ ] Golden-output test catches a deliberately-injected NDCG@10 regression.
- [ ] Two consecutive runs of the same model produce MRR within 0.02 (variance budget).
- [ ] Watchdog does not fire spurious alarms during a known-paused window.

| ID | Title | Status | Acceptance |
|----|-------|--------|------------|
| P1-01 | Smoke-integration test for `run_one_model_v2` | [ ] | Monkey-patches LM Studio, shadow stack, drain; asserts downstream_proxy populated and SC failure produces structured error |
| P1-02 | Fault-injection suite | [ ] | LM Studio kill mid-drain, port collision, SIGTERM during gather, stale processing lock — all four scenarios have tests. **Status (2026-05-03):** all four covered. Port collision via BT-07 preflight test; LM Studio kill / stale lock / signal-equivalent (KeyboardInterrupt) via post-review I-1 in `test_bench_coordinator_v2.py` |
| P1-03 | Tighten `_enrichment_active` against `BaseException` | [ ] | Cancellation test asserts flag is cleared even on `asyncio.CancelledError`. Original BT-13 test was synthetic (built an inner task) — post-review I-2 replaced it with one that runs the real `server._enrichment_loop` and parks it at `preflight_lm_studio` (commit `85215d3`) |
| P1-04 | Add `Commands::Serve` alias to daemon CLI | [ ] | `hippo serve` works as alias for `hippo daemon run`; documented |
| P1-05 | Daemon `--bench` flag | [ ] | Disables FSEvents watcher, LaunchAgent self-install, refuses paths outside `XDG_DATA_HOME`. **Spec amendment (post-review I-4, 2026-05-03):** original spec said "log warning and continue if not"; overruled to fail closed (`anyhow::bail!`) because the cost of pointing the bench at prod data is unbounded corruption while the cost of a false-positive bail is a loud, recoverable startup error. Commit `f7263e9`-adjacent (the I-4 commit itself) |
| P1-06 | Daemon-side `hippo.bench.queue_depth` gauge | [ ] | Polled every 5s, emitted as OTel gauge with namespace tag |
| P1-07 | Daemon-side `hippo.daemon.db_busy_count` counter | [ ] | Incremented on every `SQLITE_BUSY` retry. Original BT-15 only instrumented the watchdog alarm-insert (cold path); post-review I-3 (2026-05-03) added instrumentation across the daemon flush hot path: `flush_idle_tick_source_health`, `flush_event_insert`, `flush_browser_event_insert`, `flush_source_health_{success,error,liveness}`. The `op` attribute now distinguishes seven distinct call sites |
| P1-08 | Watchdog pause-window suppression | [ ] | I-2/I-4/I-8 suppressed when bench pause window is active. Original BT-16 used existence-only check on the pause lockfile; post-review C-1 (2026-05-03) added a 30-min mtime staleness gate to prevent a SIGKILL'd bench from muting the watchdog indefinitely. Commit `f7263e9` |
| P1-09 | Normalize `service_namespace` matchers | [ ] | All Grafana panels use `!~".+"`; `=""` removed from `hippo-enrichment.json` |
| P1-10 | Golden-output regression test | [ ] | Frozen 20-event fixture + Q/A asserts exact Hit@1/MRR/NDCG; rank-flip injection caught |

### Phase 2 — Methodology + Statistical Power

Make the bench's verdicts actually mean something.

**Exit criteria:**
- [ ] Q/A fixture has ≥150 scoreable items, stratified by source × intent × difficulty.
- [ ] Each item's `acceptable_answer_keywords` is populated (no inert synthesis gate).
- [ ] Annotation pipeline is documented and provenance-clean (no leakage from prior retrieval runs).
- [ ] Judge-LLM rubric run on a 20-node sample emits `judge_accuracy_mean`, `judge_usefulness_mean`, `judge_ask_suitability_mean`.
- [ ] Self-consistency: each model run with 2 RNG seeds, MRR variance reported in JSONL.

| ID | Title | Status | Acceptance |
|----|-------|--------|------------|
| P2-01 | Audit current Q/A fixture annotation pipeline | [ ] | Document in `docs/baselines/QA-ANNOTATION.md` how golden_event_ids were produced; identify any retrieval-leakage |
| P2-02 | Populate `acceptable_answer_keywords` for all current Q/A items | [ ] | All non-adversarial items have ≥3 keywords; existing tests still pass |
| P2-03 | Expand Q/A fixture to ≥150 scoreable items | [ ] | Stratified across shell/claude/browser/workflow × intent × difficulty; checked into `brain/src/hippo_brain/bench/qa_template.jsonl` |
| P2-04 | Independent annotation pass for new items | [ ] | Golden uuids drawn from a pre-bench labeling pass; provenance recorded per item |
| P2-05 | Self-consistency: 2-seed run + variance reporting | [ ] | Each model run twice with different seeds; `mrr_seed_variance` field in `model_summary` JSONL record |
| P2-06 | Judge-LLM rubric automation | [ ] | 20-node sample per run scored on accuracy/usefulness/ask_suitability against `RUBRIC.md`; emitted as both JSONL field and OTel gauge |
| P2-07 | Groundedness check via answer-source cosine similarity | [ ] | Replaces inert keyword-hit; `groundedness_p50`, `groundedness_p10` per model; integrated into downstream proxy |
| P2-08 | Frozen reference corpus snapshot | [ ] | `docs/baselines/2026-05-XX-frozen/corpus.sqlite` + sha256; `hippo-bench run --frozen` uses it; documented re-freeze cadence |

### Phase 3 — Observability + Automated Regression Detection

The verdict goes from "operator reads JSONL" to "alert fires on regression".

**Exit criteria:**
- [ ] Quality metrics (Hit@K, MRR, NDCG, judge means) are durable OTel gauges with `bench_run_id` / `bench_model_id` / `bench_corpus_version` / `bench_embedding_model` / `bench_quantization` labels.
- [ ] Prometheus loads alert rules from `otel/prometheus-rules/bench-alerts.yml`.
- [ ] At least 4 alerts fire correctly against an inject-regression run.
- [ ] All bench runs produce a Markdown summary report linkable from Grafana.

| ID | Title | Status | Acceptance |
|----|-------|--------|------------|
| P3-01 | Quality metrics as durable OTel gauges | [ ] | All `ModelSummaryRecordV2` fields emitted as gauges with required labels |
| P3-02 | `bench.quantization` and `bench.context_length_tokens` resource attrs | [ ] | Set in `_build_env`; surfaced as filterable Grafana dimensions |
| P3-03 | Prometheus `rule_files` stanza + bench-alerts.yml | [ ] | 4 rules: `BenchHitAt1Regression`, `BenchSchemaValidityTooLow`, `BenchP95LatencyTooHigh`, `BenchModelsErroredTotal` |
| P3-04 | Recording rule for baseline gauges | [ ] | `bench_baseline:hit_at_1` recorded from last "approved" run; alerts compare to it |
| P3-05 | Trace spans for retrieve → score → judge | [ ] | Per-event tracing exported to Tempo; Grafana drilldown links to traces |
| P3-06 | Markdown summary report per run | [ ] | Auto-generated, attached to GH Actions artifact, linked from Grafana annotation |
| P3-07 | LogQL alert: bench panic / unhandled exception | [ ] | Loki query `{service_namespace="hippo-bench"} |~ "(?i)panic|unhandled|traceback"` fires alert |
| BT-29 | Deterministic-rerun harness (`hippo-bench determinism`) | [partial] | Harness shipped post-review (commit pending): reads N JSONL run files, computes per-model MRR/Hit@1 deltas, exits 1 on regression. The 90-min real-bench triple-run is operator-gated — see [`docs/capture/bench-runbook.md`](../../capture/bench-runbook.md). Until the operator runs it once and pastes the verdict here, "trust foundation" is unverified empirically |

### Phase 4 — Ergonomics + Ops

Daily-use polish so the team actually wants to run this on every model release.

**Exit criteria:**
- [ ] `hippo-bench compare` produces a verdict ("model A passes baseline / model B regresses on dimension Y") with confidence intervals.
- [ ] `hippo-bench models list` enumerates LM Studio's loaded models for ergonomic selection.
- [ ] Failed runs are resumable via `--start-from-model N`.
- [ ] Operator runbook exists for the most common alert.

| ID | Title | Status | Acceptance |
|----|-------|--------|------------|
| P4-01 | `hippo-bench compare run-A.jsonl run-B.jsonl` | [ ] | Side-by-side delta, paired t-test, exits 1 if any metric regresses beyond threshold |
| P4-02 | `hippo-bench models list` | [ ] | Proxies LM Studio's `/v1/models`; clear error if LM Studio unreachable |
| P4-03 | Resumability via `--start-from-model N` | [ ] | Skips models whose `model_summary` already exists in target JSONL; appends rather than overwrites |
| P4-04 | Default `--corpus-version` flip to v2 | [ ] | New runs default to corpus-v2 (not v1) |
| P4-05 | Operator runbook | [ ] | `docs/capture/bench-runbook.md`: "you got an alert — now what" tree for each of the 4 alerts |
| P4-06 | GitHub Actions nightly workflow | [ ] | Scheduled run + manual dispatch; uploads JSONL + Markdown summary; opens GH issue on regression |
| P4-07 | Daemon `--no-session-watcher` for shadow mode | [ ] | Skips FSEvents loop entirely under bench |

---

## Risk Register

| ID | Risk | Mitigation |
|----|------|-----------|
| R-01 | Q/A fixture annotation gates Phase 2 entirely | Start P2-01..P2-04 in parallel; fall back to keeping current fixture and reporting "underpowered" disclaimers |
| R-02 | Single-GPU contention with prod brain causes flaky runs | Pause lockfile (P0-05) + watchdog suppression (P1-08); document "bench needs exclusive GPU" |
| R-03 | LM Studio API surface drifts between releases | `hippo-bench models list` (P4-02) catches drift; pin LM Studio version in CI |
| R-04 | Schema bump invalidates frozen corpus | Migration test (P1's daemon `--bench` work); re-freeze on every schema bump; documented in P2-08 |
| R-05 | Ralph loop exhausts context before completing all phases | Plan ordered P0 → P1 → P2 → P3 → P4 so partial completion still leaves bench in a strictly-better state |

---

## What We Are Not Doing

- **Cross-machine bench distribution.** Single-host per project memory.
- **Closed-source model evaluation.** OSS local models only.
- **Real-time / streaming bench.** Batch nightly only.
- **Hippo-eval replacement.** `hippo-eval` and `hippo-bench` stay separate.
- **Multi-tenant or cloud-hosted bench.** Local-only is the design point.

---

## Decision Log

| Date | Decision | Rationale | Owner |
|------|----------|-----------|-------|
| 2026-05-03 | Bench measures BOTH retrieval AND enrichment quality | A high-MRR / low-accuracy model is worse for hippo's mission than the inverse; methodology expert flagged inert synthesis gate | PM |
| 2026-05-03 | Frozen reference corpus, re-freeze every 6 months | Cross-model comparison requires identical input distribution; rolling corpus makes baselines incomparable | PM |
| 2026-05-03 | First-class `hippo daemon run --bench` flag | Single auditable place where bench-mode behavior diverges; env-var-only sandbox doesn't cover all macOS APIs | PM |
| 2026-05-03 | No timeline; phased priority replaces dates | User explicitly does not care about timeline; phase-gate ordering matters more than calendar | PM |
| 2026-05-03 | North star = realistic activity bench (not synthetic retrieval) | Operator-stated direction. Bench must measure what matters to the user, not what's easy to score. | Operator |
| 2026-05-03 | Add daemon-feel (latency/responsiveness) as secondary axis | Operator-stated. Quality alone isn't enough; an accurate-but-slow model fails hippo's daemon contract. | Operator |
| 2026-05-03 | Keep LM Studio behind a swappable client interface | Future on-device inference (phone + enclaves) needs a non-LM-Studio backend. Don't bake assumptions in. | PM |
| 2026-05-03 | BT-10 sandbox guard fails closed (`bail!`) instead of soft warn | Original spec said "log warning and continue if not"; review surfaced that the env-var threading is the only thing keeping the bench off prod data, so a false-positive bail (loud, recoverable) beats a missed leak (silent corruption). | post-review I-4 |
| 2026-05-03 | BT-16 watchdog suppression requires lockfile mtime < 30 min | A SIGKILL'd bench was leaving the pause lockfile in place, muting I-2/I-4/I-8 indefinitely. 30 min is comfortably longer than any realistic drain but bounded enough that a crashed bench doesn't blind the watchdog for days. | post-review C-1 |
| 2026-05-03 | BT-29 stays operator-gated; CI runs only `determinism` harness | The 90-min real-bench run is too disruptive to fire from autonomous loops (pauses prod brain × 3, consumes LM Studio). The harness is autonomous-safe and `bench-runbook.md` documents the operator procedure. | post-review BT-29 |

---

## Post-Review Findings (2026-05-03)

After the autonomous loop closed Phase 0 + 1, a clean-context code review found
six issues: two Critical (lockfile staleness, partial silent-except cleanup),
three Important (tautological cancellation test, single-site `db_busy_count`,
sandbox guard too soft), and one Minor (`O_EXCL` docstring lie). Each row below
links to the spec amendment + the commit that landed the fix.

| ID | Severity | Issue | Fix | Commit |
|---|---|---|---|---|
| C-1 | Critical | `bench_pause_window_active` had no mtime gate — a SIGKILL'd bench muted I-2/I-4/I-8 indefinitely. Zero test coverage. | Refactored into testable `is_pause_lockfile_active(path, now)` with 30-min staleness window; 4 unit tests. | `f7263e9` |
| C-2 | Critical | BT-04 only killed 5 of 11 silent excepts in `coordinator_v2.py`; corrupted DBs / bad JSONL silently produced empty `event_ids` and `[]` corpus. | Per-table `OperationalError` swallows now log at debug; outer `Exception` swallows propagate to existing `_capture` pattern; `_load_corpus_entries` call site wrapped with `_capture("load_corpus", e)`. | `0b5d36a` |
| I-1 | Important | P1-02 fault-injection suite was missing 3 of 4 scenarios. | New tests in `test_bench_coordinator_v2.py`: drain-times-out-on-stuck-queue, stale-processing-row-blocks-drain, BaseException-mid-lifecycle-tears-down, plus a load_corpus-failure capture test. | `<post-review I-1>` |
| I-2 | Important | BT-13 cancellation test was synthetic — never imported `_enrichment_loop`. | Replaced with a test that schedules the real `server._enrichment_loop`, parks it at `preflight_lm_studio` via monkey-patch, cancels, and asserts the flag is cleared by the loop's own `finally`. | `85215d3` |
| I-3 | Important | BT-15 `db_busy_count` only instrumented the watchdog alarm-insert path (cold). Bench-load contention happens in the daemon flush hot path. | Added `metrics::record_db_busy(err, op)` helper; instrumented six new sites in `flush_events`. `op` attribute distinguishes call sites for Prometheus drilldowns. | `<post-review I-3>` |
| I-4 | Important | BT-10 sandbox guard logged a warning instead of failing closed. | `warn!` → `anyhow::bail!`. Spec amendment recorded in Decision Log above. | `<post-review I-4>` |
| I-5 | Minor | `_write_lockfile_atomic` docstring claimed `O_EXCL` but code uses `O_TRUNC`. Single-host design makes practical impact zero, but docstring was misleading. | Not yet fixed (out of scope for this review pass). Tracked here so it doesn't get lost. | (pending) |

**What this doesn't change:** Phase 2 methodology work (BT-23..BT-28) is still
operator-blocked on annotation strategy + judge-LLM choice. Phase 3 + 4 work is
still future. BT-29 still requires a real operator-run before "trust foundation"
can be claimed empirically.
