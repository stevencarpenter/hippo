# Capture-Reliability Overhaul: Task Queue

**Status (2026-04-26):** P0, P1, P2, P3 shipped. T-7 (#88), T-8 (#89), and T-9 shipped — investigations #49–#53 closed (3 resolved by capture work, 2 spun off to #90/#91 as out-of-scope follow-ups; see `00-overview.md` § Investigations Closed). **All milestones (M1–M4) ratified.** M3 decision archived at [`docs/archive/capture-reliability-overhaul/m3-decision.md`](../archive/capture-reliability-overhaul/m3-decision.md).

**Workflow note:** This was originally framed as a Ralph Loop autonomous queue (T-1 through T-6 were eligible to be picked up by a loop). In practice, every task shipped via standard PR review by hand — the Ralph Loop framing is now retired. The doc remains useful as an ordered tracker of what's done, what's left, and what gates each step. Status fields and DoD checkboxes below reflect the actual state of `main`, not aspirational planning.

---

## How to use this doc

1. **Reading it.** Tasks below are in dependency order. Each has `Status:` (`open` / `done` / `blocked`), the PR that shipped it (if any), the files it touched, and a DoD checklist. Phase gates between sections describe what must hold before the next phase can start.
2. **Editing it.** When you ship a task, flip its `Status:` and check off completed DoD items in the same PR. This file is the source of truth for "what's left" — keep it honest. If a task grows beyond its `Files:` list, split it (`T-N` → `T-Na` + `T-Nb`) rather than letting scope drift silently.
3. **Phase gates.** A blocked task only unblocks when its gate predicate is genuinely true. Don't flip `blocked` → `open` because the predicate is "close enough" — fix the gate or document why it's not load-bearing.

---

## Shipped (P0) — v0.16.0, 2026-04-24

| Task | GitHub | Status |
|------|--------|--------|
| T-0.1 source_health table + v7→v8 migration + probe_tag columns | #67 | done |
| T-0.2 flush_events + insert_segments write paths + rolling-count recompute | #68 | done |
| T-0.3 doctor checks 1, 4, 7, 8 + fail_count exit code | #70 | done |
| T-0.4 OTel `source` attribute (rename type→source) | #66 | done |
| T-0.5 daily log rotation (7-day retention) | #69 | done |

**Milestone M1** ✅ — doctor reports per-source staleness; outage diagnosable via `SELECT * FROM source_health` in a single query.

---

# Task Queue (Execute In Order)

## T-1 · P1.1a — Watchdog core (feature-flagged off)

- **Status:** done
- **Phase:** P1
- **Depends on:** (P0 — all done)
- **Branch:** `feat/p1.1a-watchdog-core`
- **Files:**
  - `crates/hippo-daemon/src/watchdog.rs` (new)
  - `crates/hippo-core/src/schema.sql` (v8→v9 migration; `capture_alarms` table)
  - `crates/hippo-core/src/storage.rs` (migration block; bump `EXPECTED_VERSION` to 9)
  - `crates/hippo-daemon/src/main.rs` (register `watchdog run` subcommand)
  - `crates/hippo-core/src/config.rs` (`WatchdogConfig` struct)
  - `config/config.default.toml` (`[watchdog]` section)
  - `brain/src/hippo_brain/schema_version.py` (bump `EXPECTED_SCHEMA_VERSION` to 9; keep 8 in `ACCEPTED_READ_VERSIONS`)
- **DoD:**
  - [ ] v8→v9 migration adds `capture_alarms` table per `04-watchdog.md`; falling-cascade pattern in `open_db` matches the v7→v8 precedent.
  - [ ] `hippo watchdog run`: (step 1) upserts own heartbeat into `source_health WHERE source='watchdog'`; (step 2) reads full `source_health` in one `SELECT *`; (step 3) asserts I-1..I-10 per `02-invariants.md` against in-memory rows; (step 4) inserts `capture_alarms` rows for violations; (step 5) exits 0.
  - [ ] Rate limiter: before INSERT, checks for un-acked alarm for same `invariant_id` within `alarm_rate_limit_minutes` window; skips INSERT if found (still writes structured log line).
  - [ ] `[watchdog].enabled = false` default; `alarm_rate_limit_minutes = 60` default (not 15 — step down in v0.18 after soak); `notify_macos = false` default.
  - [ ] Pre-migration safe: creates `source_health` + seeds `watchdog` row if either absent; exits clean without alarms on first-run fresh install.
  - [ ] Unit tests cover: rate-limit boundary (14min = still suppressed at 15-default, or 59min = still suppressed at 60-default); each invariant's detection predicate with seeded fixture; `source_health` table absent → no panic.
  - [ ] `cargo clippy -p hippo-daemon --all-targets -- -D warnings` clean.
  - [ ] `cargo fmt --check` clean.
- **Success criterion:**
  ```bash
  cargo test -p hippo-daemon -- watchdog:: && \
    cargo clippy -p hippo-daemon --all-targets -- -D warnings && \
    cargo fmt --check
  ```
- **Consensus review:** yes

---

## T-2 · P1.1b — Watchdog launchd install + `hippo alarms` CLI

- **Status:** done
- **Phase:** P1
- **Depends on:** T-1 (done)
- **Branch:** `feat/p1.1b-watchdog-install`
- **Files:**
  - `launchd/com.hippo.watchdog.plist` (new)
  - `crates/hippo-daemon/src/main.rs` (install integration; extend `daemon install`)
  - `crates/hippo-daemon/src/commands.rs` (new `alarms` subcommand: `list`, `ack`)
  - `crates/hippo-daemon/src/cli.rs` (argparse for `alarms list/ack`)
  - `tests/shell/test-watchdog-install.sh` (new)
  - `config/config.default.toml` (flip `[watchdog].enabled = true`)
- **DoD:**
  - [ ] Plist uses `StartInterval=60`, `RunAtLoad=false`, `KeepAlive` absent; `EnvironmentVariables` include `HOME` and `PATH`; stdout/stderr log to `$DATA_DIR/watchdog.{stdout,stderr}.log`.
  - [ ] `hippo daemon install` copies the plist alongside daemon/brain plists with `__HIPPO_BIN__` / `__HOME__` / `__PATH__` / `__DATA_DIR__` substitutions.
  - [ ] `hippo alarms list` prints un-acked rows (exits 1 if any, 0 if none).
  - [ ] `hippo alarms ack <id> [--note <text>]` updates `acked_at` and `ack_note`; re-ack is a no-op (WHERE `acked_at IS NULL`).
  - [ ] When `notify_macos = true`, a new (non-rate-limited) alarm row triggers exactly one `osascript -e 'display notification …'`.
  - [ ] `[watchdog].enabled = true` flipped as default in this PR.
  - [ ] `test-watchdog-install.sh` asserts: plist appears in `$HOME/Library/LaunchAgents/`, `launchctl print gui/$(id -u)/com.hippo.watchdog` reports loaded, mock alarm row round-trips through list/ack.
- **Success criterion:**
  ```bash
  cargo test -p hippo-daemon -- alarms:: && \
    bash tests/shell/test-watchdog-install.sh && \
    cargo clippy -p hippo-daemon --all-targets -- -D warnings
  ```
- **Consensus review:** yes

---

## T-3 · P1.2 — Firefox extension heartbeat + popup badge

- **Status:** done
- **Phase:** P1
- **Depends on:** (P0 — all done)
- **Branch:** `feat/p1.2-browser-heartbeat`
- **Files:**
  - `extension/firefox/src/background.ts` (add `sendHeartbeat()` + 5min `setInterval`)
  - `extension/firefox/src/popup/popup.tsx` (or equivalent; show heartbeat age with green/yellow/red badge)
  - `extension/firefox/src/popup/popup.css`
  - `crates/hippo-daemon/src/native_messaging.rs` (parse `{"type":"heartbeat", ...}` frames; forward to daemon via new socket request)
  - `crates/hippo-daemon/src/commands.rs` (handle new `DaemonRequest::UpdateSourceHealthHeartbeat { source, ts }`)
  - `crates/hippo-daemon/src/daemon.rs` (update `source_health.browser.last_heartbeat_ts` in write_db)
  - `extension/firefox/tests/heartbeat.test.ts` (new)
  - `crates/hippo-daemon/tests/nm_heartbeat_integration.rs` (new)
- **DoD:**
  - [ ] Extension fires `sendHeartbeat()` on startup and every 5 minutes; payload matches `ExtensionHeartbeat` schema in `05-synthetic-probes.md`.
  - [ ] NM host deserializes heartbeat, forwards via `UpdateSourceHealthHeartbeat` socket request; no SQLite write from NM process itself (AP-1 compliance).
  - [ ] Daemon handler UPSERTs `source_health WHERE source='browser' SET last_heartbeat_ts = ts, updated_at = now`.
  - [ ] Popup badge: green if `last_heartbeat_ts` < 2min old; yellow < 10min; red otherwise; shows human age ("4m ago").
  - [ ] Integration test: spawn daemon + mock NM client sending a heartbeat frame; assert `source_health.browser.last_heartbeat_ts` updated within 500ms.
  - [ ] No probe_tag writer here — the column exists from P0.1 and is owned by T-6 (probes).
- **Success criterion:**
  ```bash
  cargo test -p hippo-daemon -- source_health_heartbeat && \
    (cd extension/firefox && bun test) && \
    cargo clippy -p hippo-daemon --all-targets -- -D warnings
  ```
- **Consensus review:** yes

---

## T-4 · P1.3 — Doctor checks 2, 5, 6, 9, 10

- **Status:** done
- **Phase:** P1
- **Depends on:** (P0 — all done). Note: Check 3 (browser ext dist/) already shipped via #54; Check 8 already shipped via #70 but is dark until T-1 lands.
- **Branch:** `feat/p1.3-doctor-checks-2`
- **Files:**
  - `crates/hippo-daemon/src/commands.rs` (add `check_nm_manifest`, `check_claude_session_db`, `check_session_hook_log`, `check_fallback_age`, `check_schema_version`; wire into `handle_doctor`)
  - `crates/hippo-daemon/tests/doctor_checks_2_5_6_9_10.rs` (new)
- **DoD:**
  - [ ] Check 2 (native-msg manifest healthy): stat + JSON parse + `allowed_extensions` contains `hippo-browser@local` + `path` is executable.
  - [ ] Check 5 (live-session vs DB reconciliation): glob `~/.claude/projects/**/*.jsonl` with `mtime < 5min`, parse first line for `sessionId`, SELECT from `claude_sessions`; `[!!]` per missing (capped at +3 `fail_count`).
  - [ ] Check 6 (session-hook log vs DB): `tail -n 10000 $DATA_DIR/session-hook-debug.log`, count `"hook invoked"` lines in last 1h, compare to `claude_sessions` rows in same window.
  - [ ] Check 9 (fallback file age): stat `*.jsonl` under `$fallback_dir`; `[!!]` if any > 24h while daemon reachable.
  - [ ] Check 10 (schema version): `PRAGMA user_version` on daemon DB vs brain `/health`'s `expected_schema_version` (reuse the brain HTTP call already made earlier in `handle_doctor`).
  - [ ] Each check has a negative test with a seeded failing fixture.
  - [ ] Total doctor wall-clock under 2s asserted in an integration test (`cargo test -p hippo-daemon -- doctor_perf_budget`).
  - [ ] `--explain` output includes CAUSE/FIX/DOC for each new `[!!]`.
- **Success criterion:**
  ```bash
  cargo test -p hippo-daemon -- doctor::checks_2_5_6_9_10 && \
    cargo test -p hippo-daemon -- doctor_perf_budget && \
    cargo clippy -p hippo-daemon --all-targets -- -D warnings
  ```
- **Consensus review:** yes

---

## P1 PHASE GATE — Milestone M2

**Predicate:** T-1, T-2, T-3, T-4 all `done` AND the following one-line drill passes:

```bash
hippo daemon restart && pkill -f firefox && sleep 180 && \
  hippo doctor 2>&1 | grep -E '(browser events|claude-session DB).*\[!!\]' && \
  sqlite3 ~/.local/share/hippo/hippo.db \
    "SELECT COUNT(*) FROM capture_alarms WHERE acked_at IS NULL AND raised_at > strftime('%s','now')*1000 - 300000" \
  | grep -v '^0$'
```

If that chain exits 0, the watchdog fired at least one alarm within one poll interval of a seeded invariant violation. M2 ratified.

---

## T-5 · P2.1 — Claude session FS watcher (dual-run mode)

- **Status:** done — shipped via [PR #86](https://github.com/stevencarpenter/hippo/pull/86), schema v10
- **Phase:** P2
- **Depends on:** (P0 — all done). Independent of T-1..T-4; ran in parallel with P1.
- **Branch:** `feat/p2.1-claude-session-watcher` (merged to `main` as `f441066`)
- **Files:**
  - `crates/hippo-daemon/src/watch_claude_sessions.rs` (new)
  - `crates/hippo-core/src/schema.sql` (v9→v10: `claude_session_offsets` + `claude_session_parity` tables)
  - `crates/hippo-core/src/storage.rs` (bumped `EXPECTED_VERSION` to 10)
  - `launchd/com.hippo.claude-session-watcher.plist` (new; `KeepAlive=true`, `RunAtLoad=true`)
  - `crates/hippo-daemon/src/main.rs` (subcommand + install integration)
  - `config/config.default.toml` (`[capture] claude_session_mode = "tmux-tailer"` default) — *retired in T-8*
  - `shell/claude-session-hook.sh` (branches on `claude_session_mode` via `hippo capture-mode`) — *retired in T-8*
  - `Cargo.toml` (added `notify` crate)
  - `brain/src/hippo_brain/schema_version.py` (bumped to 10)
  - `crates/hippo-daemon/tests/claude_session_watcher_integration.rs` (new; 5 integration tests)
- **DoD:**
  - [x] `notify` crate FSEvents subscription on `~/.claude/projects/**/*.jsonl`; startup scan + continuous events.
  - [x] `claude_session_offsets` tracks `path`/`session_id`/`byte_offset`/`inode`/`device`/`size_at_last_read` per file.
  - [x] Inode/device change OR size-regression triggers offset reset; partial-line safety (only advance past `\n`-terminated bytes); 30s per-file timeout + 60s backoff.
  - [x] `claude_session_parity` row inserted hourly during `claude_session_mode = "both"` — **but see M3 caveat below: `mismatch_count` is structurally always 0 because the tmux tailer writes to `events`, not `claude_sessions`. The parity table records watcher activity, not divergence.**
  - [x] `source_health WHERE source='claude-session-watcher'` heartbeat upserted every 30s (verified live: row exists, `updated_at` advances).
  - [x] Integration test covers random mutation sequence across 3 files (`claude_session_watcher_integration.rs`).
  - [x] Property test (proptest) — `claude_session_watcher_integration.rs:246` `proptest!` block covers random append sequences.
  - [x] `shell/claude-session-hook.sh` branches on `hippo capture-mode`; `watcher` path skips tmux spawn entirely; `both`/`tmux-tailer` retain the tmux-spawn path. *(All branching retired in T-8 — the hook is now a 14-line no-op.)*
  - [x] NFS/iCloud detection via `statfs` `f_fstypename` at startup; log warning if remote FS.
- **Success criterion:**
  ```bash
  cargo test -p hippo-daemon --test claude_session_watcher_integration && \
    cargo test -p hippo-daemon -- watch_claude_sessions:: && \
    cargo clippy -p hippo-daemon --all-targets -- -D warnings
  ```
- **Consensus review:** yes

---

## T-6 · P2.2 — Synthetic probes + probe_tag exclusion filters + Semgrep lint

- **Status:** done
- **Phase:** P2
- **Depends on:** (P0 — all done). Independent of T-1..T-5.
- **Branch:** `feat/p2.2-synthetic-probes`
- **Files:**
  - `crates/hippo-daemon/src/probe.rs` (new)
  - `crates/hippo-daemon/src/main.rs` (register `probe` subcommand + install integration)
  - `crates/hippo-daemon/src/cli.rs` (`SendEvent` gets `--probe-tag`, `--source-kind`, `--tool-name`)
  - `launchd/com.hippo.probe.plist` (new; `StartInterval=300`, `RunAtLoad=false`)
  - `crates/hippo-daemon/src/commands.rs` — add `probe_tag IS NULL` to `GetStatus`, `handle_query_raw`, `handle_sessions`, `handle_events`.
  - `brain/src/hippo_brain/server.py` — `/query`, `/ask` endpoints filter `probe_tag IS NULL`.
  - `brain/src/hippo_brain/enrichment.py` — `claim_pending_events_by_session` filters upstream.
  - `brain/src/hippo_brain/claude_sessions.py` — `claim_pending_claude_segments` filters.
  - `brain/src/hippo_brain/browser_enrichment.py` — `claim_pending_browser_events` filters.
  - `brain/src/hippo_brain/mcp_queries.py` — `search_events_impl`, `search_knowledge_lexical`, `get_entities_impl`, `get_lessons_impl` filter.
  - `brain/src/hippo_brain/rag.py` — `ask()` RAG retrieval filters.
  - `brain/src/hippo_brain/retrieval.py` — `search()` hybrid FTS5/vec0 filters.
  - `.semgrep.yml` (new rule: `unfiltered-event-table-select`)
  - `brain/tests/test_probe_exclusion.py` (new; enumerates every brain query)
  - `crates/hippo-daemon/tests/probe_exclusion.rs` (new; enumerates every daemon query)
- **DoD:**
  - [ ] Shell probe: `hippo send-event shell --cmd __hippo_probe__ --probe-tag <uuid>`; polls `events WHERE envelope_id = :uuid AND probe_tag = :uuid` up to 10s; writes `probe_ok`, `probe_lag_ms`, `probe_last_run_ts` to `source_health`.
  - [ ] Claude-tool probe: same pattern with `--source-kind claude-tool --tool-name Bash`.
  - [ ] Claude-session probe (assertion-based): for each `~/.claude/projects/*/*.jsonl` with `mtime < 5min`, assert `claude_sessions` row exists with `source_file = path`.
  - [ ] Browser probe (NM-stdio direct): bypasses Firefox; `probe.hippo.local` allowlisted via `[browser] probe_domain`.
  - [ ] Upstream filter: daemon `flush_events` skips enqueuing into enrichment queues when `probe_tag IS NOT NULL`.
  - [ ] Every file in the grep list of `05-synthetic-probes.md` has `WHERE probe_tag IS NULL` (or equivalent); integration tests enumerate every query function and assert exclusion.
  - [ ] Semgrep rule `unfiltered-event-table-select` in `.semgrep.yml` matches `SELECT .* FROM (events|claude_sessions|browser_events)` lacking `probe_tag IS NULL` and fails CI.
  - [ ] Probe launchd plist installed by `hippo daemon install`.
- **Success criterion:**
  ```bash
  cargo test -p hippo-daemon --test probe_exclusion && \
    cargo test -p hippo-daemon -- probe:: && \
    uv run --project brain pytest brain/tests/test_probe_exclusion.py && \
    semgrep --config .semgrep.yml --error crates/ brain/src/
  ```
- **Consensus review:** yes

---

<a id="p2-phase-gate--milestone-m3"></a>
## P2 PHASE GATE — Milestone M3

> **⚠️ DECISION NEEDED — gate as written is non-functional.**
> The original predicate below assumes `claude_session_parity.mismatch_count > 0` would surface watcher/tailer divergence. It cannot, by construction:
>
> - The watcher writes to `claude_sessions` (`watch_claude_sessions.rs:188-204` → `claude_session::insert_segments`).
> - The tmux tailer (`claude_session::ingest_tail` invoked via the hook's inline path) writes only to the `events` table; it never inserts `claude_sessions` rows. The non-tmux fallback `ingest_batch` does write `claude_sessions`, but the hook only reaches it when no tmux server is running.
> - `write_parity_row` (`watch_claude_sessions.rs:299-308`) computes `total = COUNT(*) FROM claude_sessions WHERE source_file = ?` and `tailer_count = total - watcher_count`. With the watcher as the sole writer, `total ≡ watcher_count` and `mismatch_count ≡ 0` regardless of whether the tailer is running, dead, or running and dropping events.
>
> **Empirical confirmation (2026-04-25, this machine):** 2,037 parity rows in the live DB; aggregate `watcher_count = 632`, `tailer_count = 0`, `mismatch_count = 0`. The "M3 clean" predicate already passes — but it would also pass if the watcher were the only thing capturing.
>
> Until this is resolved, T-7 must not be unblocked solely by the predicate below. The four candidate resolutions are tracked in [`docs/archive/capture-reliability-overhaul/m3-decision.md`](../archive/capture-reliability-overhaul/m3-decision.md). Pick one before flipping T-7 to `open`.

**Original predicate (kept for reference; no longer load-bearing):**

```bash
sqlite3 ~/.local/share/hippo/hippo.db \
  "SELECT COUNT(*) FROM claude_session_parity \
   WHERE mismatch_count > 0 \
     AND window_start > strftime('%s','now') * 1000 - 172800000;" \
  | grep -qx '0' && \
sqlite3 ~/.local/share/hippo/hippo.db \
  "SELECT COUNT(*) FROM source_health \
   WHERE probe_last_run_ts IS NULL \
      OR probe_last_run_ts < strftime('%s','now') * 1000 - 900000;" \
  | grep -qx '0'
```

The second clause (probe freshness) is still meaningful — it confirms T-6 probes are running. The first clause is the broken one.

---

## T-7 · P3.1 — Flip `claude_session_mode` default to `watcher`

- **Status:** done — shipped via PR #88
- **Phase:** P3
- **Depends on:** T-5 (done), T-6 (done), and the M3 decision recorded in [`docs/archive/capture-reliability-overhaul/m3-decision.md`](../archive/capture-reliability-overhaul/m3-decision.md) (Option D — empirical validation against 7 days of dual-run data on `main`).
- **Branch:** `feat/p3.1-watcher-default` (merged to `main`)
- **Files:**
  - `config/config.default.toml` (default flipped to `"watcher"`)
  - `crates/hippo-core/src/config.rs` (Rust-side default + `ClaudeSessionMode` doc comments)
  - `crates/hippo-daemon/src/commands.rs` (doctor `check_capture_mode` emits `[WW]` for `tmux-tailer`; tests added)
  - `docs/archive/capture-reliability-overhaul/m3-decision.md` (Outcome section recording Option D; archived after T-8)
  - `docs/capture-reliability/07-roadmap.md` (this file: T-7 status flipped)
- **DoD:**
  - [x] Default config flag flipped from `"tmux-tailer"` to `"watcher"` in both `config.default.toml` and `CaptureConfig::default()`.
  - [x] Doctor warns (`[WW]`, not `[!!]`) on `tmux-tailer` setting with pointer to the watcher service. (Doctor warning was subsequently removed in T-8 along with the rest of the tmux path; the watcher is now the only mode.)
  - [x] Release note (PR description) calls out the switch, the parity evidence, and the single-command rollback (`hippo config set capture.claude_session_mode tmux-tailer && hippo daemon restart`).
  - [x] No code changes to the watcher/tailer paths themselves.
- **Success criterion:**
  ```bash
  cargo test -p hippo-daemon -- check_capture_mode default_capture_mode
  ```
- **Consensus review:** yes

---

## T-8 · P3.2 — Retire tmux from hippo entirely

- **Status:** done — shipped via PR #89
- **Phase:** P3
- **Depends on:** T-7 (done). The original 7-day soak was dropped on 2026-04-25 — the M3 evidence already showed the watcher was strictly better than the tailer, and an additional soak would have only re-confirmed that.
- **Branch:** `feat/p3.2-hook-simplify` (in PR #89)
- **Files:**
  - `shell/claude-session-hook.sh` (slimmed from 127 → 14 lines, no-op log)
  - `tests/shell/test-claude-session-hook.sh` (deleted)
  - `tests/shell/test-claude-session-hook-extended.sh` (deleted)
  - `tests/shell/test-hook-pid-ppid.sh` (deleted)
  - `crates/hippo-daemon/src/main.rs` (deleted tmux-spawn branch and `CaptureMode` subcommand)
  - `crates/hippo-daemon/src/cli.rs` (deleted `--inline` and `--batch` flags from `ingest claude-session`; deleted `CaptureMode` subcommand)
  - `crates/hippo-daemon/src/claude_session.rs` (deleted `ingest_tail`)
  - `crates/hippo-daemon/src/watch_claude_sessions.rs` (deleted parity-row writer, mode-based idle, and stale fields on `FileState`)
  - `crates/hippo-daemon/src/commands.rs` (deleted `check_capture_mode` from T-7; updated doctor explain text)
  - `crates/hippo-core/src/config.rs` (deleted `CaptureConfig` struct, `ClaudeSessionMode` enum, and the `[capture]` section from `HippoConfig`)
  - `config/config.default.toml` (deleted `[capture]` section)
  - `crates/hippo-daemon/tests/source_audit/claude_session_tailer.rs` (deleted; was always `#[ignore]`'d skeleton)
  - `CLAUDE.md`, `docs/capture-reliability/00-overview.md`, `08-anti-patterns.md`, `09-test-matrix.md` (rewrites and historical-status notes); `06-claude-session-watcher.md` was archived to `docs/archive/capture-reliability-overhaul/` since the design has fully shipped
- **DoD:**
  - [x] `shell/claude-session-hook.sh` is 14 lines, no tmux invocations, no PID walks, no JSON parsing.
  - [x] No tmux strings remain in any production code path (`grep -rn 'tmux\|TMUX' crates/ shell/ config/` returns only dev-only strings or none).
  - [x] No required tmux dependency in install scripts, launchd plists, Cargo.toml, or mise.toml.
  - [x] Doctor explain text references the watcher service, not tmux.
  - [x] Stale config keys (`[capture]`) are silently ignored (serde default), so existing user configs continue to load.
- **Success criterion:**
  ```bash
  cargo test -p hippo-core -p hippo-daemon && \
    cargo clippy --all-targets -- -D warnings && \
    cargo fmt --check && \
    [ "$(wc -l < shell/claude-session-hook.sh)" -le 15 ] && \
    ! grep -qE "tmux|TMUX" shell/claude-session-hook.sh
  ```
- **Consensus review:** yes

---

## T-9 · P3.3 — Close investigation issues #49–#53

- **Status:** done — issues closed 2026-04-26; #52 and #53 spun off as #90 and #91 to preserve audit trail
- **Phase:** P3
- **Depends on:** T-8 (done)
- **Branch:** `docs/p3.3-close-investigations`
- **Files:**
  - `docs/capture-reliability/00-overview.md` (added `## Investigations Closed` section)
  - GitHub issues #49, #50, #51, #52, #53 (closing comments)
  - GitHub issues #90 (redaction follow-up, spun from #52), #91 (lessons follow-up, spun from #53)
- **DoD:**
  - [x] Each of #49, #50, #51, #52, #53 closed with a findings comment citing the PR(s) that mitigated it or — for the two out-of-scope items — the spun-off follow-up issue.
  - [x] `00-overview.md` gains a `## Investigations Closed` section linking each issue number to its closing PR(s) or follow-up.
  - [x] No investigation issue closes without either a fix-PR reference or an explicit "out-of-scope, tracked at #N" note.
- **Success criterion:**
  ```bash
  for n in 49 50 51 52 53; do \
    gh issue view $n --json state --jq '.state' | grep -qx 'CLOSED' || { echo "issue #$n still open"; exit 1; }; \
  done
  ```
- **Consensus review:** no (docs + issue hygiene only)

---

## P3 PHASE GATE — Milestone M4

**Predicate:** T-7, T-8, T-9 all `done` AND:

```bash
! grep -qE "tmux (new-window|send-keys|new-session)" shell/claude-session-hook.sh && \
gh issue list --state open --search 'number:49 OR number:50 OR number:51 OR number:52 OR number:53' \
  | grep -vc '^$' | grep -qx '0'
```

Tmux-spawn code gone AND no investigation issue still open → M4 ratified.

---

# Risk Register (historical)

All P3 work has shipped; the rows below were the live risks tracked while T-7, T-8, and T-9 were in flight. They are kept as a record of what was watched and how it was mitigated, not as open items.

| Risk | Likelihood | Impact | Mitigation (now applied) |
|------|-----------|--------|-----------|
| FSEvents quirks cause the watcher to miss events after T-7 default flip | Low | High | M3 Option D (empirical 7-day dual-run validation) gave a real "watcher captured everything the tailer would have" signal before T-7 shipped. Single-command rollback documented in T-7 release note (made moot by T-8). |
| Closing #49–#53 in T-9 without surfacing actual root causes | Medium | Medium | T-9 closing comments cite the mitigating PR(s) or, for #52/#53, the spun-off follow-up issue (#90/#91). No bare closures. |
| Watcher heartbeat column drift in `source_health` | Low | Low | Verified pre-T-7: doctor's freshness query uses `updated_at`, which advances correctly for the watcher row. |

Retired risks (kept for historical reference): migration failure mid-deploy (handled by `ACCEPTED_READ_VERSIONS`), watchdog alarm fatigue (T-1 defaults shipped), `probe_tag` filter gaps (T-6 semgrep rule + upstream filter shipped), schema version collisions (T-5 ended up at v10 cleanly), AP-10 tmux patches during dev (none merged), `daemon.rs` hot-file merges (no conflicts hit).
