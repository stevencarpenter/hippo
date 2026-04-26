# Doctor Upgrade Specification

> **Status: shipped.** Checks 1, 4, 7, 8 in T-0.3 / PR #70; checks 2, 5, 6, 9, 10 in T-1.3 / PR #81. This doc is the live reference for what each check asserts, its threshold, and its exit-code contribution.

**TL;DR:** Ten new isolated checks for `hippo doctor`, each with exact queries, thresholds, exit-code contribution, and performance budget. Doctor exits with a non-zero count of failures, enabling CI and scripted incident response.

---

## Existing doctor structure (baseline)

Current: `handle_doctor` at `crates/hippo-daemon/src/commands.rs:525–613`. Format `[OK]` / `[!!]` / `[--]`. Exit code: always `Ok(())` — never non-zero today.

## Exit-code strategy

Each check contributes to a mutable `fail_count: u32`. Doctor exits with `std::process::exit(fail_count as i32)` after all checks complete.

| Symbol | Color | Meaning | `fail_count` |
|---|---|---|---|
| `[OK]` | green | passed | +0 |
| `[WW]` | yellow | warning | +0 |
| `[!!]` | red | failure | +1 |
| `[--]` | gray | N/A | +0 |

`NO_COLOR` env var and `--no-color` flag suppress ANSI.

**Isolation guarantee.** Every check is wrapped in `catch_unwind` / `try/except`: a malformed DB or missing file never prevents subsequent checks.

## Output format

Left column: fixed-width label (32 chars). Right column: status + detail.

```
shell events               [OK] 4s ago
claude-session DB          [!!] session abc123f not in DB (FAIL, active JSONL 2m old)
browser ext dist/          [!!] dist/background.js missing
zsh hook sourced           [OK] sourced in /Users/you/.config/zsh/.zshrc
native-msg manifest        [OK] path=/usr/local/bin/hippo, extension ID matches
watchdog heartbeat         [WW] 95s ago (WARN, expected < 60s)
log file sizes             [OK] all under 50MB
fallback files             [OK] none pending
schema version             [OK] 8
session-hook log           [OK] 3 invocations, 2 DB rows (last 1h)
```

## Performance budget

Combined: **< 2 seconds**. Run independent groups via `tokio::join!`. Reuse the brain HTTP call from `print_brain_health_details` for check 10 — no extra round-trip.

---

## Check 1 — Per-source staleness

**Label:** `<source> events` (one line each)

**Query:**
```sql
SELECT source, last_event_ts, last_error_msg, consecutive_failures,
       events_last_1h, probe_ok
FROM source_health
WHERE source IN ('shell', 'browser', 'claude-session', 'claude-tool');
```

**Thresholds:**

| Source | WARN | FAIL | Suppression |
|---|---|---|---|
| `shell` | > 60 s | > 300 s | `probe_ok = 0` |
| `claude-session` | > 300 s | > 1800 s | no active JSONL |
| `claude-tool` | > 300 s | > 600 s | no live tool_use |
| `browser` | > 120 s | > 600 s | Firefox not running |

If no row: `[--] <source> events: no data (watchdog not running)`.

**Why this catches the sev1:** Doctor previously checked process liveness but never asked whether events were actually landing. Per-source staleness is the first thing an operator needs during incident response.

**Performance:** Single SQLite read, < 5 ms.

---

## Check 2 — Native Messaging manifest healthy

**Label:** `native-msg manifest`

**Sub-checks:** manifest file exists; valid JSON; `path` field is executable; `allowed_extensions` includes `hippo-browser@local` (from `extension/firefox/manifest.json:33`).

**Thresholds:** All pass → `[OK]`; any fail → `[!!]` (+1).

**Why this catches the sev1:** A stale NM manifest path causes Firefox to silently fail to launch the native host.

**Performance:** Filesystem + minimal python3, < 50 ms.

---

## Check 3 — Extension dist/ present

**Label:** `browser ext dist/`

Derives extension path from NM manifest's binary path (`repo_root = dirname³(hippo_bin)`, `ext_dir = repo_root/extension/firefox`). Checks `dist/background.js` and `dist/content.js` exist.

If manifest absent (check 2 failed): `[--] browser ext dist/: skipped`.

**Thresholds:** Both present → `[OK]`; either missing → `[!!]` (+1 per file).

**Why this catches the sev1:** This is precisely H2 — the exact regression would have been caught.

**Performance:** Two `stat` calls, < 5 ms.

---

## Check 4 — zsh hook sourced

**Label:** `zsh hook sourced`

Greps for `hippo.zsh` in `~/.zshrc`, `~/.zshenv`, `~/.config/zsh/.zshrc`, `~/.config/zsh/.zshenv`. If found, extracts the sourced path and confirms the script exists.

**Thresholds:** Found + script exists → `[OK]`. Found but script missing → `[WW]`. Not found anywhere → `[!!]` (+1).

**Why this catches the sev1:** A `.zshrc` refactor that removes the source line silently stops all shell capture.

**Performance:** grep on ≤ 4 small files, < 20 ms.

---

## Check 5 — Live-session vs DB reconciliation

**Label:** `claude-session DB`

See I-2 detection predicate. Summary: glob active JSONLs, compare their `sessionId` against `claude_sessions.session_id`.

