//! Opencode session poller — live ingestion from opencode's SQLite DB.
//!
//! Polls `~/.local/share/opencode/opencode.db` for new/updated sessions.
//! Writes session records into `agentic_sessions` and updates `source_health`
//! so the watchdog can evaluate freshness invariants. Each upsert is
//! transactional and enqueues a row in `agentic_enrichment_queue` for the
//! brain to consume.

use anyhow::{Context, Result, anyhow};
use hippo_core::agentic::render_command;
use hippo_core::config::HippoConfig;
use hippo_core::redaction::RedactionEngine;
use rusqlite::{OptionalExtension, params};
use serde_json::Value;
use std::collections::HashSet;
use std::path::Path;
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
    context: OpencodeContext,
}

#[derive(Debug, Clone, Default)]
struct OpencodeContext {
    user_prompts: Vec<String>,
    assistant_texts: Vec<String>,
    tool_calls: Vec<String>,
    files_touched: Vec<String>,
    message_count: i64,
    token_count: i64,
}

/// High-water cursor in the opencode DB. Tracks `time_updated` so updates
/// to an already-ingested session are re-read on the next poll;
/// `ON CONFLICT DO UPDATE` keeps the destination row idempotent.
#[derive(Debug, Clone)]
struct Cursor {
    last_seen_updated_at: i64,
    last_id: String,
}

// --- Cursor management (writes to Hippo's own DB) ---

impl Cursor {
    fn read(conn: &rusqlite::Connection, source_key: &str) -> Result<Self> {
        let result: Option<(i64, String)> = conn
            .query_row(
                "SELECT last_seen_updated_at, last_id FROM agentic_cursor WHERE source_key = ?",
                params![source_key],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;

        let (ts, lid) = result.unwrap_or((0, String::new()));
        Ok(Self {
            last_seen_updated_at: ts,
            last_id: lid,
        })
    }

    fn upsert(conn: &rusqlite::Connection, source_key: &str, c: &Self) -> Result<()> {
        let now = chrono::Utc::now().timestamp_millis();
        conn.execute(
            "INSERT INTO agentic_cursor (source_key, last_seen_updated_at, last_id, updated_at)
             VALUES (?1, ?2, ?3, ?4)
             ON CONFLICT(source_key) DO UPDATE SET
                 last_seen_updated_at = excluded.last_seen_updated_at,
                 last_id              = excluded.last_id,
                 updated_at           = excluded.updated_at",
            params![source_key, c.last_seen_updated_at, &c.last_id, now],
        )?;
        Ok(())
    }
}

// --- Opencode DB read helpers ---

fn make_source_key(db_path: &Path) -> Result<String> {
    let meta = std::fs::metadata(db_path)
        .with_context(|| format!("failed to stat opencode DB at {}", db_path.display()))?;
    #[cfg(target_os = "macos")]
    {
        use std::os::unix::fs::MetadataExt;
        Ok(format!("opencode-{}", meta.ino()))
    }
    #[cfg(not(target_os = "macos"))]
    {
        // Hippo is macOS-only in production; non-macOS builds exist for CI
        // tests. Use the file size + mtime as a coarse inode substitute so
        // distinct test DBs don't collide on a single cursor row.
        let mtime = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_millis())
            .unwrap_or(0);
        Ok(format!("opencode-{}-{}", meta.len(), mtime))
    }
}

fn read_new_sessions(
    conn: &rusqlite::Connection,
    cursor: &Cursor,
    known_session_ids: &HashSet<String>,
) -> Result<Vec<OpencodeSession>> {
    // Use `>=` (not `>`) and rely on ON CONFLICT DO UPDATE in `upsert_session`
    // for idempotency. The previous tuple-cursor `(time_updated > ?1 OR
    // (time_updated = ?1 AND id > ?2))` was unsafe because opencode session
    // ids are random UUIDs: a new session inserted with the same `time_updated`
    // as the previous tail but a lexicographically *earlier* id would be
    // permanently skipped. Re-reading the at-boundary cluster every tick is
    // cheap (typically 1–2 rows) and the upsert is idempotent so duplicates
    // never land.
    // Read the opencode session index and filter in Rust. The `time_updated`
    // cursor keeps normal polling cheap, while the `known_session_ids` check
    // guarantees historical rows that are missing from Hippo are backfilled
    // even when the cursor has already advanced past them.
    let sql = "SELECT id, slug, title, directory, parent_id, agent, model,
                time_created, time_updated,
                summary_additions, summary_deletions, summary_files,
                summary_diffs
         FROM session
         ORDER BY time_updated ASC, id ASC";
    let mut stmt = conn.prepare(sql)?;
    let sessions = stmt
        .query_map([], |row| {
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
                context: OpencodeContext::default(),
            })
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;

    Ok(sessions
        .into_iter()
        .filter(|s| {
            s.time_updated >= cursor.last_seen_updated_at || !known_session_ids.contains(&s.id)
        })
        .collect())
}

