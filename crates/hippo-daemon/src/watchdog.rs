//! Capture-reliability watchdog — `hippo watchdog run`
//!
//! Short-lived process invoked every 60 s by launchd (`com.hippo.watchdog`).
//! Asserts invariants I-1..I-10 against the `source_health` table and writes
//! rows to `capture_alarms` for any violations detected.  Rate-limited per
//! invariant per sliding window (default 60 min).
//!
//! Five-step flow (per `docs/capture/architecture.md`):
//!   1. Upsert own heartbeat into `source_health WHERE source='watchdog'`
//!   2. Read full `source_health` in one `SELECT *`
//!   3. Assert I-1..I-10 against in-memory rows
//!   4. Insert `capture_alarms` rows for violations (rate-limited)
//!   5. Update `last_success_ts` on watchdog row; return `Ok(())`
//!
//! Writes a structured JSON line to `watchdog-alarms.log` for every violation
//! (rate-limited or not) so `tail -f` and OTel Loki ingestion work regardless
//! of whether the alarm was suppressed.

use anyhow::Result;
use hippo_core::config::HippoConfig;
use rusqlite::Connection;
use serde_json::json;
use std::collections::HashSet;
use std::io::Write;
use std::path::Path;
use tracing::{error, info, warn};

/// Number of consecutive clean watchdog ticks required to auto-resolve an
/// active alarm. Set to 2 so a single transient recovery doesn't clear an
/// alarm that's about to flap back.
const AUTO_RESOLVE_CLEAN_TICKS: i64 = 2;

// DDL used by the pre-migration safety path only.  The authoritative definition
// lives in `schema.sql` and `storage.rs`; this is a fallback for the case where
// the watchdog runs on a database that hasn't been migrated yet.
const SOURCE_HEALTH_FALLBACK_DDL: &str = "
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
";

// ---------------------------------------------------------------------------
// Public data types
// ---------------------------------------------------------------------------

/// One row from `source_health`, loaded into memory for invariant checking.
/// All nullable integer columns map to `Option<i64>`.
#[derive(Debug, Clone)]
pub struct SourceHealthRow {
    pub source: String,
    pub last_event_ts: Option<i64>,
    pub last_success_ts: Option<i64>,
    pub last_error_ts: Option<i64>,
    pub last_error_msg: Option<String>,
    pub consecutive_failures: i64,
    pub events_last_1h: i64,
    pub events_last_24h: i64,
    pub expected_min_per_hour: Option<i64>,
    pub probe_ok: Option<i64>,
    pub probe_lag_ms: Option<i64>,
    pub probe_last_run_ts: Option<i64>,
    pub last_heartbeat_ts: Option<i64>,
    pub updated_at: i64,
}

/// A detected invariant violation, ready to be inserted into `capture_alarms`.
#[derive(Debug)]
pub struct InvariantViolation {
    /// Short identifier matching the spec (e.g. `"I-1"`, `"I-4"`).
    pub invariant_id: String,
    /// The capture source that is violating the invariant.
    pub source: String,
    /// How long the source has been in the failing state, in milliseconds.
    pub since_ms: i64,
    /// Invariant-specific diagnostic context serialized as a JSON value.
    pub details: serde_json::Value,
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

/// Run one watchdog cycle.  Returns `Ok(())` on success; the caller is expected
/// to call `std::process::exit(0)` if desired (launchd treats any non-zero exit
/// as a failure).
pub fn run(config: &HippoConfig) -> Result<()> {
    // Feature-flag guard: watchdog is shipped disabled until the launchd plist
    // (T-2) is in place.  Any code path that calls run() should check this
    // flag, but we also guard here so run() is safe to call unconditionally.
    if !config.watchdog.enabled {
        info!("watchdog: disabled (watchdog.enabled = false); skipping cycle");
        return Ok(());
    }

    let db_path = config.db_path();

    // `open_db` handles all schema migrations, including v8→v9 that creates
    // `capture_alarms`. On a totally fresh install it seeds the DB from
    // `schema.sql` (which also contains `capture_alarms`).
    let conn = hippo_core::storage::open_db(&db_path)?;

    let now_ms = chrono::Utc::now().timestamp_millis();

    // ── Pre-migration safety ──────────────────────────────────────────────
    // `open_db` always runs migrations, so `source_health` should exist here.
    // This check is a belt-and-suspenders guard for hypothetical edge cases
    // (e.g. the watchdog binary is newer than the daemon binary and the DB
    // hasn't been touched by `open_db` yet via a different code path).
    if !source_health_table_exists(&conn)? {
        // Create and seed the table so subsequent invocations work correctly.
        conn.execute_batch(SOURCE_HEALTH_FALLBACK_DDL)?;
        conn.execute(
            "INSERT OR IGNORE INTO source_health (source, updated_at) VALUES ('watchdog', ?1)",
            rusqlite::params![now_ms],
        )?;
        info!("source_health absent on first run; created table and seeded watchdog row");
        // Exit clean — no alarms on a fresh install.
        return Ok(());
    }

    // Ensure the watchdog row exists (idempotent INSERT OR IGNORE).
    conn.execute(
        "INSERT OR IGNORE INTO source_health (source, updated_at) VALUES ('watchdog', ?1)",
        rusqlite::params![now_ms],
    )?;

    // ── Step 1: Write own heartbeat ───────────────────────────────────────
    // Only `updated_at` is set here.  `last_success_ts` is updated at the end
    // of step 4 so doctor can distinguish "started but crashed" (updated_at
    // recent, last_success_ts stale) from "never ran" (both NULL).
    conn.execute(
        "UPDATE source_health SET updated_at = ?1 WHERE source = 'watchdog'",
        rusqlite::params![now_ms],
    )?;

    // ── Step 2: Read all source_health rows ───────────────────────────────
    let rows = read_source_health(&conn)?;

    // ── Step 3: Assert invariants I-1..I-10 ──────────────────────────────
    let violations = check_invariants(&rows, now_ms);

    // ── Step 4: Insert capture_alarms rows for violations ─────────────────
    let log_path = resolve_log_path(config);
    let rate_limit_ms = config.watchdog.alarm_rate_limit_minutes as i64 * 60 * 1_000;

    for v in &violations {
        // Always write a structured log line (regardless of rate-limit).
        append_alarm_log(
            &log_path,
            now_ms,
            &v.invariant_id,
            &v.source,
            v.since_ms,
            &v.details,
        );

        let rate_limited = check_rate_limit(&conn, &v.invariant_id, now_ms, rate_limit_ms)?;
        if rate_limited {
            warn!(
                invariant = %v.invariant_id,
                source = %v.source,
                since_ms = v.since_ms,
                "watchdog: rate-limited — alarm already active within window"
            );
            continue;
        }

        // Rate limit not hit: insert a new alarm row.
        let details_json = json!({
            "source": v.source,
            "since_ms": v.since_ms,
            "details": v.details,
        })
        .to_string();

        // Insert the alarm row, with a single retry on SQLITE_BUSY before
        // giving up.  busy_timeout=5000 handles the common case; this retry
        // covers the rare window where a second BUSY fires after the first
        // timeout expires.  On persistent failure we log and continue so
        // the remaining invariants are still evaluated.
        let insert_result = conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json)
             VALUES (?1, ?2, ?3)",
            rusqlite::params![&v.invariant_id, now_ms, &details_json],
        );
        if let Err(e) = insert_result {
            if is_sqlite_busy(&e) {
                std::thread::sleep(std::time::Duration::from_millis(100));
                if let Err(retry_err) = conn.execute(
                    "INSERT INTO capture_alarms (invariant_id, raised_at, details_json)
                     VALUES (?1, ?2, ?3)",
                    rusqlite::params![&v.invariant_id, now_ms, &details_json],
                ) {
                    error!(
                        invariant = %v.invariant_id,
                        error = %retry_err,
                        "watchdog: alarm insert failed after SQLITE_BUSY retry; skipping"
                    );
                    continue;
                }
            } else {
                return Err(e.into());
            }
        }

