# Hippo-Bench v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace hippo-bench v1's LLM-only shakeout with an end-to-end isolated benchmark that runs hippo as close to production as practical against ~1000 events per candidate model, including a downstream-proxy retrieval-quality gate.

**Architecture:** Per-model ephemeral shadow stack (fresh `hippo-daemon` + `hippo-brain` process tree per candidate model with a fresh DB seeded from a SQLite snapshot). Bench auto-pauses prod brain via a new graceful-pause HTTP RPC; prod daemon keeps capturing. Telemetry isolated via `service.namespace=hippo-bench` resource attribute on a single shared OTel collector stack.

**Tech Stack:** Python 3.14 (uv, ruff, pytest), Rust edition 2024 (cargo, clippy), SQLite WAL + sqlite-vec + FTS5, OpenTelemetry, Starlette HTTP server, LM Studio HTTP API.

**Spec:** [docs/superpowers/specs/2026-04-27-hippo-bench-v2-design.md](../specs/2026-04-27-hippo-bench-v2-design.md)

---

## File Map

### Rust changes

| File | Change | Purpose |
|---|---|---|
| `crates/hippo-daemon/src/telemetry.rs` | Modify lines 18-22 | Merge `EnvResourceDetector` so `OTEL_RESOURCE_ATTRIBUTES` env propagates |
| `crates/hippo-daemon/tests/telemetry_env_resource_test.rs` | Create | Verify env-var picked up into Resource |

### Python brain changes

| File | Change | Purpose |
|---|---|---|
| `brain/src/hippo_brain/server.py` | Modify | Add `POST /control/pause` and `POST /control/resume` routes |
| `brain/src/hippo_brain/_fixtures/eval_questions.json` | Extend | Bump from 40 to 100+ Q/A items across 4 sources (filename retained for spec consistency) |

### Python bench module

| File | Action | Purpose |
|---|---|---|
| `brain/src/hippo_brain/bench/snapshot.py` | Create | SQLite snapshot generation, copy-with-schema-assert |
| `brain/src/hippo_brain/bench/qa_set.py` | Create | Q/A loader, filter by available `golden_event_id` |
| `brain/src/hippo_brain/bench/downstream_proxy.py` | Create | Per-mode retrieval scoring (Hit@K, MRR, NDCG) |
| `brain/src/hippo_brain/bench/shadow_stack.py` | Create | Process-group spawn/wait/teardown of bench daemon+brain |
| `brain/src/hippo_brain/bench/pause_rpc.py` | Create | HTTP client for prod brain pause/resume |
| `brain/src/hippo_brain/bench/corpus.py` | Rewrite | v2 time-bucketed sampling + snapshot+JSONL output |
| `brain/src/hippo_brain/bench/preflight.py` | Modify | New checks: snapshot present, prod pause-able, schema match |
| `brain/src/hippo_brain/bench/coordinator.py` | Rewrite | Per-model lifecycle: shadow-stack spawn, seed, drain, downstream-proxy, teardown |
| `brain/src/hippo_brain/bench/orchestrate.py` | Modify | Wire pause RPC, per-model coordinator dispatch |
| `brain/src/hippo_brain/bench/runner.py` | Modify | Main pass = wait for queue drain (not direct LLM calls) |
| `brain/src/hippo_brain/bench/output.py` | Modify | New record fields per spec |
| `brain/src/hippo_brain/bench/cli.py` | Modify | New flags + corpus subcommands |
| `brain/src/hippo_brain/bench/config.py` | Modify | New config fields |
| `brain/src/hippo_brain/bench/paths.py` | Modify | New paths for snapshot, run-tree per model, qa fixture |
| `brain/src/hippo_brain/bench/summary.py` | Modify | Aggregate `downstream_proxy` into `model_summary` |
| `brain/src/hippo_brain/bench/README.md` | Update | v2 usage and architecture |

### New tests (Python)

