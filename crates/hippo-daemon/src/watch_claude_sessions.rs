//! Persistent FSEvents watcher for Claude session JSONL files.
//!
//! Subscribes to `~/.claude/projects/**/*.jsonl` via `notify`/FSEvents and
//! re-extracts session segments whenever a file grows.  Runs as a long-lived
//! launchd service (`com.hippo.claude-session-watcher`, KeepAlive=true).
//!
//! Key invariants:
//! - Full-file reparse on every FSEvents notification; `INSERT OR IGNORE` on
//!   `(session_id, segment_index)` makes repeated processing idempotent.
//!   `size_at_last_read` is compared to current file size to skip no-op wakeups.
//!   `byte_offset` is stored as `current_size` (matching `size_at_last_read`) so a
//!   future seek-based optimisation can use it without a schema change.
//! - Resets offset on inode/device change (file replaced) or size regression (truncated).
//! - Writes `source_health WHERE source='claude-session-watcher'` every 30 s.

use std::collections::HashMap;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use chrono::Utc;
use notify::{
    Config as NotifyConfig, Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher,
};
use rusqlite::{Connection, params};
use tokio::signal::unix::{SignalKind, signal as unix_signal};
use tokio::sync::mpsc;
use tracing::{debug, info, warn};

use hippo_core::config::HippoConfig;
use hippo_core::storage::open_db;

use crate::claude_session::ingest_session_file;
use crate::is_missing_source_health_table_error;

const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(30);
const BACKOFF_DURATION: Duration = Duration::from_secs(60);
const PER_FILE_TIMEOUT: Duration = Duration::from_secs(30);

/// Per-file tracking state (in memory; persisted to `claude_session_offsets`).
#[derive(Default)]
struct FileState {
    byte_offset: u64,
    inode: u64,
    device: u64,
    size_at_last_read: u64,
    /// When to retry after a processing timeout.
    cooldown_until: Option<Instant>,
}

/// Load all saved offsets from `claude_session_offsets` into memory.
fn load_offsets(conn: &Connection) -> Result<HashMap<PathBuf, FileState>> {
    let mut stmt = conn.prepare(
        "SELECT path, byte_offset, inode, device, size_at_last_read
         FROM claude_session_offsets",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok((
            PathBuf::from(row.get::<_, String>(0)?),
            row.get::<_, i64>(1).unwrap_or(0) as u64,
            row.get::<_, i64>(2).unwrap_or(0) as u64,
            row.get::<_, i64>(3).unwrap_or(0) as u64,
            row.get::<_, i64>(4).unwrap_or(0) as u64,
        ))
    })?;

    let mut map = HashMap::new();
    for row in rows {
        let (path, offset, inode, device, size) = row?;
        map.insert(
            path,
            FileState {
                byte_offset: offset,
                inode,
                device,
                size_at_last_read: size,
                cooldown_until: None,
            },
        );
    }
    Ok(map)
}