fn read_known_opencode_session_ids(conn: &rusqlite::Connection) -> Result<HashSet<String>> {
    let mut stmt =
        conn.prepare("SELECT session_id FROM agentic_sessions WHERE harness = 'opencode'")?;
    stmt.query_map([], |row| row.get::<_, String>(0))?
        .collect::<std::result::Result<HashSet<_>, _>>()
        .map_err(Into::into)
}

fn opencode_table_exists(conn: &rusqlite::Connection, table_name: &str) -> Result<bool> {
    Ok(conn.query_row(
        "SELECT EXISTS(
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = ?1
        )",
        params![table_name],
        |row| row.get(0),
    )?)
}

fn trunc(s: &str, max_chars: usize) -> String {
    s.chars().take(max_chars).collect()
}

fn first_non_empty_str<'a>(value: &'a Value, keys: &[&str]) -> Option<&'a str> {
    keys.iter()
        .find_map(|key| value.get(*key).and_then(Value::as_str))
        .filter(|s| !s.trim().is_empty())
}

fn tokens_from_value(value: &Value) -> i64 {
    match value {
        Value::Number(n) => n.as_i64().unwrap_or(0),
        Value::Object(obj) => {
            if let Some(total) = obj.get("total").and_then(Value::as_i64) {
                return total;
            }
            obj.values().map(tokens_from_value).sum()
        }
        _ => 0,
    }
}

fn push_redacted(
    target: &mut Vec<String>,
    text: &str,
    max_chars: usize,
    redaction: &RedactionEngine,
) {
    let redacted = redaction.redact(&trunc(text.trim(), max_chars)).text;
    if !redacted.is_empty() && !target.contains(&redacted) {
        target.push(redacted);
    }
}

fn summarize_tool(tool: &str, part: &Value, redaction: &RedactionEngine) -> String {
    let input = part
        .pointer("/state/input")
        .or_else(|| part.get("input"))
        .unwrap_or(&Value::Null);
    let rendered = render_command(tool, input);
    let mut summary = if rendered == tool {
        first_non_empty_str(
            input,
            &[
                "command",
                "cmd",
                "filePath",
                "file_path",
                "path",
                "query",
                "pattern",
                "description",
            ],
        )
        .map(ToOwned::to_owned)
        .unwrap_or_else(|| tool.to_string())
    } else {
        rendered
    };

    if let Some(output) = part.pointer("/state/output").and_then(Value::as_str) {
        let first_line = output.lines().find(|line| !line.trim().is_empty());
        if let Some(first_line) = first_line {
            summary.push_str(" -> ");
            summary.push_str(&trunc(first_line, 120));
        }
    }

    redaction.redact(&trunc(&summary, 300)).text
}

fn extract_patch_files(part: &Value) -> Vec<String> {
    let Some(files) = part.get("files").and_then(Value::as_array) else {
        return Vec::new();
    };
    files
        .iter()
        .filter_map(|file| {
            file.as_str()
                .or_else(|| first_non_empty_str(file, &["path", "name"]))
        })
        .filter(|path| !path.trim().is_empty())
        .map(|path| trunc(path, 240))
        .collect()
}