`brain/tests/test_bench_snapshot.py`, `test_bench_corpus_v2.py`, `test_bench_qa_set.py`, `test_bench_downstream_proxy.py`, `test_bench_shadow_stack.py`, `test_bench_pause_rpc.py`, `test_bench_telemetry_isolation.py`, `test_brain_control_endpoints.py`.

### Grafana dashboards

| File | Action | Purpose |
|---|---|---|
| `otel/grafana/dashboards/bench-run-overview.json` | Create | Per-`bench.run_id` view |
| `otel/grafana/dashboards/bench-model-drilldown.json` | Create | Per-`bench.model_id` deep dive |
| `otel/grafana/dashboards/bench-model-comparison.json` | Create | Multi-model side-by-side |
| `otel/grafana/dashboards/hippo-overview.json` | Modify | Add `{service_namespace=""}` filter |
| `otel/grafana/dashboards/hippo-daemon.json` | Modify | Add `{service_namespace=""}` filter |
| `otel/grafana/dashboards/hippo-enrichment.json` | Modify | Add `{service_namespace=""}` filter |
| `otel/grafana/dashboards/hippo-processes.json` | Modify | Add `{service_namespace=""}` filter |

---

## Phase 0: Prerequisites (Rust + brain server)

### Task 0.1: Rust EnvResourceDetector merge

**Files:**
- Modify: `crates/hippo-daemon/src/telemetry.rs:18-22`
- Test: `crates/hippo-daemon/tests/telemetry_env_resource_test.rs` (create)

- [ ] **Step 0.1.1: Write failing test** — see spec for full test code (asserts `service.namespace` and `bench.run_id` from `OTEL_RESOURCE_ATTRIBUTES` env land in the built Resource).
- [ ] **Step 0.1.2: Run test, verify FAIL** — `cargo test --features otel -p hippo-daemon --test telemetry_env_resource_test`.
- [ ] **Step 0.1.3: Implement** — replace `resource()` body with:
  ```rust
  Resource::builder()
      .with_service_name(service_name.to_string())
      .with_detectors(&[Box::new(opentelemetry_sdk::resource::EnvResourceDetector::new())])
      .build()
  ```
  Add `pub fn test_only_build_resource(service_name: &str) -> Resource { resource(service_name) }` gated `#[cfg(feature = "otel")]`.
- [ ] **Step 0.1.4: Verify PASS** — same cargo command above.
- [ ] **Step 0.1.5: Full daemon validation** — `cargo test --all-features -p hippo-daemon && cargo clippy --all-targets --all-features -- -D warnings && cargo fmt --check`.
- [ ] **Step 0.1.6: Commit** — `feat(daemon): merge EnvResourceDetector into OTel Resource builder`.

### Task 0.2: Brain `/control/pause` and `/control/resume` endpoints

**Files:**
- Modify: `brain/src/hippo_brain/server.py` (around line 1350 where `/health` is defined)
- Test: `brain/tests/test_brain_control_endpoints.py` (create)

- [ ] **Step 0.2.1: Write failing tests** — pytest with starlette TestClient. Tests: pause sets state, idempotent, resume clears, idempotent, /health reflects paused.
- [ ] **Step 0.2.2: FAIL** — `uv run --project brain --extra dev pytest brain/tests/test_brain_control_endpoints.py -v`.
- [ ] **Step 0.2.3: Implement** — in `BrainServer.__init__`, set `self._paused = False` and `self._instance_id = uuid.uuid4().hex` (also surfaced via `/health`). Add async route handlers `pause()` and `resume()` returning `JSONResponse` per spec. Append routes to the route list. In the enrichment loop, early-`continue` when `self._paused`.
- [ ] **Step 0.2.4: PASS** — same pytest command.
- [ ] **Step 0.2.5: Full brain lint+test** — `uv run --project brain --extra dev ruff check brain/ && pytest brain/tests -v -x`.
- [ ] **Step 0.2.6: Commit** — `feat(brain): /control/pause and /control/resume HTTP endpoints`.

---

