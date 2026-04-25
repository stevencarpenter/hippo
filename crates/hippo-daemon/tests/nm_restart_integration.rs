//! Regression guard for capture-reliability F-7 (issue #51).
//!
//! Failure mode: the daemon restarts while the Firefox extension is sending
//! a browser visit via Native Messaging. The extension's connection drops;
//! the event is silently lost because NM is a best-effort transport and
//! there is no client-side retry or on-disk queue on the Firefox side.
//!
//! The defence in depth is: every inbound NM message that fails to land in
//! `browser_events` (because the daemon socket is down) gets written to
//! `fallback/` as JSONL. When the daemon comes back up, fallback drain
//! replays the file into SQLite. That path is the load-bearing one — if it
//! ever regresses, browser capture silently loses every event that arrived
//! during a restart window.
//!
//! This test file covers one concrete slice of that contract — fallback
//! files written during a down-daemon window are drained successfully when
//! the daemon comes back up. End-to-end NM-across-restart (spawning a real
//! `hippo native-messaging-host` subprocess, bouncing the daemon
//! mid-write) is `#[ignore]` because it requires harness plumbing that
//! does not exist on `main` today.
//!
//! Tracking: docs/capture-reliability/09-test-matrix.md row F-7.

use std::collections::HashMap;
use std::fs;

use chrono::{TimeZone, Utc};
use hippo_core::config::HippoConfig;
use hippo_core::events::{BrowserEvent, EventEnvelope, EventPayload};
use hippo_core::storage;
use tempfile::TempDir;
use uuid::Uuid;

fn new_config() -> (HippoConfig, TempDir) {
    let temp = tempfile::tempdir().unwrap();
    let mut config = HippoConfig::default();
    config.storage.data_dir = temp.path().join("data");
    config.storage.config_dir = temp.path().join("config");
    fs::create_dir_all(config.fallback_dir()).unwrap();
    (config, temp)
}

fn make_browser_envelope(url: &str, ts_ms: i64) -> EventEnvelope {
    EventEnvelope {
        envelope_id: Uuid::new_v4(),
        producer_version: 1,
        timestamp: Utc.timestamp_millis_opt(ts_ms).single().unwrap(),
        payload: EventPayload::Browser(Box::new(BrowserEvent {
            url: url.to_string(),
            title: String::new(),
            domain: "example.com".to_string(),
            dwell_ms: 1000,
            scroll_depth: 0.0,
            extracted_text: None,
            search_query: None,
            referrer: None,
            content_hash: None,
        })),
        probe_tag: None,
    }
}

/// Ground truth: if the fallback path accepts a browser event while the
/// daemon is down, then a fresh daemon-side drain recovers it into
/// `browser_events`. This is the "silent loss" defence for F-7.
#[test]
fn fallback_jsonl_survives_daemon_restart_and_drains_browser_events() {
    let (config, _keep) = new_config();

    // Simulate "daemon was down, NM bridge wrote to fallback instead". The
    // NM handler uses storage::write_fallback_jsonl under the hood; we
    // call it directly to isolate from socket/runtime setup.
    let envelope = make_browser_envelope("https://example.com/a", 1_000_000);
    storage::write_fallback_jsonl(&config.fallback_dir(), &envelope).unwrap();

    let files = storage::list_fallback_files(&config.fallback_dir()).unwrap();
    assert_eq!(files.len(), 1, "fallback file must exist after write");

    // Simulate daemon restart: fresh SQLite connection, fallback drain.
    let conn = storage::open_db(&config.db_path()).unwrap();
    let mut session_map: HashMap<String, i64> = HashMap::new();
    let (recovered, errors) =
        storage::recover_fallback_files(&conn, &config.fallback_dir(), &mut session_map).unwrap();
    assert_eq!(errors, 0, "fallback drain must not error");
    assert_eq!(
        recovered, 1,
        "daemon restart must drain the single queued browser event"
    );

    let count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM browser_events WHERE url = ?",
            ["https://example.com/a"],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(
        count, 1,
        "the recovered envelope must land in browser_events after restart"
    );
}

/// Three events queued before restart, all must land. Guards against "drain
/// stops after first event" (a silent-swallow hazard — see AP-11 / F-17).
#[test]
fn fallback_drain_recovers_multiple_browser_events_across_restart() {
    let (config, _keep) = new_config();

    for (i, url) in [
        "https://a.example",
        "https://b.example",
        "https://c.example",
    ]
    .iter()
    .enumerate()
    {
        let envelope = make_browser_envelope(url, 1_000_000 + i as i64);
        storage::write_fallback_jsonl(&config.fallback_dir(), &envelope).unwrap();
    }

    let conn = storage::open_db(&config.db_path()).unwrap();
    let mut session_map: HashMap<String, i64> = HashMap::new();
    let (recovered, errors) =
        storage::recover_fallback_files(&conn, &config.fallback_dir(), &mut session_map).unwrap();
    assert_eq!(errors, 0);
    assert_eq!(
        recovered, 3,
        "all three events must recover; none silently dropped"
    );

    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM browser_events", [], |row| row.get(0))
        .unwrap();
    assert_eq!(count, 3);
}

/// After successful drain, the fallback file is renamed `.jsonl.done` — NOT
/// deleted and NOT left at `.jsonl`. Guards the "did we actually drain?"
/// invariant: a residual `.jsonl` file means drain didn't run.
#[test]
fn fallback_file_is_renamed_done_after_successful_drain() {
    let (config, _keep) = new_config();

    let envelope = make_browser_envelope("https://example.com/done", 2_000_000);
    storage::write_fallback_jsonl(&config.fallback_dir(), &envelope).unwrap();

    let conn = storage::open_db(&config.db_path()).unwrap();
    let mut session_map: HashMap<String, i64> = HashMap::new();
    let (recovered, _errors) =
        storage::recover_fallback_files(&conn, &config.fallback_dir(), &mut session_map).unwrap();
    assert_eq!(recovered, 1);

    // After drain: zero .jsonl files, one .jsonl.done file.
    let active = storage::list_fallback_files(&config.fallback_dir()).unwrap();
    assert_eq!(
        active.len(),
        0,
        ".jsonl files must be renamed after successful drain"
    );

    let done_count = fs::read_dir(config.fallback_dir())
        .unwrap()
        .filter_map(|e| e.ok())
        .filter(|e| e.path().to_string_lossy().ends_with(".jsonl.done"))
        .count();
    assert_eq!(
        done_count, 1,
        "exactly one .jsonl.done sentinel must exist after drain"
    );
}

#[test]
#[ignore = "blocked on test harness — needs a NM-stdio stream driver that can \
            survive a mid-stream daemon restart. When P0 (source_health writes) \
            or P2 (synthetic probes) land, wire this up through a real \
            `hippo native-messaging-host` subprocess rather than calling \
            write_fallback_jsonl directly."]
fn nm_stdio_across_daemon_restart_loses_no_events() {
    // Intended shape:
    //   1. Spawn `hippo native-messaging-host` as a subprocess piped to
    //      stdin/stdout.
    //   2. Start an in-process daemon bound to a temp socket.
    //   3. Write a BrowserVisit to the NM subprocess stdin; assert it
    //      lands in browser_events via the socket path.
    //   4. Kill + respawn the daemon.
    //   5. Write a second BrowserVisit; assert it lands — whether via
    //      restored socket or via fallback drain on next flush.
    //   6. Assert NO events were silently swallowed (count == writes).
    unimplemented!("remove #[ignore] once an NM-stdio test harness exists");
}
