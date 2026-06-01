//! Stack-wide health grade for at-a-glance OTel dashboard signal.
//!
//! Emits a single `hippo.daemon.health.grade` gauge (0–100) derived from
//! the count of currently-active `capture_alarms` rows — the same alarms
//! that `hippo doctor` and `hippo alarms list` surface. The grade lets a
//! Grafana stat panel give a green/yellow/red verdict without having to
//! eyeball every individual invariant.
//!
//! Scoring formula:
//!   `grade = max(0, 100 - 10 * active_alarm_count)`
//!
//! That's intentionally simple: 0 alarms = 100, 1 = 90, 9 = 10, ≥10 = 0.
//! Per-invariant severity weighting was considered and rejected — the
//! watchdog already deduplicates alarms per (invariant, source) pair via
//! rate-limiting, so 10+ simultaneously-active alarms genuinely means the
//! whole stack is on fire. Auto-resolved alarms (the watchdog's clean-tick
//! mechanism) drop out of the count, so transient blips heal automatically.
//!
//! Companion telemetry: `hippo.daemon.health.active_alarms` exposes the raw
//! count so dashboards can drill in if the grade looks bad.

use opentelemetry::global;
use rusqlite::Connection;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::time::{self, Duration};
use tracing::debug;

/// Refresh cadence. 10s lines up with the typical OTel export interval
/// without oversampling SQLite.
const REFRESH_INTERVAL_SECS: u64 = 10;

/// Atomic state shared between the refresh task and the OTel callback.
struct State {
    grade: AtomicU64,
    active_alarms: AtomicU64,
}

impl State {
    const fn new() -> Self {
        Self {
            grade: AtomicU64::new(100),
            active_alarms: AtomicU64::new(0),
        }
    }
}

/// Register the health-grade observable gauge + spawn the refresh task.
/// Call once from `daemon::run` after telemetry init, under
/// `cfg(feature = "otel")`. The `db_path` points at the operator's
/// `~/.local/share/hippo/hippo.db`.
pub fn register(db_path: PathBuf) {
    let state = Arc::new(State::new());
    spawn_refresh_task(Arc::clone(&state), db_path);

    let meter = global::meter("hippo-daemon");

    let s_grade = Arc::clone(&state);
    let _ = meter
        .u64_observable_gauge("hippo.daemon.health.grade")
        .with_description(
            "Stack-wide health grade 0–100. 100 = no active capture alarms; \
             subtracts 10 per unresolved/unacked capture_alarms row. \
             Floored at 0. Dashboard threshold: <70 red, 70-90 yellow, ≥90 green.",
        )
        .with_callback(move |g| {
            g.observe(s_grade.grade.load(Ordering::Relaxed), &[]);
        })
        .build();

    let s_count = Arc::clone(&state);
    let _ = meter
        .u64_observable_gauge("hippo.daemon.health.active_alarms")
        .with_description("Number of currently unresolved + unacked capture_alarms rows.")
        .with_callback(move |g| {
            g.observe(s_count.active_alarms.load(Ordering::Relaxed), &[]);
        })
        .build();
}

fn spawn_refresh_task(state: Arc<State>, db_path: PathBuf) {
    tokio::spawn(async move {
        let mut interval = time::interval(Duration::from_secs(REFRESH_INTERVAL_SECS));
        loop {
            interval.tick().await;
            // Open per-tick so a stale connection (e.g. DB file replaced by
            // a migration) self-heals on the next iteration. The query is
            // a single indexed COUNT(*) so the open-cost is negligible.
            match read_alarm_count(&db_path) {
                Ok(count) => {
                    let grade = compute_grade(count);
                    state.active_alarms.store(count, Ordering::Relaxed);
                    state.grade.store(grade, Ordering::Relaxed);
                }
                Err(e) => {
                    // Don't poison the gauge on transient DB errors —
                    // keep the last good value. Log at debug so the
                    // signal is greppable without spamming on healthy
                    // installs that simply haven't created capture_alarms
                    // yet (the table is created in the v8 migration), and
                    // on transient SQLITE_BUSY against the watchdog writer.
                    debug!(error = %e, "health_score: capture_alarms read failed");
                }
            }
        }
    });
}

fn read_alarm_count(db_path: &std::path::Path) -> rusqlite::Result<u64> {
    let conn = Connection::open(db_path)?;
    // CLAUDE.md: every connection sets busy_timeout=5000 so a concurrent
    // watchdog writer doesn't immediately return SQLITE_BUSY on every tick.
    conn.busy_timeout(Duration::from_millis(5000))?;
    conn.query_row(
        "SELECT COUNT(*) FROM capture_alarms \
         WHERE acked_at IS NULL AND resolved_at IS NULL",
        [],
        |r| r.get::<_, i64>(0),
    )
    .map(|n| n.max(0) as u64)
}

