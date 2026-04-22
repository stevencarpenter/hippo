# Capture-Reliability Invariants

**TL;DR:** Ten named, machine-checkable invariants that capture reliability must uphold. Each carries a formal detection predicate, concrete thresholds with rationale, context-awareness rules to suppress false positives, and a required backstop action on violation.

---

## Scope and notation

- `now_ms` — current Unix epoch milliseconds (`CAST(strftime('%s','now') * 1000 AS INTEGER)`).
- `sh.<source>.<col>` — shorthand for `SELECT <col> FROM source_health WHERE source = '<source>'`.
- All SQL assumes the `source_health` table designed in `01-source-health.md`.
- "Watchdog" refers to the background process specified in `04-watchdog.md`.
- "Doctor" refers to `hippo doctor` (`crates/hippo-daemon/src/commands.rs:525`).

---

## I-1 Shell liveness

**Assertion.** When the user has an active zsh session and `hippo.zsh` is sourced, shell commands must produce rows in the `events` table (with `source_kind='shell'`) within 60 seconds of being run.

**Threshold: 60 s.** The shell hook round-trip to the daemon socket is 20–50 ms on a healthy system. 60 seconds provides a 1000× buffer for daemon startup delays, SQLite WAL flushes, and transient socket contention.

**Detection predicate:**

```sql
SELECT
    (now_ms - sh.shell.last_event_ts) > 60000   AS stale,
    sh.shell.probe_ok                             AS shell_active
FROM source_health
WHERE source = 'shell';
-- violation = stale = 1 AND shell_active = 1
```

**Defining `shell_active` (`probe_ok`).** The watchdog sets `probe_ok=1` when ALL of:

1. `pgrep -x zsh` returns at least one PID.
2. At least one of `~/.zshrc`, `~/.zshenv`, `~/.config/zsh/.zshrc`, `~/.config/zsh/.zshenv` contains a `source` line matching `hippo.zsh` (static check at watchdog startup, not per-probe).
3. The macOS HID idle time (`ioreg -c IOHIDSystem` → `HIDIdleTime`, in ns) is < 300 × 10⁹ ns (user interacted within 5 min).

If any of the three fails, `probe_ok=0` and I-1 is suppressed.

**Context-awareness.**

| Suppression | Mechanism |
|---|---|
| No zsh process | `pgrep -x zsh` empty → `probe_ok=0` |
| Idle > 5 min | `ioreg HIDIdleTime` over threshold |
| 0–6am local, no recent command | 30-min window suppression |

**Backstop.** Watchdog: increment `sh.shell.consecutive_failures`, structured log `WARN source=shell reason=stale_events`, OTel counter `hippo.watchdog.invariant_violation{source="shell"}`. Doctor: `[!!] shell events: Xs ago (FAIL)`.

---

## I-2 Claude-session end-to-end

**Assertion.** For every Claude session JSONL under `~/.claude/projects/` with `mtime < 5 min`, a `claude_sessions` row with matching `session_id` must exist.

**Threshold: 5 min.** Tailer startup + `--wait-for-file 30` + JSONL first-segment lag. Below 2 min produces false positives on fresh sessions.

**Detection predicate:**

```python
now = time.time()
active = [f for f in glob("~/.claude/projects/**/*.jsonl", recursive=True)
          if (now - os.path.getmtime(f)) < 300]

missing = []
for path in active:
    with open(path) as fh:
        first = fh.readline().strip()
    try:
        session_id = json.loads(first).get("sessionId", "")
    except Exception:
        continue
    if not session_id:
        continue
    row = conn.execute(
        "SELECT 1 FROM claude_sessions WHERE session_id = ? LIMIT 1",
        (session_id,)
    ).fetchone()
    if not row:
        missing.append((session_id, path))
# violation: missing is non-empty
```

**Context-awareness.** Only fires when `len(active) > 0`. No time-of-day suppression.

**Backstop.** Watchdog log naming each missing `session_id`. Doctor: `[!!] claude-session DB: session <id12>… missing (<path>)`.

---

## I-3 Claude-tool concurrency

**Assertion.** If a live Claude JSONL has received a `tool_use` line within 5 min, at least one matching `events` row (`source_kind='claude-tool'`) must exist in that window.

**Threshold: 5 min.** Same ingest path as shell; generous for burst scenarios.

**Backstop.** Structured log only (no alarm by default — noisier than I-2). Opt-in via `[watchdog] claude_tool_alarm = true`.

---

## I-4 Browser round-trip

**Assertion.** If Firefox is running AND the extension has sent a heartbeat within 2 min, a `browser_events` row must appear within that 2-min window.

**Threshold: 2 min.** NM is synchronous; a 2-min gap is a structural break.

**Detection predicate:**

