# M3 Decision: how do we validate the watcher before flipping the default?

**Status:** resolved 2026-04-25 — proceeded with **Option D** (skip the formal validation step) after a query session against the live DB on `main` produced ground-truth evidence equivalent to what Option C would have produced. T-7 shipped in PR #88 on 2026-04-25.

**Outcome summary:**
- Last 7 days: 466 JSONL files modified on disk; **463 captured by the watcher** (99.36%).
- The 3 unrepresented files were verified by hand: in all three, `claude_session_offsets.byte_offset == size_at_last_read == filesize` (watcher fully read them); `extract_segments` returned 0 segments for legitimate reasons (very short conversations or non-conversation snapshot files).
- Same 7 days: tmux tailer captured 94 distinct sessions; watcher captured 727. Tailer-only sessions: **0**. Watcher-only sessions: **633**. The watcher is dramatically more reliable than the tailer it replaces.
- The query session that produced these numbers is recorded in PR #88 (T-7) as the M3 evidence.

The Option C reconciliation script was **not** built. The full original analysis is preserved below for posterity.

---

## Original analysis (2026-04-25)

The M3 phase gate as originally written cannot detect what it claims to detect. We need to pick a different validation strategy for T-7. Four options below; **recommendation: Option C** (one-shot manual reconciliation script). It's ~50 LOC, gives ground truth, and avoids adding ongoing runtime cost or new tests.

---

## What the gate was supposed to do

T-5 shipped a "dual-run" mode (`claude_session_mode = "both"`) so we could run the new FS watcher *alongside* the existing tmux tailer for ≥48 h, compare their outputs, and only flip the default to `"watcher"` after the comparison was clean. The fear being mitigated: FSEvents on macOS sometimes drops events for high-churn paths or specific filesystems (NFS, iCloud-synced volumes, fast atomic-rename patterns). If the watcher silently missed something the tailer caught, dual-run would surface it.

The mechanism was supposed to be the `claude_session_parity` table — one row per file per hour with `tailer_count`, `watcher_count`, `mismatch_count`. The M3 gate is `SELECT COUNT(*) WHERE mismatch_count > 0` returning zero across the last 48 h.

## What the gate actually does

Two separate ingestion paths, two different tables.

**Watcher path** (`crates/hippo-daemon/src/watch_claude_sessions.rs:188-204`):
```rust
let task = tokio::task::spawn_blocking(move || {
    let conn = open_db(&db_path_owned)?;
    let (inserted, skipped, errors) = ingest_session_file(&conn, &path_owned);
    ...
});
```
`ingest_session_file` → `insert_segments` → `INSERT OR IGNORE INTO claude_sessions`.

**Tmux tailer path** (`shell/claude-session-hook.sh:96` → `hippo ingest claude-session --inline` → `crates/hippo-daemon/src/claude_session.rs:1088 ingest_tail`):
```rust
for envelope in envelopes {
    send_event_fire_and_forget(socket_path, &envelope, timeout_ms).await
}
```
Sends per-line envelopes over the daemon socket. The daemon writes them to the **`events`** table via `storage::insert_event_at` (`daemon.rs:362`). It does **not** call `write_session_segments`. It does **not** insert `claude_sessions` rows.

(There's one exception: `claude_session::ingest_batch` — used only when no tmux server is running, hook line 124 — *does* call `write_session_segments` after streaming events. This is a corner case the parity gate isn't designed around.)

**Parity computation** (`watch_claude_sessions.rs:299-308, 319-320`):
```rust
let total = SELECT COUNT(*) FROM claude_sessions WHERE source_file = ?;
let tailer_count = total.saturating_sub(watcher_count);
let mismatch_count = tailer_count;
```
The watcher is the sole writer to `claude_sessions`. So `total ≡ watcher_count`. So `tailer_count ≡ 0`. So `mismatch_count ≡ 0`. Always. The function-internal comment at `watch_claude_sessions.rs:314-318` already acknowledges this.

**Live evidence on this machine (2026-04-25, ~2h sample):**
```
2,037 parity rows
SUM(watcher_count)  = 632
SUM(tailer_count)   = 0
SUM(mismatch_count) = 0
```
The gate "passes" trivially. It would also pass if the tailer were dead, the watcher were the only thing capturing, or the file were never read at all.

## What we actually want from M3

Plainly: *"I am confident that defaulting to `watcher`-only mode will not silently drop Claude sessions that the existing tailer would have captured."*

That confidence can come from three independent signals:

1. **Comparative measurement** — for each session file, the segments the watcher inserted equal the segments a perfect ingestion would have produced. (Counted once at decision time, or continuously during the soak.)
2. **Coverage measurement** — every recent session file in `~/.claude/projects/` has a corresponding `claude_sessions` row.
3. **Operational evidence** — the watcher has been running in `both` (or even `watcher`-only) mode for some period without surfacing issues in your day-to-day use.

Signal #3 is partially earned already — your machine has been on `both` mode and the watcher has captured 632 segments in the recent window with no complaints. That's not zero confidence; it's just not formal validation.

---

## Options

### Option A — Make the tailer also write `claude_sessions`

**What:** Modify `claude_session::ingest_tail` to also call `write_session_segments` (or equivalent) after the per-line envelope dispatch, so both paths populate `claude_sessions`. Then `total = COUNT(*)` would be the union of both writers' inserts, and (because of `INSERT OR IGNORE` on `(session_id, segment_index)`) any mismatch would be a real divergence.

**Cost:** ~30-50 LOC in `claude_session.rs`; the tailer process now does extra SQLite work it didn't before; new test coverage required to confirm the dual-write doesn't double-count; need to be careful about which writer "wins" the offset advance and how `claude_session_offsets` interacts.

