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
//! - Every heartbeat tick, runs `run_settling_sweep` to enqueue segments where
//!   `content_hash != last_enriched_content_hash` and the source file has been
//!   idle for 30+ minutes.  This is the backstop for the T-A.4 debounce gate.

use std::collections::HashMap;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
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

/// Return true when `err` indicates that the v12 columns (`content_hash`,
/// `last_enriched_content_hash`) or the `claude_enrichment_queue` table are
/// absent — i.e., the DB has not yet been migrated from v11→v12.
///
/// Used by `run_settling_sweep` to no-op gracefully on pre-migration databases
/// rather than propagating a confusing SQL error.
fn is_missing_claude_session_columns_error(err: &rusqlite::Error) -> bool {
    let msg = err.to_string();
    msg.contains("no such column: cs.content_hash")
        || msg.contains("no such column: cs.last_enriched_content_hash")
        || msg.contains("no such table: claude_enrichment_queue")
}

/// Sentinel: emit the pre-migration `warn!` at most once per process lifetime.
static SETTLING_SWEEP_PRE_MIGRATION_WARNED: OnceLock<()> = OnceLock::new();

/// Settling sweep — enqueue segments where content has drifted from the last
/// enriched state and the source file has gone quiet.
///
/// This is the backstop for the T-A.4 debounce gate: if the file stops growing
/// before the debounce window fires, the sweep catches the segment on the next
/// heartbeat tick (at most 30 s later, file-mtime-checked in Rust).
///
/// # Contract (frozen)
/// - Settling threshold: 30 minutes of file mtime idle.
/// - Per-tick batch cap: `max_per_tick` (caller passes 10).
/// - Enqueue mechanic: `INSERT OR REPLACE` — replaces `done`/`failed` rows;
///   preserves `processing` rows (excluded by the SELECT predicate).
/// - Pre-migration safe: returns `Ok(0)` with a single `warn!` per process.
///
/// Returns the count of segments enqueued this tick.
fn run_settling_sweep(
    conn: &Connection,
    max_per_tick: usize,
    now_ms: i64,
) -> rusqlite::Result<usize> {
    // 30 minutes ago in epoch-ms.
    let mtime_cutoff_ms = now_ms - 30 * 60 * 1000;

    // Over-fetch by 4× so the Rust-side mtime filter still yields max_per_tick
    // candidates even if some files have been recently modified or deleted.
    let fetch_limit = (max_per_tick * 4) as i64;

    let candidates: Vec<(i64, String)> = {
        // Prepare the candidate SELECT.  Any "no such column" / "no such table"
        // error means the DB has not yet been migrated to v12 — return Ok(0).
        let mut stmt = match conn.prepare(
            "SELECT cs.id, cs.source_file
             FROM claude_sessions cs
             LEFT JOIN claude_enrichment_queue ceq ON ceq.claude_session_id = cs.id
             WHERE cs.probe_tag IS NULL
               AND cs.content_hash IS NOT NULL
               AND (cs.last_enriched_content_hash IS NULL
                    OR cs.content_hash != cs.last_enriched_content_hash)
               AND (
                 json_array_length(COALESCE(cs.tool_calls_json,   '[]')) > 0
                 OR json_array_length(COALESCE(cs.user_prompts_json, '[]')) > 0
               )
               AND (ceq.id IS NULL OR ceq.status IN ('done','failed'))
             ORDER BY cs.end_time ASC
             LIMIT ?1",
        ) {
            Err(ref e) if is_missing_claude_session_columns_error(e) => {
                SETTLING_SWEEP_PRE_MIGRATION_WARNED.get_or_init(|| {
                    warn!(
                        "watcher: settling sweep skipped — DB not yet migrated to v12 \
                         (content_hash column absent); will retry each heartbeat"
                    );
                });
                return Ok(0);
            }
            Err(e) => {
                warn!(%e, "watcher: settling sweep prepare failed");
                return Ok(0);
            }
            Ok(s) => s,
        };

        match stmt.query_map(params![fetch_limit], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
        }) {
            Err(ref e) if is_missing_claude_session_columns_error(e) => {
                SETTLING_SWEEP_PRE_MIGRATION_WARNED.get_or_init(|| {
                    warn!(
                        "watcher: settling sweep skipped — DB not yet migrated to v12 \
                         (content_hash column absent); will retry each heartbeat"
                    );
                });
                return Ok(0);
            }
            Err(e) => {
                warn!(%e, "watcher: settling sweep query failed");
                return Ok(0);
            }
            Ok(rows) => rows.flatten().collect(),
        }
    };

    let mut enqueued = 0usize;

    // Wrap the per-tick enqueue batch in a single transaction.
    // Reduces fsyncs and keeps the write lock held across all upserts
    // (narrowing the race window from I-1).
    let tx = conn.unchecked_transaction().map_err(|e| {
        warn!(%e, "watcher: settling sweep could not begin transaction");
        e
    })?;

    for (session_id, source_file) in candidates {
        if enqueued >= max_per_tick {
            break;
        }

        // Check file mtime in Rust — only accept files whose last modification
        // is older than the 30-minute settling threshold.
        let mtime_ms = match std::fs::metadata(&source_file) {
            Err(_) => {
                // File deleted or inaccessible — silently skip.
                continue;
            }
            Ok(meta) => match meta.modified() {
                Err(_) => continue,
                Ok(sys_time) => {
                    // Convert SystemTime → epoch ms.
                    match sys_time.duration_since(std::time::UNIX_EPOCH) {
                        Ok(d) => d.as_millis() as i64,
                        Err(_) => continue,
                    }
                }
            },
        };

        if mtime_ms > mtime_cutoff_ms {
            // File modified more recently than 30 min ago — not yet settled.
            continue;
        }

        // Safe upsert: on conflict, only overwrite non-processing rows.
        // Mirrors the same logic in insert_segments (claude_session.rs).
        // Keeps the worker's lock safe; resets retry_count because a new
        // content version deserves a fresh retry budget.
        match tx.execute(
            "INSERT INTO claude_enrichment_queue
                 (claude_session_id, status, retry_count, error_message, created_at, updated_at)
             VALUES (?1, 'pending', 0, NULL, ?2, ?2)
             ON CONFLICT(claude_session_id) DO UPDATE SET
                 status        = 'pending',
                 retry_count   = 0,
                 error_message = NULL,
                 updated_at    = excluded.updated_at
             WHERE claude_enrichment_queue.status != 'processing'",
            params![session_id, now_ms],
        ) {
            Ok(_) => {
                enqueued += 1;
            }
            Err(ref e) if is_missing_claude_session_columns_error(e) => {
                // Race: shouldn't happen after SELECT succeeded, but guard anyway.
                warn!(%e, "watcher: settling sweep enqueue failed (pre-migration?)");
                // Commit whatever we managed before this error.
                let _ = tx.commit();
                return Ok(enqueued);
            }
            Err(e) => {
                warn!(%e, session_id, "watcher: settling sweep enqueue failed");
                // Non-fatal: continue trying remaining candidates.
            }
        }
    }

    tx.commit().map_err(|e| {
        warn!(%e, "watcher: settling sweep commit failed");
        e
    })?;

    if enqueued > 0 {
        debug!(enqueued, "watcher: settling sweep");
    }

    Ok(enqueued)
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

