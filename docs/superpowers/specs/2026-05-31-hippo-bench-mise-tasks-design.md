# hippo-bench mise task suite — design

**Date:** 2026-05-31
**Status:** approved (brainstorming), pending implementation plan
**Scope:** `mise.toml` + bench docs only. No Python/Rust source changes.

## 1. Problem

The `hippo-bench` suite (a benchmark that scores enrichment models against
hippo's real workload) is fully functional but has **zero `mise` tasks** and a
~6-step cold-start chain that is easy to forget. Operators must remember to seed
the Q/A fixture, init and verify a corpus snapshot, validate Q/A coverage, run,
and read the scorecard — each as a bare `uv run --project brain hippo-bench …`
incantation. There is no single "benchmark this model id" entry point, and the
orchestrator's own console output is never persisted (only the shadow
subprocess logs land in the run tree).

The goal: a `bench:*` mise task suite that makes `mise run bench:run <model-id>`
"just work," automates the safe-to-forget details, and keeps the benchmark a
first-class but clearly-separate activity from day-to-day hippo.

## 2. Key prior fact: isolation is already enforced in code

This is **not** a re-architecture. The bench already routes everything away from
the day-to-day hippo tool path. Verified facts (do not re-implement these — the
tasks rely on them):

- **Separate data root.** All bench artifacts live under
  `~/.local/share/hippo-bench/` (XDG: `$XDG_DATA_HOME/hippo-bench`), a *sibling*
  of prod `~/.local/share/hippo/`, never a child. (`paths.py:hippo_bench_root()`,
  comment: "Sibling of prod hippo data, NOT a child.")
- **Re-rooted shadow stack.** `shadow_stack.py` spawns its own daemon + brain
  with `HOME` and `XDG_DATA_HOME` overridden to the per-run tree and
  `XDG_CONFIG_HOME` removed, `TMPDIR` set to a per-run `mkdtemp`. Enrichment
  writes only to `<run_tree>/hippo.db` (a copy of the corpus snapshot). The
  shadow brain binds a fixed non-prod port (18923).
- **Sandbox assertion.** The daemon, launched as `hippo serve --bench`, hard
  fails at startup ("BT-10/I-4 bench mode sandbox violation") if its DB path
  escapes the run tree. Env mis-threading is a loud crash, not silent prod
  corruption.
- **Prod brain pause/resume.** `pause_rpc.py` POSTs `/control/pause` before the
  per-model runs and `/control/resume` after, guarded by a lockfile at
  `~/.local/share/hippo-bench/pause.lock`, an `atexit` resume, and
  `recover_stale_pause()` (which heals a SIGKILLed run on the next invocation).
- **The only prod touches** are (a) pausing/resuming the prod brain and
  (b) `corpus init` reading the live `~/.local/share/hippo/hippo.db`
  **read-only** to sample a frozen snapshot. The shared inference server is used
  by both (prod paused) and the shadow brain.

The gap this design fills is purely the **ergonomics + transcript-routing
layer**.

## 3. Decisions (locked during brainstorming)

1. **Guided prereqs.** `bench:run` auto-runs only the *safe* preconditions
   (recover stale pause; seed Q/A fixture if missing — both idempotent). If the
   corpus snapshot is missing it **fails fast** with the exact
   `mise run bench:corpus:init` command rather than silently sampling the live
   prod DB.
2. **Tee transcript.** `bench:run` tees the full console transcript to
   `~/.local/share/hippo-bench/runs/<run-stem>.log` next to the results JSONL.
3. **BT-29 guarded.** Include `bench:bt29 <model-id>` (3 runs → determinism
   gate) but refuse to run without `BENCH_BT29_CONFIRM=1`.
4. **Positional model arg.** `mise run bench:run <model-id>` (matches existing
   `vectors:search` / `redact:test` `${1}` style). Comma-separated for multiple.
5. **Keep `bench:status`** (a bench "doctor") and **keep the retrieval-metrics
   line** in `bench:run` output.

## 4. Task suite

All tasks live in `mise.toml` under the `bench:` namespace, matching the
existing `otel:` / `re-enrich:` / `build:` convention (colon-separated, up to
three segments — `build:otel:release` is precedent). Multi-step tasks use the
here-doc `#!/usr/bin/env bash` + `set -euo pipefail` pattern; trivial wrappers
use a single-line `run =`. Every task has a short imperative `description`.

### 4.1 Building blocks

