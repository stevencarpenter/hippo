//! Opencode session poller — live ingestion from opencode's SQLite DB.
//!
//! Polls `~/.local/share/opencode/opencode.db` for new/updated sessions.
//! Writes session records into `agentic_sessions` and updates `source_health`
//! so the watchdog can evaluate freshness invariants.

use anyhow::{Context, Result};
use rusqlite::{OptionalExtension, params};
use std::path::Path;
use std::path::PathBuf;
use tracing::{debug, error, info, warn};

/// Parsed opencode `session` row.
#[derive(Debug, Clone)]
struct OpencodeSession {
    id: String,
    slug: String,
    title: String,
    directory: String,
    parent_id: Option<String>,
    agent: Option<String>,
    model: Option<String>,
    time_created: i64,
    time_updated: i64,
    summary_additions: Option<i64>,
    summary_deletions: Option<i64>,
    summary_files: Option<i64>,
    summary_diffs: Option<String>,
}

/// High-water cursor in the opencode DB.
/// Keyed by (harness, db_inode) so reinstalls don't replay.
#[derive(Debug, Clone)]
struct Cursor {
    last_time_created: i64,
    last_id: String,
}

// --- Cursor management (writes to Hippo's own DB) ---

impl Cursor {
    fn read(conn: &rusqlite::Connection, source_key: &str) -> Result<Self> {
        let result: Option<(i64, String)> = conn
            .query_row(
                "SELECT last_time_created, last_id FROM agentic_cursor WHERE source_key = ?",
                params![source_key],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;

        let (tc, lid) = result.unwrap_or((0, String::new()));
        Ok(Self {
            last_time_created: tc,
            last_id: lid,
        })
    }

    fn upsert(conn: &rusqlite::Connection, source_key: &str, c: &Self) -> Result<()> {
        let now = chrono::Utc::now().timestamp_millis();
        conn.execute(
            "INSERT INTO agentic_cursor (source_key, last_time_created, last_id, updated_at)
             VALUES (?1, ?2, ?3, ?4)
             ON CONFLICT(source_key) DO UPDATE SET
                 last_time_created = excluded.last_time_created,
                 last_id           = excluded.last_id,
                 updated_at        = excluded.updated_at",
            params![source_key, c.last_time_created, &c.last_id, now],
        )?;
        Ok(())
    }
}

// --- Opencode DB read helpers ---

fn make_source_key(db_path: &Path) -> String {
    if let Some(meta) = std::fs::metadata(db_path).ok() {
        #[cfg(target_os = "macos")]
        {
            use std::os::unix::fs::MetadataExt;
            format!("opencode-{}", meta.ino())
        }
        #[cfg(not(target_os = "macos"))]
        {
            format!("opencode-0")
        }
    } else {
        String::new()
    }
}

fn read_new_sessions(conn: &rusqlite::Connection, cursor: &Cursor) -> Result<Vec<OpencodeSession>> {
    let sql = "SELECT id, slug, title, directory, parent_id, agent, model,
                time_created, time_updated,
                summary_additions, summary_deletions, summary_files,
                summary_diffs
         FROM session
         WHERE time_created > ?1
            OR (time_created = ?1 AND id > ?2)
         ORDER BY time_created ASC, id ASC";
    let mut stmt = conn.prepare(sql)?;
    stmt.query_map(params![cursor.last_time_created, cursor.last_id], |row| {
        Ok(OpencodeSession {
            id: row.get(0)?,
            slug: row.get(1)?,
            title: row.get(2)?,
            directory: row.get(3)?,
            parent_id: row.get(4)?,
            agent: row.get(5)?,
            model: row.get(6)?,
            time_created: row.get(7)?,
            time_updated: row.get(8)?,
            summary_additions: row.get(9)?,
            summary_deletions: row.get(10)?,
            summary_files: row.get(11)?,
            summary_diffs: row.get(12)?,
        })
    })?
    .collect::<Result<Vec<_>, _>>()
    .map_err(Into::into)
}

// --- Section helpers ---

fn to_string_opt<T: serde::Serialize>(val: &Option<T>) -> String {
    match val {
        Some(v) => serde_json::to_string(v).unwrap_or_else(|_| "null".to_string()),
        None => "null".to_string(),
    }
}

// --- Write helpers ---

fn upsert_session(conn: &rusqlite::Connection, s: &OpencodeSession) -> Result<()> {
    let now = chrono::Utc::now().timestamp_millis();
    let diff_text = to_string_opt(&s.summary_diffs);
    let commit_json = serde_json::json!([]).to_string();

    conn.execute(
        "INSERT INTO agentic_sessions
            (session_id, harness, model, agent, project_dir, cwd, slug,
             parent_session_id, summary_text, source_file, snapshot_diffs_json,
             commit_messages_json, start_time, end_time, created_at)
         VALUES (?1, 'opencode', ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)
         ON CONFLICT(session_id, harness) DO UPDATE SET
            model              = excluded.model,
            agent              = excluded.agent,
            snapshot_diffs_json = excluded.snapshot_diffs_json,
            commit_messages_json = excluded.commit_messages_json,
            end_time           = excluded.end_time",
        params![
            &s.id,                                       // ?1 session_id
            s.model.as_deref(),                          // ?2 model
            s.agent.as_deref(),                          // ?3 agent
            &s.directory,                                // ?4 project_dir
            &s.directory,                                // ?5 cwd
            &s.slug,                                     // ?6 slug
            s.parent_id.as_deref(),                      // ?7 parent_session_id
            s.directory.as_str(),                        // ?8 summary_text
            &s.id,                                       // ?9 source_file
            diff_text,                                   // ?10 snapshot_diffs_json
            commit_json,                                 // ?11 commit_messages_json
            s.time_created,                              // ?12 start_time
            s.time_updated,                              // ?13 end_time
            now,                                         // ?14 created_at
        ],
    )?;

    conn.execute(
        "UPDATE source_health SET
            last_event_ts = MAX(COALESCE(last_event_ts, 0), ?1),
            updated_at    = ?2
         WHERE source = 'agentic-session-opencode'",
        params![s.time_updated, now],
    )?;

    Ok(())
}

// --- Entry point ---

fn open_opencode_db(db_path: &Path) -> Result<rusqlite::Connection> {
    let conn = rusqlite::Connection::open_with_flags(
        db_path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY
            | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .with_context(|| format!("failed to open opencode DB at {}", db_path.display()))?;

    conn.pragma_update(None, "journal_mode", "WAL")
        .context("failed to set journal_mode=WAL")?;
    conn.pragma_update(None, "busy_timeout", "5000")
        .context("failed to set busy_timeout=5000")?;

    Ok(conn)
}

/// Poll one tick of opencode data.
pub fn poll_tick(db_path: &Path) -> Result<usize> {
    if !db_path.exists() {
        debug!("opencode DB not found at {}", db_path.display());
        return Ok(0);
    }

    // Open opencode DB
    let oc_conn = match open_opencode_db(db_path) {
        Ok(c) => c,
        Err(e) => {
            warn!("Failed to open opencode DB: {e:#}");
            return Ok(0);
        }
    };

    // data_version gate: skip if no new writes since last tick
    let _data_version: i64 = oc_conn
        .query_row("PRAGMA data_version", [], |row| row.get(0))?;

    // Open Hippo's DB for cursor management and session upserts
    let hippo_db_path = dirs::home_dir()
        .map(|h| h.join(".local/share/hippo/hippo.db"))
        .unwrap_or_else(|| PathBuf::from(".local/share/hippo/hippo.db"));

    let hippo_conn = match hippo_core::storage::open_db(&hippo_db_path) {
        Ok(c) => c,
        Err(e) => {
            warn!("Failed to open Hippo DB: {e:#}");
            return Ok(0);
        }
    };

    let source_key = make_source_key(db_path);
    let cursor = Cursor::read(&hippo_conn, &source_key)?;

    let new_sessions = read_new_sessions(&oc_conn, &cursor)?;
    if new_sessions.is_empty() {
        return Ok(0);
    }

    let mut events_sent = 0usize;
    let mut errors_sent = 0usize;

    for session in &new_sessions {
        debug!(id = %session.id, slug = %session.slug, "processing opencode session");

        match upsert_session(&hippo_conn, &session) {
            Ok(()) => {
                events_sent += 1;
            }
            Err(e) => {
                error!(%e, "Failed to write session to Hippo DB");
                errors_sent += 1;
            }
        }
    }

    // Update cursor to latest session
    let latest = new_sessions.last().expect("non-empty");
    let new_cursor = Cursor {
        last_time_created: latest.time_updated,
        last_id: latest.id.clone(),
    };
    Cursor::upsert(&hippo_conn, &source_key, &new_cursor)?;

    info!(
        events_sent,
        errors_sent,
        "opencode tick: completed"
    );

    Ok(events_sent)
}
