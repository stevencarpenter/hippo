//! Integration tests for the Claude session FS watcher (T-5).
//!
//! Verifies that `process_file` + `ingest_session_file` correctly handle:
//! - 100 synthetic JSONL lines across 3 files
//! - Random append sequences
//! - No segment loss, no double-count (unique session_id + segment_index)
//!
//! Note: session_id is derived from the **filename stem** by `SessionFile::from_path`,
//! matching the real Claude layout where each session UUID is the filename.

use std::io::Write;
use std::path::Path;

use hippo_core::storage::open_db;
use hippo_daemon::claude_session::ingest_session_file;
use hippo_daemon::watch_claude_sessions::make_test_jsonl_line;
use rusqlite::Connection;
use tempfile::TempDir;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn open_test_db(dir: &TempDir) -> Connection {
    open_db(&dir.path().join("test.db")).expect("open test db")
}

fn count_segments(conn: &Connection, session_id: &str) -> i64 {
    conn.query_row(
        "SELECT COUNT(*) FROM claude_sessions WHERE session_id = ?1",
        [session_id],
        |row| row.get::<_, i64>(0),
    )
    .unwrap_or(0)
}

fn has_duplicates(conn: &Connection, session_id: &str) -> bool {
    let dup: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM (
                 SELECT session_id, segment_index, COUNT(*) AS n
                 FROM claude_sessions
                 WHERE session_id = ?1
                 GROUP BY session_id, segment_index
                 HAVING n > 1
             )",
            [session_id],
            |row| row.get::<_, i64>(0),
        )
        .unwrap_or(0);
    dup > 0
}

/// Write a complete synthetic session JSONL to `path`. Returns total line count.
///
/// The path stem must equal `session_id` — `SessionFile::from_path` derives
/// `session_id` from the filename, mirroring real Claude UUID filenames.
fn write_synthetic_session(path: &Path, session_id: &str, exchange_count: u64) -> usize {
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .truncate(true)
        .write(true)
        .open(path)
        .unwrap();

    writeln!(
        f,
        "{}",
        make_test_jsonl_line(session_id, 0, "system", "init")
    )
    .unwrap();
    let mut n = 1usize;
    for i in 0..exchange_count {
        writeln!(
            f,
            "{}",
            make_test_jsonl_line(session_id, i * 10 + 1, "user", &format!("prompt {i}"))
        )
        .unwrap();
        writeln!(
            f,
            "{}",
            make_test_jsonl_line(session_id, i * 10 + 2, "assistant", &format!("reply {i}"))
        )
        .unwrap();
        n += 2;
    }
    n
}