## Phase 1: Bench data layer

### Task 1.1: Snapshot module

**Files:**
- Create: `brain/src/hippo_brain/bench/snapshot.py`
- Test: `brain/tests/test_bench_snapshot.py`

- [ ] **Step 1.1.1: Write failing test** — covers: snapshot creation writes file + corpus_meta row; sha256 hash deterministic across calls; `read_schema_version` returns the pinned value; `copy_snapshot_to` copies bytes and asserts schema match; raises `SchemaVersionMismatch` on mismatch.
- [ ] **Step 1.1.2: FAIL** — pytest.
- [ ] **Step 1.1.3: Implement** — `snapshot.py` exports `create_snapshot`, `compute_snapshot_hash`, `read_schema_version`, `copy_snapshot_to`, `SchemaVersionMismatch`. Uses SQLite's `Connection.backup()` for consistent point-in-time copy. Writes a `corpus_meta(corpus_version, schema_version, created_at_iso)` table. `copy_snapshot_to` validates `schema_version` BEFORE copying.
- [ ] **Step 1.1.4: PASS** — pytest.
- [ ] **Step 1.1.5: Lint** — ruff check + format.
- [ ] **Step 1.1.6: Commit** — `feat(bench): snapshot module for corpus materialization and per-run seed`.

### Task 1.2: Corpus v2 sampler (time-bucketed proportional)

**Files:**
- Modify (rewrite): `brain/src/hippo_brain/bench/corpus.py`
- Test: `brain/tests/test_bench_corpus_v2.py`

- [ ] **Step 1.2.1: Write failing tests** — tests: `build_corpus_v2` emits `.sqlite` + `.jsonl` atomically; JSONL and SQLite encode the same event set; sampling deterministic given same seed; per-source minimum honored; per-source proportional fill above the floor.
- [ ] **Step 1.2.2: FAIL** — pytest.
- [ ] **Step 1.2.3: Implement** — `corpus.py` exposes `CorpusBuildSpec`, `SamplingResult`, `build_corpus_v2(source_db, out_dir, spec)`, `load_corpus_jsonl(path)`. Algorithm: compute time-bucket edges from source DB max ts; per (source, bucket) cell compute quota = floor + proportional share; query candidates per cell with `is_enrichment_eligible` filter and probe-tag exclusion; seeded `random.shuffle`; copy sampled rows + matching `*_enrichment_queue` rows into a temp DB; `create_snapshot` to final path; emit JSONL sidecar with redacted content via `redact_text`.
- [ ] **Step 1.2.4: PASS** — pytest.
- [ ] **Step 1.2.5: Lint**.
- [ ] **Step 1.2.6: Commit** — `feat(bench): time-bucketed proportional sampler + snapshot+JSONL output`.

### Task 1.3: Adversarial overlay + `corpus add-adversarial` CLI

**Files:**
- Modify: `brain/src/hippo_brain/bench/corpus.py` (append overlay functions)
- Modify: `brain/src/hippo_brain/bench/cli.py` (add subcommand)
- Modify: `brain/src/hippo_brain/bench/paths.py` (add `overlay_path`)
- Test: `brain/tests/test_bench_corpus_v2.py` (extend)

- [ ] **Step 1.3.1: Add overlay tests** — empty overlay starts at 0; `add_to_overlay` increments count; cap at 50 raises `OverlayFull`.
- [ ] **Step 1.3.2: FAIL** — pytest.
- [ ] **Step 1.3.3: Implement overlay** — `corpus.py` adds `OverlayFull`, `_OVERLAY_CAP=50`, `create_overlay`, `read_overlay_count`, `add_to_overlay`. Schema mirrors main snapshot + extra `overlay_log` table tracking source, event_id, reason, added_at_iso.
- [ ] **Step 1.3.4: Wire CLI** — `corpus add-adversarial <event_id> --source <s> --reason <r> --db-path <p> --corpus-version corpus-v2`.
- [ ] **Step 1.3.5: PASS** — pytest + manual smoke (`uv run --project brain hippo-bench corpus add-adversarial 1 --source shell --reason test --db-path <tmp>`).
- [ ] **Step 1.3.6: Commit** — `feat(bench): adversarial overlay + corpus add-adversarial CLI`.

