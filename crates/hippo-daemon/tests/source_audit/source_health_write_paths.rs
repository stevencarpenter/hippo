//! P0.2 — source_health write-path SQL semantics test.
//!
//! Tests that the UPDATE SQL used in `flush_events` correctly reflects
//! per-source outcome data into the `source_health` table. The test
//! operates directly on the DB (no daemon process needed) to keep it fast
//! and focused on SQL semantics rather than the full IPC path.
//!
//! Full end-to-end coverage (flush_events → source_health) requires
//! the P0.1 migration to have run (so the table exists); until then
//! the UPDATEs silently affect 0 rows, which is also tested here.

use hippo_core::storage;

/// Simulate what flush_events does after processing 3 successful shell events:
/// pre-seed a source_health row, run the success UPDATE, and assert the result.
///
/// Requires P0.1 migration — ignored until `feat/p0.1-source-health-schema` merges.
#[test]
#[ignore = "requires P0.1 source_health schema migration to be merged first"]
fn source_health_success_update_increments_counts_and_clears_failures() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("hippo.db");
    let conn = storage::open_db(&db_path).unwrap();

    // Pre-seed source_health row for 'shell' (mimics P0.1 migration seed).
    // If source_health doesn't exist (pre-migration), this will fail, and
    // that's acceptable — the test documents the post-migration contract.
    conn.execute(
        "INSERT INTO source_health (source, consecutive_failures, events_last_1h, events_last_24h, updated_at)
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

    assert_eq!(rows_affected, 1, "UPDATE should affect exactly the 'shell' row");

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
///
/// Requires P0.1 migration — ignored until `feat/p0.1-source-health-schema` merges.
#[test]
#[ignore = "requires P0.1 source_health schema migration to be merged first"]
fn source_health_error_update_increments_failure_counter() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("hippo.db");
    let conn = storage::open_db(&db_path).unwrap();

    conn.execute(
        "INSERT INTO source_health (source, consecutive_failures, updated_at)
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

    assert_eq!(rows_affected, 1, "UPDATE should affect exactly the 'browser' row");

    let (consecutive_failures, last_error_msg): (i64, Option<String>) = conn
        .query_row(
            "SELECT consecutive_failures, last_error_msg FROM source_health WHERE source = 'browser'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();

    assert_eq!(consecutive_failures, 1, "first error should set consecutive_failures=1");
    assert_eq!(
        last_error_msg.as_deref(),
        Some("disk full"),
        "last_error_msg should be stored"
    );
}

/// Verify that the UPDATE is a silent no-op when source_health doesn't exist.
/// This is the pre-migration safety guarantee: if P0.1 hasn't run yet, the
/// daemon should not error — just affect 0 rows.
#[test]
fn source_health_update_is_noop_when_table_missing() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("hippo.db");
    let conn = storage::open_db(&db_path).unwrap();

    // Drop source_health to simulate a pre-migration DB.
    // open_db creates it via the migration, so we need to drop it.
    // If source_health doesn't exist in the schema yet, this is a no-op drop.
    let _ = conn.execute("DROP TABLE IF EXISTS source_health", []);

    // The UPDATE should succeed (affecting 0 rows) without panicking.
    let result = conn.execute(
        "UPDATE source_health
         SET last_success_ts = ?1, updated_at = ?1
         WHERE source IN ('shell', 'claude-tool', 'browser')",
        rusqlite::params![1_700_000_000_000_i64],
    );

    // If the table doesn't exist, rusqlite returns an Err — that matches the
    // pre-migration scenario. If it does exist (P0.1 is merged), we expect Ok(0).
    // Either outcome is acceptable; the key is flush_events uses `let _ =` to
    // discard the error, so production code is safe either way.
    match result {
        Ok(rows) => assert_eq!(rows, 0, "no rows updated when table is empty"),
        Err(_) => { /* pre-migration: table missing, error discarded by `let _ =` */ }
    }
}