        error!(
            invariant = %v.invariant_id,
            source = %v.source,
            since_ms = v.since_ms,
            "watchdog: new alarm raised"
        );

        #[cfg(feature = "otel")]
        {
            use opentelemetry::KeyValue;
            crate::metrics::WATCHDOG_ALARMS_FIRED
                .add(1, &[KeyValue::new("invariant_id", v.invariant_id.clone())]);
            crate::metrics::WATCHDOG_INVARIANT_VIOLATION
                .add(1, &[KeyValue::new("source", v.source.clone())]);
        }

        // Optional macOS notification (only on new alarm row).
        if config.watchdog.notify_macos {
            let message = format!(
                "{} violated: {} silent {}s",
                v.invariant_id,
                v.source,
                v.since_ms / 1_000
            );
            fire_macos_notification(&message, &config.watchdog.osascript_title);
        }
    }

    // ── Step 4b: Auto-resolve alarms whose invariant has stayed clean ─────
    // Build the (invariant_id, source) set of *currently-violated* pairs and
    // walk all active (un-acked, un-resolved) alarms. Each clean tick bumps
    // `clean_ticks`; on AUTO_RESOLVE_CLEAN_TICKS we set `resolved_at`.
    // Any alarm whose pair is currently violated has its counter reset.
    let resolved = auto_resolve_alarms(&conn, &violations, now_ms)?;
    if resolved > 0 {
        info!(count = resolved, "watchdog: auto-resolved alarms");
        #[cfg(feature = "otel")]
        crate::metrics::WATCHDOG_ALARMS_AUTO_RESOLVED.add(resolved as u64, &[]);
    }

    // Tick-end snapshot. Emitted as a structured info! line so the existing
    // OTel→Loki pipeline picks it up — no new metric-type infrastructure
    // needed. Operators can build a Grafana panel from these fields by
    // querying `{job="hippo"} | json | line=~"watchdog: tick complete"`.
    let (active_count, resolved_unacked_count) = count_alarm_states(&conn).unwrap_or_else(|e| {
        warn!(
            error = %e,
            "watchdog: count_alarm_states failed; emitting -1 sentinel"
        );
        (-1, -1)
    });
    info!(
        active = active_count,
        resolved_unacked = resolved_unacked_count,
        new_violations = violations.len(),
        auto_resolved = resolved,
        "watchdog: tick complete"
    );

    // ── Step 5: Mark cycle complete ───────────────────────────────────────
    conn.execute(
        "UPDATE source_health SET last_success_ts = ?1 WHERE source = 'watchdog'",
        rusqlite::params![now_ms],
    )?;

    #[cfg(feature = "otel")]
    crate::metrics::WATCHDOG_RUN.add(1, &[]);

    Ok(())
}

