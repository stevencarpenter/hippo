//! P0.2 — source_health write-path SQL semantics test.
//!
//! Tests that the UPDATE SQL used in `flush_events` correctly reflects
//! per-source outcome data into the `source_health` table. The test
//! operates directly on the DB (no daemon process needed) to keep it fast
//! and focused on SQL semantics rather than the full IPC path.
//!
//! Full end-to-end coverage (flush_events → source_health) requires
//! the P0.1 migration to have run (so the table exists); until then
//! UPDATEs against the missing table return an error that production
//! code intentionally ignores via `let _ =`, which is also tested here.

use hippo_core::storage;

/// Simulate what flush_events does after processing 3 successful shell events:
/// pre-seed a source_health row, run the success UPDATE, and assert the result.
#[test]
fn source_health_success_update_increments_counts_and_clears_failures() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("hippo.db");
    let conn = storage::open_db(&db_path).unwrap();

    // Pre-seed source_health row for 'shell' with known initial state.
    // Use INSERT OR REPLACE because the v7→v8 migration already seeds one row
    // per source; we need to overwrite it with a specific consecutive_failures=2
    // to test that the success UPDATE clears it.
    conn.execute(
        "INSERT OR REPLACE INTO source_health (source, consecutive_failures, events_last_1h, events_last_24h, updated_at)
         VALUES ('shell', 2, 0, 0, 0)",
        [],
    )
    .expect("source_health table should exist — requires P0.1 migration");

    let now_ms: i64 = 1_700_000_000_000;
    let latest_ts: i64 = 1_699_999_999_000;
    let count: i64 = 3;

    // Run the same UPDATE SQL flush_events issues on success.
    let rows_affected = conn
        .execute(
            "UPDATE source_health
             SET last_event_ts        = MAX(COALESCE(last_event_ts, 0), ?1),
                 last_success_ts      = ?2,
                 events_last_1h       = events_last_1h  + ?3,
                 events_last_24h      = events_last_24h + ?3,
                 consecutive_failures = 0,
                 updated_at           = ?2
             WHERE source = ?4",
            rusqlite::params![latest_ts, now_ms, count, "shell"],
        )
        .unwrap();

    assert_eq!(
        rows_affected, 1,
        "UPDATE should affect exactly the 'shell' row"
    );

    // Assert the row reflects the flush outcome.
    let (last_success_ts, consecutive_failures, events_last_1h, events_last_24h): (
        Option<i64>,
        i64,
        i64,
        i64,
    ) = conn
        .query_row(
            "SELECT last_success_ts, consecutive_failures, events_last_1h, events_last_24h
             FROM source_health WHERE source = 'shell'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .unwrap();

    assert!(
        last_success_ts.is_some(),
        "last_success_ts must be set after a successful flush"
    );
    assert_eq!(
        last_success_ts.unwrap(),
        now_ms,
        "last_success_ts should equal now_ms"
    );
    assert_eq!(
        consecutive_failures, 0,
        "consecutive_failures must be cleared on success"
    );
    assert_eq!(
        events_last_1h, 3,
        "events_last_1h should reflect the 3 events flushed"
    );
    assert_eq!(
        events_last_24h, 3,
        "events_last_24h should reflect the 3 events flushed"
    );
}

/// Simulate what flush_events does after a storage error: pre-seed a row,
/// run the error UPDATE, and assert failure counters increment.
#[test]
fn source_health_error_update_increments_failure_counter() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("hippo.db");
    let conn = storage::open_db(&db_path).unwrap();

    conn.execute(
        "INSERT OR REPLACE INTO source_health (source, consecutive_failures, updated_at)
         VALUES ('browser', 0, 0)",
        [],
    )
    .expect("source_health table should exist — requires P0.1 migration");

    let now_ms: i64 = 1_700_000_000_000;
    let err_msg = "disk full".to_string();

    let rows_affected = conn
        .execute(
            "UPDATE source_health
             SET last_error_ts        = ?1,
                 last_error_msg       = ?2,
                 consecutive_failures = consecutive_failures + 1,
                 updated_at           = ?1
             WHERE source = ?3",
            rusqlite::params![now_ms, &err_msg, "browser"],
        )
        .unwrap();

    assert_eq!(
        rows_affected, 1,
        "UPDATE should affect exactly the 'browser' row"
    );

    let (consecutive_failures, last_error_msg): (i64, Option<String>) = conn
        .query_row(
            "SELECT consecutive_failures, last_error_msg FROM source_health WHERE source = 'browser'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();

    assert_eq!(
        consecutive_failures, 1,
        "first error should set consecutive_failures=1"
    );
    assert_eq!(
        last_error_msg.as_deref(),
        Some("disk full"),
        "last_error_msg should be stored"
    );
}

/// Verify the idle-tick SQL: when the event buffer is empty flush_events advances
/// last_success_ts for all known sources without touching last_heartbeat_ts.
/// Spec guarantee: "even if it processed zero events (idle tick)" (01-source-health.md:36).
/// last_heartbeat_ts is browser-only and is set only by the extension, not here.
#[test]
fn source_health_idle_tick_advances_success_ts_not_heartbeat_ts() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("hippo.db");
    let conn = storage::open_db(&db_path).unwrap();

    let now_ms: i64 = 1_700_000_000_000;

    // Run the same UPDATE SQL the idle-tick branch in flush_events uses.
    let rows_affected = conn
        .execute(
            "UPDATE source_health
             SET last_success_ts = ?1, updated_at = ?1
             WHERE source IN ('shell', 'claude-tool', 'browser')",
            rusqlite::params![now_ms],
        )
        .unwrap();

    // Migration pre-seeds 'shell', 'claude-tool', 'browser' rows.
    assert_eq!(
        rows_affected, 3,
        "idle-tick UPDATE should touch all 3 sources"
    );

    for source in &["shell", "claude-tool", "browser"] {
        let (last_heartbeat_ts, last_success_ts): (Option<i64>, Option<i64>) = conn
            .query_row(
                "SELECT last_heartbeat_ts, last_success_ts FROM source_health WHERE source = ?1",
                rusqlite::params![source],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();

        assert_eq!(
            last_success_ts,
            Some(now_ms),
            "{source}: last_success_ts must advance on idle tick"
        );
        assert_eq!(
            last_heartbeat_ts, None,
            "{source}: last_heartbeat_ts must not be touched by idle-tick flush (browser-extension-only column)"
        );
    }
}

/// Verify the pre-migration safety guarantee: when source_health is absent,
/// UPDATE returns "no such table" which production code ignores via `let _ =`.
#[test]
fn source_health_update_errors_when_table_missing() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("hippo.db");
    let conn = storage::open_db(&db_path).unwrap();

    // Drop source_health to simulate a pre-migration DB.
    conn.execute("DROP TABLE IF EXISTS source_health", [])
        .unwrap();

    // SQLite returns an error ("no such table") — not Ok(0).
    // Production code silences this with `let _ =`.
    let result = conn.execute(
        "UPDATE source_health
         SET last_success_ts = ?1, updated_at = ?1
         WHERE source IN ('shell', 'claude-tool', 'browser')",
        rusqlite::params![1_700_000_000_000_i64],
    );

    assert!(result.is_err(), "UPDATE on missing table must return Err");
}
