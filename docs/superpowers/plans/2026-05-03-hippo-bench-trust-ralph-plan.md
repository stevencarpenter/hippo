# Hippo-Bench Trust Initiative — Ralph Loop Plan

**Status:** Ready for autonomous execution
**Tracking doc:** `docs/superpowers/plans/2026-05-03-hippo-bench-trust-tracking.md`
**State file:** `.ralph/hippo-bench-trust-state.json`
**Task prefix:** `BT-`
**Branch:** `feat/bench-trust` (off main `c2f3ff3`)

---

## How to Read This Plan

Each task has:
- **ID** — `BT-NN`, referenced by `deps`
- **Phase** — tasks in the same phase are order-independent; pick any whose deps are `completed`
- **Budget** — soft estimate; hard budget is 1.5x, after which the loop marks `blocked`
- **File(s)** — real paths in the worktree, never invented
- **Work** — exactly what to implement; no ambiguity. Code shown is target shape, not literal copy/paste.
- **Verify** — verbatim shell command(s) the loop MUST run; **all must exit 0** for `completed`

**Worktree root:** `/Users/carpenter/projects/hippo/.claude/worktrees/bench`
All paths are relative to the worktree root unless prefixed with `~`.

**Safety invariants** (loop must enforce):
- Never modify `main`. Branch is `feat/bench-trust`.
- Never `git push --force` to a remote. Local pushes only with `--force-with-lease` if needed.
- Never delete data under `~/.local/share/hippo/` or `~/.config/hippo/`.
- Never call `mise run nuke` or any LaunchAgent removal during the loop.
- After every task: `git status` — there should be no untracked files outside this plan's scope.

**BT-22 is the absolute last task.** It is the only task that needs creative human judgment (writing `acceptable_answer_keywords` requires domain knowledge of what answers should contain). It depends on **every other task** so the loop reaches it only when nothing else can run. If BT-22 cannot be completed autonomously, the loop **must halt** and surface the question to the operator — do not skip it silently, do not mark it complete with empty content, do not heuristically guess keywords just to mark it done. Better to halt and ask than to ship inert keywords.

---

## State File Schema

`.ralph/hippo-bench-trust-state.json`:

```json
{
  "schema_version": 1,
  "plan_file": "docs/superpowers/plans/2026-05-03-hippo-bench-trust-ralph-plan.md",
  "branch": "feat/bench-trust",
  "tasks": {
    "BT-01": {"status": "pending", "deps": [], "last_attempt_iso": null, "last_error": null}
  }
}
```

`status` values: `"pending"` | `"in_progress"` | `"completed"` | `"blocked"`

The loop picks the next `pending` task whose every `dep` is `completed`, marks it `in_progress`, attempts it, marks `completed` or `blocked` (with `last_error`), then exits the iteration.

After a task is marked `completed`, the loop **must commit** with message:
```
fix(bench): BT-NN <short title>

<one-line description>

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

The loop **must not** push to remote or merge anything during the run.

---

## Phase Map

```
Phase 0 — Stop the bleed (BT-01..BT-08)
  - Hippo serve fix, orphan teardown, schema-mismatch hardfail, pause lockfile, port preflight, FD leak

Phase 1 — Trust foundation (BT-09..BT-20)
  - Test coverage, daemon CLI alias + bench flag, daemon metrics, watchdog suppression,
    namespace-matcher normalization, golden-output regression test

Phase 2 — Methodology bootstrapping (BT-21..BT-28)
  - acceptable_answer_keywords backfill, Q/A annotation audit, self-consistency runs,
    groundedness check skeleton, judge-LLM scaffolding

Phase 3 — Acceptance gate (BT-29..BT-30)
  - Full test suite green; deliberate-regression injection test
