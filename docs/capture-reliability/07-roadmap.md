# Capture-Reliability Overhaul: Ralph Loop Task Queue

**TL;DR:** P0 shipped in v0.16.0. Everything below is an ordered, self-contained task queue. A Ralph Loop picks the lowest-numbered task whose `Status:` is `open` and whose `Depends on:` are all `done`, implements it on the named branch, runs the `Success criterion`, opens the PR, and waits for `review` → `done`. Update `Status:` in this file *in the same PR* that finishes the task — this doc is the source of truth, not GitHub.

---

## Ralph Loop Execution Contract

Violating any invariant below breaks autonomous execution:

1. **Pick-up rule.** Take the lowest-numbered task with `Status: open` whose `Depends on` is empty or all `done`. Never claim a task marked `review` — a human is mid-ack.
2. **Branching.** Check out the task's `Branch:` in an isolated worktree. One branch per task; never share.
3. **DoD gate.** Every `[ ]` under `DoD:` must be checked off before opening the PR. The `Success criterion:` is the machine-checkable subset — it MUST exit 0 locally before `git push`.
4. **Consensus review.** Tasks marked `Consensus review: yes` require human ack. On push, flip `Status: review` and wait. Do not merge on your own.
5. **Status update.** The PR that finishes the task must include an edit to this file flipping the task's `Status:` to `done` (or `review` on push, then `done` at merge). No silent drift.
6. **Scope creep.** If a task grows beyond its `Files:` list, STOP. Open a doc-only PR splitting the task (`T-N` → `T-Na` + `T-Nb`) before touching implementation.
7. **Regression.** If `Success criterion:` fails on `main` after merge, revert the PR, set `Status: open`, add a regression test *first*, then retry.

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

**Known transitional issue:** Check 8 (watchdog heartbeat) ships on v0.16.0 but always returns `[--] no data` until T-1 (watchdog) lands. Flag this in v0.17 release notes.

---

# Task Queue (Execute In Order)

## T-1 · P1.1a — Watchdog core (feature-flagged off)

- **Status:** review
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

- **Status:** open
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

- **Status:** open
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

- **Status:** open
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