### Task 1.4: Q/A set extension and loader

**Files:**
- Modify: `brain/src/hippo_brain/_fixtures/eval_questions.json` (extend to 100+)
- Create: `brain/src/hippo_brain/bench/qa_set.py`
- Test: `brain/tests/test_bench_qa_set.py`

- [ ] **Step 1.4.1: Write failing tests** — load_qa_set returns QAItem dataclass instances; filter_by_corpus keeps items whose golden_event_id parses to a (source, id) in the available set; dropped items returned separately.
- [ ] **Step 1.4.2: FAIL** — pytest.
- [ ] **Step 1.4.3: Implement** — `qa_set.py` exports `QAItem` (frozen dataclass with `qa_id`, `question`, `golden_event_id`, `source_filter`, `acceptable_answer_keywords`, `tags`, `parse_golden() -> tuple[str, int]`), `load_qa_set(path)`, `filter_by_corpus(items, available_event_ids)`.
- [ ] **Step 1.4.4: PASS** — pytest.
- [ ] **Step 1.4.5: Hand-curate Q/A extension** — extend `_fixtures/eval_questions.json` from 40 to 100+ items. Target distribution: 40 shell, 30 claude, 20 browser, 10 workflow. This step requires operator collaboration to source `golden_event_id` references from real prod hippo data.
- [ ] **Step 1.4.6: Commit** — `feat(bench): Q/A set loader + extend seed Q/A to 100+ items`.

---

## Phase 2: Bench process layer

### Task 2.1: Shadow stack module

**Files:**
- Create: `brain/src/hippo_brain/bench/shadow_stack.py`
- Test: `brain/tests/test_bench_shadow_stack.py`

- [ ] **Step 2.1.1: Write failing tests** — using a fake-daemon Python script (writes to a file, sleeps until SIGTERM): `start()` creates a process group; `stop()` reaps both children with no orphans; `build_env()` injects OTEL_RESOURCE_ATTRIBUTES, OTEL_EXPORTER_OTLP_ENDPOINT, HIPPO_OTEL_ENABLED, XDG_DATA_HOME.
- [ ] **Step 2.1.2: FAIL** — pytest.
- [ ] **Step 2.1.3: Implement** — `shadow_stack.py` exports `ShadowStackConfig` (dataclass), `ShadowStack`. `start()` spawns daemon with `start_new_session=True` (creates process group); spawns brain into the same group via `preexec_fn=lambda: os.setpgid(0, pgid)`. `stop(timeout_sec)` sends SIGTERM to pgid via `os.killpg`, polls `os.killpg(pgid, 0)` until ProcessLookupError, sends SIGKILL on timeout.
- [ ] **Step 2.1.4: PASS** — pytest.
- [ ] **Step 2.1.5: Commit** — `feat(bench): shadow-stack process-group spawn + clean teardown`.

### Task 2.2: Pause RPC client

**Files:**
- Create: `brain/src/hippo_brain/bench/pause_rpc.py`
- Test: `brain/tests/test_bench_pause_rpc.py`

- [ ] **Step 2.2.1: Write failing tests** — against a Starlette stub server: `pause()` then `resume()` flips state; unreachable URL raises `BrainNotReachable`; `assert_same_instance()` raises `ProdBrainRestarted` when `instance_id` changes between calls.
- [ ] **Step 2.2.2: FAIL** — pytest.
- [ ] **Step 2.2.3: Implement** — `pause_rpc.py` exports `PauseRpcClient(base_url, http=None)`, `BrainNotReachable`, `ProdBrainRestarted`. `pause()` records `instance_id` from `/health`, posts `/control/pause`. `resume()` posts `/control/resume`. `assert_same_instance()` re-reads `/health`, raises if `instance_id` changed.
- [ ] **Step 2.2.4: PASS** — pytest.
- [ ] **Step 2.2.5: Add `instance_id` to brain server `/health`** — bump `BrainServer` to set `self._instance_id = uuid.uuid4().hex` at construction; surface in `/health` JSON. Update `test_brain_control_endpoints.py` to assert this.
- [ ] **Step 2.2.6: Commit** — `feat(bench): pause-RPC client + instance_id surfaced for restart detection`.

