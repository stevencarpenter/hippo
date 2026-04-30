# Capture-Reliability Anti-Patterns

**TL;DR:** Eleven concrete failure modes observed in hippo's capture layer, each with a forbidden behavior, the reason it is forbidden, and the required alternative. Any of these patterns in a PR is a review blocker.

---

## AP-1: Blocking the Shell Hook on Health Writes

**Forbidden.** Writing to `source_health` or any SQLite table synchronously inside `shell/claude-session-hook.sh`, or inside `send_event_fire_and_forget` in `crates/hippo-daemon/src/commands.rs`.

**Why.** The shell hook runs in the user's interactive prompt critical path. The `&!` disown pattern exists to keep the hook from adding perceptible latency. `send_event_fire_and_forget` documents its durability contract at `commands.rs:96–103`: success means the frame hit the socket, not that SQLite was touched. Health writes belong in `flush_events` (background Tokio timer).

**The right way.** `source_health` is updated inside `flush_events` (`daemon.rs`) after the batch write succeeds or fails. Shell hook writes nothing to disk; sends one fire-and-forget frame and returns.

---

## AP-2: Coupling Capture Health to Enrichment Health

**Forbidden.** Setting `source_health.status = 'degraded'` because LM Studio is unreachable, the `enrichment_queue` is backed up, or `hippo-brain` is offline.

**Why.** Orthogonal failure domains. Capture has operated correctly while enrichment was offline for days. Coupling them means a routine LM Studio model swap triggers a false capture alarm. `hippo doctor` already separates brain health from daemon health; `source_health` must maintain the same boundary. See I-10.

**The right way.** `source_health` tracks only capture-layer observables: `last_event_ts`, `consecutive_failures`, `events_last_1h/24h`. Enrichment health lives in the brain `/health` endpoint. Watchdog fires alarms only on capture metrics; never inspects `enrichment_queue` depth.

---

## AP-3: Alerting on Absolute Silence Without Upstream Context

**Forbidden.** Firing `capture_alarms` or macOS notification whenever `source_health.last_event_ts` is older than N minutes, unconditionally.

**Why.** Shell silence overnight is normal. Browser silence is normal when Firefox is closed. Unconditional alarms train users to dismiss them — and they'll dismiss a real outage for the same reason. Alerting must distinguish "user active, source silent" from "user asleep, source correctly quiet."

**The right way.** Watchdog gates silence alarms on corroborating signals: terminal is frontmost app, recent keystroke activity, source process confirmed running (`pgrep -x firefox` for browser, `pgrep -x zsh` + HIDIdleTime for shell). Thresholds per-source, configurable, conservative defaults.

---

## AP-4: Adding Notification Types Outside the Single Alarm Channel

**Forbidden.** Calling `osascript -e 'display notification'` from anywhere other than the watchdog's `notify_macos` path. Adding a new `capture_alarms.alarm_type` without updating the rate-limiter.

**Why.** Hippo already has multiple notification paths (doctor, version mismatch, brain error). Fragmented paths cannot be rate-limited uniformly. Users will disable all hippo notifications after three spurious alerts during bring-up.

**The right way.** One notification channel: watchdog reads `capture_alarms`, applies rate limit (1 macOS notification per invariant per hour via `last_notified_at`), calls `osascript` once per qualifying alarm. `notify_macos` defaults to `false`. All other code paths write to `capture_alarms` and let the watchdog decide.

---

## AP-5: Reinventing Metrics Storage Alongside OTel

**Forbidden.** Adding in-process `HashMap<String, u64>` or `AtomicU64` metrics store for per-source event counts, then exposing via a new HTTP endpoint or a new SQLite table distinct from `source_health`.

**Why.** `crates/hippo-daemon/src/metrics.rs` already defines OTel counters (`EVENTS_INGESTED`, `FLUSH_EVENTS`, `FALLBACK_WRITES`). A parallel store diverges from the OTel pipeline, creates two sources of truth, and is invisible to Grafana/Prometheus. This mistake happened once already — `MetricsCollector` in the brain was a parallel store noted in CLAUDE.md "for future OTel export" that was never wired.

