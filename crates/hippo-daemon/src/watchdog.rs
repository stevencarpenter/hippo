//! Capture-reliability watchdog — `hippo watchdog run`
//!
//! Short-lived process invoked every 60 s by launchd (`com.hippo.watchdog`).
//! Asserts invariants I-1..I-10 against the `source_health` table and writes
//! rows to `capture_alarms` for any violations detected.  Rate-limited per
//! invariant per sliding window (default 60 min).
//!
//! Five-step flow (per `docs/capture-reliability/04-watchdog.md`):
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
use std::io::Write;
use std::path::Path;
use tracing::{error, info, warn};

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

        conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json)
             VALUES (?1, ?2, ?3)",
            rusqlite::params![v.invariant_id, now_ms, details_json],
        )?;

        error!(
            invariant = %v.invariant_id,
            source = %v.source,
            since_ms = v.since_ms,
            "watchdog: new alarm raised"
        );

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

    // ── Step 5: Mark cycle complete ───────────────────────────────────────
    conn.execute(
        "UPDATE source_health SET last_success_ts = ?1 WHERE source = 'watchdog'",
        rusqlite::params![now_ms],
    )?;

    Ok(())
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
    let count: i64 = conn.query_row(
        "SELECT COUNT(*) FROM capture_alarms
         WHERE invariant_id = ?1
           AND acked_at IS NULL
           AND raised_at > ?2",
        rusqlite::params![invariant_id, cutoff_ms],
        |row| row.get(0),
    )?;
    Ok(count > 0)
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
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS capture_alarms (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                invariant_id TEXT    NOT NULL,
                raised_at    INTEGER NOT NULL,
                details_json TEXT    NOT NULL,
                acked_at     INTEGER,
                ack_note     TEXT
             );
             CREATE INDEX IF NOT EXISTS idx_capture_alarms_invariant_active
                 ON capture_alarms (invariant_id, acked_at)
                 WHERE acked_at IS NULL;",
        )
        .unwrap();
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

    /// When source_health does not exist the watchdog must not panic — it should
    /// create the table, seed the watchdog row, and return `Ok(())`.
    #[test]
    fn watchdog_source_health_absent_no_panic() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        // Do NOT create source_health — simulate a bare database.

        // Verify absence.
        assert!(!source_health_table_exists(&conn).unwrap());

        // The early-return path creates the table and seeds the watchdog row.
        conn.execute_batch(SOURCE_HEALTH_FALLBACK_DDL).unwrap();
        let now_ms = chrono::Utc::now().timestamp_millis();
        conn.execute(
            "INSERT OR IGNORE INTO source_health (source, updated_at) VALUES ('watchdog', ?1)",
            rusqlite::params![now_ms],
        )
        .unwrap();

        // Confirm table exists and watchdog row is present — no panic.
        assert!(source_health_table_exists(&conn).unwrap());
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM source_health WHERE source = 'watchdog'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
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
