# Capture-Reliability Overhaul: Implementation Roadmap

**TL;DR:** The overhaul ships in four phases across ~7 days using parallel agent teams. P0 lands the schema foundation same-day; every subsequent phase builds on it without big-bang migrations. Each PR is independently mergeable and carries a verification test.

## Guiding Principles

- **Reliability-per-effort first.** PRs ordered by invariant coverage / implementation risk. `source_health` table (P0.1) unlocks every other check and comes first.
- **Narrow, independently mergeable PRs.** No PR touches more than one logical subsystem. Parallel worktrees avoid merge conflicts.
- **No big-bang migrations.** Structural changes (FS watcher, probes) feature-flagged and dual-run before old path removal.
- **Verification before merge.** Every PR adds or updates at least one automated assertion that the new invariant holds.

---

## Phase P0 — Same-Day Foundation (~0.5 days, 4-5 parallel agents)

P0.1 is the only blocking dependency.

| PR | Scope | Files Touched | Owner | Effort | Deps | Invariant | Verification |
|----|-------|---------------|-------|--------|------|-----------|--------------|
| **P0.1** | `source_health` table + migration + rolling-count recompute scheduling | `crates/hippo-core/src/schema.sql`, `storage.rs` | schema-migration | ~4h | none | I-1 | `cargo test -p hippo-core -- source_health_table_exists` |
| **P0.2** | Write paths: `flush_events`, `claude_session.rs`, `native_messaging.rs` upsert `source_health` | `crates/hippo-daemon/src/daemon.rs`, `claude_session.rs`, `native_messaging.rs` | write-paths | ~4h | P0.1 | I-2, I-3 | Integration: write 3 events, assert `source_health.last_event_ts` updated, `consecutive_failures=0` |
| **P0.3** | Doctor checks 1, 4, 7, 8 (staleness, hook sourced, log size, watchdog stub) | `crates/hippo-daemon/src/commands.rs` | doctor-checks | ~3h | P0.1 | I-4, I-5, I-7 | `cargo test -p hippo-daemon -- doctor_staleness_check` seeded with stale row |
| **P0.4** | OTel `source` attribute on `events.ingested`, `flush.events`, `fallback.writes` | `crates/hippo-daemon/src/metrics.rs` + call sites | otel-attrs | ~2h | none | I-6 | `#[cfg(feature="otel")]` test that `EVENTS_INGESTED.add` includes `source` key |
| **P0.5** | Log rotation via `tracing-appender` rolling writer (daily, keep 7) | daemon `main.rs`/`telemetry.rs`, brain `main.py` | log-rotation | ~2h | none | I-7 | Doctor check (P0.3) passes; synthetic rotation test asserts old file renamed |

**Parallelization:** All five launch simultaneously. P0.2 and P0.3 drafted against agreed schema; integrate after P0.1 merges. P0.4 and P0.5 have zero deps.

---

## Phase P1 — Days 2–3 (3 parallel workstreams)

Starts after P0.1 and P0.2 merge. P1.3 additionally waits for P0.3.

| PR | Scope | Files Touched | Owner | Effort | Deps | Invariant | Verification |
|----|-------|---------------|-------|--------|------|-----------|--------------|
| **P1.1** | `hippo watchdog` subcommand + `capture_alarms` table + launchd plist + ack CLI + rate limiter | `crates/hippo-daemon/src/watchdog.rs` (new), `schema.sql`, `launchd/com.hippo.watchdog.plist` (new), `main.rs` | watchdog | ~1.5d | P0.1, P0.2 | I-7 (watchdog liveness), I-5 (alarm on consecutive_failures) | Seed `consecutive_failures=5`, run tick, assert alarm row; `notify_macos=false` suppresses `osascript` |
| **P1.2** | Extension heartbeat (5 min) + NM host forwarding + `probe_tag` column stub + popup badge | `extension/firefox/src/background.ts`, `native_messaging.rs`, `schema.sql` | browser-heartbeat | ~1d | P0.1 | I-4 (extension heartbeat visible) | Unit: synthetic NM heartbeat → `source_health.browser.last_heartbeat_ts` upserted |
| **P1.3** | Doctor checks 2, 3, 5, 6, 9, 10 | `crates/hippo-daemon/src/commands.rs` | doctor-checks-2 | ~6h | P0.3 merged | I-2, I-5 extended | Each check has negative test |

**Parallelization:** P1.1 and P1.2 fully parallel. P1.3 after P0.3.

---

## Phase P2 — Days 4–7 (2 parallel workstreams)

Highest-risk structural phase. Coordinate schema migration numbers before branching.