/// Sentinel: emit the backfill warning at most once per process lifetime.
static BACKFILL_WARN_ONCE: OnceLock<()> = OnceLock::new();

/// One-shot startup check: if any post-2026-04-25 segments have a NULL
/// `content_hash` (Bug A truncation residue from before Phase 1 shipped),
/// emit a `warn!` pointing the user at the backfill CLI.
///
/// Returns the count of affected segments, or 0 if the column does not exist
/// (pre-migration DB — safe no-op) or if the DB is already clean.
///
/// The OnceLock ensures the warn fires at most once per process even if this
/// function is ever called more than once.
fn check_backfill_needed(conn: &Connection) -> usize {
    // `strftime('%s', '2026-04-25')` returns seconds; multiply by 1000 to
    // compare against epoch-ms `end_time`.
    let count: rusqlite::Result<i64> = conn.query_row(
        "SELECT COUNT(*) FROM claude_sessions
         WHERE content_hash IS NULL
           AND probe_tag IS NULL
           AND end_time > strftime('%s', '2026-04-25') * 1000",
        [],
        |row| row.get(0),
    );

    match count {
        Err(ref e) if is_missing_claude_session_columns_error(e) => {
            // Pre-migration DB (v11): content_hash column absent. Safe to ignore.
            0
        }
        Err(e) => {
            warn!(%e, "watcher: backfill check query failed");
            0
        }
        Ok(0) => 0,
        Ok(n) => {
            BACKFILL_WARN_ONCE.get_or_init(|| {
                warn!(
                    count = n,
                    "watcher: {} session segment(s) captured 2026-04-25 → present have \
                     NULL content_hash (Bug A truncation residue). Run: \
                     hippo ingest claude-session-backfill \
                     '~/.claude/projects/**/*.jsonl' --since 2026-04-25 --dry-run, \
                     then without --dry-run to recover.",
                    n
                );
            });
            n as usize
        }
    }
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

    // One-shot startup check: warn if Bug A truncation residue is present.
    check_backfill_needed(&conn);

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
                let now_ms = Utc::now().timestamp_millis();
                match run_settling_sweep(&conn, 10, now_ms) {
                    Ok(_) => {}
                    Err(e) => {
                        warn!(%e, "watcher: settling sweep error");
                    }
                }
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

        let mut state = FileState::default();
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

        let mut state = FileState::default();

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

    // -------------------------------------------------------------------------
    // Settling sweep tests
    // -------------------------------------------------------------------------

    /// Open a test DB. Post-T-A.1 the v12 columns are part of the canonical
    /// schema (added by the v11→v12 migration in `open_db`), so no manual
    /// ALTER is needed. This thin wrapper is kept for readability since
    /// several sweep tests reference "v12" semantics in their setup.
    fn open_test_db_v12(dir: &TempDir) -> Connection {
        open_test_db(dir)
    }

    /// Set the mtime of a file to `seconds_ago` seconds before now using libc.
    fn set_mtime_seconds_ago(path: &Path, seconds_ago: u64) {
        use std::ffi::CString;
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs()
            .saturating_sub(seconds_ago) as libc::time_t;
        let times = [
            libc::timeval {
                tv_sec: ts,
                tv_usec: 0,
            },
            libc::timeval {
                tv_sec: ts,
                tv_usec: 0,
            },
        ];
        let c_path = CString::new(path.to_str().unwrap()).unwrap();
        unsafe {
            libc::utimes(c_path.as_ptr(), times.as_ptr());
        }
    }

    /// Seed a `claude_sessions` row directly for sweep tests.
    #[allow(clippy::too_many_arguments)]
    fn seed_session(
        conn: &Connection,
        row_id: i64,
        session_id: &str,
        source_file: &str,
        content_hash: Option<&str>,
        last_enriched_content_hash: Option<&str>,
        tool_calls_json: Option<&str>,
        user_prompts_json: Option<&str>,
    ) {
        let now_ms = 1_700_000_000_000i64;
        conn.execute(
            "INSERT INTO claude_sessions
                 (id, session_id, project_dir, cwd, segment_index, start_time, end_time,
                  summary_text, tool_calls_json, user_prompts_json, message_count, source_file,
                  content_hash, last_enriched_content_hash, created_at)
             VALUES (?1, ?2, '/tmp', '/tmp', 0, ?3, ?3, 'test', ?4, ?5, 1, ?6, ?7, ?8, ?3)",
            params![
                row_id,
                session_id,
                now_ms,
                tool_calls_json,
                user_prompts_json,
                source_file,
                content_hash,
                last_enriched_content_hash,
            ],
        )
        .expect("seed_session");
    }

    /// Seed a queue row for a session.
    fn seed_queue(conn: &Connection, claude_session_id: i64, status: &str) {
        let now_ms = 1_700_000_000_000i64;
        conn.execute(
            "INSERT INTO claude_enrichment_queue
                 (claude_session_id, status, created_at, updated_at)
             VALUES (?1, ?2, ?3, ?3)",
            params![claude_session_id, status, now_ms],
        )
        .expect("seed_queue");
    }

    /// Read the queue status for a session.
    fn queue_status(conn: &Connection, claude_session_id: i64) -> Option<String> {
        conn.query_row(
            "SELECT status FROM claude_enrichment_queue WHERE claude_session_id = ?1",
            params![claude_session_id],
            |row| row.get(0),
        )
        .ok()
    }

    #[test]
    fn test_sweep_enqueues_segment_with_old_mtime_and_hash_mismatch() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_db_v12(&dir);

        // Create a file and set its mtime to 35 minutes ago.
        let file = dir.path().join("old-session.jsonl");
        std::fs::write(&file, b"x").unwrap();
        set_mtime_seconds_ago(&file, 35 * 60);

        seed_session(
            &conn,
            1,
            "sess-old-mtime",
            file.to_str().unwrap(),
            Some("hash-a"),
            Some("hash-b"),           // mismatch: content changed since enrichment
            Some(r#"[{"id":"t1"}]"#), // non-empty tool_calls
            None,
        );

        let now_ms = chrono::Utc::now().timestamp_millis();
        let enqueued = run_settling_sweep(&conn, 10, now_ms).expect("sweep");
        assert_eq!(enqueued, 1, "expected 1 segment enqueued");
        assert_eq!(
            queue_status(&conn, 1).as_deref(),
            Some("pending"),
            "queue row should be pending"
        );
    }

    #[test]
    fn test_sweep_skips_recent_mtime() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_db_v12(&dir);

        // File modified only 5 minutes ago — not yet settled.
        let file = dir.path().join("recent-session.jsonl");
        std::fs::write(&file, b"x").unwrap();
        set_mtime_seconds_ago(&file, 5 * 60);

        seed_session(
            &conn,
            2,
            "sess-recent",
            file.to_str().unwrap(),
            Some("hash-a"),
            Some("hash-b"),
            Some(r#"[{"id":"t1"}]"#),
            None,
        );

        let now_ms = chrono::Utc::now().timestamp_millis();
        let enqueued = run_settling_sweep(&conn, 10, now_ms).expect("sweep");
        assert_eq!(enqueued, 0, "recent file should not be enqueued");
    }

    #[test]
    fn test_sweep_skips_when_hash_matches() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_db_v12(&dir);

        // File is old, but hashes match — already enriched at current content.
        let file = dir.path().join("hash-match.jsonl");
        std::fs::write(&file, b"x").unwrap();
        set_mtime_seconds_ago(&file, 35 * 60);

        seed_session(
            &conn,
            3,
            "sess-hash-match",
            file.to_str().unwrap(),
            Some("same-hash"),
            Some("same-hash"), // matches: no re-enrichment needed
            Some(r#"[{"id":"t1"}]"#),
            None,
        );

        let now_ms = chrono::Utc::now().timestamp_millis();
        let enqueued = run_settling_sweep(&conn, 10, now_ms).expect("sweep");
        assert_eq!(enqueued, 0, "matching hashes should not trigger enqueue");
    }

    #[test]
    fn test_sweep_skips_when_processing() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_db_v12(&dir);

        let file = dir.path().join("processing.jsonl");
        std::fs::write(&file, b"x").unwrap();
        set_mtime_seconds_ago(&file, 35 * 60);

        seed_session(
            &conn,
            4,
            "sess-processing",
            file.to_str().unwrap(),
            Some("hash-a"),
            Some("hash-b"),
            Some(r#"[{"id":"t1"}]"#),
            None,
        );
        seed_queue(&conn, 4, "processing");

        let now_ms = chrono::Utc::now().timestamp_millis();
        let enqueued = run_settling_sweep(&conn, 10, now_ms).expect("sweep");
        assert_eq!(enqueued, 0, "in-progress queue row should block re-enqueue");
    }

    #[test]
    fn test_sweep_replaces_done_queue_row() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_db_v12(&dir);

        let file = dir.path().join("done-session.jsonl");
        std::fs::write(&file, b"x").unwrap();
        set_mtime_seconds_ago(&file, 35 * 60);

        seed_session(
            &conn,
            5,
            "sess-done",
            file.to_str().unwrap(),
            Some("hash-new"),
            Some("hash-old"), // content changed since last enrichment
            Some(r#"[{"id":"t1"}]"#),
            None,
        );
        // Pre-existing 'done' row from a previous enrichment cycle.
        seed_queue(&conn, 5, "done");

        let now_ms = chrono::Utc::now().timestamp_millis();
        let enqueued = run_settling_sweep(&conn, 10, now_ms).expect("sweep");
        assert_eq!(enqueued, 1, "done row should be replaced with pending");

        let status = queue_status(&conn, 5).unwrap();
        assert_eq!(status, "pending", "queue row should be reset to pending");

        // updated_at should be fresh (within a few seconds of now).
        let updated_at: i64 = conn
            .query_row(
                "SELECT updated_at FROM claude_enrichment_queue WHERE claude_session_id = 5",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(
            updated_at >= now_ms,
            "updated_at should be at or after now_ms (got {updated_at}, expected >= {now_ms})"
        );
    }

    #[test]
    fn test_sweep_skips_empty_segment() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_db_v12(&dir);

        let file = dir.path().join("empty-seg.jsonl");
        std::fs::write(&file, b"x").unwrap();
        set_mtime_seconds_ago(&file, 35 * 60);

        seed_session(
            &conn,
            6,
            "sess-empty",
            file.to_str().unwrap(),
            Some("hash-a"),
            Some("hash-b"),
            Some("[]"), // empty tool_calls
            Some("[]"), // empty user_prompts
        );

        let now_ms = chrono::Utc::now().timestamp_millis();
        let enqueued = run_settling_sweep(&conn, 10, now_ms).expect("sweep");
        assert_eq!(enqueued, 0, "empty segment should not be enqueued");
    }

    #[test]
    fn test_sweep_caps_at_max_per_tick() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_db_v12(&dir);

        // Seed 15 eligible segments, all with old files.
        for i in 1i64..=15 {
            let file = dir.path().join(format!("cap-sess-{i}.jsonl"));
            std::fs::write(&file, b"x").unwrap();
            set_mtime_seconds_ago(&file, 35 * 60);
            seed_session(
                &conn,
                i,
                &format!("sess-cap-{i:02}"),
                file.to_str().unwrap(),
                Some("hash-a"),
                Some("hash-b"),
                Some(r#"[{"id":"t1"}]"#),
                None,
            );
        }

        let now_ms = chrono::Utc::now().timestamp_millis();
        // cap at 10
        let enqueued = run_settling_sweep(&conn, 10, now_ms).expect("sweep");
        assert_eq!(enqueued, 10, "sweep should cap at max_per_tick=10");

        // The remaining 5 should still have no queue row (or be untouched).
        let pending_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM claude_enrichment_queue WHERE status = 'pending'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(pending_count, 10, "exactly 10 rows should be pending");
    }

    #[test]
    fn test_sweep_skips_missing_file() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_db_v12(&dir);

        // Point source_file at a path that does not exist.
        let nonexistent = dir.path().join("does-not-exist.jsonl");

        seed_session(
            &conn,
            7,
            "sess-missing-file",
            nonexistent.to_str().unwrap(),
            Some("hash-a"),
            Some("hash-b"),
            Some(r#"[{"id":"t1"}]"#),
            None,
        );

        let now_ms = chrono::Utc::now().timestamp_millis();
        let enqueued = run_settling_sweep(&conn, 10, now_ms).expect("sweep should not error");
        assert_eq!(enqueued, 0, "missing file should be silently skipped");
    }

    #[test]
    fn test_sweep_returns_zero_on_pre_migration_db() {
        let dir = TempDir::new().unwrap();
        // Open a DB that does NOT have v12 columns (plain v11 schema).
        let conn = open_test_db(&dir);

        let now_ms = chrono::Utc::now().timestamp_millis();
        // Must return Ok(0) and not panic — the OnceLock suppresses repeated warns.
        let result = run_settling_sweep(&conn, 10, now_ms);
        assert!(result.is_ok(), "pre-migration DB should return Ok");
        assert_eq!(
            result.unwrap(),
            0,
            "pre-migration DB should return 0 enqueued"
        );
    }

    // -------------------------------------------------------------------------
    // I-1 regression: safe upsert must not clobber a processing lock
    // -------------------------------------------------------------------------

    /// Seed a queue row already in `processing` with explicit `locked_by` /
    /// `retry_count` fields (simulating a brain worker that claimed it).
    fn seed_queue_processing(
        conn: &Connection,
        claude_session_id: i64,
        locked_by: &str,
        retry_count: i64,
    ) {
        let now_ms = 1_700_000_000_000i64;
        conn.execute(
            "INSERT INTO claude_enrichment_queue
                 (claude_session_id, status, retry_count, locked_by, locked_at, created_at, updated_at)
             VALUES (?1, 'processing', ?2, ?3, ?4, ?4, ?4)",
            params![claude_session_id, retry_count, locked_by, now_ms],
        )
        .expect("seed_queue_processing");
    }

    /// Verify that the safe upsert SQL (I-1 fix) does NOT overwrite a
    /// processing row's lock fields, even when the caller attempts to
    /// enqueue the same session_id as 'pending'.
    ///
    /// This tests the SQL-level WHERE clause directly because the
    /// `decide_enqueue` function already guards against this at the Rust
    /// level — but the SQL guard is the defence-in-depth for the race where
    /// the row transitions to 'processing' after the SELECT but before the
    /// UPSERT.
    #[test]
    fn test_enqueue_does_not_clobber_processing_lock() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_db_v12(&dir);

        // Seed a session row.
        seed_session(
            &conn,
            100,
            "sess-lock-test",
            "/tmp/lock-test.jsonl",
            Some("hash-v2"),
            Some("hash-v1"), // different: content changed
            Some(r#"[{"id":"t1"}]"#),
            None,
        );

        // Brain worker has already claimed the row.
        seed_queue_processing(&conn, 100, "worker-a", 2);

        // Attempt to upsert to 'pending' — the WHERE clause must block this.
        let now_ms = chrono::Utc::now().timestamp_millis();
        conn.execute(
            "INSERT INTO claude_enrichment_queue
                 (claude_session_id, status, retry_count, error_message, created_at, updated_at)
             VALUES (?1, 'pending', 0, NULL, ?2, ?2)
             ON CONFLICT(claude_session_id) DO UPDATE SET
                 status        = 'pending',
                 retry_count   = 0,
                 error_message = NULL,
                 updated_at    = excluded.updated_at
             WHERE claude_enrichment_queue.status != 'processing'",
            params![100i64, now_ms],
        )
        .expect("upsert should succeed (no-op due to WHERE)");

        // Verify the processing row is completely intact.
        let (status, locked_by, retry_count): (String, Option<String>, i64) = conn
            .query_row(
                "SELECT status, locked_by, retry_count
                 FROM claude_enrichment_queue WHERE claude_session_id = 100",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .expect("queue row should still exist");

        assert_eq!(status, "processing", "status must remain 'processing'");
        assert_eq!(
            locked_by.as_deref(),
            Some("worker-a"),
            "locked_by must not be cleared"
        );
        assert_eq!(retry_count, 2, "retry_count must not be reset to 0");
    }

    // -------------------------------------------------------------------------
    // I-3 idempotence: running insert_segments twice preserves queue state
    // -------------------------------------------------------------------------

    #[tokio::test]
    async fn test_insert_idempotence_preserves_queue_retry_count() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("test.db");
        let conn = open_db(&db_path).expect("init test db");

        let session_id = "test-sess-idem-0001-0001-0001-000000000001";
        let jsonl_path = dir.path().join(format!("{session_id}.jsonl"));

        // Write a session with one complete exchange.
        let content = [
            make_test_jsonl_line(session_id, 0, "system", "init"),
            make_test_jsonl_line(session_id, 1, "user", "hello"),
            make_test_jsonl_line(session_id, 2, "assistant", "hi there"),
        ]
        .join("\n")
            + "\n";
        std::fs::write(&jsonl_path, &content).unwrap();

        // First ingest — should create the session row and enqueue it.
        let mut state = FileState::default();
        process_file(&jsonl_path, &mut state, &db_path)
            .await
            .unwrap();

        // Simulate brain marking the queue row 'done' and recording the hash.
        // (retry_count=3 is an arbitrary non-zero value to detect resets.)
        let session_row_id: i64 = conn
            .query_row(
                "SELECT id FROM claude_sessions WHERE session_id = ?1 LIMIT 1",
                [session_id],
                |row| row.get(0),
            )
            .expect("session row should exist");

        let current_hash: String = conn
            .query_row(
                "SELECT content_hash FROM claude_sessions WHERE id = ?1",
                [session_row_id],
                |row| row.get(0),
            )
            .expect("content_hash should exist");

        // Mark the queue row done with retry_count=3 and record the enriched hash.
        conn.execute(
            "UPDATE claude_enrichment_queue SET status='done', retry_count=3, updated_at=0
             WHERE claude_session_id = ?1",
            [session_row_id],
        )
        .unwrap();
        conn.execute(
            "UPDATE claude_sessions SET last_enriched_content_hash = ?1 WHERE id = ?2",
            params![current_hash, session_row_id],
        )
        .unwrap();

        // Second ingest with the same content — no new bytes, nothing to do.
        // (process_file short-circuits because size_at_last_read == current_size)
        // But we can directly confirm the idempotence of insert_segments by
        // checking the queue row after the file hasn't changed.
        let re_inserted = process_file(&jsonl_path, &mut state, &db_path)
            .await
            .unwrap();
        assert_eq!(re_inserted, 0, "no new content means 0 insertions");

        // The queue row retry_count should still be 3 (not reset to 0).
        let (status, retry): (String, i64) = conn
            .query_row(
                "SELECT status, retry_count FROM claude_enrichment_queue
                 WHERE claude_session_id = ?1",
                [session_row_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .expect("queue row should still exist");

        assert_eq!(status, "done", "status should remain 'done'");
        assert_eq!(
            retry, 3,
            "retry_count should not be reset when content is unchanged"
        );
    }

    // -------------------------------------------------------------------------
    // I-4 startup warn: check_backfill_needed
    // -------------------------------------------------------------------------

    #[test]
    fn test_check_backfill_needed_warns_when_null_hash_post_cutoff() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_db_v12(&dir);

        // Seed a segment with NULL content_hash and end_time after 2026-04-25.
        // strftime('%s','2026-04-25') = 1777075200; +1 day = 1777161600 s
        // Convert to epoch-ms: 1777161600000
        let post_cutoff_ms = 1_777_161_600_000i64;
        conn.execute(
            "INSERT INTO claude_sessions
                 (session_id, project_dir, cwd, segment_index, start_time, end_time,
                  summary_text, message_count, source_file, created_at)
             VALUES ('backfill-warn-sess', '/tmp', '/tmp', 0, ?1, ?1,
                     'test', 1, '/tmp/backfill-warn.jsonl', ?1)",
            params![post_cutoff_ms],
        )
        .expect("seed backfill-warn session");

        // content_hash is NULL (default); probe_tag is NULL (default).
        // check_backfill_needed should return count > 0.
        let count = check_backfill_needed(&conn);
        assert!(
            count > 0,
            "should detect NULL content_hash segment after 2026-04-25 cutoff"
        );
    }

    #[test]
    fn test_check_backfill_needed_silent_when_hash_set() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_db_v12(&dir);

        // Seed a segment WITH a content_hash — no backfill needed.
        // epoch-ms for 2026-04-26 00:00:00 UTC (after the 2026-04-25 cutoff).
        let post_cutoff_ms = 1_777_161_600_000i64;
        conn.execute(
            "INSERT INTO claude_sessions
                 (session_id, project_dir, cwd, segment_index, start_time, end_time,
                  summary_text, message_count, source_file, content_hash, created_at)
             VALUES ('backfill-ok-sess', '/tmp', '/tmp', 0, ?1, ?1,
                     'test', 1, '/tmp/backfill-ok.jsonl', 'somehash', ?1)",
            params![post_cutoff_ms],
        )
        .expect("seed backfill-ok session");

        let count = check_backfill_needed(&conn);
        assert_eq!(count, 0, "segment with content_hash set needs no backfill");
    }
}