```

The first ralph run targets **Phase 0 + Phase 1** (BT-01..BT-20). Phase 2 needs user input on Q/A annotation methodology before it can be fully executed; Phase 3 runs only after Phase 0+1 are completed.

---

## Tasks (Detailed)

### Phase 0 — Stop the Bleed

#### BT-01 — Bootstrap state file

**Deps:** none
**Budget:** 5 min
**File:** `.ralph/hippo-bench-trust-state.json`

**Work:**

Create `.ralph/` directory if it doesn't exist. Write the initial state JSON with dependencies populated per this plan. Also create `.gitignore` entry for `.ralph/` if not present (state is per-machine).

Initial statuses:
- `BT-01..BT-21, BT-29, BT-30`: `"status": "pending"`
- `BT-23..BT-28` (Phase 2 sketches): `"status": "blocked"`, `"last_error": "design review pending — methodology decisions required from human, see plan section 'Phase 2 — Methodology Bootstrapping' before unblocking"`. The loop must NOT attempt these.
- `BT-22` (final keyword backfill): `"status": "pending"`, `"deps": ["BT-01","BT-02",...,"BT-21","BT-23","BT-24","BT-25","BT-26","BT-27","BT-28","BT-29","BT-30"]` (i.e. depends on every other task). Since BT-23..BT-28 start `blocked`, BT-22 is reachable only after every other pending task is `completed` AND the blocked sketches are unblocked by a human, OR the loop's "ready to run" predicate is `dep.status in ("completed", "blocked")` — see implementation note below.

**Implementation note for the loop's task-picker:** A task is ready when every dep is in status `"completed"` OR `"blocked"`. Treating `blocked` as "satisfied for downstream gating" lets BT-22 attempt without waiting for the sketched Phase 2 tasks to be unblocked. This is intentional — BT-22 should run last regardless of whether the Phase 2 sketches were ever expanded.

**Verify:**
```bash
test -f .ralph/hippo-bench-trust-state.json
python3 -c "import json; s=json.load(open('.ralph/hippo-bench-trust-state.json')); assert s['schema_version']==1; assert len(s['tasks'])==30, f'expected 30 tasks, got {len(s[\"tasks\"])}'"
grep -q "^.ralph/" .gitignore
```

---

#### BT-02 — Fix `hippo serve` invocation

**Deps:** BT-01
**Budget:** 10 min
**File:** `brain/src/hippo_brain/bench/shadow_stack.py`

**Work:**

Line 112 currently calls `[hippo_bin, "serve"]`. The hippo CLI has no `serve` subcommand — it has `hippo daemon run`. Verified by `./target/release/hippo serve` returning `error: unrecognized subcommand 'serve'`.

Change line 112 from `[hippo_bin, "serve"]` to `[hippo_bin, "daemon", "run"]`. Update the comment at line 108 to match. Add a regression comment:

```python
# NOTE: Use "daemon run" — there is no `hippo serve` subcommand. PR #127 shipped
# `[hippo_bin, "serve"]` which silently failed: shadow daemon crashed on spawn,
# brain still came up against the pre-copied corpus DB so JSONL output kept
# appearing while bench had no daemon-side telemetry. Caught by panel review.
```

**Verify:**
```bash
grep -q '"daemon", "run"' brain/src/hippo_brain/bench/shadow_stack.py
! grep -E '\[hippo_bin, "serve"\]' brain/src/hippo_brain/bench/shadow_stack.py
./target/release/hippo daemon run --help 2>&1 | grep -q "Run the daemon" || ./target/release/hippo daemon --help 2>&1 | grep -qE "(Run|run)"
uv run --project brain pytest brain/tests/test_bench_shadow_stack.py -q 2>&1 | tail -3
```

---

#### BT-03 — Wrap `run_one_model_v2` in try/finally(teardown_shadow_stack)

**Deps:** BT-01
**Budget:** 20 min
**File:** `brain/src/hippo_brain/bench/coordinator_v2.py`, `brain/tests/test_bench_coordinator_v2.py` (new)

**Work:**

The body of `run_one_model_v2` (`coordinator_v2.py:200-335`) calls `spawn_shadow_stack` (line 218) then must reach `teardown_shadow_stack` (line 305) for cleanup. Today: any exception between spawn and teardown leaks the shadow stack.

Wrap the body so `stack` (whatever name it uses) is reliably torn down:

```python
def run_one_model_v2(...):
    stack = None
    try:
        stack = spawn_shadow_stack(...)
        ...  # all existing body
        return result
    finally:
        if stack is not None:
            try:
                teardown_shadow_stack(stack)
            except Exception:
                logger.exception("teardown failed for run_one_model_v2 — manual cleanup may be required")