/// Returns `(active, resolved_unacked)` from `capture_alarms`. Errors are
/// handled by the caller; a `(−1, −1)` sentinel logged means "DB query
/// failed" and is preferable to crashing the whole tick over telemetry.
fn count_alarm_states(conn: &Connection) -> Result<(i64, i64)> {
    let active: i64 = conn.query_row(
        "SELECT COUNT(*) FROM capture_alarms
         WHERE acked_at IS NULL AND resolved_at IS NULL",
        [],
        |row| row.get(0),
    )?;
    let resolved: i64 = conn.query_row(
        "SELECT COUNT(*) FROM capture_alarms
         WHERE acked_at IS NULL AND resolved_at IS NOT NULL",
        [],
        |row| row.get(0),
    )?;
    Ok((active, resolved))
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

fn source_health_table_exists(conn: &Connection) -> Result<bool> {
    let count: i64 = conn.query_row(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='source_health'",
        [],
        |row| row.get(0),
    )?;
    Ok(count > 0)
}

fn resolve_log_path(config: &HippoConfig) -> std::path::PathBuf {
    if config.watchdog.log_path.is_empty() {
        config.storage.data_dir.join("watchdog-alarms.log")
    } else {
        std::path::PathBuf::from(&config.watchdog.log_path)
    }
}

/// Read all rows from `source_health` into memory.
pub fn read_source_health(conn: &Connection) -> Result<Vec<SourceHealthRow>> {
    let mut stmt = conn.prepare(
        "SELECT source, last_event_ts, last_success_ts, last_error_ts, last_error_msg,
                consecutive_failures, events_last_1h, events_last_24h, expected_min_per_hour,
                probe_ok, probe_lag_ms, probe_last_run_ts, last_heartbeat_ts, updated_at
         FROM source_health",
    )?;

    let rows = stmt.query_map([], |row| {
        Ok(SourceHealthRow {
            source: row.get(0)?,
            last_event_ts: row.get(1)?,
            last_success_ts: row.get(2)?,
            last_error_ts: row.get(3)?,
            last_error_msg: row.get(4)?,
            consecutive_failures: row.get(5)?,
            events_last_1h: row.get(6)?,
            events_last_24h: row.get(7)?,
            expected_min_per_hour: row.get(8)?,
            probe_ok: row.get(9)?,
            probe_lag_ms: row.get(10)?,
            probe_last_run_ts: row.get(11)?,
            last_heartbeat_ts: row.get(12)?,
            updated_at: row.get(13)?,
        })
    })?;

    rows.collect::<rusqlite::Result<Vec<_>>>()
        .map_err(Into::into)
}

// ---------------------------------------------------------------------------
// Invariant evaluation
// ---------------------------------------------------------------------------

/// Evaluate I-1..I-10 against the in-memory `source_health` rows.
///
/// Returns one `InvariantViolation` per triggered invariant.
/// Invariants that require filesystem access (I-2 proxy, I-9) or are
/// architectural (I-5, I-10) or doctor-only (I-7) either return a proxy
/// violation or `None`; their full implementations land in later tasks.
pub fn check_invariants(rows: &[SourceHealthRow], now_ms: i64) -> Vec<InvariantViolation> {
    let by_source: std::collections::HashMap<&str, &SourceHealthRow> =
        rows.iter().map(|r| (r.source.as_str(), r)).collect();

    let mut violations = Vec::new();

    // I-1: Shell liveness (>60 s stale while probe says active)
    if let Some(v) = check_i1_shell_liveness(&by_source, now_ms) {
        violations.push(v);
    }

    // I-2: Claude-session coverage proxy (consecutive_failures > 3)
    // Full JSONL-based predicate lands in T-4 (doctor checks).
    if let Some(v) = check_i2_claude_session_proxy(&by_source, now_ms) {
        violations.push(v);
    }

    // I-3: Claude-tool concurrency — structured log only per spec; no alarm row.
    // Omitted from T-1; activated in future when probe data is available.

    // I-4: Browser round-trip (>2 min stale while probe says active)
    if let Some(v) = check_i4_browser_roundtrip(&by_source, now_ms) {
        violations.push(v);
    }

    // I-5: Drop visibility — architectural / OTel counter; no source_health proxy.
    // I-6: Buffer saturation — requires drop-rate counters; not in source_health.
    // I-7: Watchdog heartbeat — doctor-only check; watchdog cannot alarm about itself.
    // I-9: Fallback file age — requires filesystem stat; implemented in T-4.
    // I-10: Decoupling — architectural enforcement via CI test; not a runtime alarm.

    // I-8: Probe freshness (> 15 min stale OR probe_ok = 0)
    violations.extend(check_i8_probe_freshness(rows, now_ms));

    violations
}

/// I-1: Shell liveness.
/// Fires when `shell.last_event_ts` is more than 60 s old **and**
/// `shell.probe_ok = 1` (shell active: zsh running, hippo.zsh sourced,
/// user not idle).  Sources with `last_event_ts IS NULL` (never seen) are
/// skipped — a fresh install should not alarm before the first shell event.
pub fn check_i1_shell_liveness(
    by_source: &std::collections::HashMap<&str, &SourceHealthRow>,
    now_ms: i64,
) -> Option<InvariantViolation> {
    let row = by_source.get("shell")?;

    // Skip if the source has never delivered an event.
    let last_event = row.last_event_ts?;

    // Suppress when probe says the shell is not active.
    if row.probe_ok != Some(1) {
        return None;
    }

    let age_ms = now_ms - last_event;
    if age_ms > 60_000 {
        Some(InvariantViolation {
            invariant_id: "I-1".to_string(),
            source: "shell".to_string(),
            since_ms: age_ms,
            details: json!({
                "consecutive_failures": row.consecutive_failures,
                "events_last_1h": row.events_last_1h,
            }),
        })
    } else {
        None
    }
}

/// I-2 proxy: Claude-session coverage.
/// Full predicate (iterate JSONL files, cross-check with DB) belongs in T-4
/// (doctor check 5).  Here we use `consecutive_failures > 3` as a proxy
/// signal that the claude-session ingest path is actively broken.
pub fn check_i2_claude_session_proxy(
    by_source: &std::collections::HashMap<&str, &SourceHealthRow>,
    now_ms: i64,
) -> Option<InvariantViolation> {
    let row = by_source.get("claude-session")?;

    // Skip if the source has never delivered an event.
    let last_event = row.last_event_ts?;

    if row.consecutive_failures > 3 {
        let age_ms = now_ms - last_event;
        return Some(InvariantViolation {
            invariant_id: "I-2".to_string(),
            source: "claude-session".to_string(),
            since_ms: age_ms,
            details: json!({
                "consecutive_failures": row.consecutive_failures,
                "note": "proxy predicate; full JSONL check in T-4",
            }),
        });
    }

    None
}

/// I-4: Browser round-trip.
/// Fires when `browser.last_event_ts` is more than 2 min old **and**
/// `browser.probe_ok = 1` (Firefox running + extension heartbeat fresh).
pub fn check_i4_browser_roundtrip(
    by_source: &std::collections::HashMap<&str, &SourceHealthRow>,
    now_ms: i64,
) -> Option<InvariantViolation> {
    let row = by_source.get("browser")?;

    // Skip if the source has never delivered an event.
    let last_event = row.last_event_ts?;

    // probe_ok = 1 encodes (Firefox running) AND (heartbeat fresh < 2 min).
    if row.probe_ok != Some(1) {
        return None;
    }

    let age_ms = now_ms - last_event;
    if age_ms > 120_000 {
        Some(InvariantViolation {
            invariant_id: "I-4".to_string(),
            source: "browser".to_string(),
            since_ms: age_ms,
            details: json!({
                "last_heartbeat_ts": row.last_heartbeat_ts,
                "consecutive_failures": row.consecutive_failures,
            }),
        })
    } else {
        None
    }
}

/// I-8: Probe freshness.
///
/// For each source where `probe_last_run_ts IS NOT NULL`:
///   - alarm if `probe_ok = 0` (probe ran and failed), OR
///   - alarm if `probe_last_run_ts < now - 15 min` (probe hasn't run recently).
///
/// Yields one violation per affected source.
pub fn check_i8_probe_freshness(rows: &[SourceHealthRow], now_ms: i64) -> Vec<InvariantViolation> {
    rows.iter()
        .filter_map(|row| {
            let probe_run = row.probe_last_run_ts?; // skip sources with no probe

            let age_ms = now_ms - probe_run;
            let is_stale = age_ms > 900_000; // > 15 min
            let is_failing = row.probe_ok == Some(0);

            if is_stale || is_failing {
                Some(InvariantViolation {
                    invariant_id: "I-8".to_string(),
                    source: row.source.clone(),
                    since_ms: age_ms,
                    details: json!({
                        "probe_ok": row.probe_ok,
                        "probe_lag_ms": row.probe_lag_ms,
                        "probe_last_run_ts": probe_run,
                        "stale": is_stale,
                        "failing": is_failing,
                    }),
                })
            } else {
                None
            }
        })
        .collect()
}

// ---------------------------------------------------------------------------
// Rate limiting
// ---------------------------------------------------------------------------

/// Returns `true` if an un-acked alarm for `invariant_id` was raised within
/// the `rate_limit_ms` sliding window ending at `now_ms`.
///
/// When rate-limited the caller MUST still write the structured log line but
/// MUST NOT insert a new `capture_alarms` row.
pub fn check_rate_limit(
    conn: &Connection,
    invariant_id: &str,
    now_ms: i64,
    rate_limit_ms: i64,
) -> Result<bool> {
    let cutoff_ms = now_ms - rate_limit_ms;
    // Use EXISTS so the query short-circuits on the first matching row instead
    // of scanning the full index and counting.
    let exists: bool = conn.query_row(
        "SELECT EXISTS(
             SELECT 1 FROM capture_alarms
             WHERE invariant_id = ?1
               AND acked_at IS NULL
               AND resolved_at IS NULL
               AND raised_at > ?2
             LIMIT 1
         )",
        rusqlite::params![invariant_id, cutoff_ms],
        |row| row.get(0),
    )?;
    Ok(exists)
}