---

## Phase 3: Downstream-proxy gate

### Task 3.1: Per-mode retrieval scorer

**Files:**
- Create: `brain/src/hippo_brain/bench/downstream_proxy.py`
- Test: `brain/tests/test_bench_downstream_proxy.py`

- [ ] **Step 3.1.1: Write failing tests** — Hit@K correctness at multiple ranks; MRR = 1/rank or 0; NDCG@10 = 1/log2(rank+1); aggregate over a list returns means.
- [ ] **Step 3.1.2: FAIL** — pytest.
- [ ] **Step 3.1.3: Implement** — `downstream_proxy.py` exports `RetrievalScore` (frozen dataclass with hit_at_1, hit_at_3, hit_at_5, hit_at_10, mrr, ndcg_at_10, qa_id, golden_event_id), `score_retrieval(ranked_results, golden_event_id, qa_id)`, `aggregate_mode_scores(items)`.
- [ ] **Step 3.1.4: PASS** — pytest.
- [ ] **Step 3.1.5: Commit** — `feat(bench): downstream-proxy retrieval scoring (Hit@K, MRR, NDCG)`.

### Task 3.2: Run downstream-proxy gate against bench DB

**Files:**
- Modify: `brain/src/hippo_brain/bench/downstream_proxy.py` (add runner)
- Modify: `brain/tests/test_bench_downstream_proxy.py` (extend)

- [ ] **Step 3.2.1: Add runner test** — uses a fake `search_fn` returning per-mode deterministic results; asserts `result["modes"]["hybrid"|"semantic"|"lexical"]` shape.
- [ ] **Step 3.2.2: FAIL** — pytest.
- [ ] **Step 3.2.3: Implement runner** — `run_downstream_proxy(bench_db_path, qa_items, search_fn=None)`. Default `search_fn` binds `hippo_brain.retrieval.search` to the bench DB at `bench_db_path`. Iterates 3 modes × N Q/A items; aggregates per-mode.
- [ ] **Step 3.2.4: PASS** — pytest.
- [ ] **Step 3.2.5: Commit** — `feat(bench): downstream-proxy runner (per-mode scoring against bench DB)`.

---

## Phase 4: Bench orchestration update

### Task 4.1: Preflight v2

**Files:**
- Modify: `brain/src/hippo_brain/bench/preflight.py`
- Modify: `brain/tests/test_bench_preflight.py`

- [ ] **Step 4.1.1: Write failing tests** — `check_snapshot_present` pass/fail; `check_snapshot_schema_match` pass/fail; `check_prod_brain_pause_able` pass/skip-on-unreachable.
- [ ] **Step 4.1.2: FAIL** — pytest.
- [ ] **Step 4.1.3: Implement** — append `check_snapshot_present(snapshot_path)`, `check_snapshot_schema_match(snapshot_path, expected)`, `check_prod_brain_pause_able(base_url, http_client=None)`. Each returns the existing `CheckResult` shape with `status` ∈ {`pass`, `warn`, `fail`, `skip`}. Pause-check verifies reachability by issuing `pause` then `resume` (no state left behind).
- [ ] **Step 4.1.4: PASS** — pytest.
- [ ] **Step 4.1.5: Commit** — `feat(bench): v2 preflight checks (snapshot, schema, prod-brain-pause)`.

### Task 4.2: Coordinator v2 — per-model lifecycle

**Files:**
- Modify (rewrite): `brain/src/hippo_brain/bench/coordinator.py`
- Modify: `brain/tests/test_bench_coordinator.py`