```

Add a new test file `brain/tests/test_bench_coordinator_v2.py`:
- Monkeypatch `spawn_shadow_stack` to return a sentinel.
- Monkeypatch `teardown_shadow_stack` to set a flag.
- Monkeypatch the next call after spawn (`wait_for_brain_ready` or equivalent) to raise `RuntimeError("simulated")`.
- Call `run_one_model_v2` and assert the teardown flag was set.

**Verify:**
```bash
grep -q "stack = None" brain/src/hippo_brain/bench/coordinator_v2.py
grep -q "if stack is not None" brain/src/hippo_brain/bench/coordinator_v2.py
test -f brain/tests/test_bench_coordinator_v2.py
uv run --project brain pytest brain/tests/test_bench_coordinator_v2.py -v 2>&1 | tail -10
uv run --project brain pytest brain/tests/test_bench_ -q 2>&1 | tail -3
```

---

#### BT-04 — Replace `except Exception: pass` with structured error capture

**Deps:** BT-03
**Budget:** 25 min
**File:** `brain/src/hippo_brain/bench/coordinator_v2.py`

**Work:**

Five `except Exception: pass` blocks silently swallow failures (per QA panel report at `coordinator_v2.py:104, 122, 128, 132, 239+`). For each:
1. Find the block.
2. Replace with `except Exception as e: logger.exception("BT-04: <step name> failed: %s", e); errors.append({"step": "<step name>", "error": str(e), "type": type(e).__name__})` where `errors` is a list accumulated through the run.
3. Add `errors: list[dict[str, str]]` to the model_summary dict that gets written to the JSONL output.

Update `output_v2.py::ModelSummaryRecordV2` to include an `errors: list[dict] = field(default_factory=list)` field.

Add test in `brain/tests/test_bench_coordinator_v2.py` that injects a raise in one of the wrapped operations and asserts the error appears in `errors`.

**Verify:**
```bash
# No bare "except Exception" without a body that logs or re-raises.
test "$(grep -A1 'except Exception' brain/src/hippo_brain/bench/coordinator_v2.py | grep -c '^[[:space:]]*pass$')" = "0"
grep -q "errors: list" brain/src/hippo_brain/bench/output_v2.py
uv run --project brain pytest brain/tests/test_bench_coordinator_v2.py -v 2>&1 | tail -10
uv run --project brain ruff check brain/src/hippo_brain/bench/ 2>&1 | tail -3
```

---

#### BT-05 — `_wait_for_queue_drain` hard-fails on missing queue tables

**Deps:** BT-04
**Budget:** 20 min
**File:** `brain/src/hippo_brain/bench/coordinator_v2.py`, `brain/tests/test_bench_coordinator_v2.py`

**Work:**

Currently `_wait_for_queue_drain` (around `coordinator_v2.py:72-109`) catches `sqlite3.OperationalError` per-table and logs a warning if `tables_found == 0`, but still returns `False` (success-as-drained) — masking schema mismatches as "queue drained instantly".

Change the contract:
- After the first iteration of the polling loop, if `tables_found == 0`, raise `RuntimeError(f"_wait_for_queue_drain: no queue tables present in {bench_db} — schema mismatch, refusing to declare drained")`. Do NOT just warn.
- Move the `tables_found == 0` check BEFORE the `total_pending == 0` check so we fail fast on schema, not after two empty polls.

Add test in `test_bench_coordinator_v2.py`:
- Build a sqlite DB with no queue tables.
- Call `_wait_for_queue_drain` with a 5s timeout.
- Assert `RuntimeError` is raised within ~1s (not a 5s timeout).

**Verify:**
```bash
grep -q "no queue tables present" brain/src/hippo_brain/bench/coordinator_v2.py
uv run --project brain pytest brain/tests/test_bench_coordinator_v2.py -k "queue_drain" -v 2>&1 | tail -10
```

---

#### BT-06 — Pause lockfile + crash recovery

**Deps:** BT-01
**Budget:** 30 min
**File:** `brain/src/hippo_brain/bench/pause_rpc.py`, `brain/src/hippo_brain/bench/cli.py`

**Work:**

Add a lockfile-based recovery so that a SIGKILL'd bench leaves prod brain unpaused on the next bench start.

1. In `pause_rpc.py`, add module-level constant `PAUSE_LOCKFILE = Path("~/.local/share/hippo-bench/pause.lock").expanduser()`.
2. Modify `PauseRpcClient.pause()`: write the lockfile atomically (`os.O_CREAT|os.O_EXCL` then rename) **before** `httpx.post(...)`. Body of file is JSON with `{"started_iso": ..., "brain_url": ..., "pid": <bench pid>}`.
3. Modify `PauseRpcClient.resume()`: after successful POST, `unlink(missing_ok=True)` the lockfile.
4. Add a new module-level function `recover_stale_pause(default_brain_url: str) -> bool`: if lockfile exists, read it, send POST to `<brain_url>/control/resume`, unlink the file, return `True`. Return `False` if no lockfile.
5. In `cli.py`, add `recover` subcommand: calls `recover_stale_pause`, prints result. Also call `recover_stale_pause` automatically at the top of `_cmd_run` before any other action — if the prior run was killed, recover before starting.

Add tests in a new file `brain/tests/test_bench_pause_recovery.py`:
- `test_lockfile_written_on_pause` — assert file exists after pause.
- `test_lockfile_removed_on_resume` — assert file gone after resume.
- `test_recover_resumes_when_stale_lockfile_present` — pre-create lockfile, assert recovery POST is sent.

**Verify:**
```bash
grep -q "PAUSE_LOCKFILE" brain/src/hippo_brain/bench/pause_rpc.py
grep -q "recover_stale_pause" brain/src/hippo_brain/bench/pause_rpc.py
grep -q "recover" brain/src/hippo_brain/bench/cli.py
test -f brain/tests/test_bench_pause_recovery.py
uv run --project brain pytest brain/tests/test_bench_pause_recovery.py -v 2>&1 | tail -10
```

---

#### BT-07 — Port-conflict preflight on shadow brain port

**Deps:** BT-01
**Budget:** 15 min
**File:** `brain/src/hippo_brain/bench/preflight_v2.py`, `brain/src/hippo_brain/bench/coordinator_v2.py`

**Work:**

Today `coordinator_v2.py:217` hard-codes `brain_port=18923` per model. If a prior model leaked the brain process, the next spawn races on the port silently.

1. In `preflight_v2.py`, add `check_brain_port_free(port: int) -> CheckResult`. Use `socket.socket(AF_INET, SOCK_STREAM); s.bind(("127.0.0.1", port)); s.close()` — if `OSError`, return fail. Otherwise pass.
2. Add it to `run_all_preflight_v2` (returns hard-fail if not free).
3. In `coordinator_v2.py`'s per-model loop: BEFORE `spawn_shadow_stack`, call `check_brain_port_free(brain_port)`; if it fails, raise with a clear message including the result of `lsof -i :{port}` (subprocess shell-out, swallow if lsof missing).

**Verify:**
```bash
grep -q "check_brain_port_free" brain/src/hippo_brain/bench/preflight_v2.py
grep -q "check_brain_port_free" brain/src/hippo_brain/bench/coordinator_v2.py
uv run --project brain pytest brain/tests/test_bench_ -q 2>&1 | tail -3
```

---

#### BT-08 — Connection-leak fix in queue-drain poll loop

**Deps:** BT-05
**Budget:** 20 min
**File:** `brain/src/hippo_brain/bench/coordinator_v2.py`

**Work:**

`_wait_for_queue_drain` opens a fresh `sqlite3.connect(...)` on every poll without WAL or busy_timeout, and the `conn.close()` isn't in a `try/finally`. Per Python panel, on long drains this leaks file descriptors and hits macOS's 256-fd limit.

Restructure the poll loop:

```python
import contextlib