```sql
SELECT
    (now_ms - sh.browser.last_event_ts) > 120000  AS stale,
    sh.browser.probe_ok                             AS extension_active
FROM source_health WHERE source = 'browser';
-- violation = stale = 1 AND extension_active = 1
```

`probe_ok = 1` iff (a) Firefox process running, (b) `source_health.browser.last_heartbeat_ts` < 120 s old.

**Backstop.** Watchdog alarm. Doctor: `[!!] browser events: 21d ago (FAIL, extension active)`.

---

## I-5 Fire-and-forget drop visibility

**Assertion.** Every event dropped by the daemon (socket accept + crash, or buffer overflow) must increment a persistent monotonically-increasing counter. Zero tolerance for invisible drops.

**Rationale.** The fire-and-forget contract (`commands.rs:96–103`) allows drops on daemon crash — but drops must never be silent.

**Backstop.** OTel counter `hippo.daemon.drops_total{source=<name>}`. No user-visible alarm for isolated drops; alarm triggers at I-6 threshold.

---

## I-6 Buffer non-saturation

**Assertion.** Sustained drop rate over any 5-min sliding window must not exceed 0.1% of total event traffic.

**Threshold: 0.1% / 5 min.** At 100 events/min, 0.1% ≈ 1 lost event per 10 min. Above this is structural (daemon crash loop, disk full, socket backlog).

**Backstop.** Watchdog alarm. Doctor: `[!!] drop-rate: 2.3% over 5 min (FAIL, threshold 0.1%)`.

---

## I-7 Watchdog is alive

**Assertion.** The watchdog must write to `source_health WHERE source='watchdog'` at least every 60 s. Stale row > 180 s = alarm.

**Threshold: 180 s stale.** 3× the write interval — one miss may be a hiccup; three misses indicate crash.

**Backstop.** Doctor only (dead watchdog cannot alarm about itself). Doctor: `[!!] watchdog heartbeat: stale 4m ago (FAIL)`.

---

## I-8 Probe freshness (from D3)

**Assertion.** For each source in `source_health` with `probe_last_run_ts IS NOT NULL`: `probe_ok = 1` OR `probe_last_run_ts > now_ms - 900_000` (15 min).

A probe run within 15 min that returned `probe_ok = 0` is an active violation; a probe that hasn't run in over 15 min is also a violation.

**Threshold: 15 min.** 3× the probe interval (5 min) — one missed cycle tolerable, two is alarm-worthy.

**Backstop.** Watchdog alarm. Doctor: `[!!] <source> probe: stale (FAIL)` or `[!!] <source> probe: failing (FAIL)`.

---

## I-9 Fallback file age

**Assertion.** If any JSONL fallback file under `~/.local/share/hippo/` is older than 24 h AND the daemon socket is responsive, recovery is broken.

**Threshold: 24 h.** Once the daemon is up, fallback drain should complete in minutes.

**Backstop.** Doctor: `[!!] fallback files: N files > 24h (recovery broken)`. Watchdog alarm.

---

## I-10 Enrichment-capture decoupling

**Assertion (architectural).** Brain being down (HTTP 5xx/timeout) must NOT prevent `source_health` updates for capture sources. Source-health writes are the daemon/watchdog's responsibility, not the brain's.

**Detection (canary, not runtime):**

```bash
pkill -f hippo-brain
sleep 10
UPDATED=$(sqlite3 ~/.local/share/hippo/hippo.db \
  "SELECT (strftime('%s','now')*1000 - updated_at) < 30000
   FROM source_health WHERE source='shell';")
[ "$UPDATED" = "1" ] || echo "VIOLATION"
```

**Backstop.** Architectural enforcement + CI integration test. If violated, all other alarms become unreliable when the brain is degraded.

---

## Threshold summary

| Invariant | Threshold | Rationale |
|---|---|---|
| I-1 shell liveness | 60 s | 1000× typical round-trip |
| I-2 claude-session coverage | 5 min | tailer startup + file-wait |
| I-3 claude-tool concurrency | 5 min | same ingest path; generous |
| I-4 browser round-trip | 2 min | NM synchronous; 2 min = broken |
| I-5 drop visibility | every drop | zero tolerance |
| I-6 drop rate | 0.1% / 5 min | ~1 lost / 10 min @ 100/min |
| I-7 watchdog heartbeat | 180 s stale | 3× interval |
| I-8 probe freshness | 15 min | 3× probe interval |
| I-9 fallback file age | 24 h | should drain in minutes |
| I-10 decoupling | architectural | brain-down must not mask alarms |

## Context-awareness rules (consolidated)

| Invariant | Suppress when |
|---|---|
| I-1 | No zsh process; idle > 5 min; night hours w/ no recent command |
| I-2 | No JSONL mtime < 5 min |
| I-3 | No live JSONL with recent `tool_use` |
| I-4 | Firefox not running; heartbeat file absent/stale |
| I-9 | — (always applicable if daemon up) |