// ---------------------------------------------------------------------------
// Auto-resolution
// ---------------------------------------------------------------------------

/// Walk all active (un-acked, un-resolved) alarms and either reset their
/// clean-tick counter (still violating) or increment it (currently clean).
/// When the counter reaches `AUTO_RESOLVE_CLEAN_TICKS`, set `resolved_at` so
/// the alarm stops contributing to the doctor exit code and stops suppressing
/// new raises via rate-limit.
///
/// `current_violations` is the live set produced by `check_invariants` for
/// this tick. Membership is keyed by `(invariant_id, source)` because a
/// single invariant (e.g. I-8 probe freshness) can apply to multiple sources
/// independently.
///
/// Returns the count of alarms that transitioned to resolved on this tick.
pub fn auto_resolve_alarms(
    conn: &Connection,
    current_violations: &[InvariantViolation],
    now_ms: i64,
) -> Result<usize> {
    let violating: HashSet<(String, String)> = current_violations
        .iter()
        .map(|v| (v.invariant_id.clone(), v.source.clone()))
        .collect();

    struct Row {
        id: i64,
        invariant_id: String,
        source: Option<String>,
        clean_ticks: i64,
    }

    // Scope `stmt` so its borrow of `conn` is released before we open the
    // transaction below.
    let rows: Vec<Row> = {
        let mut stmt = conn.prepare(
            "SELECT id, invariant_id, details_json, clean_ticks
             FROM capture_alarms
             WHERE acked_at IS NULL AND resolved_at IS NULL",
        )?;
        stmt.query_map([], |r| {
            let id: i64 = r.get(0)?;
            let invariant_id: String = r.get(1)?;
            let details_json: String = r.get(2)?;
            let source = match serde_json::from_str::<serde_json::Value>(&details_json) {
                Ok(v) => v
                    .get("source")
                    .and_then(|s| s.as_str())
                    .map(|s| s.to_string()),
                Err(e) => {
                    warn!(
                        alarm_id = id,
                        invariant = %invariant_id,
                        error = %e,
                        "watchdog: failed to parse alarm details_json; skipping auto-resolve for this row"
                    );
                    None
                }
            };
            Ok(Row {
                id,
                invariant_id,
                source,
                clean_ticks: r.get(3)?,
            })
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?
    };

    // Batch every per-row UPDATE into one transaction. With a backlog of
    // stale alarms this collapses N fsync-bounded autocommits into one and
    // gives us atomicity if a future change adds more side effects below.
    let tx = conn.unchecked_transaction()?;
    let mut resolved = 0usize;
    for row in rows {
        // Alarms with no parseable source can't be matched to a current
        // violation pair; conservatively treat them as still violating so
        // they require manual ack rather than auto-resolving in error.
        let Some(source) = row.source else {
            continue;
        };

        let key = (row.invariant_id.clone(), source.clone());
        let still_violating = violating.contains(&key);

        if still_violating {
            // Reset the counter only if it has drifted from 0; spares a write
            // on the steady-state case where the alarm is freshly raised.
            if row.clean_ticks != 0 {
                tx.execute(
                    "UPDATE capture_alarms SET clean_ticks = 0 WHERE id = ?1",
                    rusqlite::params![row.id],
                )?;
                #[cfg(feature = "otel")]
                crate::metrics::WATCHDOG_ALARMS_RESET.add(1, &[]);
            }
            continue;
        }

        let new_ticks = row.clean_ticks + 1;
        if new_ticks >= AUTO_RESOLVE_CLEAN_TICKS {
            let updated = tx.execute(
                "UPDATE capture_alarms
                 SET clean_ticks = ?1, resolved_at = ?2
                 WHERE id = ?3 AND resolved_at IS NULL",
                rusqlite::params![new_ticks, now_ms, row.id],
            )?;
            if updated > 0 {
                resolved += 1;
                info!(
                    alarm_id = row.id,
                    invariant = %row.invariant_id,
                    source = %source,
                    "watchdog: alarm auto-resolved"
                );
            }
        } else {
            tx.execute(
                "UPDATE capture_alarms SET clean_ticks = ?1 WHERE id = ?2",
                rusqlite::params![new_ticks, row.id],
            )?;
        }
    }
    tx.commit()?;

    Ok(resolved)
}

// ---------------------------------------------------------------------------
// Side-effect helpers
// ---------------------------------------------------------------------------

/// Append one JSON line to the watchdog alarm log.
/// Errors are silently swallowed — a logging failure must never stop the cycle.
fn append_alarm_log(
    log_path: &Path,
    ts: i64,
    invariant: &str,
    source: &str,
    since_ms: i64,
    details: &serde_json::Value,
) {
    let line = json!({
        "ts": ts,
        "level": "error",
        "invariant": invariant,
        "source": source,
        "since_ms": since_ms,
        "details": details,
    });

    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path)
    {
        let _ = writeln!(f, "{line}");
    }
}

/// Fire a macOS `display notification` via `osascript`.
/// Errors are swallowed — notification failure must not affect the cycle.
fn fire_macos_notification(message: &str, title: &str) {
    // Use %{message} Debug formatting to safely escape embedded quotes.
    let script = format!("display notification {:?} with title {:?}", message, title);
    let _ = std::process::Command::new("osascript")
        .args(["-e", &script])
        .output();
}

/// Returns `true` when the rusqlite error is SQLITE_BUSY (error code 5).
/// Used by the alarm-insert retry path.
fn is_sqlite_busy(e: &rusqlite::Error) -> bool {
    matches!(
        e,
        rusqlite::Error::SqliteFailure(
            rusqlite::ffi::Error {
                code: rusqlite::ErrorCode::DatabaseBusy,
                ..
            },
            _,
        )
    )
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    // ── Helpers ────────────────────────────────────────────────────────────

    fn open_test_conn(dir: &TempDir) -> Connection {
        let path = dir.path().join("watchdog_test.db");
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA foreign_keys=ON;
             PRAGMA busy_timeout=5000;",
        )
        .unwrap();
        conn
    }

    fn create_capture_alarms_table(conn: &Connection) {
        // v11-shaped table so tests exercise the same DDL the production
        // schema lands on after migration.
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS capture_alarms (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                invariant_id TEXT    NOT NULL,
                raised_at    INTEGER NOT NULL,
                details_json TEXT    NOT NULL,
                acked_at     INTEGER,
                ack_note     TEXT,
                resolved_at  INTEGER,
                clean_ticks  INTEGER NOT NULL DEFAULT 0
             );
             CREATE INDEX IF NOT EXISTS idx_capture_alarms_invariant_active
                 ON capture_alarms (invariant_id, acked_at)
                 WHERE acked_at IS NULL AND resolved_at IS NULL;",
        )
        .unwrap();
    }

    /// Insert one active (un-acked, un-resolved) alarm with a parseable
    /// `details_json.source` field. Returns the new row id.
    fn insert_active_alarm(
        conn: &Connection,
        invariant_id: &str,
        source: &str,
        raised_at: i64,
    ) -> i64 {
        let details = format!("{{\"source\":\"{}\",\"since_ms\":90000}}", source);
        conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json)
             VALUES (?1, ?2, ?3)",
            rusqlite::params![invariant_id, raised_at, details],
        )
        .unwrap();
        conn.last_insert_rowid()
    }

    fn alarm_state(conn: &Connection, id: i64) -> (i64, Option<i64>) {
        conn.query_row(
            "SELECT clean_ticks, resolved_at FROM capture_alarms WHERE id = ?1",
            rusqlite::params![id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap()
    }

    fn create_source_health_table(conn: &Connection) {
        conn.execute_batch(SOURCE_HEALTH_FALLBACK_DDL).unwrap();
    }

    /// Build a baseline `SourceHealthRow` with all optionals as `None` / 0.
    fn blank_row(source: &str) -> SourceHealthRow {
        SourceHealthRow {
            source: source.to_string(),
            last_event_ts: None,
            last_success_ts: None,
            last_error_ts: None,
            last_error_msg: None,
            consecutive_failures: 0,
            events_last_1h: 0,
            events_last_24h: 0,
            expected_min_per_hour: None,
            probe_ok: None,
            probe_lag_ms: None,
            probe_last_run_ts: None,
            last_heartbeat_ts: None,
            updated_at: 0,
        }
    }

    fn by_source(rows: &[SourceHealthRow]) -> std::collections::HashMap<&str, &SourceHealthRow> {
        rows.iter().map(|r| (r.source.as_str(), r)).collect()
    }

    const NOW: i64 = 1_700_000_000_000i64; // arbitrary reference epoch ms

    // ── I-1 ────────────────────────────────────────────────────────────────

    #[test]
    fn watchdog_i1_fires_when_stale_and_probe_active() {
        let row = SourceHealthRow {
            last_event_ts: Some(NOW - 90_000), // 90 s ago > 60 s threshold
            probe_ok: Some(1),
            ..blank_row("shell")
        };
        let rows = vec![row];
        let result = check_i1_shell_liveness(&by_source(&rows), NOW);
        assert!(result.is_some(), "expected I-1 violation");
        let v = result.unwrap();
        assert_eq!(v.invariant_id, "I-1");
        assert_eq!(v.source, "shell");
        assert!(v.since_ms >= 90_000);
    }

    #[test]
    fn watchdog_i1_suppressed_when_not_stale() {
        let row = SourceHealthRow {
            last_event_ts: Some(NOW - 30_000), // 30 s ago < 60 s threshold
            probe_ok: Some(1),
            ..blank_row("shell")
        };
        let rows = vec![row];
        assert!(check_i1_shell_liveness(&by_source(&rows), NOW).is_none());
    }

    #[test]
    fn watchdog_i1_suppressed_when_probe_inactive() {
        let row = SourceHealthRow {
            last_event_ts: Some(NOW - 120_000), // stale
            probe_ok: Some(0),                  // probe says inactive
            ..blank_row("shell")
        };
        let rows = vec![row];
        assert!(check_i1_shell_liveness(&by_source(&rows), NOW).is_none());
    }

    #[test]
    fn watchdog_i1_suppressed_when_probe_null() {
        let row = SourceHealthRow {
            last_event_ts: Some(NOW - 120_000),
            probe_ok: None, // no probe result
            ..blank_row("shell")
        };
        let rows = vec![row];
        assert!(check_i1_shell_liveness(&by_source(&rows), NOW).is_none());
    }

    #[test]
    fn watchdog_i1_suppressed_when_never_seen() {
        let row = SourceHealthRow {
            last_event_ts: None, // never delivered an event
            probe_ok: Some(1),
            ..blank_row("shell")
        };
        let rows = vec![row];
        assert!(check_i1_shell_liveness(&by_source(&rows), NOW).is_none());
    }

    // ── I-2 proxy ──────────────────────────────────────────────────────────

    #[test]
    fn watchdog_i2_proxy_fires_on_consecutive_failures() {
        let row = SourceHealthRow {
            last_event_ts: Some(NOW - 600_000), // 10 min ago
            consecutive_failures: 5,
            ..blank_row("claude-session")
        };
        let rows = vec![row];
        let result = check_i2_claude_session_proxy(&by_source(&rows), NOW);
        assert!(result.is_some());
        assert_eq!(result.unwrap().invariant_id, "I-2");
    }

    #[test]
    fn watchdog_i2_proxy_suppressed_when_failures_low() {
        let row = SourceHealthRow {
            last_event_ts: Some(NOW - 600_000),
            consecutive_failures: 2, // <= 3
            ..blank_row("claude-session")
        };
        let rows = vec![row];
        assert!(check_i2_claude_session_proxy(&by_source(&rows), NOW).is_none());
    }

    #[test]
    fn watchdog_i2_proxy_suppressed_when_never_seen() {
        let row = SourceHealthRow {
            last_event_ts: None,
            consecutive_failures: 10,
            ..blank_row("claude-session")
        };
        let rows = vec![row];
        assert!(check_i2_claude_session_proxy(&by_source(&rows), NOW).is_none());
    }

    // ── I-4 ────────────────────────────────────────────────────────────────

    #[test]
    fn watchdog_i4_fires_when_stale_and_probe_active() {
        let row = SourceHealthRow {
            last_event_ts: Some(NOW - 180_000), // 3 min > 2 min threshold
            probe_ok: Some(1),
            last_heartbeat_ts: Some(NOW - 60_000),
            ..blank_row("browser")
        };
        let rows = vec![row];
        let result = check_i4_browser_roundtrip(&by_source(&rows), NOW);
        assert!(result.is_some());
        let v = result.unwrap();
        assert_eq!(v.invariant_id, "I-4");
        assert!(v.since_ms >= 180_000);
    }

    #[test]
    fn watchdog_i4_suppressed_when_fresh() {
        let row = SourceHealthRow {
            last_event_ts: Some(NOW - 60_000), // 1 min < 2 min threshold
            probe_ok: Some(1),
            ..blank_row("browser")
        };
        let rows = vec![row];
        assert!(check_i4_browser_roundtrip(&by_source(&rows), NOW).is_none());
    }

    #[test]
    fn watchdog_i4_suppressed_when_probe_not_active() {
        let row = SourceHealthRow {
            last_event_ts: Some(NOW - 300_000),
            probe_ok: Some(0), // extension not active / Firefox not running
            ..blank_row("browser")
        };
        let rows = vec![row];
        assert!(check_i4_browser_roundtrip(&by_source(&rows), NOW).is_none());
    }

    // ── I-8 ────────────────────────────────────────────────────────────────

    #[test]
    fn watchdog_i8_fires_when_probe_stale() {
        let row = SourceHealthRow {
            probe_last_run_ts: Some(NOW - 1_000_000), // ~16 min > 15 min threshold
            probe_ok: Some(1),
            ..blank_row("shell")
        };
        let violations = check_i8_probe_freshness(&[row], NOW);
        assert_eq!(violations.len(), 1);
        assert_eq!(violations[0].invariant_id, "I-8");
    }

    #[test]
    fn watchdog_i8_fires_when_probe_failing() {
        let row = SourceHealthRow {
            probe_last_run_ts: Some(NOW - 300_000), // 5 min — fresh but failing
            probe_ok: Some(0),
            ..blank_row("shell")
        };
        let violations = check_i8_probe_freshness(&[row], NOW);
        assert_eq!(violations.len(), 1);
        assert_eq!(violations[0].invariant_id, "I-8");
    }

    #[test]
    fn watchdog_i8_suppressed_when_fresh_and_passing() {
        let row = SourceHealthRow {
            probe_last_run_ts: Some(NOW - 300_000), // 5 min — within threshold
            probe_ok: Some(1),
            ..blank_row("shell")
        };
        let violations = check_i8_probe_freshness(&[row], NOW);
        assert!(violations.is_empty());
    }

    #[test]
    fn watchdog_i8_skipped_when_no_probe() {
        let row = SourceHealthRow {
            probe_last_run_ts: None, // no probe configured for this source
            ..blank_row("shell")
        };
        let violations = check_i8_probe_freshness(&[row], NOW);
        assert!(violations.is_empty());
    }

    // ── Rate limit ─────────────────────────────────────────────────────────

    /// 59-minute-old alarm MUST still suppress at the 60-minute default.
    #[test]
    fn watchdog_rate_limit_59min_suppressed_at_60min_default() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        create_capture_alarms_table(&conn);

        let rate_limit_ms = 60 * 60 * 1_000i64; // 60 minutes
        let raised_59min_ago = NOW - (59 * 60 * 1_000i64);

        conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json)
             VALUES ('I-1', ?1, '{}')",
            rusqlite::params![raised_59min_ago],
        )
        .unwrap();

        let limited = check_rate_limit(&conn, "I-1", NOW, rate_limit_ms).unwrap();
        assert!(
            limited,
            "alarm 59 min ago must still suppress at 60-min default"
        );
    }

    /// Alarm from 61 minutes ago must NOT suppress (outside window).
    #[test]
    fn watchdog_rate_limit_61min_not_suppressed() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        create_capture_alarms_table(&conn);

        let rate_limit_ms = 60 * 60 * 1_000i64;
        let raised_61min_ago = NOW - (61 * 60 * 1_000i64);

        conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json)
             VALUES ('I-1', ?1, '{}')",
            rusqlite::params![raised_61min_ago],
        )
        .unwrap();

        let limited = check_rate_limit(&conn, "I-1", NOW, rate_limit_ms).unwrap();
        assert!(
            !limited,
            "alarm 61 min ago must not suppress at 60-min default"
        );
    }

    /// An acked alarm inside the window must NOT suppress a new alarm
    /// (ack = "I saw this," rate-limit resets after ack).
    #[test]
    fn watchdog_rate_limit_acked_alarm_not_suppressed() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        create_capture_alarms_table(&conn);

        let rate_limit_ms = 60 * 60 * 1_000i64;
        let raised_30min_ago = NOW - (30 * 60 * 1_000i64);

        conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json, acked_at)
             VALUES ('I-1', ?1, '{}', ?2)",
            rusqlite::params![raised_30min_ago, NOW],
        )
        .unwrap();

        let limited = check_rate_limit(&conn, "I-1", NOW, rate_limit_ms).unwrap();
        assert!(!limited, "acked alarm must not suppress new alarm");
    }

    /// No alarm in table → not rate-limited.
    #[test]
    fn watchdog_rate_limit_empty_table_not_suppressed() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        create_capture_alarms_table(&conn);

        let limited = check_rate_limit(&conn, "I-1", NOW, 60 * 60 * 1_000).unwrap();
        assert!(!limited);
    }

    // ── source_health absent ───────────────────────────────────────────────

    /// Call `run()` on a fresh temp DB to prove first-boot initialization
    /// completes without panic or error.  `open_db` inside `run()` applies the
    /// normal migration chain (creating `source_health`, `capture_alarms`,
    /// etc.), so this test exercises successful fresh-DB startup rather than
    /// the belt-and-suspenders pre-migration safety fallback (which would only
    /// trigger if `source_health` were somehow absent on an already-migrated
    /// DB — an edge case outside `open_db`'s contract).
    #[test]
    fn watchdog_source_health_absent_no_panic() {
        let dir = TempDir::new().unwrap();

        // Build a minimal config pointing at the temp dir so run() uses an
        // isolated DB and log file — never touches ~/.local/share/hippo.
        let mut config = HippoConfig::default();
        config.storage.data_dir = dir.path().to_path_buf();
        config.watchdog.enabled = true;

        let result = run(&config);
        assert!(
            result.is_ok(),
            "run() failed on a fresh DB: {:?}",
            result.err()
        );

        // After a successful cycle the watchdog row must exist in source_health.
        let conn = hippo_core::storage::open_db(&config.db_path()).unwrap();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM source_health WHERE source = 'watchdog'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(
            count, 1,
            "watchdog row missing in source_health after run()"
        );
    }

    /// `read_source_health` must return all seeded rows without error.
    #[test]
    fn watchdog_read_source_health_returns_rows() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        create_source_health_table(&conn);
        let now_ms = chrono::Utc::now().timestamp_millis();

        conn.execute(
            "INSERT INTO source_health (source, updated_at) VALUES ('shell', ?1)",
            rusqlite::params![now_ms],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO source_health (source, updated_at) VALUES ('watchdog', ?1)",
            rusqlite::params![now_ms],
        )
        .unwrap();

        let rows = read_source_health(&conn).unwrap();
        assert_eq!(rows.len(), 2);
        let sources: Vec<&str> = rows.iter().map(|r| r.source.as_str()).collect();
        assert!(sources.contains(&"shell"));
        assert!(sources.contains(&"watchdog"));
    }

    // ── Auto-resolve ───────────────────────────────────────────────────────

    /// One clean tick on an active alarm increments clean_ticks but does NOT
    /// resolve (threshold is 2 consecutive clean ticks).
    #[test]
    fn auto_resolve_one_clean_tick_increments_only() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        create_capture_alarms_table(&conn);
        let id = insert_active_alarm(&conn, "I-1", "shell", NOW - 90_000);

        // No current violations → alarm's invariant is clean for this tick.
        let resolved = auto_resolve_alarms(&conn, &[], NOW).unwrap();
        assert_eq!(resolved, 0, "must not resolve on first clean tick");

        let (clean_ticks, resolved_at) = alarm_state(&conn, id);
        assert_eq!(clean_ticks, 1);
        assert!(resolved_at.is_none());
    }

    /// Two consecutive clean ticks resolve the alarm.
    #[test]
    fn auto_resolve_two_clean_ticks_resolves() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        create_capture_alarms_table(&conn);
        let id = insert_active_alarm(&conn, "I-4", "browser", NOW - 200_000);

        // Tick 1: clean.
        assert_eq!(auto_resolve_alarms(&conn, &[], NOW).unwrap(), 0);
        // Tick 2: clean again.
        let resolved = auto_resolve_alarms(&conn, &[], NOW + 60_000).unwrap();
        assert_eq!(resolved, 1, "second clean tick must resolve the alarm");

        let (clean_ticks, resolved_at) = alarm_state(&conn, id);
        assert_eq!(clean_ticks, AUTO_RESOLVE_CLEAN_TICKS);
        assert_eq!(resolved_at, Some(NOW + 60_000));
    }

    /// A clean tick followed by a re-violation resets clean_ticks to 0,
    /// preventing the alarm from auto-resolving on flapping invariants.
    #[test]
    fn auto_resolve_resets_clean_ticks_on_reviolation() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        create_capture_alarms_table(&conn);
        let id = insert_active_alarm(&conn, "I-1", "shell", NOW - 90_000);

        // Tick 1: clean → counter = 1.
        auto_resolve_alarms(&conn, &[], NOW).unwrap();
        assert_eq!(alarm_state(&conn, id).0, 1);

        // Tick 2: invariant violates again → counter resets to 0.
        let still_violating = vec![InvariantViolation {
            invariant_id: "I-1".to_string(),
            source: "shell".to_string(),
            since_ms: 60_000,
            details: serde_json::json!({}),
        }];
        let resolved = auto_resolve_alarms(&conn, &still_violating, NOW + 60_000).unwrap();
        assert_eq!(resolved, 0);

        let (clean_ticks, resolved_at) = alarm_state(&conn, id);
        assert_eq!(clean_ticks, 0, "re-violation must reset counter");
        assert!(resolved_at.is_none());
    }

    /// Already-resolved alarms must not be touched by subsequent ticks.
    #[test]
    fn auto_resolve_skips_already_resolved() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        create_capture_alarms_table(&conn);
        let id = insert_active_alarm(&conn, "I-1", "shell", NOW - 90_000);
        // Manually resolve.
        conn.execute(
            "UPDATE capture_alarms SET resolved_at = ?1, clean_ticks = 2 WHERE id = ?2",
            rusqlite::params![NOW - 60_000, id],
        )
        .unwrap();

        let resolved = auto_resolve_alarms(&conn, &[], NOW).unwrap();
        assert_eq!(resolved, 0);

        let (clean_ticks, resolved_at) = alarm_state(&conn, id);
        assert_eq!(clean_ticks, 2, "resolved row must be untouched");
        assert_eq!(resolved_at, Some(NOW - 60_000));
    }

    /// Acked alarms must not be re-evaluated by the auto-resolve loop.
    #[test]
    fn auto_resolve_skips_acked() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        create_capture_alarms_table(&conn);
        let id = insert_active_alarm(&conn, "I-1", "shell", NOW - 90_000);
        conn.execute(
            "UPDATE capture_alarms SET acked_at = ?1 WHERE id = ?2",
            rusqlite::params![NOW - 30_000, id],
        )
        .unwrap();

        auto_resolve_alarms(&conn, &[], NOW).unwrap();
        // counter unchanged (still 0)
        assert_eq!(alarm_state(&conn, id).0, 0);
    }

    /// Per-source matching: an I-8 alarm against `browser` must NOT auto-
    /// resolve when the only current violation is I-8 against a different
    /// source. Each (invariant, source) pair is tracked independently.
    #[test]
    fn auto_resolve_matches_on_invariant_and_source_pair() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        create_capture_alarms_table(&conn);
        let browser_id = insert_active_alarm(&conn, "I-8", "browser", NOW - 1_000_000);

        // I-8 violates for shell only (different source).
        let other_violation = vec![InvariantViolation {
            invariant_id: "I-8".to_string(),
            source: "shell".to_string(),
            since_ms: 0,
            details: serde_json::json!({}),
        }];
        auto_resolve_alarms(&conn, &other_violation, NOW).unwrap();
        // Browser alarm is clean (its pair isn't in current violations).
        assert_eq!(alarm_state(&conn, browser_id).0, 1);
    }

    /// Alarms whose details_json has no parseable source are conservatively
    /// left alone (counter unchanged) so a malformed row never silently
    /// auto-resolves.
    #[test]
    fn auto_resolve_skips_alarm_with_unparseable_source() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        create_capture_alarms_table(&conn);
        conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json)
             VALUES ('I-1', ?1, '{}')",
            rusqlite::params![NOW - 90_000],
        )
        .unwrap();
        let id = conn.last_insert_rowid();

        auto_resolve_alarms(&conn, &[], NOW).unwrap();
        let (clean_ticks, resolved_at) = alarm_state(&conn, id);
        assert_eq!(clean_ticks, 0);
        assert!(resolved_at.is_none());
    }

    /// A `details_json` whose `source` field is non-string (number, null,
    /// missing entirely) must NOT cause the alarm to silently auto-resolve.
    /// Locks in the contract that only string-valued sources participate
    /// in pair-matching.
    #[test]
    fn auto_resolve_skips_alarm_with_non_string_source() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        create_capture_alarms_table(&conn);

        // Three rows, each with a problematic `source` field shape.
        for details in [
            r#"{"source": 42}"#,       // numeric
            r#"{"source": null}"#,     // explicit null
            r#"{"other_field": "x"}"#, // missing entirely
        ] {
            conn.execute(
                "INSERT INTO capture_alarms (invariant_id, raised_at, details_json)
                 VALUES ('I-1', ?1, ?2)",
                rusqlite::params![NOW - 90_000, details],
            )
            .unwrap();
        }

        let resolved = auto_resolve_alarms(&conn, &[], NOW).unwrap();
        assert_eq!(
            resolved, 0,
            "alarms with non-string source must never auto-resolve"
        );

        let counters: Vec<i64> = conn
            .prepare("SELECT clean_ticks FROM capture_alarms")
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        assert!(
            counters.iter().all(|&c| c == 0),
            "counters must stay at 0 for unparseable rows; got {counters:?}"
        );
    }

    /// Resolved alarms must not suppress new alarms via rate-limit. A
    /// resolved row from 5 minutes ago should leave check_rate_limit free
    /// to allow a brand-new raise.
    #[test]
    fn watchdog_rate_limit_resolved_alarm_not_suppressed() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        create_capture_alarms_table(&conn);

        let raised_5min_ago = NOW - 5 * 60 * 1_000;
        conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json, resolved_at)
             VALUES ('I-1', ?1, '{}', ?2)",
            rusqlite::params![raised_5min_ago, NOW - 60_000],
        )
        .unwrap();

        let limited = check_rate_limit(&conn, "I-1", NOW, 60 * 60 * 1_000).unwrap();
        assert!(
            !limited,
            "resolved alarm must not suppress new raises via rate-limit"
        );
    }

    /// check_invariants must return an empty Vec when no violations exist.
    #[test]
    fn watchdog_check_invariants_no_violations_on_fresh_rows() {
        // All rows have NULL last_event_ts and NULL probe_ok → everything suppressed.
        let rows = vec![
            blank_row("shell"),
            blank_row("claude-session"),
            blank_row("browser"),
            blank_row("watchdog"),
        ];
        let violations = check_invariants(&rows, NOW);
        assert!(violations.is_empty());
    }
}