- [ ] **Step 4.2.1: Write failing test** — `run_one_model` happy-path test mocking `ShadowStack`, `_wait_for_queue_drain`, `run_downstream_proxy`. Asserts: `process_ready_ms` recorded; `downstream_proxy` attached; `attempt` records emitted with correct `purpose`; teardown called even on exception.
- [ ] **Step 4.2.2: FAIL** — pytest.
- [ ] **Step 4.2.3: Implement** — `coordinator.py` exports `ModelRunResult` and `run_one_model(model_id, run_id, cfg, snapshot_path, qa_items)`. Lifecycle:
  1. Create per-model run-tree dir
  2. `copy_snapshot_to(snapshot_path, bench_db, expected_schema=EXPECTED_SCHEMA_VERSION)`
  3. Build OTel resource attrs `{service.namespace, bench.run_id, bench.model_id, bench.corpus_version}`
  4. `ShadowStack(cfg).start()`; record `spawn_start = time.monotonic()`
  5. `_wait_for_ready` polls daemon socket + brain `/health`; record `process_ready_ms`
  6. `_run_warmup(count=3)` via daemon socket
  7. `MetricsSampler(cadence_ms=250).start()`
  8. `_wait_for_queue_drain(bench_db)` polls `*_enrichment_queue` for status pending|processing
  9. `filter_by_corpus(qa_items, _available_event_ids(bench_db))`
  10. `run_downstream_proxy(bench_db_path=bench_db, qa_items=ds_qa)`
  11. `_run_self_consistency_pass(...)` (carry-forward v1)
  12. `sampler.stop()`; gather peaks
  13. `_cooldown_until_settled(timeout_sec=90)` polls `os.getloadavg()[0] <= 2.0`
  14. `finally: stack.stop(timeout_sec=10)`
- [ ] **Step 4.2.4: PASS** — pytest.
- [ ] **Step 4.2.5: Commit** — `feat(bench): v2 per-model coordinator with shadow stack + queue drain`.

### Task 4.3: Orchestrate v2 — pause RPC + per-model dispatch

**Files:**
- Modify: `brain/src/hippo_brain/bench/orchestrate.py`
- Modify: `brain/tests/test_bench_orchestrate.py`

- [ ] **Step 4.3.1: Write failing tests** — pause/resume sequencing; per-model dispatch happy path; mid-run `ProdBrainRestarted` writes `run_end` with `reason: "prod_brain_restarted"` and exits non-zero; `--skip-prod-pause` skips the RPC.
- [ ] **Step 4.3.2: FAIL** — pytest.
- [ ] **Step 4.3.3: Implement** — `orchestrate_run(args, config)` flow: optional `PauseRpcClient.pause()`; allocate `run_id`; build `run_manifest` per spec; instantiate `RunWriter`; iterate `args.models`, calling `run_one_model` for each, writing `model_summary` records, calling `pause_client.assert_same_instance()` between models; final `run_end` with completion reason; `finally: pause_client.resume()` on every exit path.
- [ ] **Step 4.3.4: PASS** — pytest.
- [ ] **Step 4.3.5: Commit** — `feat(bench): orchestrate v2 wires pause RPC + per-model dispatch`.

---

## Phase 5: CLI + output format

### Task 5.1: New CLI flags

**Files:**
- Modify: `brain/src/hippo_brain/bench/cli.py`
- Modify: `brain/tests/test_bench_cli.py`

- [ ] **Step 5.1.1: Add `run` subparser flags** — `--auto-pause` (default True), `--skip-prod-pause` (sets `--auto-pause` False), `--with-ask-synthesis`, `--ask-synthesis-sample 10`, `--corpus-version corpus-v2`.
- [ ] **Step 5.1.2: Add `corpus init` flags** — `--corpus-days 90`, `--corpus-buckets 9`, `--shell-min 50`, `--claude-min 50`, `--browser-min 50`, `--workflow-min 50`, `--target-total 1000`, `--bump-version`.
- [ ] **Step 5.1.3: Add tests** — extend `test_bench_cli.py` with arg-parsing assertions for each new flag.
- [ ] **Step 5.1.4: Commit** — `feat(bench): v2 CLI flags (--auto-pause, corpus init time-window, ask-synthesis)`.

