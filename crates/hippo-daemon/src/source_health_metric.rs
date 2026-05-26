//! Per-source freshness gauge sourced from the `source_health` table.
//!
//! The table is the single SQL ground truth for "did the event land?" per
//! capture source — every capture path updates its row in the same transaction
//! as the event insert. This module exposes that ground truth to OTel as
//! `hippo.daemon.source_health.lag` so dashboards can show one line per source
//! ("how stale am I, in ms?") without enumerating sources by hand.
//!
//! Why this matters: counters like `hippo.daemon.events.ingested` only cover
//! sources that flow through the Unix socket (shell, browser, claude-tool).
//! Session pollers (opencode/codex/cursor) write directly to SQLite and never
//! increment a daemon counter — so their visibility came purely from the
//! brain's enrichment metrics, which fire *after* inference. This gauge closes
//! the gap: every source with a `source_health` row gets a freshness line on
//! every export tick, with no per-source instrumentation in the polling code.
//!
//! ## Companion gauges
//!
//! Three observable gauges are registered together because they all derive
//! from the same `source_health` row read:
//!
//! - `hippo.daemon.source_health.lag` (ms) — `now - max(last_event_ts,
//!   last_heartbeat_ts)`. The primary freshness signal.
//! - `hippo.daemon.source_health.consecutive_failures` (count) — value of the
//!   `consecutive_failures` column. Spikes when a source's writes start
//!   erroring; useful as an "alarm leading indicator."
//! - `hippo.daemon.source_health.probe_ok` (0/1) — last probe verdict. Lets
//!   the dashboard show a per-source canary status without parsing the
//!   `capture_alarms` table.

use crate::is_missing_source_health_table_error;
use opentelemetry::KeyValue;
use opentelemetry::global;
use rusqlite::{Connection, OpenFlags};
use std::path::PathBuf;
use std::time::Duration;
use tracing::debug;

/// Open the operator DB read-only with the standard `busy_timeout` so the
/// observer never starves a concurrent writer, and never creates a stray
/// file if the path is missing (the OTel callback runs on its own thread —
/// fail open, don't pollute the data dir). Returns `None` on any error so
/// the gauges omit the tick rather than fail loud — a transient SQLITE_BUSY
/// against the watchdog would otherwise blank the dashboard.
fn open_conn(db_path: &std::path::Path) -> Option<Connection> {
    let conn = Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .ok()?;
    conn.busy_timeout(Duration::from_millis(5000)).ok()?;
    Some(conn)
}

/// One row's worth of derived gauge state, populated by a single
/// `SELECT … FROM source_health`. Three observable gauges are registered
/// from this module and each runs its own callback (the OTel API binds one
/// callback per instrument), so the table is read up to three times per
/// export tick. That's accepted on purpose: the query is an indexed scan of
/// a ~10-row table and the connection is read-only + NO_MUTEX, so the cost
/// is negligible compared to the complexity of a shared TTL cache.
struct Row {
    source: String,
    lag_ms: Option<u64>,
    consecutive_failures: u64,
    probe_ok: Option<u8>,
}

fn read_rows(db_path: &std::path::Path, now_ms: i64) -> Vec<Row> {
    let Some(conn) = open_conn(db_path) else {
        return Vec::new();
    };
    let mut stmt = match conn.prepare(
        "SELECT source, last_event_ts, last_heartbeat_ts, consecutive_failures, probe_ok
           FROM source_health",
    ) {
        Ok(s) => s,
        // Pre-migration DBs lack `source_health`; this is the expected steady
        // state until the schema migration runs, so don't log on every tick.
        // Any other prepare error is genuinely surprising — keep that visible.
        Err(e) if is_missing_source_health_table_error(&e) => return Vec::new(),
        Err(e) => {
            debug!(error = %e, "source_health_metric: prepare failed");
            return Vec::new();
        }
    };
    let rows = stmt.query_map([], |r| {
        let source: String = r.get(0)?;
        let last_event_ts: Option<i64> = r.get(1)?;
        let last_heartbeat_ts: Option<i64> = r.get(2)?;
        let consecutive_failures: i64 = r.get(3)?;
        let probe_ok: Option<i64> = r.get(4)?;

        // Freshness is the more-recent of the two liveness signals. A source
        // with only heartbeats (no events yet — fresh install) still gets a
        // bounded lag instead of NULL, which would silently drop from the
        // dashboard.
        let latest = match (last_event_ts, last_heartbeat_ts) {
            (Some(a), Some(b)) => Some(a.max(b)),
            (Some(a), None) => Some(a),
            (None, Some(b)) => Some(b),
            (None, None) => None,
        };
        let lag_ms = latest.map(|ts| (now_ms - ts).max(0) as u64);

        Ok(Row {
            source,
            lag_ms,
            consecutive_failures: consecutive_failures.max(0) as u64,
            probe_ok: probe_ok.map(|n| if n != 0 { 1u8 } else { 0u8 }),
        })
    });
    let rows = match rows {
        Ok(r) => r,
        Err(e) => {
            debug!(error = %e, "source_health_metric: query failed");
            return Vec::new();
        }
    };
    rows.filter_map(|r| r.ok()).collect()
}

