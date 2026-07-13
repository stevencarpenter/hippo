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
//! This test file covers both the fallback drain in isolation and the full
//! Native Messaging stdio path across a real daemon restart.
//!
//! Tracking: docs/capture/test-matrix.md row F-7.

use std::collections::HashMap;
use std::fs;
use std::io::{Read, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::thread;
use std::time::{Duration, Instant};

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

struct ChildGuard(Child);

impl ChildGuard {
    fn stop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

impl Drop for ChildGuard {
    fn drop(&mut self) {
        self.stop();
    }
}

fn hippo_command(temp: &TempDir) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_hippo"));
    command
        .env("XDG_DATA_HOME", temp.path().join("xdg-data"))
        .env("XDG_CONFIG_HOME", temp.path().join("xdg-config"))
        .env("RUST_LOG", "warn");
    command
}

fn process_config(temp: &TempDir) -> HippoConfig {
    let mut config = HippoConfig::default();
    config.storage.data_dir = temp.path().join("xdg-data/hippo");
    config.storage.config_dir = temp.path().join("xdg-config/hippo");
    config
}

fn spawn_daemon(temp: &TempDir) -> ChildGuard {
    let mut command = hippo_command(temp);
    let child = command
        .args(["daemon", "run"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn daemon");
    ChildGuard(child)
}

fn spawn_native_host(temp: &TempDir) -> (ChildGuard, ChildStdin, Receiver<serde_json::Value>) {
    let mut command = hippo_command(temp);
    let mut child = command
        .arg("native-messaging-host")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn native messaging host");
    let stdin = child.stdin.take().expect("native host stdin");
    let mut stdout = child.stdout.take().expect("native host stdout");
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        loop {
            let mut len_buf = [0u8; 4];
            if stdout.read_exact(&mut len_buf).is_err() {
                break;
            }
            let len = u32::from_ne_bytes(len_buf) as usize;
            let mut payload = vec![0u8; len];
            if stdout.read_exact(&mut payload).is_err() {
                break;
            }
            let Ok(response) = serde_json::from_slice(&payload) else {
                break;
            };
            if tx.send(response).is_err() {
                break;
            }
        }
    });
    (ChildGuard(child), stdin, rx)
}

fn send_visit(stdin: &mut ChildStdin, url: &str, timestamp: i64) {
    let payload = serde_json::to_vec(&serde_json::json!({
        "url": url,
        "title": "restart integration",
        "domain": "docs.rs",
        "dwell_ms": 1_000,
        "scroll_depth": 0.5,
        "extracted_text": null,
        "search_query": null,
        "referrer": null,
        "timestamp": timestamp
    }))
    .unwrap();
    stdin
        .write_all(&(payload.len() as u32).to_ne_bytes())
        .unwrap();
    stdin.write_all(&payload).unwrap();
    stdin.flush().unwrap();
}

fn wait_until(timeout: Duration, mut condition: impl FnMut() -> bool) {
    let deadline = Instant::now() + timeout;
    while !condition() {
        assert!(
            Instant::now() < deadline,
            "condition timed out after {timeout:?}"
        );
        thread::sleep(Duration::from_millis(25));
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

/// Generous upper bound for every readiness gate in the restart test. These
/// are ceilings, not sleeps — a healthy machine satisfies each condition in
/// milliseconds and never waits this long. Sizing it well above real latency
/// keeps the test from flaking when a loaded CI runner is slow to spawn a
/// subprocess, bind a socket, or flush a batch.
const READY: Duration = Duration::from_secs(30);

#[test]
fn nm_stdio_across_daemon_restart_loses_no_events() {
    let temp = tempfile::tempdir().unwrap();
    let config = process_config(&temp);
    let mut daemon = spawn_daemon(&temp);
    wait_until(READY, || config.socket_path().exists());

    let (mut native_host, mut stdin, responses) = spawn_native_host(&temp);
    let first_url = "https://docs.rs/hippo-restart-first";
    send_visit(&mut stdin, first_url, 1_700_000_000_000);
    let response = responses
        .recv_timeout(READY)
        .expect("native host response while daemon is up");
    assert_eq!(response["status"], "ok");

    wait_until(READY, || {
        let Ok(conn) = storage::open_db(&config.db_path()) else {
            return false;
        };
        conn.query_row(
            "SELECT COUNT(*) FROM browser_events WHERE url = ?1",
            [first_url],
            |row| row.get::<_, i64>(0),
        )
        .unwrap_or(0)
            == 1
    });

    daemon.stop();
    // SIGKILL leaves the Unix socket file on disk. Remove it before the second
    // visit so (a) the NM host gets a clean connection refusal and falls back,
    // and (b) the post-restart `socket_path().exists()` gate waits for the NEW
    // daemon to bind rather than passing instantly on the stale file. The
    // daemon is already stopped, so there is no race with a live listener.
    let _ = std::fs::remove_file(config.socket_path());
    let second_url = "https://docs.rs/hippo-restart-fallback";
    send_visit(&mut stdin, second_url, 1_700_000_000_001);
    let response = responses
        .recv_timeout(READY)
        .expect("native host response while daemon is down");
    assert_eq!(
        response["status"], "ok",
        "durably queued fallback must be acknowledged"
    );
    wait_until(READY, || {
        storage::list_fallback_files(&config.fallback_dir()).is_ok_and(|files| files.len() == 1)
    });

    let mut restarted_daemon = spawn_daemon(&temp);
    wait_until(READY, || config.socket_path().exists());
    wait_until(READY, || {
        let Ok(conn) = storage::open_db(&config.db_path()) else {
            return false;
        };
        conn.query_row("SELECT COUNT(*) FROM browser_events", [], |row| {
            row.get::<_, i64>(0)
        })
        .unwrap_or(0)
            == 2
    });

    let third_url = "https://docs.rs/hippo-restart-after";
    send_visit(&mut stdin, third_url, 1_700_000_000_002);
    let response = responses
        .recv_timeout(READY)
        .expect("native host response after daemon restart");
    assert_eq!(response["status"], "ok");
    wait_until(READY, || {
        let Ok(conn) = storage::open_db(&config.db_path()) else {
            return false;
        };
        conn.query_row("SELECT COUNT(*) FROM browser_events", [], |row| {
            row.get::<_, i64>(0)
        })
        .unwrap_or(0)
            == 3
    });

    assert!(
        storage::list_fallback_files(&config.fallback_dir())
            .unwrap()
            .is_empty(),
        "restart must drain the queued fallback file"
    );

    restarted_daemon.stop();
    native_host.stop();
}
