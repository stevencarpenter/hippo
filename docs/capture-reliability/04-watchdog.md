# Watchdog Process

> **Status: shipped.** Core in T-1 / PR #79; launchd plist + `hippo alarms` CLI in T-2 / PR #83. This doc is the live reference for the watchdog's process model, plist, and the `capture_alarms` schema.

**TL;DR:** A short-lived launchd agent (`com.hippo.watchdog`) runs every 60 seconds, asserts invariants I-1..I-10 against `source_health`, and writes structured alarms to a new `capture_alarms` table when violations are detected. A wedged daemon cannot silence its own alarm because the watchdog is an independent process under a separate launchd job.

## Process Model

New subcommand `hippo watchdog run` installed as a launchd LaunchAgent. Plist at `launchd/com.hippo.watchdog.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hippo.watchdog</string>
    <key>ProgramArguments</key>
    <array>
        <string>__HIPPO_BIN__</string>
        <string>watchdog</string>
        <string>run</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key><string>__HOME__</string>
        <key>PATH</key><string>__PATH__</string>
    </dict>
    <key>StartInterval</key><integer>60</integer>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key><string>__DATA_DIR__/watchdog.stdout.log</string>
    <key>StandardErrorPath</key><string>__DATA_DIR__/watchdog.stderr.log</string>
    <key>WorkingDirectory</key><string>__HOME__</string>
</dict>
</plist>
```

`KeepAlive` absent (defaults `false`). `StartInterval=60` spawns fresh every 60 s. Deliberately not long-lived — a long-lived tokio task inside `hippo-daemon` would be silenced if the daemon panics or deadlocks. Critical property: watchdog's failure domain is independent from what it monitors.

`hippo daemon install` installs this plist alongside `com.hippo.daemon.plist` and `com.hippo.brain.plist` (pattern from `crates/hippo-daemon/src/main.rs:244–265`).

## Responsibilities (Ordered)

**Step 1 — Write own heartbeat.**

```sql
INSERT INTO source_health (source, updated_at, last_success_ts)
VALUES ('watchdog', :now_ms, :now_ms)
ON CONFLICT(source) DO UPDATE SET
    updated_at      = excluded.updated_at,
    last_success_ts = excluded.last_success_ts;
```

Runs before any assertion work. If the watchdog crashes after step 1, heartbeat is still committed. Doctor treats stale `watchdog.updated_at` as failure (see `03-doctor-upgrades.md`).

**Step 2 — Read all `source_health` rows.**

```sql
SELECT * FROM source_health;
```

One query over a small table. If absent (pre-migration install), create it from canonical DDL in `01-source-health.md` and exit clean without alarms.

**Step 3 — Assert invariants I-1..I-10.** Evaluate each as a predicate over in-memory rows from step 2. No additional queries. All required columns present.

**Step 4 — Raise alarms for each failing invariant.** See Alarm Contract.

**Step 4b — Auto-resolve recovered alarms.** After raising new alarms, walk every active (un-acked, un-resolved) row and check whether its `(invariant_id, source)` pair appears in the *current* tick's violation set. Pairs absent from the violation set get `clean_ticks += 1`; pairs present get `clean_ticks = 0`. When `clean_ticks` reaches `2`, set `resolved_at = now_ms`. Two ticks (≈120 s) is the minimum window that prevents single-tick flap from prematurely clearing an alarm. Resolved rows persist until `hippo alarms prune` acks them with `ack_note='auto-resolved'`.

**Step 5 — Exit clean.** `std::process::exit(0)`. launchd re-launches after `StartInterval`.

## Alarm Contract

### Table

```sql
CREATE TABLE IF NOT EXISTS capture_alarms (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    invariant_id TEXT    NOT NULL,
    raised_at    INTEGER NOT NULL,
    details_json TEXT    NOT NULL,
    acked_at     INTEGER,
    ack_note     TEXT,
    -- v11: set when the watchdog observes 2 consecutive clean ticks for
    -- this alarm's (invariant_id, source) pair. Resolved rows do not
    -- suppress new alarms (rate-limit ignores them) and do not contribute
    -- to the doctor exit code.
    resolved_at  INTEGER,
    -- v11: consecutive-clean tick counter, reset to 0 whenever the
    -- invariant violates again on a subsequent tick.
    clean_ticks  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_capture_alarms_invariant_active
    ON capture_alarms (invariant_id, acked_at)
    WHERE acked_at IS NULL AND resolved_at IS NULL;
```

### Rate Limiting

Before INSERT, query for recent un-acked alarm for same invariant:

```sql
SELECT id FROM capture_alarms
WHERE invariant_id = :id
  AND acked_at IS NULL
  AND resolved_at IS NULL
  AND raised_at > :cutoff_ms
LIMIT 1;
```

The `resolved_at IS NULL` clause means an auto-resolved alarm stops blocking new raises — if the invariant flaps back, the next tick raises a fresh row instead of being silently suppressed.

where `cutoff_ms = now_ms - (alarm_rate_limit_minutes * 60 * 1000)`. Default `15`.

If found: skip INSERT, still log structured line (below).

If not found: INSERT with `details_json` containing `source`, `since_ms`, invariant-specific context (e.g., `consecutive_failures`, `events_last_1h`, `expected_min_per_hour`).

### Structured Log Line

Regardless of rate-limit, append one JSON line to `[watchdog] log_path`:

```json
{"ts":1745200000000,"level":"error","invariant":"I-3","source":"shell","since_ms":7200000,"details":{"consecutive_failures":4}}
```