fn read_session_context(
    conn: &rusqlite::Connection,
    session_id: &str,
    redaction: &RedactionEngine,
) -> Result<OpencodeContext> {
    let mut context = OpencodeContext::default();

    if opencode_table_exists(conn, "message")? {
        let mut stmt = conn.prepare(
            "SELECT data
             FROM message
             WHERE session_id = ?1
             ORDER BY time_created ASC, id ASC",
        )?;
        let rows = stmt.query_map(params![session_id], |row| row.get::<_, String>(0))?;
        for row in rows {
            let data = row?;
            let Ok(value) = serde_json::from_str::<Value>(&data) else {
                continue;
            };
            context.message_count += 1;
            if let Some(tokens) = value.get("tokens") {
                context.token_count += tokens_from_value(tokens);
            }
        }
    }

    if !opencode_table_exists(conn, "part")? {
        return Ok(context);
    }
    let count_step_finish_tokens = context.token_count == 0;

    let has_message_table = opencode_table_exists(conn, "message")?;
    let sql = if has_message_table {
        "SELECT COALESCE(m.data, '{}'), p.data
         FROM part p
         LEFT JOIN message m ON m.id = p.message_id
         WHERE p.session_id = ?1
         ORDER BY p.time_created ASC, p.id ASC"
    } else {
        "SELECT '{}', p.data
         FROM part p
         WHERE p.session_id = ?1
         ORDER BY p.time_created ASC, p.id ASC"
    };
    let mut stmt = conn.prepare(sql)?;
    let rows = stmt.query_map(params![session_id], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;

    for row in rows {
        let (message_data, part_data) = row?;
        let message_value = serde_json::from_str::<Value>(&message_data).unwrap_or(Value::Null);
        let part_value = serde_json::from_str::<Value>(&part_data).unwrap_or(Value::Null);
        let role = message_value
            .get("role")
            .and_then(Value::as_str)
            .unwrap_or("");
        let part_type = part_value.get("type").and_then(Value::as_str).unwrap_or("");

        match part_type {
            "text" => {
                if let Some(text) = part_value.get("text").and_then(Value::as_str) {
                    if role == "user" {
                        push_redacted(&mut context.user_prompts, text, 500, redaction);
                    } else if role == "assistant" {
                        push_redacted(&mut context.assistant_texts, text, 300, redaction);
                    }
                }
            }
            "tool" => {
                let tool = part_value
                    .get("tool")
                    .and_then(Value::as_str)
                    .unwrap_or("tool");
                let summary = summarize_tool(tool, &part_value, redaction);
                if !summary.is_empty() {
                    let line = format!("{tool}: {summary}");
                    if !context.tool_calls.contains(&line) {
                        context.tool_calls.push(line);
                    }
                }
            }
            "patch" => {
                for file in extract_patch_files(&part_value) {
                    if !context.files_touched.contains(&file) {
                        context.files_touched.push(file);
                    }
                }
            }
            "file" => {
                if let Some(path) = first_non_empty_str(&part_value, &["path", "filename", "name"])
                    && !context.files_touched.iter().any(|file| file == path)
                {
                    context.files_touched.push(trunc(path, 240));
                }
            }
            "step-finish" => {
                if count_step_finish_tokens && let Some(tokens) = part_value.get("tokens") {
                    context.token_count += tokens_from_value(tokens);
                }
            }
            _ => {}
        }
    }

    Ok(context)
}

// --- Section helpers ---

/// Build the prompt text that lands in `agentic_sessions.summary_text` and is
/// later passed verbatim to the LLM by the brain's enrichment loop. Mirrors
/// the brain's `build_opencode_enrichment_prompt` shape so the prompt has
/// real content the model can reason about (not the cwd path).
fn build_summary_text(s: &OpencodeSession) -> String {
    let mut lines = Vec::new();
    lines.push(format!(
        "Opencode session (project: {}, slug: {})",
        s.directory, s.slug
    ));
    if !s.title.is_empty() {
        lines.push(format!("Title: {}", s.title));
    }
    if let Some(agent) = s.agent.as_deref().filter(|a| !a.is_empty()) {
        lines.push(format!("Agent: {}", agent));
    }
    if let Some(model) = s.model.as_deref().filter(|m| !m.is_empty()) {
        lines.push(format!("Model: {}", model));
    }
    let adds = s.summary_additions.unwrap_or(0);
    let dels = s.summary_deletions.unwrap_or(0);
    let files = s.summary_files.unwrap_or(0);
    if adds > 0 || dels > 0 || files > 0 {
        lines.push(format!(
            "Snapshot diffs: +{}/-{} lines, {} files",
            adds, dels, files
        ));
    }
    if !s.context.user_prompts.is_empty() {
        lines.push("User requests:".to_string());
        for (idx, prompt) in s.context.user_prompts.iter().take(20).enumerate() {
            lines.push(format!("  {}. \"{}\"", idx + 1, prompt));
        }
    }
    if !s.context.tool_calls.is_empty() {
        lines.push("Work performed:".to_string());
        for tool in s.context.tool_calls.iter().take(40) {
            lines.push(format!("  - {tool}"));
        }
    }
    if !s.context.files_touched.is_empty() {
        lines.push("Files touched:".to_string());
        for path in s.context.files_touched.iter().take(40) {
            lines.push(format!("  - {path}"));
        }
    }
    if !s.context.assistant_texts.is_empty() {
        lines.push("Assistant excerpts:".to_string());
        for text in s.context.assistant_texts.iter().take(10) {
            lines.push(format!("  - \"{text}\""));
        }
    }
    lines.join("\n")
}

// --- Write helpers ---

fn upsert_session(conn: &rusqlite::Connection, s: &OpencodeSession) -> Result<()> {
    let now = chrono::Utc::now().timestamp_millis();
    // opencode stores `summary_diffs` already serialized as JSON. Pass through
    // verbatim (NULL → "null") to avoid double-encoding it as a JSON string.
    let diff_text = s.summary_diffs.as_deref().unwrap_or("null").to_string();
    let commit_json = "[]".to_string();
    let summary_text = build_summary_text(s);

    // AP-1: the agentic_sessions write, the queue enqueue, and the
    // source_health bump must land in the same transaction so the watchdog
    // sees them in lockstep.
    let tx = conn.unchecked_transaction()?;

    tx.execute(
        "INSERT INTO agentic_sessions
            (session_id, harness, model, agent, project_dir, cwd, slug, title,
             parent_session_id, summary_text, source_file, snapshot_diffs_json,
             commit_messages_json, message_count, token_count, start_time, end_time, created_at)
         VALUES (?1, 'opencode', ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, '', ?10, ?11, ?12, ?13, ?14, ?15, ?16)
         ON CONFLICT(session_id, harness) DO UPDATE SET
            model              = excluded.model,
            agent              = excluded.agent,
            title              = excluded.title,
            summary_text       = excluded.summary_text,
            snapshot_diffs_json = excluded.snapshot_diffs_json,
            commit_messages_json = excluded.commit_messages_json,
            message_count      = excluded.message_count,
            token_count        = excluded.token_count,
            end_time           = excluded.end_time",
        params![
            &s.id,                            // ?1 session_id
            s.model.as_deref().unwrap_or(""), // ?2 model (NOT NULL)
            s.agent.as_deref().unwrap_or(""), // ?3 agent
            &s.directory,                     // ?4 project_dir
            &s.directory,                     // ?5 cwd
            &s.slug,                          // ?6 slug
            &s.title,                         // ?7 title
            s.parent_id.as_deref(),           // ?8 parent_session_id (nullable)
            summary_text,                     // ?9 summary_text
            diff_text,                        // ?10 snapshot_diffs_json
            commit_json,                      // ?11 commit_messages_json
            s.context.message_count,          // ?12 message_count
            s.context.token_count,            // ?13 token_count
            s.time_created,                   // ?14 start_time
            s.time_updated,                   // ?15 end_time
            now,                              // ?16 created_at
        ],
    )?;

    // Look up the destination rowid (needed for the enrichment-queue FK).
    let agentic_session_id: i64 = tx.query_row(
        "SELECT id FROM agentic_sessions WHERE session_id = ?1 AND harness = 'opencode'",
        params![&s.id],
        |r| r.get(0),
    )?;

    // Enqueue (or re-pend) for the brain. Mirrors claude_session.rs:
    // re-pending a finished session is fine because the brain's claim WHERE
    // excludes `processing`. ON CONFLICT keeps re-ingests idempotent.
    tx.execute(
        "INSERT INTO agentic_enrichment_queue
             (session_id, status, retry_count, error_message, enqueued_at, updated_at)
         VALUES (?1, 'pending', 0, NULL, ?2, ?2)
         ON CONFLICT(session_id) DO UPDATE SET
             status        = 'pending',
             retry_count   = 0,
             error_message = NULL,
             updated_at    = excluded.updated_at
         WHERE agentic_enrichment_queue.status != 'processing'",
        params![agentic_session_id, now],
    )?;

    // Bump source_health for a successful upsert. Resetting
    // `consecutive_failures` here is the key piece: it lets watchdog I-11
    // (which gates on `consecutive_failures > 3`) actually become reachable.
    // `events_last_1h/24h` are intentionally NOT bumped — the poller uses a
    // `time_updated >= cursor` predicate that re-reads at-boundary rows on
    // every tick, so a per-tick increment would over-count idempotent
    // re-ingests. The daemon's `flush_events` increments those counters
    // because it processes only fresh socket frames.
    tx.execute(
        "UPDATE source_health
         SET last_event_ts        = MAX(COALESCE(last_event_ts, 0), ?1),
             last_success_ts      = ?2,
             consecutive_failures = 0,
             updated_at           = ?2
         WHERE source = 'agentic-session-opencode'",
        params![s.time_updated, now],
    )?;

    tx.commit()?;
    Ok(())
}

/// Record a failed upsert for the watchdog. Mirrors `daemon.rs::flush_events`
/// error path so I-11 (`consecutive_failures > 3` against
/// `agentic-session-opencode`) can actually fire when the poller is broken.
fn record_upsert_error(conn: &rusqlite::Connection, err: &anyhow::Error) {
    let now = chrono::Utc::now().timestamp_millis();
    let err_msg = format!("{err:#}");
    if let Err(e) = conn.execute(
        "UPDATE source_health
         SET last_error_ts        = ?1,
             last_error_msg       = ?2,
             consecutive_failures = consecutive_failures + 1,
             updated_at           = ?1
         WHERE source = 'agentic-session-opencode'",
        params![now, err_msg],
    ) {
        warn!("source_health error update failed: {e}");
    }
}

// --- Entry point ---

fn open_opencode_db(db_path: &Path) -> Result<rusqlite::Connection> {
    // Read-only open of opencode's own DB. Do NOT set journal_mode here —
    // the WAL pragma requires write access to the DB header and would fail
    // with SQLITE_READONLY. opencode manages its own journaling; we are only
    // a reader.
    let conn = rusqlite::Connection::open_with_flags(
        db_path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .with_context(|| format!("failed to open opencode DB at {}", db_path.display()))?;

    conn.pragma_update(None, "busy_timeout", "5000")
        .context("failed to set busy_timeout=5000")?;
    // foreign_keys=ON is a behavioral no-op on a read-only connection (FK
    // enforcement only fires on writes), but the project's CLAUDE.md rule
    // says "on every connection" without exception, so we set it explicitly
    // for consistency rather than silent omission.
    conn.pragma_update(None, "foreign_keys", "ON")
        .context("failed to set foreign_keys=ON")?;

    Ok(conn)
}

/// Poll one tick of opencode data. The caller (a launchd-scheduled oneshot)
/// owns scheduling; this function is a single read+upsert cycle.
pub fn poll_tick(config: &HippoConfig) -> Result<usize> {
    if !config.opencode.enabled {
        debug!("opencode poll disabled by config");
        return Ok(0);
    }

    let db_path = &config.opencode.db_path;
    if !db_path.exists() {
        debug!("opencode DB not found at {}", db_path.display());
        return Ok(0);
    }

    let oc_conn = match open_opencode_db(db_path) {
        Ok(c) => c,
        Err(e) => {
            warn!("Failed to open opencode DB: {e:#}");
            return Ok(0);
        }
    };

    // Resolve the Hippo DB path via the same XDG-aware helper the rest of
    // the daemon uses, so XDG_DATA_HOME overrides apply consistently.
    let hippo_db_path = config.db_path();
    let hippo_conn = match hippo_core::storage::open_db(&hippo_db_path) {
        Ok(c) => c,
        Err(e) => {
            warn!("Failed to open Hippo DB: {e:#}");
            return Ok(0);
        }
    };

    let source_key = make_source_key(db_path)
        .map_err(|e| anyhow!("could not derive source_key for opencode DB: {e:#}"))?;
    let cursor = Cursor::read(&hippo_conn, &source_key)?;
    let known_session_ids = read_known_opencode_session_ids(&hippo_conn)?;

    let mut new_sessions = read_new_sessions(&oc_conn, &cursor, &known_session_ids)?;
    if new_sessions.is_empty() {
        return Ok(0);
    }
    let redaction = RedactionEngine::builtin();
    for session in &mut new_sessions {
        match read_session_context(&oc_conn, &session.id, &redaction) {
            Ok(context) => session.context = context,
            Err(e) => warn!(id = %session.id, "opencode context read failed: {e:#}"),
        }
    }

    let mut events_sent = 0usize;
    let mut errors_sent = 0usize;
    let mut latest_ok: Option<&OpencodeSession> = None;

    for session in &new_sessions {
        debug!(id = %session.id, slug = %session.slug, "processing opencode session");

        match upsert_session(&hippo_conn, session) {
            Ok(()) => {
                events_sent += 1;
                latest_ok = Some(session);
            }
            Err(e) => {
                error!(%e, id = %session.id, "Failed to write session to Hippo DB");
                errors_sent += 1;
                record_upsert_error(&hippo_conn, &e);
            }
        }
    }

    // Advance the cursor only past sessions that landed successfully. If a
    // session in the middle of the batch failed, we leave the cursor on the
    // last successful one so the failed row (and everything after it in
    // time_updated order) is re-attempted on the next tick.
    if let Some(last) = latest_ok {
        let new_cursor = Cursor {
            last_seen_updated_at: last.time_updated,
            last_id: last.id.clone(),
        };
        Cursor::upsert(&hippo_conn, &source_key, &new_cursor)?;
    }

    info!(events_sent, errors_sent, "opencode tick: completed");

    Ok(events_sent)
}