**Risk:** Adds runtime cost to a code path we're about to delete (T-8 deletes the tmux-spawn path entirely). Spending engineering effort to instrument code that's scheduled for removal.

**Bottom line:** Defensible, but you're optimising for a measurement that you'll throw away.

### Option B — Compute a "shadow tailer count" from the `events` table

**What:** Instead of changing the tailer, derive what the tailer "should have written" by counting `events WHERE source_kind = 'claude-tool' AND envelope_id LIKE ...` (or some proxy) within the parity window, then comparing to `watcher_count`. The mapping between Claude session JSONL lines and `events` rows is many-to-many in places (a single tool-use line can produce two envelopes; non-tool messages don't produce events at all), so the comparison would have to be a proxy like "number of distinct session_ids in `events` matches number of distinct session_ids in `claude_sessions`."

**Cost:** ~40-80 LOC in `watch_claude_sessions::write_parity_row`; nontrivial reasoning about which `events` rows correspond to which JSONL lines; the comparison won't be exact.

**Risk:** False positives. The tailer captures *tool calls*; the watcher captures *conversation segments*. Forcing them into a count comparison invites spurious "mismatches" that everyone learns to ignore — exactly the alarm-fatigue pattern that the watchdog defaults already try to prevent.

**Bottom line:** Engineering busy-work for a metric that was never going to be apples-to-apples.

### Option C — One-shot manual reconciliation script ★ recommended

**What:** A short script (~50 LOC, can live in `scripts/m3-reconcile.sh` or as a `hippo` subcommand) that:

1. Globs `~/.claude/projects/**/*.jsonl` modified in the last 7 days.
2. For each file, runs `extract_segments` once (the same parser the watcher and batch importer use) and records the expected segment count.
3. Queries `SELECT COUNT(*) FROM claude_sessions WHERE source_file = ?` for the same path.
4. Reports any file where actual ≠ expected, plus the magnitude of the gap.

Run it once before T-7. If it returns clean across N days of session files, M3 is satisfied empirically. The `claude_session_parity` table stays as a "watcher is alive" trace; the M3 gate predicate becomes "this script exits 0 across at least 7 days of session files."

**Cost:** ~50 LOC of Rust (or shell + sqlite3). Reuses `extract_segments` directly. No runtime overhead. No new tests beyond a smoke test on the script itself.

**Risk:** Only validates at the moment you run it — doesn't catch a future regression. But T-8 deletes the tailer code anyway, so there's no "future regression" to catch except FSEvents itself, which a continuous parity check wouldn't validate either.

**Bottom line:** Cheapest, gives you the actual ground-truth answer, no ongoing maintenance.

### Option D — Drop dual-run entirely; soak + spot check

**What:** Acknowledge that you've already been running `both` mode on your daily-driver machine for some time, watcher has captured 632+ segments in the recent window, no issues observed. Skip M3 as a formal gate. Flip T-7's `Depends on` to "30 days of `both` or `watcher` operational, no user-reported gaps." Spot-check 5–10 sessions before flipping.

**Cost:** Zero engineering. The risk register entry "FSEvents quirks cause the watcher to miss events" remains, but it always was going to remain — the parity table never could have caught a systematic FSEvents issue without independent ingestion to compare against.

**Risk:** Highest in theory (no measurement), but lowest in practice if you have soak time. Honest assessment of what dual-run was buying.

**Bottom line:** The most intellectually honest option, but easy for reviewers/future-you to read as "skipped the validation step."

---

### Recommendation (superseded — see Outcome at top of file)

> Note: this section is the pre-decision draft recommendation. The actual outcome was Option D (skip the formal validation step entirely after the live-DB query produced equivalent ground-truth evidence). Kept for the historical reasoning.

**Option C.** Reasoning:

- It produces the only number you actually care about: "for the JSONL files on disk, did we capture the right number of segments?"
- The implementation reuses an existing function (`extract_segments`) — no new parsing logic, no new tests for the parser.
- It avoids investing engineering effort in code that T-8 will delete (Option A) or in a metric that's structurally noisy (Option B).
- It produces a single boolean ("M3 passed at <date>") that can go in the T-7 PR description as evidence, rather than a continuous gauge that gets argued about.

If you also want continuous monitoring after T-7 ships, a reduced version of the same script can run as a periodic check (probe-style). But that's a P4 question, not an M3 question.

### What I would NOT recommend (pre-decision draft)

- Trying to "fix" `claude_session_parity` to produce a useful number. The table's design assumed both paths write `claude_sessions`. They don't. Salvaging it costs more than replacing it.
- Removing the `claude_session_parity` table now. It's a cheap heartbeat trace and may become useful if a future ingestion path does write `claude_sessions`. Leaving it is harmless.
- Waiting for "more parity data" before deciding. More rows of `(632, 0, 0)` won't change the answer.

### If Option C is approved, here's the work to unblock T-7 (not done — Option D was chosen)

> Note: This work was never done — Option D was chosen instead. Kept as historical context for what the Option C path would have looked like.

1. Implement `scripts/m3-reconcile.sh` (or `hippo doctor m3-reconcile`).
2. Run it across ≥7 days of session files. Confirm exit 0.
3. Edit `07-roadmap.md` — flip T-7 `Status: blocked` → `open` and update its `Depends on:` to cite the reconciliation result.
4. Ship T-7 (it's a 3-file, ~10-line behaviour change).
5. Wait the documented 7-day soak under the new default before unblocking T-8.

Total effort to unblock T-7: ~2 hours (script + 7-day waiting period for the reconciliation window).