| PR | Scope | Files Touched | Owner | Effort | Deps | Invariant | Verification |
|----|-------|---------------|-------|--------|------|-----------|--------------|
| **P2.1** | `hippo watch-claude-sessions` + `claude_session_offsets` table + launchd plist + `claude_session_mode` flag + dual-run parity | `crates/hippo-daemon/src/watch_claude_sessions.rs` (new), `schema.sql`, `launchd/com.hippo.claude-session-watcher.plist` (new), `main.rs`, `config/config.toml` | claude-watcher | ~2-3d | P0.1, P0.2 | I-2 (no missed sessions) | Integration: synthetic JSONL → offsets advance, events ingested; parity: tailer and watcher yield identical rows |
| **P2.2** | Synthetic probes (4 sources) + `probe_tag` column + probe launchd plist + exclusion filter in all user-facing queries | `crates/hippo-daemon/src/probe.rs` (new), `schema.sql`, `launchd/com.hippo.probe.plist` (new), `commands.rs`, brain modules | probes | ~1.5d | P0.1 | I-8 (probe freshness) | Row with `probe_tag='synthetic'` absent from `hippo ask`/`hippo events`; present in `source_health` |

**Parallelization:** Fully parallel. Only shared file is `schema.sql` — assign migration version numbers before branching.

---

## Phase P3 — Cleanup (Day 7+)

Blocked on 48 h dual-run observation. Single sequential workstream.

| Task | Description | Blocks |
|------|-------------|--------|
| **P3.1** | Flip `claude_session_mode` default to `watcher`; add doctor warning if `tailer` | P2.1 dual-run clean 48 h |
| **P3.2** | Delete tmux-spawn from `shell/claude-session-hook.sh`; reduce to marker write | P3.1 merged |
| **P3.3** | Close investigation issues #49–#53 with findings; update `docs/capture-reliability/` index | P3.2 merged |

---

## Agent-Team Execution Plan

### Team `hippo-capture-p0` — Day 1

4-5 members, each in their own git worktree, PRs targeting `main`.

| Member | Task | Depends On | Output |
|--------|------|------------|--------|
| `schema-migration` | `source_health` table, migration, `EXPECTED_VERSION` bump | none | P0.1 |
| `write-paths` | Upsert `source_health` in 3 write sites; rolling-count recompute | P0.1 merged | P0.2 |
| `doctor-checks` | Staleness + hook-sourced + log-size + watchdog-stub checks | P0.1 merged | P0.3 |
| `otel-attrs` | `source` KeyValue on counters | none | P0.4 |
| `log-rotation` | `tracing-appender` rolling writer | none | P0.5 |

**Consensus review:** After P0.1 merges, team lead reviews P0.2 and P0.3 together to confirm `source_health` upsert semantics match before both merge.

### Team `hippo-capture-p1` — Days 2-3

3 members.

| Member | Task | Depends On | Output |
|--------|------|------------|--------|
| `watchdog` | subcommand + alarms table + plist + ack CLI + rate limiter | P0.1, P0.2 | P1.1 |
| `browser-heartbeat` | TS heartbeat + NM forwarding + `probe_tag` stub + popup badge | P0.1 | P1.2 |
| `doctor-checks-2` | Remaining 6 doctor checks | P0.3 merged | P1.3 |

**Consensus review:** Team lead reviews all three together; confirm doctor output format consistent across P0.3 and P1.3.

### Team `hippo-capture-p2` — Days 4-7

2 members. Team lead assigns schema migration numbers synchronously.

| Member | Task | Depends On | Output |
|--------|------|------------|--------|
| `claude-watcher` | FS-watcher + offsets table + dual-run flag + plist | P0.1, P0.2 | P2.1 |
| `probes` | Per-source probes + `probe_tag` + plist + exclusion filters | P0.1 | P2.2 |

**Consensus review:** Team lead reviews both; specifically verify probe exclusion filters cover every user-facing query path.

### Team `hippo-capture-p3` — Day 7+

Single `cleanup` agent. Sequential P3.1 → P3.2 → P3.3.

---

## Milestones + Success Criteria

| Milestone | Phase End | Criteria |
|-----------|-----------|----------|
| **M1** | P0 | `hippo doctor` reports per-source staleness; future source outage diagnosable via `SELECT * FROM source_health` in < 5 min without log-diving |
| **M2** | P1 | Watchdog fires alarm row within one poll interval of invariant violation; extension heartbeat visible in `source_health.last_heartbeat_ts` |
| **M3** | P2 | Claude sessions ingested by FS watcher with zero missed lines (confirmed by dual-run parity); all 4 sources produce probe round-trips every 5 min |
| **M4** | P3 | tmux-spawn code deleted; investigation issues #49–#53 closed with findings |

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Migration failure mid-deploy (daemon new schema, brain lags) | Medium | High | Reuse existing `schema_handshake` (v0.13 pattern); block watcher writes until brain agrees on `EXPECTED_SCHEMA_VERSION` |
| Watchdog alarm fatigue during bring-up | High | Medium | Default `notify_macos = false`; conservative thresholds; opt-in notifications |
| FSEvents quirks cause watcher to miss events | Low | High | Dual-run mode (`claude_session_mode = "both"`) logs parity mismatches; P3.1 flips only after 48 h clean |
| `probe_tag` filter missing from a query path | Medium | Medium | P2.2 integration test enumerating all query fns on event tables and asserting exclusion present |
| Schema version collision between P2.1 and P2.2 worktrees | Low | Low | Team lead assigns migration numbers synchronously before branching; PR template includes `PRAGMA user_version` check |
| New tmux-targeting patch during P0–P2 | High | Low | AP-10 blocks PRs of this shape; pointer to P2.1 as structural fix |