/// Persist a file's offset to `claude_session_offsets`.
fn save_offset(conn: &Connection, path: &Path, state: &FileState) -> Result<()> {
    let now_ms = Utc::now().timestamp_millis();
    let session_id = path.file_stem().and_then(|s| s.to_str()).unwrap_or("");
    conn.execute(
        "INSERT INTO claude_session_offsets
             (path, session_id, byte_offset, inode, device, size_at_last_read, updated_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
         ON CONFLICT(path) DO UPDATE SET
             session_id        = excluded.session_id,
             byte_offset       = excluded.byte_offset,
             inode             = excluded.inode,
             device            = excluded.device,
             size_at_last_read = excluded.size_at_last_read,
             updated_at        = excluded.updated_at",
        params![
            path.to_string_lossy(),
            session_id,
            state.byte_offset as i64,
            state.inode as i64,
            state.device as i64,
            state.size_at_last_read as i64,
            now_ms,
        ],
    )?;
    Ok(())
}

/// Process new content in a single file.  Returns the number of segments inserted.
///
/// Runs `ingest_session_file` in a `spawn_blocking` task so the async executor
/// is never stalled by SQLite I/O.  `PER_FILE_TIMEOUT` is enforced as a hard
/// upper bound; a timeout puts the file into cooldown just as a parsing error would.
async fn process_file(path: &Path, state: &mut FileState, db_path: &Path) -> Result<usize> {
    if let Some(until) = state.cooldown_until {
        if Instant::now() < until {
            return Ok(0);
        }
        state.cooldown_until = None;
    }

    let meta = match std::fs::metadata(path) {
        Ok(m) => m,
        Err(e) => {
            warn!(path = %path.display(), %e, "watcher: cannot stat file");
            return Ok(0);
        }
    };

    let current_size = meta.len();
    let current_inode = meta.ino();
    let current_device = meta.dev();

    // Detect file replacement (inode or device changed).
    if state.inode != 0 && (current_inode != state.inode || current_device != state.device) {
        info!(
            path = %path.display(),
            old_inode = state.inode,
            new_inode = current_inode,
            "watcher: file replaced, resetting offset"
        );
        state.byte_offset = 0;
        state.size_at_last_read = 0;
    }
    state.inode = current_inode;
    state.device = current_device;

    // Detect truncation.
    if current_size < state.byte_offset {
        warn!(
            path = %path.display(),
            prev_offset = state.byte_offset,
            current_size,
            "watcher: file truncated, resetting offset"
        );
        state.byte_offset = 0;
        state.size_at_last_read = 0;
    }

    // No new complete content.  We use size_at_last_read (not byte_offset) to
    // detect growth because extract_segments/reader.lines() stops before an
    // unterminated final line — so byte_offset can equal size_at_last_read
    // even when the raw file is slightly larger.
    if current_size <= state.size_at_last_read {
        return Ok(0);
    }

    let start = Instant::now();
    let path_owned = path.to_path_buf();
    let db_path_owned = db_path.to_path_buf();

    let task = tokio::task::spawn_blocking(move || -> Result<(usize, usize, usize)> {
        let conn = open_db(&db_path_owned)?;
        let (inserted, skipped, errors) = ingest_session_file(&conn, &path_owned);
        if errors == 0 {
            let snap = FileState {
                byte_offset: current_size,
                inode: current_inode,
                device: current_device,
                size_at_last_read: current_size,
                ..Default::default()
            };
            save_offset(&conn, &path_owned, &snap).unwrap_or_else(|e| {
                warn!(path = %path_owned.display(), %e, "watcher: failed to save offset");
            });
        }
        Ok((inserted, skipped, errors))
    });

    let join_result = match tokio::time::timeout(PER_FILE_TIMEOUT, task).await {
        Err(_elapsed) => {
            warn!(
                path = %path.display(),
                timeout_secs = PER_FILE_TIMEOUT.as_secs(),
                "watcher: per-file processing timed out; entering cooldown"
            );
            state.cooldown_until = Some(Instant::now() + BACKOFF_DURATION);
            return Ok(0);
        }
        Ok(r) => r,
    };

    let elapsed_ms = start.elapsed().as_secs_f64() * 1000.0;

    #[cfg(feature = "otel")]
    crate::metrics::WATCHER_PROCESS_DURATION_MS.record(elapsed_ms, &[]);

    let (inserted, _skipped, errors) = match join_result {
        Err(join_err) => {
            warn!(path = %path.display(), %join_err, "watcher: spawn_blocking task panicked");
            state.cooldown_until = Some(Instant::now() + BACKOFF_DURATION);
            return Ok(0);
        }
        Ok(Err(e)) => {
            warn!(path = %path.display(), %e, "watcher: failed to open DB in blocking task");
            state.cooldown_until = Some(Instant::now() + BACKOFF_DURATION);
            return Ok(0);
        }
        Ok(Ok(triple)) => triple,
    };

    if errors > 0 {
        state.cooldown_until = Some(Instant::now() + BACKOFF_DURATION);
    } else {
        // Advance offset past the content we just processed.  `extract_segments`
        // uses `reader.lines()` which stops before an unterminated line, so we
        // record the raw file size; on the next pass the comparison
        // `current_size <= size_at_last_read` will hold until more bytes arrive.
        state.byte_offset = current_size;
        state.size_at_last_read = current_size;
        #[cfg(feature = "otel")]
        if inserted > 0 {
            crate::metrics::WATCHER_SEGMENTS_INGESTED.add(inserted as u64, &[]);
        }
    }

    Ok(inserted)
}

/// Walk `~/.claude/projects/**/*.jsonl` recursively (catches subagent files too).
fn find_session_files(projects_dir: &Path) -> Vec<PathBuf> {
    walkdir::WalkDir::new(projects_dir)
        .follow_links(false)
        .into_iter()
        .flatten()
        .filter(|e| {
            e.file_type().is_file() && e.path().extension().is_some_and(|ext| ext == "jsonl")
        })
        .map(|e| e.into_path())
        .collect()
}

/// Upsert the watcher's own heartbeat in `source_health`.
fn upsert_heartbeat(conn: &Connection) {
    let now_ms = Utc::now().timestamp_millis();
    let res = conn.execute(
        "INSERT INTO source_health (source, last_success_ts, updated_at)
         VALUES ('claude-session-watcher', ?1, ?1)
         ON CONFLICT(source) DO UPDATE SET
             last_success_ts = excluded.last_success_ts,
             updated_at      = excluded.updated_at",
        params![now_ms],
    );
    match res {
        Err(e) if !is_missing_source_health_table_error(&e) => {
            warn!(%e, "watcher: heartbeat upsert failed");
        }
        _ => {}
    }
}

/// Check whether `path` lives on a remote filesystem (NFS, SMB, etc.) and log
/// a warning.  FSEvents may not fire reliably for remote volumes.
/// `statfs::f_fstypename` is macOS-specific; this is a no-op on other platforms.
#[cfg(target_os = "macos")]
fn warn_if_remote_fs(path: &Path) {
    use std::ffi::{CStr, CString};

    let Ok(c_path) = CString::new(path.to_string_lossy().as_bytes()) else {
        return;
    };
    let mut buf: libc::statfs = unsafe { std::mem::zeroed() };
    if unsafe { libc::statfs(c_path.as_ptr(), &mut buf) } != 0 {
        return;
    }
    let fs_type = unsafe { CStr::from_ptr(buf.f_fstypename.as_ptr()) }
        .to_string_lossy()
        .into_owned();
    if matches!(fs_type.as_str(), "nfs" | "smbfs" | "cifs" | "ntfs") {
        warn!(
            path = %path.display(),
            fs_type,
            "watcher: projects dir is on a remote filesystem; FSEvents may miss events"
        );
    }
}

#[cfg(not(target_os = "macos"))]
fn warn_if_remote_fs(_path: &Path) {}

/// Return the path to `~/.claude/projects/`.
fn projects_dir() -> Option<PathBuf> {
    dirs::home_dir().map(|h| h.join(".claude").join("projects"))
}

/// Entry point — runs until SIGTERM/ctrl-c.
pub async fn run(config: &HippoConfig) -> Result<()> {
    let db_path = config.db_path();

    let projects = match projects_dir() {
        Some(p) => p,
        None => {
            warn!("watcher: cannot determine home directory; exiting");
            return Ok(());
        }
    };

    warn_if_remote_fs(&projects);

    // Open our own write connection (WAL, separate from the daemon).
    let conn = open_db(&db_path).context("watcher: failed to open DB")?;

    // Load saved offsets and build initial state map.
    let mut states: HashMap<PathBuf, FileState> = load_offsets(&conn).unwrap_or_default();

    // Startup scan — catch up on any content written while we were down.
    let initial_files = find_session_files(&projects);
    info!(count = initial_files.len(), "watcher: startup scan");
    for path in &initial_files {
        let state = states.entry(path.clone()).or_default();
        match process_file(path, state, &db_path).await {
            Ok(n) if n > 0 => {
                info!(path = %path.display(), inserted = n, "watcher: startup catch-up");
            }
            _ => {}
        }
    }

    upsert_heartbeat(&conn);

    // Set up FSEvents subscription.
    let (tx, mut rx) = mpsc::channel::<Event>(256);
    let mut watcher = RecommendedWatcher::new(
        move |event: notify::Result<Event>| {
            if let Ok(e) = event {
                // try_send never blocks the notify thread; Full drops events (the full-file
                // reparse on the next write catches anything missed); Closed means shutdown.
                if let Err(tokio::sync::mpsc::error::TrySendError::Full(_)) = tx.try_send(e) {
                    debug!("watcher: FSEvents channel full; event dropped");
                    #[cfg(feature = "otel")]
                    crate::metrics::WATCHER_EVENTS_DROPPED.add(1, &[]);
                }
            }
        },
        NotifyConfig::default(),
    )
    .context("watcher: failed to create FSEvents watcher")?;

    // Watch `projects` if it exists; otherwise walk up to the nearest ancestor that
    // does exist (typically `~/.claude`).  This handles fresh installs where
    // `~/.claude/projects` has not yet been created by Claude — FSEvents on the
    // ancestor will fire when the subdirectory and its files appear.
    let watch_root = {
        let mut p = projects.clone();
        while !p.exists() {
            match p.parent() {
                Some(parent) => p = parent.to_path_buf(),
                None => break,
            }
        }
        p
    };
    if watch_root.exists() {
        if watch_root != projects {
            warn!(
                projects = %projects.display(),
                watching = %watch_root.display(),
                "watcher: projects directory does not exist; watching ancestor instead"
            );
        }
        watcher
            .watch(&watch_root, RecursiveMode::Recursive)
            .with_context(|| format!("watcher: failed to watch {}", watch_root.display()))?;
    } else {
        warn!("watcher: no watchable directory found; no files will be watched");
    }

    info!(
        path = %projects.display(),
        "watcher: listening for FSEvents"
    );

    let mut heartbeat_tick = tokio::time::interval(HEARTBEAT_INTERVAL);

    // Handle both SIGTERM (launchd stop / system shutdown) and SIGINT (ctrl-c).
    let mut sigterm = unix_signal(SignalKind::terminate())
        .context("watcher: failed to install SIGTERM handler")?;

    loop {
        tokio::select! {
            _ = sigterm.recv() => {
                info!("watcher: received SIGTERM, shutting down");
                break;
            }
            _ = tokio::signal::ctrl_c() => {
                info!("watcher: received SIGINT, shutting down");
                break;
            }

            _ = heartbeat_tick.tick() => {
                upsert_heartbeat(&conn);
            }

            Some(event) = rx.recv() => {
                let is_relevant = matches!(
                    event.kind,
                    EventKind::Create(_) | EventKind::Modify(_)
                );
                if !is_relevant {
                    continue;
                }

                for path in event.paths {
                    if path.extension().is_none_or(|e| e != "jsonl") {
                        continue;
                    }
                    let state = states.entry(path.clone()).or_default();
                    match process_file(&path, state, &db_path).await {
                        Ok(0) => {}
                        Ok(n) => {
                            info!(path = %path.display(), inserted = n, "watcher: ingested segments");
                        }
                        Err(e) => {
                            warn!(path = %path.display(), %e, "watcher: processing error");
                        }
                    }
                }
            }
        }
    }

    Ok(())
}

/// Generate a minimal Claude JSONL line for testing.
/// Always public so integration tests in `tests/` can import it.
pub fn make_test_jsonl_line(
    session_id: &str,
    ts_offset_secs: u64,
    msg_type: &str,
    text: &str,
) -> String {
    let ts = chrono::DateTime::from_timestamp((1_700_000_000 + ts_offset_secs) as i64, 0)
        .unwrap()
        .format("%Y-%m-%dT%H:%M:%S%.3fZ")
        .to_string();
    match msg_type {
        "system" => format!(
            r#"{{"type":"system","timestamp":"{ts}","sessionId":"{session_id}","cwd":"/tmp/test","message":{{"role":"system","content":[{{"type":"text","text":"{text}"}}]}}}}"#
        ),
        "user" => format!(
            r#"{{"type":"user","timestamp":"{ts}","sessionId":"{session_id}","cwd":"/tmp/test","message":{{"role":"user","content":[{{"type":"text","text":"{text}"}}]}}}}"#
        ),
        "assistant" => format!(
            r#"{{"type":"assistant","timestamp":"{ts}","sessionId":"{session_id}","cwd":"/tmp/test","message":{{"role":"assistant","content":[{{"type":"text","text":"{text}"}}]}}}}"#
        ),
        _ => panic!("unknown msg_type: {msg_type}"),
    }
}

// ---------------------------------------------------------------------------
// Unit tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use hippo_core::storage::open_db;
    use tempfile::TempDir;

    fn open_test_db(dir: &TempDir) -> Connection {
        open_db(&dir.path().join("test.db")).expect("open test db")
    }

    fn j(session_id: &str, ts: u64, kind: &str, text: &str) -> String {
        make_test_jsonl_line(session_id, ts, kind, text)
    }

    #[tokio::test]
    async fn process_file_inserts_segments() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("test.db");
        open_db(&db_path).expect("init test db");

        // Write a session with one complete exchange.
        let session_id = "test-sess-0001-0001-0001-0001-000000000001";
        let jsonl_path = dir.path().join(format!("{session_id}.jsonl"));
        let content = [
            j(session_id, 0, "system", "init"),
            j(session_id, 1, "user", "hello"),
            j(session_id, 2, "assistant", "hi there"),
        ]
        .join("\n")
            + "\n";
        std::fs::write(&jsonl_path, &content).unwrap();

        let mut state = FileState {
            ..Default::default()
        };
        let inserted = process_file(&jsonl_path, &mut state, &db_path)
            .await
            .unwrap();

        assert!(inserted > 0, "expected at least one segment inserted");
        assert_eq!(state.byte_offset, content.len() as u64);

        // Re-process without new content — should be idempotent.
        let re_inserted = process_file(&jsonl_path, &mut state, &db_path)
            .await
            .unwrap();
        assert_eq!(
            re_inserted, 0,
            "re-process with no new content should insert 0"
        );
    }

    #[tokio::test]
    async fn process_file_handles_truncation() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("test.db");
        open_db(&db_path).expect("init test db");

        let session_id = "test-sess-trunc-0001-0001-0001-000000000001";
        let jsonl_path = dir.path().join(format!("{session_id}.jsonl"));
        let content = j(session_id, 0, "user", "hi") + "\n";
        std::fs::write(&jsonl_path, &content).unwrap();

        let mut state = FileState {
            byte_offset: 9999,
            size_at_last_read: 9999,
            ..Default::default()
        };
        // Truncation detected: byte_offset > current_size.
        let _ = process_file(&jsonl_path, &mut state, &db_path).await;
        assert_eq!(
            state.byte_offset,
            content.len() as u64,
            "offset reset after truncation"
        );
    }

    #[test]
    fn save_and_load_offset_roundtrip() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_db(&dir);

        let path = PathBuf::from("/tmp/fake-session.jsonl");
        let state = FileState {
            byte_offset: 12345,
            inode: 42,
            device: 7,
            size_at_last_read: 12000,
            ..Default::default()
        };
        save_offset(&conn, &path, &state).unwrap();

        let loaded = load_offsets(&conn).unwrap();
        let recovered = loaded.get(&path).expect("offset not found after save");
        assert_eq!(recovered.byte_offset, 12345);
        assert_eq!(recovered.inode, 42);
        assert_eq!(recovered.device, 7);
        assert_eq!(recovered.size_at_last_read, 12000);
    }

    #[tokio::test]
    async fn no_duplicate_segments_on_repeated_processing() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("test.db");
        let conn = open_db(&db_path).expect("init test db");

        let session_id = "test-sess-dedup-0001-0001-0001-000000000001";
        let jsonl_path = dir.path().join(format!("{session_id}.jsonl"));

        // Write initial content.
        let line1 = j(session_id, 0, "user", "first") + "\n";
        let line2 = j(session_id, 1, "assistant", "first reply") + "\n";
        std::fs::write(&jsonl_path, &(line1.clone() + &line2)).unwrap();

        let mut state = FileState {
            ..Default::default()
        };

        let first = process_file(&jsonl_path, &mut state, &db_path)
            .await
            .unwrap();

        // Append more content.
        let line3 = j(session_id, 400, "user", "second prompt") + "\n";
        let line4 = j(session_id, 401, "assistant", "second reply") + "\n";
        {
            use std::io::Write;
            let mut f = std::fs::OpenOptions::new()
                .append(true)
                .open(&jsonl_path)
                .unwrap();
            write!(f, "{}{}", line3, line4).unwrap();
        }
        state.size_at_last_read = (line1.len() + line2.len()) as u64; // simulate previous state

        let second = process_file(&jsonl_path, &mut state, &db_path)
            .await
            .unwrap();

        // Verify no duplicates: count distinct (session_id, segment_index) pairs.
        let count: usize = conn
            .query_row(
                "SELECT COUNT(*) FROM claude_sessions WHERE session_id = ?1",
                [session_id],
                |row| row.get::<_, i64>(0),
            )
            .unwrap() as usize;

        assert!(
            first + second >= count,
            "total inserts should cover all unique segments"
        );

        // Check uniqueness: no two rows have the same (session_id, segment_index).
        let dup_count: usize = conn
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
            .unwrap() as usize;
        assert_eq!(dup_count, 0, "duplicate (session_id, segment_index) found");
    }
}