Default path: `~/.local/share/hippo/watchdog-alarms.log`. Suitable for `tail -f` and OTel Loki ingestion.

### macOS Notification (Optional)

When `[watchdog] notify_macos = true`, additionally:

```bash
osascript -e 'display notification "I-3 violated: shell silent 2h" with title "Hippo Watchdog"'
```

Fires only when a new alarm row is inserted (rate-limit passed). Default: `notify_macos = false`.

## Rate Limiting — Full Semantics

Each invariant rate-limited independently. Sliding window anchored to now, not fixed hour bucket. An alarm raised at 09:00 suppresses re-raises until 09:15 with a 15-min window regardless of hour boundaries.

## Ack Flow

**`hippo alarms list`** — Query `WHERE acked_at IS NULL`, print:

```
ID   INVARIANT  RAISED                  DETAILS
42   I-3        2026-04-21 09:14 UTC    shell silent 2h 0m (4 consecutive failures)
43   I-7        2026-04-21 09:14 UTC    watchdog heartbeat stale by 3m
```

Exit 0 if none active, exit 1 if any (script-friendly).

**`hippo alarms ack <id> [--note <text>]`**:

```sql
UPDATE capture_alarms
SET acked_at = :now_ms, ack_note = :note
WHERE id = :id AND acked_at IS NULL;
```

**Rate-limit reset after ack.** Rate-limit query filters `acked_at IS NULL`, so acked rows no longer block. Next cycle detecting the same violation inserts a fresh alarm. Intentional: acking means "I saw this," not "I fixed it."

### Upgrading from v10

Schema bump v10 → v11 is purely additive: two new columns on `capture_alarms` (`resolved_at`, `clean_ticks`) and a tightened partial-index predicate. Migration is idempotent and crash-safe (each ALTER pre-checks `pragma_table_info`).

Two operational notes for the first install with auto-resolve:

1. **Expect a one-time auto-resolve wave.** Every existing un-acked alarm enters v11 with `clean_ticks = 0` and `resolved_at = NULL`. After two consecutive watchdog ticks where the underlying invariant is healthy (~120 s), the row transitions to AUTO-RESOLVED. A long-standing pile of stale alarms from a past outage will all clear together. Run `hippo alarms prune` once after the wave to ack them en-masse.
2. **Rollback to a v10 binary hard-fails.** A v10 daemon opening a v11 DB hits `DB schema version mismatch: expected 10, found 11.` and refuses to start (same behavior as every prior schema bump). The added columns are read-compatible — brain accepts both v10 and v11 — but the daemon's strict mismatch guard means rolling back requires either restoring a v10 DB snapshot or manually `PRAGMA user_version = 10`.

### `hippo alarms prune`

Bulk-ack every auto-resolved row in one statement:

```sql
UPDATE capture_alarms
SET acked_at = :now_ms, ack_note = 'auto-resolved'
WHERE acked_at IS NULL AND resolved_at IS NOT NULL;
```

Use this when `hippo alarms list` shows a long AUTO-RESOLVED section from a past outage and you don't want to ack each row individually. `hippo doctor` also prints a `[--] auto-resolved alarms: N pending` line when any are present, so you can spot the cleanup opportunity without listing.

## Failure Modes for the Watchdog Itself

**Cannot open DB:** `eprintln!` the error, `exit(1)`. launchd records non-zero in `launchctl print gui/$(id -u)/com.hippo.watchdog`. Heartbeat staleness then visible in doctor.

**DB locked on alarm INSERT:** `busy_timeout=5000` handles up to 5 s (`storage.rs:24–27`). On `SQLITE_BUSY`, retry once after 100 ms. If retry fails: log to stderr, continue to next invariant (don't block). Heartbeat uses same retry; failure there → `exit(1)`.

**Crash mid-cycle:** Heartbeat (step 1) commits before assertions. Doctor distinguishes "started but crashed" from "never ran" via `last_success_ts` (updated only at end of step 4).

## Configuration

Add to `config/config.default.toml` and `crates/hippo-core/src/config.rs`:

```toml
[watchdog]
enabled                  = true
alarm_rate_limit_minutes = 15
notify_macos             = false
log_path                 = ""      # default: $data_dir/watchdog-alarms.log
osascript_title          = "Hippo Watchdog"
```

Add `WatchdogConfig` struct following existing `BrainConfig` / `DaemonConfig` pattern.

## Decoupling from Enrichment (I-10)

Watchdog reads only `source_health`, writes only `capture_alarms`. Does NOT connect to brain HTTP port, does NOT import Python, does NOT require LM Studio. I-7 (watchdog liveness) is evaluated from the row the watchdog writes itself. Capture-path invariants I-1..I-6, I-8 asserted from rows the daemon and extension update independently.

## Boot Sequence / First-Run

Before step 1, check:

```sql
SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='source_health';
```

If absent: create `source_health` from canonical DDL (`01-source-health.md`), seed watchdog row, exit clean — no alarms.

If present but no watchdog row: `INSERT OR IGNORE INTO source_health (source, updated_at) VALUES ('watchdog', :now_ms)`.

For any source with `last_event_ts IS NULL` (never captured an event), skip invariant assertions. Prevents alarm-storm on fresh install before first shell/session/visit.

## Cross-References

- Invariant definitions: `02-invariants.md`.
- Doctor integration: `03-doctor-upgrades.md` check 8.
- `source_health` schema: `01-source-health.md`.
- Alarm sink / probe integration: `05-synthetic-probes.md` (probe failures → I-8 → alarms).