### Task 5.2: Output format deltas

**Files:**
- Modify: `brain/src/hippo_brain/bench/output.py`
- Modify: `brain/src/hippo_brain/bench/summary.py`
- Modify: `brain/tests/test_bench_output.py`, `test_bench_summary.py`

- [ ] **Step 5.2.1: Extend records** — `RunManifestRecord` adds `corpus_schema_version`, `eval_qa_version` (string), `embedding_model`, `host_baseline`, `prod_state_at_start`. `AttemptRecord.purpose` enum: add `"warmup"`, `"downstream_proxy"`, `"ask_synthesis"`. `ModelSummaryRecord` adds `process_ready_ms`, `queue_drain_wall_clock_sec`, `downstream_proxy`, `prod_brain_restarted_during_bench`, `timeout_during_drain`. `RunEndRecord` adds `prod_brain_resumed_ok`, `models_with_prod_restart_event`.
- [ ] **Step 5.2.2: Update summary aggregation** — `aggregate_model_summary` populates new fields from `ModelRunResult`.
- [ ] **Step 5.2.3: Tests** — assert new fields present in serialized JSONL.
- [ ] **Step 5.2.4: Commit** — `feat(bench): v2 output record deltas`.

---

## Phase 6: Telemetry integration test

### Task 6.1: Telemetry isolation integration test

**Files:**
- Test: `brain/tests/test_bench_telemetry_isolation.py` (create)

- [ ] **Step 6.1.1: Write integration test** — spawns a real `ShadowStack` with a stub daemon binary; reads spawned process env via `ps eww <pid>` (macOS); asserts `OTEL_RESOURCE_ATTRIBUTES` contains `service.namespace=hippo-bench` and `bench.run_id=<expected>`.
- [ ] **Step 6.1.2: Pass** — pytest.
- [ ] **Step 6.1.3: Commit** — `test(bench): integration test for telemetry namespace isolation`.

---

## Phase 7: Grafana dashboards

### Task 7.1: New bench dashboards

**Files:**
- Create: `otel/grafana/dashboards/bench-run-overview.json`, `bench-model-drilldown.json`, `bench-model-comparison.json`

- [ ] **Step 7.1.1: Author dashboards** — every panel filters `{service_namespace="hippo-bench"}`. `bench-run-overview` has variable `bench_run_id`, panels for per-model p50/p95 latency, schema validity, downstream-proxy hit_at_5, system peaks. `bench-model-drilldown` has variables `bench_run_id`, `bench_model_id`, per-event distributions, span trace links. `bench-model-comparison` is multi-select side-by-side.
- [ ] **Step 7.1.2: Validate JSON** — `for f in otel/grafana/dashboards/bench-*.json; do python -m json.tool "$f" > /dev/null; done`.
- [ ] **Step 7.1.3: Commit** — `feat(otel): bench v2 Grafana dashboards`.

### Task 7.2: Filter prod dashboards by `service_namespace=""`

**Files:**
- Modify: `otel/grafana/dashboards/hippo-overview.json`, `hippo-daemon.json`, `hippo-enrichment.json`, `hippo-processes.json`

- [ ] **Step 7.2.1: Add namespace filter to every panel** — write `scripts/add_namespace_filter.py` to walk panel `targets[].expr` fields and inject `service_namespace=""` label selector. Manually verify each dashboard parses + renders.
- [ ] **Step 7.2.2: Validate JSON** — same json.tool loop.
- [ ] **Step 7.2.3: Commit** — `fix(otel): prod dashboards filter on service_namespace='' to exclude bench`.

---

## Phase 8: Acceptance + documentation