while time.monotonic() < deadline:
    total_pending = 0
    tables_found = 0
    try:
        with contextlib.closing(sqlite3.connect(str(bench_db), timeout=5.0)) as conn:
            conn.execute("PRAGMA busy_timeout = 5000")
            # ... per-table count loop
        # ... post-loop schema-mismatch check, total_pending check
    except sqlite3.OperationalError as e:
        logger.warning("transient sqlite error in queue drain: %s", e)
        # treat as non-drained, keep polling
    time.sleep(poll_interval_sec)
```

**Verify:**
```bash
grep -q "contextlib.closing" brain/src/hippo_brain/bench/coordinator_v2.py
grep -q "PRAGMA busy_timeout" brain/src/hippo_brain/bench/coordinator_v2.py
uv run --project brain pytest brain/tests/test_bench_coordinator_v2.py -v 2>&1 | tail -10
```

---

### Phase 1 — Trust Foundation

#### BT-09 — Add `Commands::Serve` alias to daemon CLI

**Deps:** BT-02
**Budget:** 25 min
**File:** `crates/hippo-daemon/src/cli.rs`, `crates/hippo-daemon/src/main.rs`

**Work:**

Even though BT-02 fixes the shadow_stack invocation, future-proof by adding `hippo serve` as a documented alias for `hippo daemon run`. This matches `--bench` ergonomics in BT-10.

In `cli.rs::Commands` enum, add:
```rust
/// Run the daemon in foreground (alias for `daemon run`).
Serve {
    #[arg(long)]
    bench: bool,
},
```

In `main.rs`, dispatch `Commands::Serve { bench }` to the same handler as `Commands::Daemon { action: DaemonAction::Run }`, threading `bench` through.

**Verify:**
```bash
cargo build -p hippo-daemon 2>&1 | tail -3
./target/debug/hippo serve --help 2>&1 | grep -qiE "(Run|alias|daemon)"
cargo test -p hippo-daemon 2>&1 | tail -5
```

---

#### BT-10 — Daemon `--bench` flag

**Deps:** BT-09
**Budget:** 30 min
**File:** `crates/hippo-daemon/src/main.rs`, `crates/hippo-daemon/src/watch_claude_sessions.rs`

**Work:**

When `--bench` is passed (via `serve --bench` or `daemon run --bench`):
1. Skip starting the FSEvents Claude session watcher (`watch_claude_sessions::spawn(...)`).
2. Skip any LaunchAgent self-install guard.
3. Refuse to write outside `XDG_DATA_HOME` (assertion at startup: `data_dir.starts_with(env::var("XDG_DATA_HOME"))` — log warning and continue if not).
4. Set OTel resource attribute `hippo.bench_mode=true` (additional to whatever OTEL_RESOURCE_ATTRIBUTES injects).

**Verify:**
```bash
cargo build -p hippo-daemon 2>&1 | tail -3
./target/debug/hippo serve --bench --help 2>&1 || ./target/debug/hippo daemon run --bench --help 2>&1 | head -5
grep -q "bench_mode" crates/hippo-daemon/src/main.rs
cargo test -p hippo-daemon 2>&1 | tail -5
```

---

#### BT-11 — Shadow stack uses `--bench` flag

**Deps:** BT-10
**Budget:** 10 min
**File:** `brain/src/hippo_brain/bench/shadow_stack.py`

**Work:**

Update the daemon spawn to include `--bench`:

```python
daemon_proc = subprocess.Popen(
    [hippo_bin, "daemon", "run", "--bench"],
    ...
)
```

Update test expectations in `test_bench_shadow_stack.py` if they assert exact argv.

**Verify:**
```bash
grep -q '"--bench"' brain/src/hippo_brain/bench/shadow_stack.py
uv run --project brain pytest brain/tests/test_bench_shadow_stack.py -v 2>&1 | tail -10
```

---

#### BT-12 — Smoke-integration test for `run_one_model_v2`

**Deps:** BT-03, BT-04, BT-05
**Budget:** 35 min
**File:** `brain/tests/test_bench_coordinator_v2.py`

**Work:**

Add a `test_run_one_model_v2_smoke` that exercises the full per-model lifecycle with monkey-patched I/O:

- Patch `spawn_shadow_stack` to return a fake `ShadowStack` dataclass with `daemon_proc/brain_proc` as `unittest.mock.MagicMock()` with `.poll()` returning `None`.
- Patch `wait_for_brain_ready` to return `0.05`.
- Patch `_wait_for_queue_drain` to return `False` (drained).
- Patch `score_downstream_proxy` to return a deterministic `{"hit_at_1": 0.4, "mrr": 0.35, ...}`.
- Patch `teardown_shadow_stack` to record it was called.
- Patch `lms.unload_all` and `lms.load` to no-op.
- Call `run_one_model_v2(model_id="test-model", ...)`.
- Assert: `result.downstream_proxy` is populated, `teardown` was called, `errors == []`.
- Run a second variant where `score_downstream_proxy` raises `RuntimeError("synthetic")` — assert errors list contains it AND teardown still called.

**Verify:**
```bash
uv run --project brain pytest brain/tests/test_bench_coordinator_v2.py -k "smoke" -v 2>&1 | tail -10
```

---

#### BT-13 — Tighten `_enrichment_active` against `BaseException`

**Deps:** BT-01
**Budget:** 15 min
**File:** `brain/src/hippo_brain/server.py`, `brain/tests/test_bench_pause_rpc.py`

**Work:**

The `try/finally` around `self._enrichment_active = True` (server.py around line 871-950) clears the flag on `Exception` but the surrounding handler is `except Exception` (line 951), which doesn't catch `BaseException` like `asyncio.CancelledError`. While the inner finally does run on cancellation today, the contract is fragile.

Add explicit `BaseException` resilience: nothing structural changes (the finally already handles it), but add a test that:
1. Starts the brain server.
2. Manually sets `server._enrichment_active = True` (simulating mid-batch).
3. Cancels the enrichment task (`server._enrichment_task.cancel()`).
4. Awaits the task (expect `CancelledError`).
5. Asserts `server._enrichment_active is False`.

This is a regression test against future "improve resilience" refactors that might add `return_exceptions=True` to the gather and accidentally swallow cancellation.

**Verify:**
```bash
uv run --project brain pytest brain/tests/test_bench_pause_rpc.py -k "cancellation or enrichment_active" -v 2>&1 | tail -10
```

---

#### BT-14 — Daemon-side `hippo.bench.queue_depth` gauge

**Deps:** BT-10
**Budget:** 25 min
**File:** `crates/hippo-daemon/src/telemetry.rs`, `crates/hippo-daemon/src/daemon.rs` (or wherever metrics live)

**Work:**

Add a polling task (every 5s when `bench_mode=true`) that runs `SELECT COUNT(*) FROM claude_enrichment_queue WHERE status='pending'` (and the equivalent for shell + browser queues), emits each as `hippo.bench.queue_depth` gauge with attribute `queue_kind` ∈ {"claude", "shell", "browser"}.

Off-bench mode: same gauge can be emitted but every 30s; bench cares about second-resolution.

**Verify:**
```bash
cargo build -p hippo-daemon --features otel 2>&1 | tail -3
grep -q "hippo.bench.queue_depth" crates/hippo-daemon/src/
cargo test -p hippo-daemon --features otel 2>&1 | tail -5
```

---

#### BT-15 — Daemon `hippo.daemon.db_busy_count` counter

**Deps:** BT-10
**Budget:** 15 min
**File:** `crates/hippo-daemon/src/storage.rs` or equivalent

**Work:**

Wherever the daemon retries on `SQLITE_BUSY`, increment a counter `hippo.daemon.db_busy_count`. Search for `BUSY` or `busy_timeout` to find call sites. Wire into the existing OTel metrics provider.

**Verify:**
```bash
cargo build -p hippo-daemon --features otel 2>&1 | tail -3
grep -q "db_busy_count" crates/hippo-daemon/src/
cargo clippy --all-targets -- -D warnings 2>&1 | tail -3
```

---

#### BT-16 — Watchdog pause-window suppression

**Deps:** BT-06
**Budget:** 30 min
**File:** `crates/hippo-daemon/src/watchdog.rs`, schema migration

**Work:**

Add a new SQL table `bench_pause_log` (start_ms, end_ms NULL while active, brain_url). When `PauseRpcClient.pause()` succeeds, INSERT a row with `end_ms=NULL`; on resume, UPDATE setting `end_ms`. Watchdog reads this; if any active row exists OR the most recent end was within last 60s, suppress invariants I-2, I-4, I-8 with a `[--]` (suppressed) status, not `[!!]`.

Add migration to bump schema version (the existing schema migration system in `crates/hippo-core/src/storage.rs`).

**Verify:**
```bash
cargo build -p hippo-daemon 2>&1 | tail -3
grep -q "bench_pause_log" crates/hippo-core/src/
cargo test -p hippo-daemon 2>&1 | tail -5
```

---

#### BT-17 — Normalize `service_namespace` matchers in dashboards

**Deps:** BT-01
**Budget:** 15 min
**File:** `otel/grafana/dashboards/hippo-enrichment.json`, possibly `hippo-overview.json`

**Work:**

Per telemetry panel: `hippo-enrichment.json` uses `service_namespace=""` (empty-string match). This is semantically OK today but a landmine — any new namespace value (e.g., dev, staging) would NOT contaminate but a future emitter sending empty-string would. Normalize to `service_namespace!~".+"` (matches missing OR empty) to match the rest of the dashboards.

Replace ALL occurrences in `hippo-enrichment.json` and any other prod dashboard still using `=""`.

**Verify:**
```bash
! grep -E 'service_namespace=""' otel/grafana/dashboards/hippo-enrichment.json
! grep -E 'service_namespace=""' otel/grafana/dashboards/hippo-overview.json
! grep -E 'service_namespace=""' otel/grafana/dashboards/hippo-processes.json
grep -c 'service_namespace!~".+"' otel/grafana/dashboards/hippo-enrichment.json
```

---

#### BT-18 — Default `--corpus-version` to v2

**Deps:** BT-01
**Budget:** 5 min
**File:** `brain/src/hippo_brain/bench/cli.py`

**Work:**

Per Python panel: `--corpus-version` currently defaults to `corpus-v1`. Bench v2 is the production path. Flip default to `corpus-v2`.

Update CLI argparse default and any associated test that asserts the v1 default.

**Verify:**
```bash
grep -E '"corpus-v2"' brain/src/hippo_brain/bench/cli.py | grep -qiE "(default|Default)"
uv run --project brain pytest brain/tests/test_bench_ -q 2>&1 | tail -3
```

---

#### BT-19 — Golden-output regression test (frozen 20-event fixture)

**Deps:** BT-04, BT-05
**Budget:** 60 min
**File:** `brain/tests/fixtures/golden_corpus_v1/` (new), `brain/tests/test_bench_golden.py` (new)

**Work:**

Build a deterministic golden test that catches retrieval-scoring regressions. Process:

1. Create `brain/tests/fixtures/golden_corpus_v1/` with:
   - `corpus.sqlite` — 20 hand-crafted knowledge nodes (no LM Studio dependency); committed binary.
   - `qa.jsonl` — 8 paired Q/A items with golden_event_ids labeled by hand. 4 should produce perfect rank-1 retrieval; 4 should produce mid-rank.
   - `expected_scores.json` — known-good Hit@1, MRR, NDCG@10 values, computed manually.
2. New test `test_golden_retrieval_scores`: invoke the scoring path with this fixture (no real LM Studio — use a mock embedding fn that returns deterministic vectors based on text hashing). Assert each metric matches `expected_scores.json` to 4 decimal places.
3. New test `test_golden_catches_rank_regression`: run as above but injecting a transformation that swaps ranks 1 and 3 on three Q/A items. Assert the test detects the metric drop (Hit@1 falls by >= 0.30, MRR falls by >= 0.10).

**Verify:**
```bash
test -d brain/tests/fixtures/golden_corpus_v1
test -f brain/tests/fixtures/golden_corpus_v1/expected_scores.json
uv run --project brain pytest brain/tests/test_bench_golden.py -v 2>&1 | tail -10
```

---

#### BT-20 — Phase-0+1 acceptance: full test suite green

**Deps:** BT-02, BT-03, BT-04, BT-05, BT-06, BT-07, BT-08, BT-09, BT-10, BT-11, BT-12, BT-13, BT-14, BT-15, BT-16, BT-17, BT-18, BT-19
**Budget:** 15 min
**File:** none (verification only)

**Work:**

Run the full test suite, lint, format check. All must be green. If any step fails, mark this task `blocked` with the failing command + output.

**Verify:**
```bash
uv run --project brain pytest brain/tests -q 2>&1 | tail -5
uv run --project brain ruff check brain/ 2>&1 | tail -3
uv run --project brain ruff format --check brain/ 2>&1 | tail -3
cargo build -p hippo-daemon 2>&1 | tail -3
cargo clippy --all-targets -- -D warnings 2>&1 | tail -3
cargo fmt --check 2>&1 | tail -3
```

---

### Phase 2 — Methodology Bootstrapping

These tasks need user input (Q/A annotation methodology, judge LLM choice). The ralph loop should attempt each but mark `blocked` with a clear question if it requires human judgment.

#### BT-21 — Audit current Q/A fixture annotation pipeline

**Deps:** BT-20
**Budget:** 30 min
**File:** `docs/baselines/QA-ANNOTATION.md` (new), reads `brain/src/hippo_brain/bench/qa_template.jsonl`

**Work:** Document how the existing 40-question fixture's `golden_event_ids` were derived. If they came from a prior retrieval run (suspected per methodology panel), flag this as leakage and note the items that need re-annotation.

**Verify:**
```bash
test -f docs/baselines/QA-ANNOTATION.md
grep -q "leakage" docs/baselines/QA-ANNOTATION.md || grep -q "provenance" docs/baselines/QA-ANNOTATION.md
```

---

#### BT-22 — Populate `acceptable_answer_keywords` for all current Q/A items (FINAL TASK)

**Deps:** every other task (BT-01..BT-21, BT-23..BT-30)
**Budget:** 60 min
**File:** `brain/src/hippo_brain/bench/qa_template.jsonl`

**This is the absolute last task in the loop. It runs only after every other task has reached `completed` or `blocked`.**

**Work:** Per methodology panel, the synthesis gate is currently inert because this field is empty. For each non-adversarial item in the fixture, add ≥3 keywords that any correct answer should contain (proper-noun, action verb, key entity).

**Halt condition:** If the loop cannot autonomously author appropriate keywords for any item — because the question is ambiguous, the answer requires hippo-specific domain knowledge, or the keywords would be guesses — the loop **MUST**:
1. Mark this task `blocked` with `last_error` set to the specific items that cannot be authored, including each item's `id` and `question`.
2. **Kill the ralph loop entirely** (exit, do not pick up other tasks).
3. Print to the operator a clear message: "BT-22 requires domain input. The following Q/A items need human-authored `acceptable_answer_keywords`: [list]. Without these, the synthesis gate stays inert and the bench cannot validate answer faithfulness. Please review and either (a) author the keywords directly, (b) confirm the heuristic guesses I produced for items I was confident about, or (c) explain why this gate doesn't matter for your model-ranking goal."

Do not silently mark the task complete with placeholder keywords. Do not heuristically generate generic keywords (e.g., the question's nouns). Do not skip the task and proceed.

**Why this strictness:** the methodology panel flagged that `keyword_hit_rate=0.000` across the existing baseline — meaning the field is empty everywhere — and that hides whether models hallucinate. If BT-22 ships with bad keywords, the bench gains a false signal of validity. Halting and asking is the safer failure mode.

**Verify:**
```bash
python3 -c "
import json
items = [json.loads(l) for l in open('brain/src/hippo_brain/bench/qa_template.jsonl')]
non_adv = [i for i in items if not i.get('adversarial')]
missing = [i['id'] for i in non_adv if len(i.get('acceptable_answer_keywords', [])) < 3]
assert not missing, f'items missing keywords: {missing}'
print(f'OK: {len(non_adv)} non-adversarial items, all with >= 3 keywords')
"
```

---

#### BT-23 through BT-28 — Sketch only, requires user input before autonomous execution

The remaining Phase 2 tasks (Q/A expansion to 150+ items, independent annotation pass, self-consistency runs, judge-LLM scaffolding, groundedness check, frozen reference corpus) require methodology decisions the loop should not make alone:

- **BT-23** — Expand Q/A fixture to ≥150 scoreable items (needs annotation strategy)
- **BT-24** — Independent annotation pass (needs labeling guidelines)
- **BT-25** — Self-consistency 2-seed runs (mostly mechanical, can be ralphed)
- **BT-26** — Judge-LLM rubric automation (needs decision: which judge model?)
- **BT-27** — Groundedness check via cosine similarity (mechanical, can be ralphed)
- **BT-28** — Frozen reference corpus snapshot (mechanical, can be ralphed)

The ralph loop should mark these `blocked` with the specific question for the human.

---

### Phase 3 — Acceptance Gate

#### BT-29 — Deterministic-rerun verification

**Deps:** BT-20
**Budget:** 30 min (real LM Studio dependency — may need to skip if not available)
**File:** none (procedural)

**Work:** Run `hippo-bench run --models <one>` three times against the same corpus. Assert MRR delta < 0.02 and Hit@1 delta < 0.02 across runs. If LM Studio is not running on the bench machine during the loop's execution, mark `blocked` with reason "requires real LM Studio".

**Verify:**
```bash
# This task may be skipped in autonomous mode; mark blocked if so.
test -f .ralph/bench-trust-determinism.json && python3 -c "import json; d=json.load(open('.ralph/bench-trust-determinism.json')); assert d['mrr_max_delta'] < 0.02"
```

---

#### BT-30 — Inject-regression verification

**Deps:** BT-19, BT-29
**Budget:** 20 min
**File:** none (procedural)

**Work:** Apply the rank-flip regression patched into Python source temporarily (a 5-line change to `downstream_proxy.py`'s rank computation), run the golden-output test, assert it FAILS with a clear regression message. Revert the change.

This proves the bench actually catches regressions, not just runs to completion.

**Verify:**
```bash
# Already covered by BT-19's `test_golden_catches_rank_regression`.
uv run --project brain pytest brain/tests/test_bench_golden.py::test_golden_catches_rank_regression -v 2>&1 | tail -5
```

---

## Loop Operator Notes

- The state file is per-machine. Don't commit `.ralph/`.
- If `BT-20` fails, the loop should HALT (not continue to Phase 2). Phase 0+1 must be green before any methodology work begins.
- For Phase 2 tasks marked `blocked` with a question, surface the question to the operator and pause the loop.
- After the full loop completes, run `gh pr create` ONLY if the operator has reviewed the commits. Default behavior is to leave the branch local.

---

## Future Work (Beyond This Plan)

These are tracked in the companion document `2026-05-03-hippo-bench-trust-tracking.md` as Phase 3+4 but are NOT part of this ralph plan. They will land in a follow-up plan after Phase 2 stabilizes:

- Quality metrics as durable OTel gauges + Grafana dimensions
- Prometheus alert rules + recording rules
- Trace spans for retrieve → score → judge
- `hippo-bench compare` CLI + verdict
- Resumability via `--start-from-model N`
- GitHub Actions nightly workflow
- Operator runbook for each alert