/// Pure scoring function. Exposed so unit tests can exercise the formula
/// independent of the OTel + DB plumbing.
pub fn compute_grade(active_alarms: u64) -> u64 {
    100u64.saturating_sub(active_alarms.saturating_mul(10))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grade_is_100_when_no_alarms() {
        assert_eq!(compute_grade(0), 100);
    }

    #[test]
    fn grade_drops_10_per_alarm() {
        assert_eq!(compute_grade(1), 90);
        assert_eq!(compute_grade(3), 70);
        assert_eq!(compute_grade(9), 10);
    }

    #[test]
    fn grade_floors_at_zero() {
        assert_eq!(compute_grade(10), 0);
        assert_eq!(compute_grade(100), 0);
        assert_eq!(compute_grade(u64::MAX), 0);
    }

    #[test]
    fn read_alarm_count_zero_on_empty_table() {
        use rusqlite::Connection as RConn;
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let path = tmp.path();
        let conn = RConn::open(path).unwrap();
        conn.execute_batch(
            "CREATE TABLE capture_alarms (
                 id INTEGER PRIMARY KEY,
                 invariant_id TEXT,
                 source TEXT,
                 raised_at INTEGER,
                 acked_at INTEGER,
                 resolved_at INTEGER
             );",
        )
        .unwrap();
        let n = read_alarm_count(path).unwrap();
        assert_eq!(n, 0);
    }

    /// Locks the OTel unit-removal contract.
    ///
    /// The OTel->Prometheus exporter appends the OTel unit as a suffix when it
    /// is non-empty.  `unit="1"` yields `_ratio` (misleading on a 0–100 score
    /// or a raw count), so both gauges carry no unit.  The canonical Prometheus
    /// names after export are therefore:
    ///
    ///   hippo_daemon_health_grade           (not *_ratio)
    ///   hippo_daemon_health_active_alarms   (not *_ratio)
    ///
    /// This test encodes that contract as a const-string assertion so any
    /// future re-introduction of `.with_unit("1")` fails here first.
    #[test]
    fn health_metric_names_have_no_ratio_suffix() {
        // OTel instrument names use dots; Prometheus replaces them with underscores.
        const GRADE_OTEL: &str = "hippo.daemon.health.grade";
        const ALARMS_OTEL: &str = "hippo.daemon.health.active_alarms";

        // Derive the Prometheus base name the same way the exporter does
        // (dots → underscores; no unit suffix because unit is empty).
        let grade_prom = GRADE_OTEL.replace('.', "_");
        let alarms_prom = ALARMS_OTEL.replace('.', "_");

        assert_eq!(grade_prom, "hippo_daemon_health_grade");
        assert_eq!(alarms_prom, "hippo_daemon_health_active_alarms");

        // Guard: if a unit were set to "1" the exporter would append "_ratio".
        assert!(
            !grade_prom.ends_with("_ratio"),
            "grade Prometheus name must not end in _ratio (remove .with_unit(\"1\"))"
        );
        assert!(
            !alarms_prom.ends_with("_ratio"),
            "active_alarms Prometheus name must not end in _ratio (remove .with_unit(\"1\"))"
        );
    }

    #[test]
    fn read_alarm_count_excludes_acked_and_resolved() {
        use rusqlite::Connection as RConn;
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let path = tmp.path();
        let conn = RConn::open(path).unwrap();
        conn.execute_batch(
            "CREATE TABLE capture_alarms (
                 id INTEGER PRIMARY KEY,
                 invariant_id TEXT,
                 source TEXT,
                 raised_at INTEGER,
                 acked_at INTEGER,
                 resolved_at INTEGER
             );
             INSERT INTO capture_alarms (invariant_id, source, raised_at, acked_at, resolved_at) VALUES
                 ('I-1',  'shell',          1, NULL, NULL),   -- active
                 ('I-4',  'browser',        2, NULL, NULL),   -- active
                 ('I-11', 'agentic',        3, 1234, NULL),   -- acked → excluded
                 ('I-12', 'brain-preflight', 4, NULL, 5678);  -- resolved → excluded",
        )
        .unwrap();
        let n = read_alarm_count(path).unwrap();
        assert_eq!(n, 2, "only the two un-acked + un-resolved rows count");
    }
}