| Task | Behavior |
|---|---|
| `bench:qa:seed` | `uv run --project brain python3 brain/src/hippo_brain/bench/qa_seed.py`. Idempotent copy of the committed `qa_template.jsonl` → `fixtures/eval-qa-v1.jsonl`. |
| `bench:qa:validate` | `hippo-bench qa validate --qa-path <fixtures>/eval-qa-v1.jsonl --corpus-sqlite <fixtures>/$BENCH_CORPUS_VERSION.sqlite --min-scoreable ${BENCH_MIN_SCOREABLE:-1}`. Exit 0/1. |
| `bench:corpus:init` | Prints a banner ("samples your live ~/.local/share/hippo/hippo.db READ-ONLY"). Runs `hippo-bench corpus init --corpus-version $BENCH_CORPUS_VERSION --seed ${BENCH_SEED:-42} [--db-path $BENCH_DB_PATH]`. If the corpus already exists, the CLI exits 1 unless `FORCE=1`, in which case the task passes `--bump-version $BENCH_CORPUS_VERSION`. |
| `bench:corpus:verify` | `hippo-bench corpus verify --corpus-version $BENCH_CORPUS_VERSION`. Exit 0/1. |
| `bench:summary` | `hippo-bench summary <file>`. `${1}` is the run file; if omitted, resolves the newest `runs/run-*.jsonl` (`ls -t … | head -1`) and errors clearly if none exist. |
| `bench:determinism` | `hippo-bench determinism ${@}` — thin passthrough (caller supplies ≥2 JSONL paths and any `--*-budget` / `--mode`). |
| `bench:recover` | `hippo-bench recover` — clears a stale pause lockfile from a crashed run. Idempotent. |
| `bench:status` | NEW inline task (Section 4.4). |

### 4.2 Centerpiece: `bench:run <model-id>`

Here-doc bash, `set -euo pipefail`. Steps:

1. Require `${1}` (model id); usage error if absent.
2. Print the bench-mode banner: separate data root, prod brain will be
   paused/resumed, "not normal hippo execution."
3. `mise run bench:recover` (safe — clear any stale pause).
4. If `fixtures/eval-qa-v1.jsonl` is missing → `mise run bench:qa:seed`.
5. **Corpus guard:** if `fixtures/$BENCH_CORPUS_VERSION.sqlite` is missing, print
   the exact remediation and `exit 1`:
   `mise run bench:corpus:init   # samples your live hippo.db read-only`.
6. Compute a stem: `STEM=run-$(date -u +%Y%m%dT%H%M%SZ)-$(hostname -s)`;
   `RUN_JSONL=$RUNS/$STEM.jsonl`; `RUN_LOG=$RUNS/$STEM.log`.
7. Run, teeing console to the log (preserve exit code via `set -o pipefail`):
   ```
   uv run --project brain hippo-bench run \
     --models "$1" \
     --corpus-version "$BENCH_CORPUS_VERSION" \
     ${BENCH_BASE_URL:+--base-url "$BENCH_BASE_URL"} \
     ${BENCH_EMBEDDING_MODEL:+--embedding-model "$BENCH_EMBEDDING_MODEL"} \
     --out "$RUN_JSONL" 2>&1 | tee "$RUN_LOG"
   ```
8. Print the gate scorecard: `hippo-bench summary "$RUN_JSONL"`.
9. Print the **retrieval scorecard** the stock summary omits, via an inline
   python one-liner that reads `$RUN_JSONL`, finds each `model_summary` record,
   and prints `downstream_proxy.modes.hybrid` `mrr`, `hit_at_1`, `hit_at_5`
   (and a `(retrieval metrics absent — no Q/A fixture)` note when the block is
   `{}`). This is the "performance in the hippo workload" signal.
10. Echo both paths (results JSONL + transcript log).

Exit code is the bench `run` exit code (0 ok / 2 preflight-abort / 3 all models
errored), propagated through the `tee` pipe.

### 4.3 Guarded expensive: `bench:bt29 <model-id>`

Here-doc bash. Refuses unless `BENCH_BT29_CONFIRM=1` (prints why, exit 1). When
confirmed: makes `runs/bt29-<ts>/` and invokes `hippo-bench run` **directly**
(not the `bench:run` wrapper) three times with explicit
`--out runs/bt29-<ts>/rN.jsonl`, so the determinism step has deterministic
paths; each run pauses/resumes prod. It then runs
`hippo-bench determinism runs/bt29-<ts>/r1.jsonl r2.jsonl r3.jsonl`
(honoring `BENCH_CORPUS_VERSION` / `BENCH_BASE_URL` / `BENCH_EMBEDDING_MODEL`
like `bench:run`), prints the determinism verdict, and exits with its code.
Documented as ~90 min and operator-gated (the runbook forbids the autonomous
loop from running it).