- **Status:** open
- **Phase:** P2
- **Depends on:** (P0 — all done). Independent of T-1..T-4; can run in parallel with P1.
- **Branch:** `feat/p2.1-claude-session-watcher`
- **Files:**
  - `crates/hippo-daemon/src/watch_claude_sessions.rs` (new)
  - `crates/hippo-core/src/schema.sql` (next migration: `claude_session_offsets` + `claude_session_parity` tables)
  - `crates/hippo-core/src/storage.rs` (bump `EXPECTED_VERSION`; coordinate with T-1's v9)
  - `launchd/com.hippo.claude-session-watcher.plist` (new; `KeepAlive=true`, `RunAtLoad=true`)
  - `crates/hippo-daemon/src/main.rs` (register subcommand + install integration)
  - `config/config.default.toml` (`[capture] claude_session_mode = "tmux-tailer"` default; also `"watcher"` or `"both"`)
  - `shell/claude-session-hook.sh` (branch on `claude_session_mode`; tmux path preserved)
  - `Cargo.toml` (add `notify` crate)
  - `brain/src/hippo_brain/schema_version.py` (bump to coordinated version)
  - `crates/hippo-daemon/tests/claude_session_watcher_integration.rs` (new)
- **DoD:**
  - [ ] `notify` crate FSEvents subscription on `~/.claude/projects/**/*.jsonl`; startup scan + continuous events.
  - [ ] `claude_session_offsets` tracks `path`/`session_id`/`byte_offset`/`inode`/`device`/`size_at_last_read` per file.
  - [ ] Inode/device change OR size-regression triggers offset reset; partial-line safety (only advance past `\n`-terminated bytes); 30s per-file timeout + 60s backoff.
  - [ ] `claude_session_parity` row inserted hourly during `claude_session_mode = "both"`: one row per path with `tailer_count`, `watcher_count`, `mismatch_count`, `window_start`, `window_end`.
  - [ ] `source_health WHERE source='claude-session-watcher'` heartbeat upserted every 30s regardless of file activity.
  - [ ] Parity test: write 100 synthetic Claude JSONL lines across 3 files with random append/truncate/rename; assert no loss + no double-count via `envelope_id` uniqueness.
  - [ ] Property test (proptest) covers random mutation sequences.
  - [ ] `shell/claude-session-hook.sh` branches on `claude_session_mode`; `tmux-tailer` path unchanged; `watcher` path writes marker only; `both` does both.
  - [ ] NFS/iCloud detection via `statfs` `f_fstypename` at startup; log warning if remote FS.
- **Success criterion:**
  ```bash
  cargo test -p hippo-daemon --test claude_session_watcher_integration && \
    cargo test -p hippo-daemon -- watch_claude_sessions:: && \
    cargo clippy -p hippo-daemon --all-targets -- -D warnings
  ```
- **Consensus review:** yes

---

## T-6 · P2.2 — Synthetic probes + probe_tag exclusion filters + Semgrep lint

- **Status:** open
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

## P2 PHASE GATE — Milestone M3

**Predicate:** T-5, T-6 both `done` AND `claude_session_mode = "both"` has run for ≥ 48 h AND parity is clean:

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

Both queries return 0 → M3 ratified: zero parity mismatches in the last 48 h AND every source probed within the last 15 min.

---

## T-7 · P3.1 — Flip `claude_session_mode` default to `watcher`

- **Status:** blocked
- **Phase:** P3
- **Depends on:** T-5 (done), T-6 (done), **M3 parity predicate passing for 48 h** (see above).
- **Branch:** `feat/p3.1-watcher-default`
- **Files:**
  - `config/config.default.toml` (flip default to `"watcher"`)
  - `crates/hippo-daemon/src/commands.rs` (doctor emits `[WW]` if `claude_session_mode == "tmux-tailer"` on a post-T-5 binary)
  - `docs/RELEASE.md` (release-note snippet)
- **DoD:**
  - [ ] Default config flag flipped from `"tmux-tailer"` to `"watcher"`.
  - [ ] Doctor warns (`[WW]`, not `[!!]`) on `tmux-tailer` setting with pointer to `docs/capture-reliability/06-claude-session-watcher.md`.
  - [ ] Release note calls out the switch, the parity evidence, and the single-command rollback (`hippo config set capture.claude_session_mode tmux-tailer && hippo daemon restart`).
  - [ ] No code changes to the watcher/tailer paths themselves.
- **Success criterion:**
  ```bash
  [ "$(sqlite3 ~/.local/share/hippo/hippo.db 'SELECT COUNT(*) FROM claude_session_parity WHERE mismatch_count > 0 AND window_start > strftime("%s","now")*1000 - 172800000;')" = "0" ] && \
    cargo test -p hippo-daemon -- watcher_default_warning
  ```
- **Consensus review:** yes

---

## T-8 · P3.2 — Delete tmux-spawn path from session hook

- **Status:** blocked
- **Phase:** P3
- **Depends on:** T-7 (done) AND **7 days** of clean run in `watcher` default mode (same parity predicate, 7-day window).
- **Branch:** `feat/p3.2-hook-simplify`
- **Files:**
  - `shell/claude-session-hook.sh` (reduce to ≤15 lines; marker-write only)
  - `scripts/install.sh` (drop tmux-related install steps if any)
  - `tests/shell/test-claude-session-hook-extended.sh` (update expectations)
  - `docs/capture-reliability/00-overview.md` (document `hippo ingest claude-session --batch` as manual recovery path)
- **DoD:**
  - [ ] `shell/claude-session-hook.sh` ≤15 lines, writes only to `$MARKER_DIR/<session_id>`, no tmux invocations, no PID walks.
  - [ ] Header comment includes a one-paragraph revert snippet pointing at the previous hook in git history.
  - [ ] `00-overview.md` explicitly names `hippo ingest claude-session --batch <path>` as the documented manual recovery when the watcher is wedged.
  - [ ] `test-claude-session-hook-extended.sh` updated to assert no tmux calls in the slim hook; still exercises marker write.
- **Success criterion:**
  ```bash
  [ "$(wc -l < shell/claude-session-hook.sh)" -le 15 ] && \
    bash tests/shell/test-claude-session-hook-extended.sh && \
    ! grep -qE "tmux (new-window|send-keys|new-session)" shell/claude-session-hook.sh
  ```
- **Consensus review:** yes

---

## T-9 · P3.3 — Close investigation issues #49–#53

- **Status:** blocked
- **Phase:** P3
- **Depends on:** T-8 (done)
- **Branch:** `docs/p3.3-close-investigations`
- **Files:**
  - `docs/capture-reliability/00-overview.md` (add `## Investigations Closed` section)
  - GitHub issues #49, #50, #51, #52, #53 (closing comments)
- **DoD:**
  - [ ] Each of #49, #50, #51, #52, #53 closed with a findings comment citing the PR(s) that mitigated it (e.g., "Root cause: PID-chain walk under `claade` wrapper. Mitigated by T-5 (`feat/p2.1-claude-session-watcher`) — watcher uses FS events, not PID chain. Closing.").
  - [ ] `00-overview.md` gains a `## Investigations Closed` section linking each issue number to its closing PR(s).
  - [ ] No investigation issue closes without either a fix-PR reference or an explicit "won't fix — reason" note.
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

# Parallelization Notes for Agent Teams

The Ralph Loop is single-threaded by default, but if operated as a team (per `feedback_agent_teams.md`), these tasks can run truly in parallel:

| Wave | Parallelizable tasks | Schema coordination |
|------|----------------------|---------------------|
| W1 | T-1, T-3, T-4 | T-1 owns the next schema version; T-5 if launched here must wait on assignment |
| W2 | T-2 (after T-1), T-5, T-6 | T-5 takes next-next schema version if launched alongside T-1 |
| W3 | T-7 gated on M3; T-8 gated on T-7 + 7d soak; T-9 gated on T-8 | — |

When running a team, set `Status: claimed` on the row the moment an agent checks out the branch. This prevents two agents from picking the same task.

---

# Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Migration failure mid-deploy (daemon new schema, brain lags) | Medium | High | `schema_handshake` (v0.13 pattern); `ACCEPTED_READ_VERSIONS` keeps N−1 runnable. |
| Watchdog alarm fatigue during bring-up | High | Medium | T-1 defaults `alarm_rate_limit_minutes = 60`, `notify_macos = false`; step down to 15 min in v0.18 after 7-day soak. |
| FSEvents quirks cause watcher to miss events | Low | High | Dual-run writes to `claude_session_parity`; T-7 flips default only after 48 h parity clean. |
| `probe_tag` filter missing from a query path | Medium | Medium | T-6's Semgrep rule fails CI; upstream filter at daemon is load-bearing. |
| Schema version collision between T-1 and T-5 branches | Medium | Low | Team lead assigns next version number on branch creation; PR template requires `PRAGMA user_version` check. |
| New tmux-targeting patch during P1–P2 (AP-10 violation) | High | Low | Review blocks any PR of this shape with pointer to T-5; manual recovery is `hippo ingest claude-session --batch`. |
| `daemon.rs` / `commands.rs` become hot-file merge surfaces | Medium | Low | Worktrees + sequential T-1 → T-2 + T-4 rebase cadence; coordinate via `Status:` column. |

---

# Ralph Loop Quick Reference

```bash
# Find the next task
grep -nE '^- \*\*Status:\*\* open$' docs/capture-reliability/07-roadmap.md \
  | head -1

# Check dependencies are satisfied (for task T-N)
awk '/^## T-N /,/^---/' docs/capture-reliability/07-roadmap.md | \
  grep -A1 'Depends on:' | head -2

# Run the success criterion for a task (copy from the task's section)
# When it exits 0, you can git push and open the PR.
```