**The right way.** Add `source` `KeyValue` attribute to existing OTel counters (P0.4). Aggregates for `source_health` rolling counts computed from SQLite event tables by the recompute job, not in-process. Do not create new HTTP metrics endpoints; Prometheus scrape already exists behind `otel` feature flag.

---

## AP-6: Letting Probes Appear in User-Facing Queries

**Forbidden.** Inserting synthetic probe events into `events`, `claude_sessions`, or `browser_events` without filtering them from every user-facing query: `hippo ask`, `hippo events`, `hippo sessions`, MCP `search_events`, `search_knowledge`, `get_entities`.

**Why.** A probe in RAG retrieval produces nonsense answers. A probe in `hippo events` output makes the user think they ran a command they didn't. Probe contamination is hard to detect after the fact — probe rows look structurally identical to real rows.

**The right way.** Every probe row has `probe_tag TEXT NOT NULL` set to a non-null sentinel. Every query touching event tables adds `AND probe_tag IS NULL` (or `NOT LIKE 'synthetic%'`). P2.2 ships a Rust integration test asserting this filter in all query paths. **Upstream filter is load-bearing:** daemon never enqueues probe events — downstream filters are belt-and-braces.

---

## AP-7: Making `hippo doctor` Slower Than 2 Seconds

**Forbidden.** Adding a doctor check that performs HTTP without a timeout, does a full table scan on `events` (millions of rows), spawns an unbounded subprocess, or blocks on brain returning healthy.

**Why.** `hippo doctor` is run interactively, especially during incidents. 10-second doctor is useless when something is on fire. Current LM Studio check already enforces 1-second timeout; that constraint extends to every new check.

**The right way.** All new SQL uses indexed columns only. HTTP checks carry 1-second timeout. Any check that can't complete in 500 ms on a loaded machine moves to `hippo doctor --full`. Total wall-clock asserted under 2 s in CI.

---

## AP-8: Leaving the Watchdog Itself Unmonitored

**Forbidden.** Shipping the watchdog (P1.1) without a mechanism to detect that the watchdog process has stopped.

**Why.** Watchdog is launchd-managed. LaunchAgents fail to load silently on plist errors or post-update binary moves. A dead watchdog means every downstream invariant stops firing while the user believes they're being monitored — strictly worse than no watchdog.

**The right way.** Watchdog writes heartbeat timestamp to `source_health WHERE source='watchdog'` on every cycle. Doctor check 8 (P0.3 stub, completed in P1.3) reads it and emits `[!!]` if older than `2 * poll_interval`. Watchdog also sets `HIPPO_WATCHDOG_VERSION` env so `hippo doctor --versions` confirms binary alignment.

---

## AP-9: Deleting the Fallback JSONL Path

**Forbidden.** Removing `storage::write_fallback_jsonl`, removing fallback branches in `flush_events`, or omitting fallback from any new ingestion path — even in P3 cleanup.

**Why.** Fallback JSONL is the last-resort durability backstop: catches events when SQLite is locked, DB is corrupt, or daemon mid-restart. Contract documented at `commands.rs:96–103`. At least one real event-loss incident was fully recovered from fallback files. No substitute.

**The right way.** Fallback path stays permanently. What improves is observability: P0.3 alarms on fallback > 24 h old (not replayed); P1.3 alarms on fallback count exceeding threshold. Accumulating fallback files are a symptom; response is to diagnose root cause, not remove the backstop.

---

## AP-10: Patching tmux Window Targeting (historical, retired 2026-04-25)

**Status: retired.** T-5 shipped the FS watcher; T-8 deleted the tmux-spawn path entirely from `shell/claude-session-hook.sh` and `crates/hippo-daemon/src/main.rs`. There is no tmux code in hippo to patch. Kept here as a historical record of why the watcher work was prioritized.