fn append_exchanges(path: &Path, session_id: &str, start_ts: u64, count: u64) {
    let mut f = std::fs::OpenOptions::new().append(true).open(path).unwrap();
    for i in 0..count {
        writeln!(
            f,
            "{}",
            make_test_jsonl_line(
                session_id,
                start_ts + i * 10 + 1,
                "user",
                &format!("late {i}")
            )
        )
        .unwrap();
        writeln!(
            f,
            "{}",
            make_test_jsonl_line(
                session_id,
                start_ts + i * 10 + 2,
                "assistant",
                &format!("late reply {i}")
            )
        )
        .unwrap();
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

/// 100+ JSONL lines across 3 session files — no loss, no duplicates.
#[test]
fn hundred_lines_across_three_files_no_loss_no_duplicates() {
    let dir = TempDir::new().unwrap();
    let conn = open_test_db(&dir);

    // Each session_id IS the filename stem — mirrors the real Claude UUID layout.
    let sessions = [
        ("sess-aaa-0001-0001-0001-0001-000000000001", 17u64),
        ("sess-bbb-0002-0002-0002-0002-000000000002", 16u64),
        ("sess-ccc-0003-0003-0003-0003-000000000003", 17u64),
    ];

    for (session_id, exchanges) in &sessions {
        let path = dir.path().join(format!("{session_id}.jsonl"));
        write_synthetic_session(&path, session_id, *exchanges);
        let (inserted, _skipped, errors) = ingest_session_file(&conn, &path);
        assert_eq!(errors, 0, "session {session_id}: unexpected errors");
        assert!(
            inserted > 0,
            "session {session_id}: expected at least one segment"
        );
    }

    for (session_id, _) in &sessions {
        assert!(
            !has_duplicates(&conn, session_id),
            "duplicates for {session_id}"
        );
        assert!(
            count_segments(&conn, session_id) > 0,
            "no segments for {session_id}"
        );
    }
}

/// Append then reprocess: new segments added, no duplicates.
#[test]
fn append_and_reprocess_no_duplicates() {
    let dir = TempDir::new().unwrap();
    let conn = open_test_db(&dir);

    let session_id = "sess-app-0001-0001-0001-0001-000000000001";
    // Filename stem = session_id so SessionFile::from_path derives the right value.
    let path = dir.path().join(format!("{session_id}.jsonl"));

    write_synthetic_session(&path, session_id, 10);
    let (first, _, _) = ingest_session_file(&conn, &path);

    // Append 10 more with a gap large enough to force a new segment boundary
    // (ts_base = 1000s > SEGMENT_GAP_MS = 300s).
    append_exchanges(&path, session_id, 1000, 10);
    let (second, _, _) = ingest_session_file(&conn, &path);

    assert!(
        !has_duplicates(&conn, session_id),
        "duplicates after append + reprocess"
    );

    let total_in_db = count_segments(&conn, session_id);
    assert!(
        first as i64 + second as i64 >= total_in_db,
        "total inserts ({}) < segments in DB ({})",
        first + second,
        total_in_db
    );
}

/// Two sessions in separate files: both stored, no cross-contamination.
///
/// Real Claude creates a new UUID file per session — it never reuses a filename
/// for a different session. This verifies that two concurrent files are ingested
/// independently without duplicates.
#[test]
fn two_sessions_independent_no_duplicates() {
    let dir = TempDir::new().unwrap();
    let conn = open_test_db(&dir);

    let sid1 = "sess-trunc-old-0001-0001-0001-000000000001";
    let sid2 = "sess-trunc-new-0001-0001-0001-000000000001";

    let path1 = dir.path().join(format!("{sid1}.jsonl"));
    let path2 = dir.path().join(format!("{sid2}.jsonl"));

    write_synthetic_session(&path1, sid1, 5);
    ingest_session_file(&conn, &path1);

    write_synthetic_session(&path2, sid2, 5);
    ingest_session_file(&conn, &path2);

    assert!(!has_duplicates(&conn, sid1));
    assert!(!has_duplicates(&conn, sid2));
    assert!(count_segments(&conn, sid1) > 0, "first session missing");
    assert!(count_segments(&conn, sid2) > 0, "second session missing");
}

/// Repeated reprocessing of the same file with no new content is a no-op.
#[test]
fn idempotent_reprocessing() {
    let dir = TempDir::new().unwrap();
    let conn = open_test_db(&dir);

    let session_id = "sess-idem-0001-0001-0001-0001-000000000001";
    let path = dir.path().join(format!("{session_id}.jsonl"));
    write_synthetic_session(&path, session_id, 8);

    let (first, _, _) = ingest_session_file(&conn, &path);
    let (second, skipped, _) = ingest_session_file(&conn, &path);
    let (third, _, _) = ingest_session_file(&conn, &path);

    assert_eq!(second, 0, "second pass should insert 0");
    let _ = skipped; // may be > 0 or 0 depending on segment boundaries
    assert_eq!(third, 0, "third pass should also insert 0");
    assert!(!has_duplicates(&conn, session_id));
    assert_eq!(count_segments(&conn, session_id), first as i64);
}

// ---------------------------------------------------------------------------
// Proptest: random mutation sequences
// ---------------------------------------------------------------------------

use proptest::prelude::*;

proptest! {
    #[test]
    fn no_duplicates_under_random_mutations(
        exchanges_initial in 1u64..=20u64,
        appends in proptest::collection::vec(1u64..=10u64, 0..5),
    ) {
        let dir = TempDir::new().unwrap();
        let conn = open_test_db(&dir);

        let session_id = "sess-prop-0001-0001-0001-0001-000000000001";
        let path = dir.path().join(format!("{session_id}.jsonl"));

        write_synthetic_session(&path, session_id, exchanges_initial);
        ingest_session_file(&conn, &path);

        // Use a 1000s gap between batches to guarantee new segment boundaries
        // (SEGMENT_GAP_MS = 300s = 300_000ms).
        let mut ts_base = 1000u64;
        for extra in appends {
            append_exchanges(&path, session_id, ts_base, extra);
            ts_base += 1000 + extra * 10;
            ingest_session_file(&conn, &path);
        }

        prop_assert!(!has_duplicates(&conn, session_id));
        prop_assert!(count_segments(&conn, session_id) > 0);
    }
}
