<!-- TL;DR: Defines the `source_health` table (the SQL ground truth for capture health), the migration from schema v7 to v8, exactly which functions write to it per source, the rolling-count recompute job, error-path behavior, and the literal SELECT queries that doctor and watchdog consume. -->

# Source Health Table (`source_health`)

> **Status: shipped.** Table created in v8 (T-0.1 / PR #67); write paths added in T-0.2 / PR #68. This doc is the live reference for the schema and the per-source write contracts.

## Purpose

`source_health` is the single authoritative record of whether each capture source is delivering events. It stores the timestamp of the last successful event landing, the most recent error, a rolling event count, and probe results for sources that have synthetic canary probes. Every capture path writes to this table in the same transaction as the event insert — there is no separate heartbeat mechanism, and there is no way for a source to succeed silently without updating its row. Downstream consumers (doctor, watchdog, dashboards) query this table with one-line SELECTs; they do not scrape logs or inspect process state.

## Table Schema

```sql
CREATE TABLE IF NOT EXISTS source_health (
    source                 TEXT PRIMARY KEY,
    last_event_ts          INTEGER,          -- epoch ms of the most recent successfully landed event
    last_success_ts        INTEGER,          -- epoch ms of the most recent successful flush/ingest call
    last_error_ts          INTEGER,          -- epoch ms of the most recent flush/ingest error
    last_error_msg         TEXT,             -- human-readable error string from the most recent failure
    consecutive_failures   INTEGER NOT NULL DEFAULT 0,  -- resets to 0 on any success
    events_last_1h         INTEGER NOT NULL DEFAULT 0,  -- hot-path-incremented approximation; recomputed every 5 min
    events_last_24h        INTEGER NOT NULL DEFAULT 0,  -- same; recomputed every 5 min
    expected_min_per_hour  INTEGER,          -- NULL = no threshold enforced; see rationale below
    probe_ok               INTEGER,          -- NULL if no probe exists for this source; 1=pass 0=fail
    probe_lag_ms           INTEGER,          -- round-trip ms of last probe; NULL if no probe
    probe_last_run_ts      INTEGER,          -- epoch ms of last `hippo probe` execution; NULL if never
    last_heartbeat_ts      INTEGER,          -- epoch ms of last extension heartbeat (browser only)
    updated_at             INTEGER NOT NULL  -- epoch ms of last write to this row
);
```

**Column semantics:**

| Column | Semantics |
|---|---|
| `source` | Enum literal identifying the capture source. One of: `'shell'`, `'claude-tool'`, `'claude-session'`, `'browser'`, `'watchdog'`, `'probe'`. No free-form values. |
| `last_event_ts` | Epoch ms of the most recently *successfully inserted* event for this source. Updated only on success; never touched by the error path. |
| `last_success_ts` | Epoch ms of the most recently completed flush or ingest call that did not produce an error, even if it processed zero events (idle tick). Distinct from `last_event_ts` to allow distinguishing "flush ran but source was quiet" from "flush never ran." |
| `last_error_ts` | Epoch ms when the last error was recorded. NULL if no error has ever occurred. |
| `last_error_msg` | The `.to_string()` of the most recent `anyhow::Error` from a flush or ingest failure. Truncated to 512 characters. |
| `consecutive_failures` | Count of consecutive flush/ingest calls that ended in error. Reset to 0 on any call that does not produce an error. |
| `events_last_1h` | Approximate count of events in the last 60 minutes. Incremented in the hot path; recomputed to exact values by the rolling-count job every 5 minutes. |
| `events_last_24h` | Same as above for the last 24 hours. |
| `expected_min_per_hour` | Configurable lower bound for `events_last_1h`. NULL means "no threshold" — see rationale. |
| `probe_ok` | 1 if the most recent synthetic canary probe succeeded, 0 if it failed, NULL if no probe is defined for this source. Managed by the probe system (see `05-synthetic-probes.md`). |
| `probe_lag_ms` | Round-trip latency of the most recent probe in milliseconds. NULL if no probe. |
| `probe_last_run_ts` | Epoch ms of the most recent `hippo probe` execution for this source. NULL if never run. Used by invariant I-8 (probe freshness). |
| `last_heartbeat_ts` | Epoch ms of the most recent Firefox extension heartbeat (only set on `source='browser'` row; NULL elsewhere). Used by I-4 (browser round-trip) to determine whether the extension is actively loaded in Firefox. |
| `updated_at` | Epoch ms of the most recent write to this row by any path (success, error, or probe update). |

**Rationale for nullable `expected_min_per_hour`:**

`browser` and `claude-session` legitimately go silent for long periods — the user may close all browser tabs, stop writing code, or go to sleep. Enforcing a minimum event rate for these sources on a quiet 3am machine would produce constant false alarms. `expected_min_per_hour = NULL` means "staleness is determined by `last_event_ts` age alone, not by rate." Sources like `shell` on an actively used machine may choose to set a non-null value (e.g., 1 event/hour during work hours), but the threshold is configurable per installation via `config.toml` and never hardcoded.

**Allowed `source` values:**

- `'shell'` — zsh hook events (`events.source_kind = 'shell'`)
- `'claude-tool'` — Claude Code tool call events (`events.source_kind = 'claude-tool'`)
- `'claude-session'` — Claude Code session segments (`claude_sessions`)
- `'browser'` — Firefox extension events (`browser_events`)
- `'watchdog'` — reserved for watchdog self-heartbeat (see `04-watchdog.md`)
- `'probe'` — reserved for probe subsystem metadata (see `05-synthetic-probes.md`)
- `'claude-session-watcher'` — process-health heartbeat for the FS watcher (`crates/hippo-daemon/src/watch_claude_sessions.rs`); distinct from `'claude-session'` which is data-path health

### Brain-only sources

The brain also emits enrichment metrics with `source="workflow"` and `source="codex"` labels. These are **not** first-class `source_health` rows because their ingestion path does not flow through the daemon socket:

- **`workflow`** — GitHub Actions workflow runs polled directly by the brain (`hippo gh-poll`), stored in `workflow_runs`.
- **`codex`** — GitHub Copilot (Codex) session logs ingested via the same Claude-session code path with a `source_kind` marker; they share the `'claude-session'` row in `source_health`.

Reliability of these sources is observed through brain-side metrics (`hippo.brain.enrichment.*{source=...}`) and the underlying table freshness queries in `check_source_freshness`, not through `source_health` heartbeats.

## Migration (v7 → v8)

The current schema version is `7`, defined at `crates/hippo-core/src/storage.rs:16` as `pub const EXPECTED_VERSION: i64 = 7`. The `source_health` table is added in v8.

The migration must be added to the `open_db` function in `crates/hippo-core/src/storage.rs` as a new `if (1..=7).contains(&version)` block, and `EXPECTED_VERSION` must be bumped to `8`. The migration script:

```sql
-- v7 → v8: capture reliability — source_health table
CREATE TABLE IF NOT EXISTS source_health (
    source                 TEXT PRIMARY KEY,
    last_event_ts          INTEGER,
    last_success_ts        INTEGER,
    last_error_ts          INTEGER,
    last_error_msg         TEXT,
    consecutive_failures   INTEGER NOT NULL DEFAULT 0,
    events_last_1h         INTEGER NOT NULL DEFAULT 0,
    events_last_24h        INTEGER NOT NULL DEFAULT 0,
    expected_min_per_hour  INTEGER,
    probe_ok               INTEGER,
    probe_lag_ms           INTEGER,
    probe_last_run_ts      INTEGER,
    last_heartbeat_ts      INTEGER,
    updated_at             INTEGER NOT NULL
);

-- Additional column migrations for probe_tag exclusion (see 05-synthetic-probes.md):
ALTER TABLE events           ADD COLUMN probe_tag TEXT;
ALTER TABLE claude_sessions  ADD COLUMN probe_tag TEXT;
ALTER TABLE browser_events   ADD COLUMN probe_tag TEXT;

-- Pre-seed one row per known source so queries never return empty.
-- last_event_ts is back-filled from existing data (see Back-fill Behavior below).
-- On a fresh install all tables are empty so the MAX() returns NULL — that is correct.
INSERT OR IGNORE INTO source_health (source, last_event_ts, updated_at)
VALUES
    ('shell',
     (SELECT MAX(timestamp) FROM events WHERE source_kind = 'shell'),
     unixepoch('now') * 1000),
    ('claude-tool',
     (SELECT MAX(timestamp) FROM events WHERE source_kind = 'claude-tool'),
     unixepoch('now') * 1000),
    ('claude-session',
     (SELECT MAX(start_time) FROM claude_sessions),
     unixepoch('now') * 1000),
    ('browser',
     (SELECT MAX(timestamp) FROM browser_events),
     unixepoch('now') * 1000);

PRAGMA user_version = 8;
```

The `INSERT OR IGNORE` is idempotent — safe to run twice on the same database. The `EXPECTED_VERSION` constant at `crates/hippo-core/src/storage.rs:16` must be changed from `7` to `8`, and the `PRAGMA user_version` at the bottom of `crates/hippo-core/src/schema.sql:438` must be changed from `7` to `8` (the schema.sql is used only for fresh installs via the `version == 0` path). The brain's `brain/src/hippo_brain/schema_version.py::EXPECTED_SCHEMA_VERSION` must also be bumped — the brain enforces its own version check on startup.

## Write Paths

Every capture path is responsible for updating its own `source_health` row. The update executes in the same transaction as the event insert. No source is allowed to succeed silently.

### `shell` and `claude-tool` (via `flush_events`)

**File:** `crates/hippo-daemon/src/daemon.rs`, function `flush_events` (line 210).

At the end of each batch — after all `Shell` and `Browser` payloads in the batch have been processed — `flush_events` executes one `source_health` update per source kind that appeared in the batch. The update is:

```sql
UPDATE source_health
SET last_event_ts         = ?,   -- timestamp of the most recent event of this source_kind in the batch
    last_success_ts       = ?,   -- current epoch ms
    events_last_1h        = events_last_1h  + ?,  -- count of this source_kind in the batch
    events_last_24h       = events_last_24h + ?,  -- same
    consecutive_failures  = 0,
    updated_at            = ?    -- current epoch ms
WHERE source = ?;                -- 'shell' or 'claude-tool'
```

This update runs in the same `rusqlite::Connection` write transaction that committed the batch. If the transaction rolls back, the `source_health` update also rolls back — there is no partially-updated health row.

The `Browser` payload in `flush_events` (line 315) updates `source = 'browser'` in the same manner.

### `claude-session` (via FS watcher / batch import)

**File:** The FS watcher (`crates/hippo-daemon/src/watch_claude_sessions.rs`, since T-5/PR #86) and the batch importer (`hippo ingest claude-session <path>`) both write to `claude_sessions` via `claude_session::insert_segments` and update `source_health` with `source = 'claude-session'` after each successful insert. The legacy per-session tmux tailer was removed in T-8/PR #89.

The update pattern is identical to the shell path, with `last_event_ts` set to the `start_time` of the ingested session segment.

### `browser` (via `flush_events`)

**File:** `crates/hippo-daemon/src/daemon.rs`, `flush_events`, line 315.

Note: `native_messaging.rs` does NOT write directly to SQLite. It forwards events to the daemon via Unix socket (`send_event_fire_and_forget`), which then lands in the event buffer and is flushed by `flush_events`. The `source_health` update for `browser` therefore occurs in `flush_events`, not in `native_messaging.rs`. This is the correct location — updating health in `native_messaging.rs` would reflect "extension delivered to daemon" rather than "event landed in SQLite."

### Unified Update Statement

All paths use this template — parameterized by `(latest_event_ts_in_batch, now_ms, batch_count, batch_count, now_ms, source_name)`:

```sql
UPDATE source_health
SET last_event_ts        = MAX(COALESCE(last_event_ts, 0), ?1),
    last_success_ts      = ?2,
    events_last_1h       = events_last_1h  + ?3,
    events_last_24h      = events_last_24h + ?4,
    consecutive_failures = 0,
    updated_at           = ?5
WHERE source = ?6;
```

Using `MAX(COALESCE(last_event_ts, 0), ?1)` rather than an unconditional assignment ensures that a slow-arriving batch with old timestamps does not overwrite a newer `last_event_ts` set by a concurrent flush. This is a safe monotonic update.

## Rolling-Count Recompute Job

The hot-path increments (`events_last_1h + batch_count`) are fast approximations. They do not account for the window sliding forward, and they accumulate across restarts. A background tokio interval task in the daemon recomputes exact values every 5 minutes.

**Location:** A new `recompute_rolling_counts` async fn, called from a `tokio::spawn` interval loop in `daemon.rs:run()`, alongside the existing flush task.

**Logic (pseudo-SQL for each source):**

```sql
-- shell
UPDATE source_health
SET events_last_1h  = (SELECT COUNT(*) FROM events
                        WHERE source_kind = 'shell'
                          AND timestamp   > (unixepoch('now') - 3600) * 1000),
    events_last_24h = (SELECT COUNT(*) FROM events
                        WHERE source_kind = 'shell'
                          AND timestamp   > (unixepoch('now') - 86400) * 1000),
    updated_at      = unixepoch('now') * 1000
WHERE source = 'shell';

-- claude-tool: same, WHERE source_kind = 'claude-tool'
-- claude-session: COUNT(*) FROM claude_sessions WHERE start_time > window
-- browser: COUNT(*) FROM browser_events WHERE timestamp > window
```

The recompute job uses the `read_db` connection (non-blocking for the write path) and writes the results via `write_db`. It holds `write_db` only for the duration of the four UPDATE statements — no long locks.

## Error Path

When a flush call fails (e.g., `storage::insert_event_at` returns `Err`), the existing code falls back to `write_fallback_jsonl` and increments `drop_count`. After that fallback logic, the error path also updates `source_health`:

```sql
UPDATE source_health
SET last_error_ts       = ?,      -- current epoch ms
    last_error_msg      = ?,      -- error.to_string(), truncated to 512 chars
    consecutive_failures = consecutive_failures + 1,
    updated_at          = ?
WHERE source = ?;
```

Critically, `last_event_ts` and `last_success_ts` are NOT touched on the error path. This preserves the semantics: `last_event_ts` is the timestamp of the last event that *actually landed in SQLite*, not the last event that was attempted. A rising `consecutive_failures` count with a stale `last_event_ts` is the signal that something is wrong.

## Read Queries

These are the literal SELECTs that `hippo doctor`, the watchdog, and dashboards run. They must work without joins.

```sql
-- All sources with their staleness age (used by doctor and watchdog)
SELECT
    source,
    last_event_ts,
    last_success_ts,
    consecutive_failures,
    events_last_1h,
    probe_ok,
    probe_lag_ms,
    (strftime('%s', 'now') * 1000 - last_event_ts) AS age_ms,
    updated_at
FROM source_health
ORDER BY age_ms DESC NULLS FIRST;

-- Sources in degraded state (consecutive failures threshold)
SELECT source, consecutive_failures, last_error_ts, last_error_msg
FROM source_health
WHERE consecutive_failures > 3;

-- Sources that have been silent longer than N ms (watchdog alarm query)
-- Parameterized: ?1 = threshold_ms (e.g., 3600000 for 1 hour)
SELECT source, last_event_ts, (strftime('%s', 'now') * 1000 - last_event_ts) AS age_ms
FROM source_health
WHERE last_event_ts IS NOT NULL
  AND (strftime('%s', 'now') * 1000 - last_event_ts) > ?1;

-- Sources that have never been seen (last_event_ts IS NULL, not just stale)
SELECT source
FROM source_health
WHERE last_event_ts IS NULL;

-- Per-source event rate check (against configured threshold)
SELECT source, events_last_1h, expected_min_per_hour
FROM source_health
WHERE expected_min_per_hour IS NOT NULL
  AND events_last_1h < expected_min_per_hour;
```

`doctor` formats these results into human-readable lines. `watchdog` uses the staleness query to trigger alarms. The invariant definitions (thresholds, severity) are in `02-invariants.md`.

## Interaction with OTel

`source_health` is the SQL ground truth. OTel counters in `crates/hippo-daemon/src/metrics.rs` — specifically `EVENTS_INGESTED`, `FLUSH_EVENTS`, and `FLUSH_BATCH_SIZE` — currently have no `source` attribute tag. Adding `source` as a `KeyValue` attribute to these counters is scoped to the P0 OTel tagging PR (see `07-roadmap.md`). The two systems are intentionally redundant: SQLite is queryable offline and survives OTel collector outages; OTel provides time-series visualization. Neither is the single source of truth for alerting — `source_health` wins for correctness because it is transactionally co-located with the event insert.

## Back-fill Behavior

When `open_db` runs the v7→v8 migration on an existing installation, the `INSERT OR IGNORE` pre-seeds each row with `last_event_ts` derived from `MAX(timestamp)` of the underlying table. This prevents a false "all sources have never been seen!" alarm immediately after upgrade. The logic:

- `shell`: `MAX(timestamp) FROM events WHERE source_kind = 'shell'`
- `claude-tool`: `MAX(timestamp) FROM events WHERE source_kind = 'claude-tool'`
- `claude-session`: `MAX(start_time) FROM claude_sessions`
- `browser`: `MAX(timestamp) FROM browser_events`

If a table is empty (fresh install), `MAX()` returns `NULL`, which is the correct initial state — that source has genuinely never delivered an event. On a fresh install, `hippo doctor` will show all sources as "never seen" until they each deliver their first event, which is accurate.

The back-fill is a one-time operation in the migration block. After migration, the hot-path write in `flush_events` maintains these values continuously. The 5-minute recompute job will correct `events_last_1h` and `events_last_24h` to accurate values within 5 minutes of upgrade.

## Cross-Section Concerns (from D1 agent)

- **Browser path architecture:** `native_messaging.rs` does NOT write directly to SQLite; it forwards to the daemon via Unix socket. The browser `source_health` update belongs in `flush_events`, not `native_messaging.rs`.
- **Schema version:** Current is v7. The source_health migration is v8. Bump must update three places: `crates/hippo-core/src/storage.rs:16` (`EXPECTED_VERSION`), `crates/hippo-core/src/schema.sql:438` (`PRAGMA user_version`), and `brain/src/hippo_brain/schema_version.py::EXPECTED_SCHEMA_VERSION`.
- **`events.source_kind`:** Added in v7. Per-source aggregation for shell/claude-tool must filter by `source_kind`, not by a separate table.
