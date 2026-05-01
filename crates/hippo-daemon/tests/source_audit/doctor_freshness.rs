//! Doctor extension — `hippo doctor` emits one freshness line per
//! source. The line formats and the < 2s wall-clock budget are specified
//! in `docs/archive/capture-reliability-overhaul/10-source-audit.md`
//! (and the doctor's user-facing contract in
//! `docs/capture/operator-runbook.md`); the formats are produced by the
//! pure-function helper `commands::source_freshness_verdict`.
//!
//! This test exercises two things:
//!
//! 1. `source_freshness_probes()` enumerates exactly the set of sources
//!    the audit spec claims — if a source is added/removed in code but
//!    not in the spec (or vice versa), the test fails.
//! 2. `source_freshness_verdict()` emits the right status prefix for
//!    each branch — `[OK]`, `[WW]`, `[!!]`, `[--]`.
//!
//! Additionally, the whole freshness check must run quickly (< 2s per
//! the audit spec): we measure direct-query wall-clock on a fresh DB.

use hippo_core::storage::open_db;
use hippo_daemon::commands::{
    FreshnessThresholds, source_freshness_probes, source_freshness_verdict,
};

use crate::common::test_config;

#[test]
fn probes_cover_every_source_from_the_audit_matrix() {
    let names: Vec<&'static str> = source_freshness_probes().iter().map(|p| p.name).collect();
    assert_eq!(
        names,
        vec![
            "shell",
            "claude-tool",
            "claude-session (main)",
            "claude-session (subagent)",
            "browser",
            "workflow",
        ],
        "probe list must stay in sync with docs/capture/sources.md"
    );
}

#[test]
fn verdict_zero_rows_emits_dashes() {
    let thresholds = FreshnessThresholds {
        soft_ms: 60_000,
        hard_ms: 600_000,
    };
    let line = source_freshness_verdict("shell", 0, None, 1_000_000_000, thresholds);
    assert!(
        line.starts_with("[--]"),
        "zero rows should emit [--], got: {line}"
    );
    assert!(line.contains("zero rows ever"));
}

#[test]
fn verdict_fresh_row_emits_ok() {
    let now = 1_700_000_000_000_i64;
    let thresholds = FreshnessThresholds {
        soft_ms: 60_000,
        hard_ms: 600_000,
    };
    let line = source_freshness_verdict("shell", 3, Some(now - 1_000), now, thresholds);
    assert!(
        line.starts_with("[OK]"),
        "fresh row should emit [OK], got: {line}"
    );
    assert!(line.contains("3 rows"));
}

#[test]
fn verdict_stale_past_soft_threshold_emits_ww() {
    let now = 1_700_000_000_000_i64;
    let thresholds = FreshnessThresholds {
        soft_ms: 60_000,
        hard_ms: 600_000,
    };
    let line = source_freshness_verdict("shell", 10, Some(now - 120_000), now, thresholds);
    assert!(
        line.starts_with("[WW]"),
        "past soft threshold should emit [WW], got: {line}"
    );
}

#[test]
fn verdict_stale_past_hard_threshold_emits_bangs() {
    let now = 1_700_000_000_000_i64;
    let thresholds = FreshnessThresholds {
        soft_ms: 60_000,
        hard_ms: 600_000,
    };
    let line = source_freshness_verdict("shell", 10, Some(now - 10_000_000), now, thresholds);
    assert!(
        line.starts_with("[!!]"),
        "past hard threshold should emit [!!], got: {line}"
    );
}

/// Drive every probe against a real (empty) DB and measure wall-clock.
/// Audit spec requires < 2s for the freshness check; in practice this
/// runs in single-digit ms on an empty SQLite file.
#[test]
fn freshness_check_runs_in_under_2s_on_empty_db() {
    let config = test_config();
    std::fs::create_dir_all(&config.storage.data_dir).unwrap();
    let db_path = config.db_path();
    let conn = open_db(&db_path).unwrap();

    let now_ms = chrono::Utc::now().timestamp_millis();

    let start = std::time::Instant::now();
    for probe in source_freshness_probes() {
        let (count, max_ts): (i64, Option<i64>) = conn
            .query_row(probe.query, [], |r| Ok((r.get(0)?, r.get(1)?)))
            .expect("probe query must succeed on a fresh schema");
        // Every source should be "[--] zero rows ever" on a fresh DB.
        let line = source_freshness_verdict(probe.name, count, max_ts, now_ms, probe.thresholds);
        assert!(
            line.starts_with("[--]"),
            "empty-DB probe for {} should emit [--], got: {line}",
            probe.name
        );
    }
    let elapsed = start.elapsed();
    assert!(
        elapsed < std::time::Duration::from_secs(2),
        "freshness check must run in < 2s per 10-source-audit.md, took {elapsed:?}"
    );
}

/// End-to-end: insert one shell event + one browser event, then assert
/// the verdict strings flip the appropriate sources to `[OK]`.
#[test]
fn freshness_verdicts_reflect_real_table_state() {
    let config = test_config();
    std::fs::create_dir_all(&config.storage.data_dir).unwrap();
    let db_path = config.db_path();
    let conn = open_db(&db_path).unwrap();

    let now = chrono::Utc::now().timestamp_millis();

    // Session row so events.session_id FK is satisfied.
    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username)
         VALUES (1, ?1, 'zsh', 'test-host', 'tester')",
        [now],
    )
    .unwrap();
    conn.execute(
        "INSERT INTO events
           (session_id, timestamp, command, duration_ms, cwd, hostname,
            shell, source_kind)
         VALUES (1, ?1, 'echo audit', 5, '/tmp', 'test-host', 'zsh', 'shell')",
        [now],
    )
    .unwrap();
    conn.execute(
        "INSERT INTO browser_events
           (timestamp, url, title, domain, dwell_ms)
         VALUES (?1, 'https://docs.rs/', 'docs', 'docs.rs', 1000)",
        [now],
    )
    .unwrap();

    let lines: Vec<String> = source_freshness_probes()
        .iter()
        .map(|probe| {
            let (count, max_ts): (i64, Option<i64>) = conn
                .query_row(probe.query, [], |r| Ok((r.get(0)?, r.get(1)?)))
                .unwrap();
            source_freshness_verdict(probe.name, count, max_ts, now, probe.thresholds)
        })
        .collect();

    let joined = lines.join("\n");
    assert!(
        joined.contains("[OK] Source freshness shell"),
        "shell should be OK after insert, got:\n{joined}"
    );
    assert!(
        joined.contains("[OK] Source freshness browser"),
        "browser should be OK after insert, got:\n{joined}"
    );
    assert!(
        joined.contains("[--] Source freshness claude-tool"),
        "claude-tool should still be [--], got:\n{joined}"
    );
    assert!(
        joined.contains("[--] Source freshness workflow"),
        "workflow should still be [--], got:\n{joined}"
    );
}