### 4.4 `bench:status` (bench doctor)

Inline bash/python, pure reads, never mutates. Prints:

- Bench root path and whether it exists.
- Fixtures present (✓/✗): `$BENCH_CORPUS_VERSION.sqlite`, its `.manifest.json`,
  `eval-qa-v1.jsonl`.
- Latest run: newest `runs/run-*.jsonl`, its `run_end.reason` /
  per-model `tier0_verdict.passed`, and timestamp.
- Stale pause: whether `pause.lock` exists (and its `started_iso`/`pid`).
- Prod brain reachability + paused state via `curl -sf <prod>/health` (best
  effort; "unknown" if unreachable).

Intended as the "is the bench ready, and did I leave prod paused?" glance.

## 5. Conventions & parameters

- **Positional args:** `${1}` = model id (`bench:run`, `bench:bt29`) or run file
  (`bench:summary`). `${@}` passthrough for `bench:determinism`.
- **Env knobs (all optional, with defaults):**
  - `BENCH_CORPUS_VERSION` = `corpus-v2`
  - `BENCH_MIN_SCOREABLE` = `1` (set `100` for the publish-grade Q/A gate)
  - `BENCH_BASE_URL` = unset → CLI reads `[inference].base_url` from prod config
  - `BENCH_EMBEDDING_MODEL` = unset → CLI reads `[models].embedding`
  - `BENCH_DB_PATH` = unset → CLI default `~/.local/share/hippo/hippo.db`
  - `BENCH_SEED` = `42`
  - `FORCE` = unset → set to re-init an existing corpus (`--bump-version`)
  - `BENCH_BT29_CONFIRM` = unset → required `=1` to run `bench:bt29`
- **Paths** (resolved with `${XDG_DATA_HOME:-$HOME/.local/share}/hippo-bench`,
  matching the `otel:up` env pattern): `FIXTURES=$ROOT/fixtures`,
  `RUNS=$ROOT/runs`, `PAUSE=$ROOT/pause.lock`.
- **Invocation:** every wrapper uses `uv run --project brain hippo-bench …`
  (the `run:brain` / `ingest:claude` precedent). No reliance on an installed
  `hippo` binary.

## 6. Documentation (same change, kept pristine)

- New **"Running the bench via mise"** section in
  `brain/src/hippo_brain/bench/README.md`: a table mapping each manual step to
  its task and the cold-start order
  (`bench:qa:seed` → `bench:corpus:init` → `bench:corpus:verify` →
  `bench:qa:validate` → `bench:run <model>` → `bench:summary`), plus the env
  knobs and the `bench:bt29` guard.
- A one-line pointer from `docs/capture/bench-runbook.md` to the mise tasks
  (the runbook keeps the authoritative BT-29 procedure).

## 7. Verification

mise tasks are not unit-tested, so verification is by smoke test:

- `mise run bench:status` on a fresh checkout → reports missing fixtures, no
  crash.
- `mise run bench:run foo` with no corpus → fails fast with the
  `bench:corpus:init` remediation (exit 1), does **not** touch prod.
- `mise run bench:run <real-model>` end to end → produces both
  `runs/<stem>.jsonl` and `runs/<stem>.log`, prints gate + retrieval
  scorecards, and `bench:status` afterward shows no stale `pause.lock`.
- `mise run bench:bt29 foo` without `BENCH_BT29_CONFIRM` → refuses (exit 1).
- `bench --dry-run` path: `BENCH_*`-driven `hippo-bench run --dry-run` (add
  `--skip-checks`) resolves config and writes a manifest without inference
  calls, for wiring validation.
- After edits: `mise.toml` parses (`mise tasks ls` lists the new `bench:*`
  tasks); existing `mise run lint` / `test` unaffected.

## 8. Scope boundaries (YAGNI)

- **No Python/Rust source changes.** No new `hippo-bench qa seed` subcommand —
  `bench:qa:seed` calls `qa_seed.py` directly. The retrieval-metrics line is a
  task-level python one-liner, not a change to `pretty.render_summary_text`.
- **No auto corpus-init** (guided decision): the one prod-DB-reading step stays
  explicit.
- **Single host** (per project constraint): no multi-host/path portability work.
- Not in scope: CI wiring of `bench:bt29`, a corpus GC/retention task, or a
  results dashboard. These can follow if wanted.