**Original rule (no longer applicable).** During P0–P2, PRs modifying tmux window creation, tmux session targeting, or the `TMUX_TARGET_SESSION` path were rejected. Tmux-targeting code had been patched at least four times for variations of the same bug class (index conflicts, session-name collisions, non-default `base-index`, `$TMUX_PANE` unset). The structural fix was the long-lived FS watcher (P2.1), which eliminated the tmux dependency.

**If a session-ingestion regression occurs now**, triage against the watcher (`watch_claude_sessions.rs`). Manual recovery remains `hippo ingest claude-session <path>`.

---

## AP-11: Silently Swallowing Capture Errors

**Forbidden.** Using `.filter_map(|r| r.ok())`, `.ok().unwrap_or_default()`, or `.ok()` on a `Result` inside any capture write path without logging the error and bumping a failure counter.

**Why.** Silent swallowing turns capture bugs invisible. `flush_events` in `daemon.rs` already demonstrates the correct pattern: every `Err(e)` branch calls `warn!`, writes to fallback, and calls `state.drop_count.fetch_add`. Failure is counted, logged, recoverable. Iterator patterns like `.filter_map(|r| r.ok())` produce zero log output when half the writes fail.

**The right way.** Every error in a capture write path either propagates to the caller or, where continuation is required (batch processing), calls `warn!` with the full error and increments `consecutive_failures` in `source_health`. Clippy lint `clippy::result_map_unit_fn` and custom Semgrep rule flag silent-swallow in CI.

---

## AP-12: `INSERT OR IGNORE` on a derived bucket key whose content is mutable

**Forbidden.** Using `INSERT OR IGNORE` (or `INSERT … ON CONFLICT DO NOTHING`) when the conflict key identifies a *bucket* whose row content is derived from an ever-growing source (e.g., `(session_id, segment_index)` for a JSONL segment that accretes messages over time).

**Where seen:** `crates/hippo-daemon/src/claude_session.rs::insert_segments` until 2026-04-27 (see T-A.3 and `docs/capture-reliability/11-watcher-data-loss-fix.md` Phase 1).

**Why it's wrong.** `(session_id, segment_index)` is a *bucket* key — it identifies a slice of a JSONL session file, but the slice's *content* (`message_count`, `tool_calls_json`, `assistant_texts`, etc.) grows over time as the file grows. The watcher re-runs `extract_segments` on every FSEvents notification. With `INSERT OR IGNORE`, the **first** partial extraction wins forever; subsequent reparses with more content are silently rejected. Result: every Claude session segment between 2026-04-25 and the fix was a tiny prefix of the actual content (median 2–4 messages out of hundreds; 59/63 recent segments had empty `tool_calls_json` — 6% non-empty vs ≈50%+ historical baseline).

**Detection.** A query like the following will show implausibly low tool-extraction rates after a regression:

```sql
SELECT
  ROUND(100.0 * COUNT(CASE WHEN json_array_length(tool_calls_json) > 0 THEN 1 END)
        / COUNT(*), 1) AS pct_with_tools
FROM claude_sessions
WHERE end_time > strftime('%s', 'now', '-7 days') * 1000;
```

Healthy baseline: ≥ 40%. During the bug: 6%.

**Use instead.** `INSERT … ON CONFLICT(key) DO UPDATE SET (mutable cols)`. If you also need to detect insert-vs-update for downstream side effects (e.g., re-enqueueing for enrichment), compare a content hash stored on the row rather than keying off the conflict outcome alone.

```sql
INSERT INTO claude_sessions (session_id, segment_index, content_hash, tool_calls_json, …)
VALUES (?, ?, ?, ?, …)
ON CONFLICT(session_id, segment_index) DO UPDATE SET
    content_hash    = excluded.content_hash,
    tool_calls_json = excluded.tool_calls_json,
    …;
```

**Rule of thumb.** `INSERT OR IGNORE` is correct **only** when the conflict key uniquely identifies *the entire immutable row* (e.g., `envelope_id` for a fire-and-forget event, `tool_use_id` for a per-tool event). It is wrong for *bucket* keys whose row content is derived and mutable.

**Cross-reference:** `docs/capture-reliability/11-watcher-data-loss-fix.md` Phase 1.