/// Register the per-source observable gauges. Call once from `daemon::run`
/// after telemetry init, under `cfg(feature = "otel")`.
pub fn register(db_path: PathBuf) {
    let meter = global::meter("hippo-daemon");

    // Build three callbacks that each re-read the table. Cheap (indexed PK,
    // ~10 rows on a healthy install) and avoids a separate refresh task.
    let dbp = db_path.clone();
    let _ = meter
        .u64_observable_gauge("hippo.daemon.source_health.lag")
        .with_description(
            "Per-source freshness: now - max(last_event_ts, last_heartbeat_ts). \
             A line per source means OTel sees that source as captured.",
        )
        .with_unit("ms")
        .with_callback(move |g| {
            let now_ms = chrono::Utc::now().timestamp_millis();
            for row in read_rows(&dbp, now_ms) {
                if let Some(lag) = row.lag_ms {
                    g.observe(lag, &[KeyValue::new("source", row.source)]);
                }
            }
        })
        .build();

    let dbp = db_path.clone();
    let _ = meter
        .u64_observable_gauge("hippo.daemon.source_health.consecutive_failures")
        .with_description("Per-source consecutive_failures counter from source_health.")
        .with_unit("1")
        .with_callback(move |g| {
            let now_ms = chrono::Utc::now().timestamp_millis();
            for row in read_rows(&dbp, now_ms) {
                g.observe(
                    row.consecutive_failures,
                    &[KeyValue::new("source", row.source)],
                );
            }
        })
        .build();

    let dbp = db_path;
    let _ = meter
        .u64_observable_gauge("hippo.daemon.source_health.probe_ok")
        .with_description(
            "Per-source last probe verdict (0 = fail, 1 = ok). \
             Sources without probe coverage emit nothing.",
        )
        .with_unit("1")
        .with_callback(move |g| {
            let now_ms = chrono::Utc::now().timestamp_millis();
            for row in read_rows(&dbp, now_ms) {
                if let Some(ok) = row.probe_ok {
                    g.observe(ok.into(), &[KeyValue::new("source", row.source)]);
                }
            }
        })
        .build();
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection as RConn;

    struct FixtureRow {
        source: &'static str,
        last_event_ts: Option<i64>,
        last_heartbeat_ts: Option<i64>,
        consecutive_failures: i64,
        probe_ok: Option<i64>,
    }

    fn fixture(rows: &[FixtureRow]) -> tempfile::NamedTempFile {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = RConn::open(tmp.path()).unwrap();
        conn.execute_batch(
            "CREATE TABLE source_health (
                 source TEXT PRIMARY KEY,
                 last_event_ts INTEGER,
                 last_heartbeat_ts INTEGER,
                 consecutive_failures INTEGER NOT NULL DEFAULT 0,
                 probe_ok INTEGER
             );",
        )
        .unwrap();
        for r in rows {
            conn.execute(
                "INSERT INTO source_health \
                 (source, last_event_ts, last_heartbeat_ts, consecutive_failures, probe_ok) \
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                rusqlite::params![
                    r.source,
                    r.last_event_ts,
                    r.last_heartbeat_ts,
                    r.consecutive_failures,
                    r.probe_ok,
                ],
            )
            .unwrap();
        }
        tmp
    }

    #[test]
    fn lag_uses_more_recent_of_event_and_heartbeat() {
        let now = 10_000i64;
        let db = fixture(&[
            FixtureRow {
                source: "shell",
                last_event_ts: Some(9_000),
                last_heartbeat_ts: Some(8_000),
                consecutive_failures: 0,
                probe_ok: Some(1),
            },
            FixtureRow {
                source: "browser",
                last_event_ts: Some(7_000),
                last_heartbeat_ts: Some(9_500),
                consecutive_failures: 1,
                probe_ok: Some(1),
            },
            FixtureRow {
                source: "cursor",
                last_event_ts: None,
                last_heartbeat_ts: Some(9_900),
                consecutive_failures: 0,
                probe_ok: None,
            },
        ]);
        let mut got: Vec<_> = read_rows(db.path(), now)
            .into_iter()
            .map(|r| (r.source, r.lag_ms))
            .collect();
        got.sort();
        assert_eq!(
            got,
            vec![
                ("browser".into(), Some(500)),
                ("cursor".into(), Some(100)),
                ("shell".into(), Some(1_000)),
            ]
        );
    }

    #[test]
    fn null_event_and_heartbeat_emit_no_lag() {
        let db = fixture(&[FixtureRow {
            source: "never-seen",
            last_event_ts: None,
            last_heartbeat_ts: None,
            consecutive_failures: 0,
            probe_ok: None,
        }]);
        let rows = read_rows(db.path(), 1_000);
        assert_eq!(rows.len(), 1);
        assert!(rows[0].lag_ms.is_none());
    }

    #[test]
    fn missing_db_yields_empty_rows() {
        let rows = read_rows(std::path::Path::new("/nonexistent/db"), 0);
        assert!(rows.is_empty());
    }

    #[test]
    fn consecutive_failures_and_probe_ok_pass_through() {
        let db = fixture(&[FixtureRow {
            source: "codex",
            last_event_ts: Some(100),
            last_heartbeat_ts: None,
            consecutive_failures: 7,
            probe_ok: Some(0),
        }]);
        let rows = read_rows(db.path(), 200);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].consecutive_failures, 7);
        assert_eq!(rows[0].probe_ok, Some(0));
    }
}