**Thresholds:**
- No active JSONLs → `[--]`
- All in DB → `[OK]`
- Any missing → `[!!]` (+1 per missing, capped at +3)

**Why this matters:** Any failure of the FS watcher (`com.hippo.claude-session-watcher`) — service unloaded, FSEvents not firing on the volume, ingestion timing out into permanent cooldown — surfaces as JSONLs on disk with no matching `claude_sessions` row. The sev1 fingerprint that originally motivated this check (a `tmux -t` index error in the per-session tailer) was structurally eliminated by retiring the tailer in T-8/PR #89, but the check is still load-bearing for any future watcher regression.

**Performance:** Filesystem glob + N SQLite lookups, < 100 ms.

---

## Check 6 — Session-hook debug log reconciliation

**Label:** `session-hook log`

Counts `"hook invoked"` lines in last 1 h from `~/.local/share/hippo/session-hook-debug.log` (tail-bounded to 10k lines); compares to `claude_sessions` rows created in last hour.

**Thresholds:**

| Condition | Output |
|---|---|
| Both zero | `[--]` no hook activity |
| Invocations > 0 AND rows > 0 | `[OK]` N invocations, M rows |
| Invocations > 0 AND rows = 0 AND invocations < 3 | `[WW]` too fresh |
| Invocations ≥ 3 AND rows = 0 | `[!!]` (+1) |

**Why this catches the sev1:** A hook firing repeatedly with zero DB rows is exactly the current situation. Ratio is the triage grep.

**Performance:** `tail + awk` + one SQL query, < 100 ms.

---

## Check 7 — Log file sizes

**Label:** `log file sizes`

Scans `~/.local/share/hippo/` for `*.log` and `*.jsonl`. Thresholds: 50 MB WARN, 200 MB FAIL.

**Why this catches the sev1:** No log rotation exists today. `session-hook-debug.log` can grow unbounded, eventually filling the partition.

**Performance:** `find + stat`, < 20 ms.

---

## Check 8 — Watchdog heartbeat

**Label:** `watchdog heartbeat`

**Query:**
```sql
SELECT updated_at, (strftime('%s','now')*1000 - updated_at)/1000 AS age_secs
FROM source_health WHERE source = 'watchdog' LIMIT 1;
```

**Thresholds:** < 60 s → `[OK]`; 60–180 s → `[WW]`; ≥ 180 s or no row → `[!!]` (+1).

**Why this catches the sev1:** If the watchdog dies, all other staleness checks return stale data indefinitely. This check surfaces meta-failure first.

**Performance:** Single SQLite row, < 5 ms.

---

## Check 9 — Fallback file age

**Label:** `fallback files`

Extends existing count check (`commands.rs:590–597`) with an age predicate.

**Thresholds:** None → `[OK]`; all < 24 h → `[WW]`; any > 24 h with daemon up → `[!!]` (+1).

**Why this catches the sev1:** Silent recovery failure — if the drain task breaks, events pile up forever.

**Performance:** Filesystem metadata, < 20 ms.

---

## Check 10 — Schema version match

**Label:** `schema version`

Reads daemon `PRAGMA user_version` and compares to brain's `expected_schema_version` from cached `/health` JSON (already fetched earlier in doctor).

**Thresholds:** Match OR daemon version in `accepted_read_versions` → `[OK]`; mismatch → `[!!]` (+1).

**Why this catches the sev1:** A daemon/brain schema drift crashes every enrichment pass. Doctor previously reported `[OK] Brain is running` while enrichment silently died.

**Performance:** One PRAGMA + cached JSON parse. Zero additional network.

---

## `hippo doctor --explain` mode

When `--explain` is passed, each failing check appends a remediation block:

```
[!!] browser ext dist/    dist/background.js missing in /repo/extension/firefox/
     CAUSE:  Extension not built after source change
     FIX:    cd /repo/extension/firefox && npm install && npm run build
     DOC:    docs/capture-reliability/02-invariants.md#i-4-browser-round-trip
```

Each check returns `CheckResult { status, label, detail, cause, fix, doc_ref }`.

---

## Summary table

| # | Label | Mechanism | OK | WARN | FAIL | Network |
|---|---|---|---|---|---|---|
| 1 | `<source> events` | `source_health` SQL | < thresh | near | > thresh | No |
| 2 | `native-msg manifest` | stat + JSON | all pass | — | any | No |
| 3 | `browser ext dist/` | stat | both files | — | either missing | No |
| 4 | `zsh hook sourced` | grep | found + exists | found, no script | not found | No |
| 5 | `claude-session DB` | glob + SQL | all in DB | — | any missing | No |
| 6 | `session-hook log` | awk + SQL | nonzero both | low ratio | 0 DB / ≥3 fires | No |
| 7 | `log file sizes` | find + stat | < 50 MB | 50–200 MB | > 200 MB | No |
| 8 | `watchdog heartbeat` | SQL | < 60 s | 60–180 s | > 180 s | No |
| 9 | `fallback files` | stat | none | recent | > 24 h + daemon up | No |
| 10 | `schema version` | PRAGMA + cached JSON | match | — | mismatch | reuses brain call |

All ten checks combined target < 2 s with zero additional network calls beyond the existing brain HTTP request.