### Task 8.1: Smoke test (`--dry-run`)

- [ ] **Step 8.1.1: Run** — `uv run --project brain hippo-bench run --dry-run --models qwen3.5-35b-a3b`. Expect exit 0; output JSONL contains `run_manifest` + `run_end` (`reason: "dry_run"`); zero `attempt` records.
- [ ] **Step 8.1.2: Inspect** — `jq -c '.record_type' ~/.local/share/hippo-bench/runs/run-*.jsonl | sort | uniq -c`.

### Task 8.2: Update bench README for v2

**Files:**
- Modify: `brain/src/hippo_brain/bench/README.md`

- [ ] **Step 8.2.1: Rewrite README** — describe v2 architecture (shadow stack, snapshot, downstream-proxy), new CLI flags, new output records, updated "what it does NOT measure" section. Cross-link to v2 design doc.
- [ ] **Step 8.2.2: Commit** — `docs(bench): v2 README`.

### Task 8.3: Acceptance criteria validation

Walk through spec §"Acceptance Criteria" 10 items; verify each:

- [ ] **AC1:** `corpus init` writes both formats atomically; `corpus verify` passes.
- [ ] **AC2:** `run --models <one>` produces `run_manifest` + ≥1000 `attempt` records + `model_summary` + `run_end`.
- [ ] **AC3:** Pre-flight aborts cleanly when prod brain unreachable, snapshot mismatched, or LM Studio absent.
- [ ] **AC4:** `sha256sum ~/.local/share/hippo/hippo.db` identical before/after a bench run.
- [ ] **AC5:** Grafana query `{service_namespace=""}` returns zero hippo-bench data during a bench run.
- [ ] **AC6:** Prod dashboards updated; prod brain enrichment loop resumes within 10s of `run_end`.
- [ ] **AC7:** `downstream_proxy` populated for ≥80% of Q/A items.
- [ ] **AC8:** No orphaned `hippo-daemon` or `hippo-brain` processes after teardown (`pgrep` returns nothing).
- [ ] **AC9:** All v1 tests still pass; v2 tests pass.
- [ ] **AC10:** README.md cross-links spec; design doc not edited (history-preserving).

---

## Self-Review

**Spec coverage:** All 11 locked decisions → tasks. Q1 (corpus) → Phase 1. Q2 (ephemeral) → Phase 2 / Task 2.1. Q3 (pause RPC) → Task 0.2 + 2.2. Q4 (snapshot) → Tasks 1.1-1.2. Q5 (telemetry) → Task 0.1 + 6.1 + 7.2. Q6 (downstream-proxy) → Phase 3. Q7 (overlay) → Task 1.3. Q8 (time window) → Task 5.1. Q9 (4 sources) → corpus.py. Q10 (versioning) → snapshot.py + corpus init `--bump-version`. Q11 (manual scheduling) → no scheduling code.

**Placeholder scan:** Tasks 1.2, 4.2 contain skeleton code with helpers named but bodies sketched (e.g., `_wait_for_queue_drain` algorithm specified but not full Python). These are intentional — pattern-discoverable from existing v1 code. Task 1.4 acknowledges hand-curation. No "TBD"/"TODO"/"figure out later" terms.

**Type consistency:** `ShadowStack`, `ShadowStackConfig`, `PauseRpcClient`, `BenchConfig`, `RetrievalScore`, `QAItem`, `ModelRunResult`, `SamplingResult`, `CorpusBuildSpec` — used consistently. `OTEL_RESOURCE_ATTRIBUTES` env var name preserved. `EXPECTED_SCHEMA_VERSION` constant referenced consistently.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-27-hippo-bench-v2-implementation.md`. Three execution options:

**1. Subagent-Driven (recommended for interactive iteration)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints.

**3. Ralph-Loop Autonomous** — A separate background agent is producing a ralph-loop-optimized variant of this plan + a `claude -p` runner script. That output arrives separately and supports hands-off-overnight execution.

Which approach?
