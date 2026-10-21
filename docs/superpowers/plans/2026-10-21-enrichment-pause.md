# Operator Enrichment Pause Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `hippo enrichment pause|resume|status` lets the operator stop LLM enrichment during the day and drain the backlog at night, with no data loss and no alarm noise.

**Architecture:** One small JSON state file in the hippo data directory is the single source of truth. The Rust CLI writes it; the brain, the watchdog, and doctor read it. The brain checks it on every enrichment-loop iteration, so no restart or control call is needed. The existing in-memory bench pause (`/control/pause`) is untouched and independent.

**Tech Stack:** Rust 2024 (`hippo-core`, `hippo-daemon`, clap, anyhow, rusqlite, serde_json), Python 3.14 brain (Starlette), pytest, cargo test.

**Spec:** `docs/superpowers/specs/2026-10-21-enrichment-pause-design.md`

## Global Constraints

- State file: `<storage.data_dir>/enrichment-state.json` (default `~/.local/share/hippo/enrichment-state.json`; honours `XDG_DATA_HOME`). Contents exactly `{"paused": <bool>, "changed_at_ms": <i64>}`.
- File absent: running (today's behavior). File present but unreadable or not matching the schema: treat as **paused** everywhere (fail quiet, never fans), and doctor warns.
- All timestamps are Unix epoch milliseconds.
- Paused: every LLM enrichment path in `_enrichment_loop` (shell, agentic, browser, workflow, auto-memory). Not paused: capture, embed orphan reaper, query embeddings, queries/MCP `ask`, Jev classification and reranking, watchdog, doctor, telemetry.
- Nothing queued is dropped, expired, or marked failed because of a pause. An in-flight batch finishes; nothing is cancelled.
- Bench pause and operator pause never clear each other. Enrichment runs only when neither is active.
- No schema migration; `EXPECTED_VERSION` stays 27.
- Rust: clippy clean with `-D warnings`, `cargo fmt`. Python: `ruff check` and `ruff format`.
- Docs updated in the same PR (Task 6).

## Review Focus

1. **Corrupt or hand-edited state file** (empty, truncated JSON, `{"paused":"yes"}`): every reader treats it as paused, doctor prints `[WW]`, `hippo enrichment resume` overwrites it with a valid running state. Tests in Tasks 1 and 5.
2. **Brain restarted while paused** (launchd restart, reboot, reinstall): the brain comes up paused and enriches nothing. Test in Task 3.
3. **Bench run while operator-paused:** bench `/control/resume` must not start enrichment; `hippo enrichment resume` during a bench pause must not start enrichment either. Tests in Task 3.
4. **Pause taken after oMLX was already down for minutes** (preflight failure count already above 12): no I-12 alarm while paused, and after resume I-12 needs 13 fresh failures. Tests in Tasks 3 and 4.
5. **Resume with oMLX still down:** that is a real outage and I-12 must still fire after about a minute. Test in Task 4.

---

### Task 1: Shared enrichment state (Rust + Python readers, Rust writer)

**Files:**
- Create: `crates/hippo-core/src/enrichment_state.rs`
- Modify: `crates/hippo-core/src/lib.rs` (add `pub mod enrichment_state;`)
- Create: `brain/src/hippo_brain/enrichment_state.py`
- Create: `tests/fixtures/enrichment-state-paused.json` with exactly `{"paused": true, "changed_at_ms": 1792500000000}`
- Test: inline `#[cfg(test)]` in `enrichment_state.rs`; `brain/tests/test_enrichment_state.py`

**Interfaces:**
- Produces (Rust, `hippo_core::enrichment_state`):
  - `pub const STATE_FILE: &str = "enrichment-state.json";`
  - `#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)] pub struct EnrichmentState { pub paused: bool, pub changed_at_ms: i64 }`
  - `pub enum StateRead { Missing, Valid(EnrichmentState), Invalid(String) }` with `pub fn is_paused(&self) -> bool` (`Invalid` ⇒ true) and `pub fn changed_at_ms(&self) -> Option<i64>`
  - `pub fn state_path(data_dir: &Path) -> PathBuf`
  - `pub fn read_state(data_dir: &Path) -> StateRead`
  - `pub fn write_state(data_dir: &Path, paused: bool, now_ms: i64) -> anyhow::Result<EnrichmentState>` — idempotent: if the current valid state already has `paused`, return it unchanged (keeps the original `changed_at_ms`); otherwise write atomically (temp file in the same dir + `rename`).
- Produces (Python, `hippo_brain.enrichment_state`):
  - `@dataclass(frozen=True) class StateRead: paused: bool; changed_at_ms: int | None; valid: bool`
  - `def read_state(data_dir: str | Path) -> StateRead` (missing ⇒ `paused=False, changed_at_ms=None, valid=True`; invalid ⇒ `paused=True, changed_at_ms=None, valid=False`)

- [ ] **Step 1: Write the failing Rust tests** in `enrichment_state.rs`:
  - `missing_file_reads_as_running`: `read_state(tmp)` is `StateRead::Missing` and `!is_paused()`.
  - `write_then_read_round_trips`: `write_state(tmp, true, 1000)` then `read_state` is `Valid(EnrichmentState{paused:true, changed_at_ms:1000})`.
  - `repeat_pause_keeps_original_timestamp`: write paused at 1000, then paused at 2000 ⇒ `changed_at_ms == 1000`.
  - `resume_changes_timestamp`: paused at 1000, running at 3000 ⇒ `Valid{paused:false, changed_at_ms:3000}`.
  - `invalid_contents_read_as_paused`: for each of `""`, `"{"`, `{"paused":"yes"}`, `{"changed_at_ms":1}` ⇒ `Invalid(_)` and `is_paused()`.
  - `writer_output_matches_shared_fixture`: `write_state(tmp, true, 1792500000000)`, parse the written file and `tests/fixtures/enrichment-state-paused.json` (via `env!("CARGO_MANIFEST_DIR")` + `../../tests/fixtures/...`) as `serde_json::Value`; assert equal.
  - `resume_overwrites_invalid_file`: write `"{"` by hand, `write_state(tmp, false, 5)` ⇒ `Valid{paused:false, changed_at_ms:5}`.
- [ ] **Step 2: Run** `cargo test -p hippo-core enrichment_state` — expect compile failure (module missing).
- [ ] **Step 3: Implement** the Rust interfaces above with `serde_json` (`deny_unknown_fields` is not required; missing fields or wrong types must yield `Invalid`).
- [ ] **Step 4: Run** `cargo test -p hippo-core enrichment_state` — expect all pass.
- [ ] **Step 5: Write the failing Python tests** in `brain/tests/test_enrichment_state.py`:
  - `test_missing_file_is_running`: `read_state(tmp_path) == StateRead(paused=False, changed_at_ms=None, valid=True)`.
  - `test_reads_shared_fixture`: copy repo `tests/fixtures/enrichment-state-paused.json` to `tmp_path / "enrichment-state.json"`; result `== StateRead(True, 1792500000000, True)`.
  - `test_invalid_contents_are_paused` parametrized over the same four bad payloads as Rust ⇒ `paused is True and valid is False`.
- [ ] **Step 6: Run** `uv run --project brain pytest brain/tests/test_enrichment_state.py -v` — expect ImportError, then implement `read_state`, rerun — expect pass.
- [ ] **Step 7: Commit** `feat(core): shared enrichment pause state file`.

### Task 2: `hippo enrichment pause|resume|status`

**Files:**
- Modify: `crates/hippo-daemon/src/cli.rs` (new `Commands::Enrichment { #[command(subcommand)] action: EnrichmentAction }` and `pub enum EnrichmentAction { Pause, Resume, Status }`)
- Modify: `crates/hippo-daemon/src/main.rs` (dispatch to `commands::handle_enrichment`)
- Modify: `crates/hippo-daemon/src/commands.rs` (handler + tests)

**Interfaces:**
- Consumes: Task 1 `read_state`, `write_state`, `StateRead`.
- Produces: `pub fn handle_enrichment(config: &HippoConfig, action: &EnrichmentAction, now_ms: i64) -> Result<String>` returning the text to print (main prints it); `pub fn pending_enrichment_counts(conn: &rusqlite::Connection) -> Vec<(&'static str, i64)>` over `shell`→`enrichment_queue`, `agentic`→`agentic_enrichment_queue`, `browser`→`browser_enrichment_queue`, `workflow`→`workflow_enrichment_queue`, `auto-memory`→`memory_enrichment_queue`, counting `status = 'pending'`; a missing table is skipped, not an error.

- [ ] **Step 1: Write failing tests** in `commands.rs` tests using a temp `data_dir` config and a temp DB created with the five queue tables:
  - `enrichment_pause_writes_paused_state`: `Pause` ⇒ `read_state` is `Valid{paused:true, changed_at_ms:now}`; output contains `Enrichment paused`.
  - `enrichment_resume_writes_running_state`: after pause, `Resume` ⇒ `Valid{paused:false,..}`; output contains `Enrichment resumed`.
  - `enrichment_status_reports_state_and_queue_counts`: insert 3 pending rows into `enrichment_queue` and 1 `failed`; status output contains `paused since` (when paused) and `shell` with `3`; does not count the failed row.
  - `enrichment_status_tolerates_missing_tables`: DB with only `enrichment_queue` ⇒ no error.
  - `enrichment_status_flags_invalid_state_file`: write `"{"` ⇒ status output contains `unreadable` and `treated as paused`.
- [ ] **Step 2: Run** `cargo test -p hippo-daemon enrichment_` — expect failure.
- [ ] **Step 3: Implement** the CLI variant, dispatch, `handle_enrichment`, and `pending_enrichment_counts`. Status opens the DB read-only. Output copy:
  - Pause: `Enrichment paused. Capture continues; work queues until \`hippo enrichment resume\`.`
  - Resume: `Enrichment resumed. The brain picks it up within one poll interval.`
  - Status first line: `Enrichment: running` | `Enrichment: paused since <local time>` | `Enrichment: state file unreadable (<path>), treated as paused`; then one `  <queue>  <n> pending` line per existing queue.
- [ ] **Step 4: Run** `cargo test -p hippo-daemon enrichment_` and `cargo run -q --bin hippo -- enrichment --help` — expect pass and three subcommands listed.
- [ ] **Step 5: Commit** `feat(cli): hippo enrichment pause, resume, status`.

### Task 3: Brain honours the operator pause

**Files:**
- Modify: `brain/src/hippo_brain/server.py` (`BrainServer.__init__`, `_enrichment_loop`, `/health` payload near line 570, new `_reset_preflight_failures`)
- Test: `brain/tests/test_operator_enrichment_pause.py`

**Interfaces:**
- Consumes: Task 1 `read_state(self.data_dir)`.
- Produces: `/health` keys `"enrichment_paused": bool` and `"enrichment_paused_since_ms": int | None` (`None` when running or state invalid), and `"enrichment_state_valid": bool`. Doctor (Task 5) reads these.

- [ ] **Step 1: Write failing tests** (reuse fixtures/helpers from `test_bench_pause_rpc.py` / `test_server_extended.py` for a `BrainServer` on a temp DB and data dir, with a stub inference client that records calls):
  - `test_operator_pause_stops_enrichment_claims`: write paused state; run one loop iteration; no queue row changes status from `pending`, stub client received zero chat calls.
  - `test_brain_starting_paused_stays_paused`: paused state on disk before `BrainServer` is constructed ⇒ same assertions (Review Focus 2).
  - `test_resume_on_disk_restarts_enrichment_without_restart`: paused, iterate, write running state, iterate ⇒ pending rows get claimed.
  - `test_bench_resume_does_not_lift_operator_pause`: operator paused, `POST /control/pause` then `/control/resume` ⇒ still no claims (Review Focus 3).
  - `test_operator_resume_does_not_lift_bench_pause`: bench paused, operator state running ⇒ no claims.
  - `test_operator_pause_keeps_queries_and_reaper`: operator paused ⇒ a query endpoint returns 200 (not 503) and `_embed_reaper_tick` still embeds an orphan node.
  - `test_pause_transition_resets_preflight_failures`: `source_health` row `brain-preflight` with `consecutive_failures = 40`; brain observes the paused state ⇒ row is `0`; same for the first observation after construction (Review Focus 4).
  - `test_health_reports_operator_pause`: `/health` has `enrichment_paused: true`, `enrichment_paused_since_ms: 1792500000000`, `enrichment_state_valid: true`; invalid file ⇒ `true`, `None`, `false`.
- [ ] **Step 2: Run** `uv run --project brain pytest brain/tests/test_operator_enrichment_pause.py -v` — expect failures.
- [ ] **Step 3: Implement.** In `_enrichment_loop`, treat `self._paused or read_state(self.data_dir).paused` as the pause condition at **both** existing `if self._paused:` checks (loop top, and the post-wait re-check before `_enrichment_active = True`), so an in-flight batch is never interrupted. Track the last observed operator-paused value (`None` at construction); on any change, call `_reset_preflight_failures()`, which sets `consecutive_failures = 0` for `source = 'brain-preflight'` (swallow missing-table errors like `_record_preflight_to_source_health`). Do not touch `_embed_reaper_tick`, the classification loop, or the query decorator; they keep gating on the bench `_paused` only.
- [ ] **Step 4: Run** the new tests plus `brain/tests/test_bench_pause_rpc.py brain/tests/test_bench_pause_recovery.py` — expect all pass.
- [ ] **Step 5: Commit** `feat(brain): honour the operator enrichment pause`.

### Task 4: Watchdog — no I-12 or I-19 noise from a deliberate pause

**Files:**
- Modify: `crates/hippo-daemon/src/watchdog.rs` (`run` around lines 180–210; `check_i19_stale_memory_projection` at line 802)
- Test: inline tests in `watchdog.rs`

**Interfaces:**
- Consumes: Task 1 `read_state`, `StateRead`; Task 3 behavior (brain resets the preflight counter on transitions).
- Produces: `pub fn check_i19_stale_memory_projection(conn: &Connection, now_ms: i64, resumed_at_ms: Option<i64>) -> Result<Option<InvariantViolation>>`; `pub fn suppress_operator_paused_violations(violations: &mut Vec<InvariantViolation>, state: &StateRead)` which removes `I-12` and `I-19` when `state.is_paused()`.

- [ ] **Step 1: Write failing tests:**
  - `operator_pause_suppresses_i12`: an I-12 violation (`consecutive_failures = 40`) plus an unrelated I-1 violation; `suppress_operator_paused_violations` with paused state ⇒ only I-1 remains.
  - `invalid_state_file_suppresses_like_pause`: same with `StateRead::Invalid` ⇒ I-12 removed.
  - `running_state_keeps_i12`: running state ⇒ I-12 kept (Review Focus 5: resume with oMLX down still alarms once the brain records 13 fresh failures).
  - `i19_measures_staleness_from_resume`: document `updated_at = now - 2h`, `projection_status = 'pending'`; `resumed_at_ms = Some(now - 10min)` ⇒ `None`; `resumed_at_ms = Some(now - 40min)` ⇒ violation; `None` ⇒ violation (today's behavior).
- [ ] **Step 2: Run** `cargo test -p hippo-daemon i12 i19 operator_pause` — expect failures.
- [ ] **Step 3: Implement.** I-19 SQL changes `AND updated_at < ?2` to `AND MAX(updated_at, ?3) < ?2` with `?3 = resumed_at_ms.unwrap_or(0)`. In `run`, read the state once from `config.storage.data_dir`; pass `resumed_at_ms = Some(changed_at_ms)` only for `Valid{paused:false}`; skip the I-19 call when paused; call `suppress_operator_paused_violations` right after `suppress_disabled_auto_memory_violations`. Update the existing I-19 call site and its tests for the new parameter.
- [ ] **Step 4: Run** `cargo test -p hippo-daemon` — expect all pass.
- [ ] **Step 5: Commit** `feat(watchdog): treat an operator enrichment pause as intentional`.

### Task 5: Doctor reports a pause instead of failures

**Files:**
- Modify: `crates/hippo-daemon/src/commands.rs` (`handle_doctor` at line 1252: brain JSON block near line 237, direct inference probe near line 1316; `check_auto_memory_health` at line 3635)
- Test: inline tests in `commands.rs`

**Interfaces:**
- Consumes: Task 1 `read_state`; Task 3 `/health` keys.
- Produces: `fn enrichment_pause_line(state: &StateRead) -> (String, u32)` returning the printed line and the fail-count contribution (always 0); `check_auto_memory_health` gains a `paused: bool` parameter.

- [ ] **Step 1: Write failing tests:**
  - `doctor_pause_line_running`: `Missing` ⇒ `[OK] Enrichment: running`.
  - `doctor_pause_line_paused`: `Valid{paused:true, changed_at_ms}` ⇒ starts with `[--] Enrichment: paused by operator since`.
  - `doctor_pause_line_invalid`: `Invalid` ⇒ starts with `[WW] Enrichment: state file unreadable` and mentions `hippo enrichment resume` (Review Focus 1).
  - `doctor_auto_memory_pending_while_paused_is_informational`: pending > 0, failed = 0, `paused = true` ⇒ line starts with `[--]`, contribution 0; `failed > 0` still `[!!]` and counts 1.
  - `doctor_stale_projections_while_paused_is_informational`: stale projections > 0 with `paused = true` ⇒ `[--]`.
- [ ] **Step 2: Run** `cargo test -p hippo-daemon doctor_` — expect failures.
- [ ] **Step 3: Implement.** Read the state once at the top of `handle_doctor` and print the pause line. While paused (including invalid): the direct inference probe failure prints `[--] Inference backend not reachable at <url> (enrichment paused)` and does not increment `fail_count`; the brain-JSON `Brain inference backend: unreachable` line prints `[--] ... (enrichment paused)`. Running behavior is unchanged.
- [ ] **Step 4: Run** `cargo test -p hippo-daemon` — expect all pass.
- [ ] **Step 5: Commit** `feat(doctor): report an operator enrichment pause instead of failures`.

### Task 6: Docs and end-to-end verification

**Files:**
- Modify: `README.md` (CLI command list near line 167; short "Pausing enrichment" paragraph in the operator section)
- Modify: `CLAUDE.md` (Commands section: `hippo enrichment pause|resume|status`)
- Modify: `docs/capture/operator-runbook.md` (how to pause, resume, and read status; what doctor shows)
- Modify: `docs/capture/architecture.md` (I-12 row: suppressed while operator-paused, counter reset on transitions; add I-19 resume rule wherever I-19 is described)
- Modify: `docs/brain-watchdog.md` (distinguish bench `/control/pause` from the operator pause)

- [ ] **Step 1: Update the five docs** with the behavior from the spec; no future-work sections.
- [ ] **Step 2: Run full checks:** `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test`, `uv run --project brain ruff check brain/`, `uv run --project brain ruff format --check brain/`, `uv run --project brain pytest brain/tests -q` — expect all green.
- [ ] **Step 3: Live check on this machine** after `mise run install`: `hippo enrichment pause`; confirm `hippo enrichment status` shows paused and growing shell/agentic counts over a few minutes of activity; `hippo doctor` shows `[--] Enrichment: paused by operator` and exit code unchanged by the pause; `hippo alarms list` shows no new I-12/I-19; `hippo enrichment resume`; within a minute the pending counts fall.
- [ ] **Step 4: Commit** `docs: operator enrichment pause`.
