//! Opencode session poller — live ingestion from opencode's SQLite DB.
//!
//! Polls `~/.local/share/opencode/opencode.db` for new/updated sessions.
//! Writes session records into `agentic_sessions` and updates `source_health`
//! so the watchdog can evaluate freshness invariants. Each upsert is
//! transactional and enqueues a row in `agentic_enrichment_queue` for the
//! brain to consume.

use anyhow::{Context, Result};
use hippo_core::agentic::render_command;
use hippo_core::config::HippoConfig;
use hippo_core::redaction::RedactionEngine;
use rusqlite::params;
use serde_json::Value;
use std::collections::{HashMap, HashSet};
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

// --- Opencode DB read helpers ---

fn read_new_sessions(
    conn: &rusqlite::Connection,
    known_end_times: &HashMap<String, i64>,
) -> Result<Vec<OpencodeSession>> {
    // Per-session watermark. A session is (re-)read when EITHER it is new to
    // Hippo (no row in `known_end_times`) OR the source's `time_updated` has
    // advanced past the `end_time` Hippo last stored *for that same session*.
    //
    // Each session is its own watermark — there is no global cursor. That is
    // what makes this both gap-free and duplicate-free, even under partial
    // batch failure:
    //   * An unchanged finished session has `time_updated == stored end_time`,
    //     so it is never re-selected — no re-pend, no duplicate knowledge nodes
    //     (the "loop of sadness" this guards against).
    //   * A session whose upsert failed last tick still has its *old* stored
    //     end_time (the failed transaction rolled back), so `time_updated >
    //     stored` stays true and it is retried next tick — no lost update, even
    //     when a same-ms sibling was processed in the same batch. A single
    //     global watermark cannot express "all done except this one failed row";
    //     a per-session watermark needs no such expression.
    //   * Random-UUID id ordering is irrelevant: nothing compares ids, so the
    //     historical "earlier-UUID session is skipped" hazard cannot occur.
    //
    // Assumed not to occur: a *backward* `time_updated` (clock skew,
    // restore-from-backup) would leave a change unseen until `time_updated`
    // climbs back past the stored value. opencode writes `time_updated`
    // monotonically, and the retired global cursor carried the same `>`
    // assumption, so this is not a regression.
    //
    // Read the full opencode session index on every tick and filter in Rust.
    // A bounded SQL query (WHERE time_updated > X) would skip sessions older
    // than the bound but still missing from Hippo (e.g. one that arrived while
    // the poller was stopped). The full scan plus the per-session diff backfills
    // those, and stays cheap for a typical opencode DB of a few hundred sessions.
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
        .filter(|s| match known_end_times.get(&s.id) {
            None => true,
            Some(stored_end_time) => s.time_updated > *stored_end_time,
        })
        .collect())
}

/// Map of opencode `session_id` → the `end_time` (last successfully ingested
/// `time_updated`) Hippo has stored for it. This is the per-session watermark:
/// a known session is re-read only when the source's `time_updated` exceeds the
/// value already persisted, so a failed upsert (which leaves `end_time` behind)
/// is naturally retried and an unchanged session is never re-read.
fn read_known_opencode_session_end_times(
    conn: &rusqlite::Connection,
) -> Result<HashMap<String, i64>> {
    let mut stmt = conn
        .prepare("SELECT session_id, end_time FROM agentic_sessions WHERE harness = 'opencode'")?;
    stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
    })?
    .collect::<std::result::Result<HashMap<_, _>, _>>()
    .map_err(Into::into)
}

/// opencode `session_id`s whose enrichment-queue row is currently `processing`
/// — the brain has claimed them. A content update that arrives while a session
/// is being enriched must be deferred: `upsert_session`'s queue re-pend is
/// guarded by `WHERE status != 'processing'` and would no-op, so advancing the
/// per-session watermark (`end_time`) now would strand the new content (once the
/// brain sets the row to `done`, `time_updated == end_time` and it is never
/// re-selected). Deferring leaves the watermark behind so the next tick retries
/// the session after the brain releases the row. The deferral is bounded by the
/// brain's stale-lock reaper: if a worker crashes mid-enrichment the row stays
/// `processing`, but the reaper eventually flips an abandoned claim back to
/// `pending`/`failed`, after which the poller re-selects the session normally.
fn read_processing_opencode_session_ids(conn: &rusqlite::Connection) -> Result<HashSet<String>> {
    let mut stmt = conn.prepare(
        "SELECT s.session_id FROM agentic_enrichment_queue q
         JOIN agentic_sessions s ON q.session_id = s.id
         WHERE s.harness = 'opencode' AND q.status = 'processing'",
    )?;
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

    // segment_index is hard-coded to 0: opencode sessions are not segmented
    // (one row per session), and the v17 UNIQUE constraint
    // `(session_id, harness, segment_index)` requires an explicit value here.
    tx.execute(
        "INSERT INTO agentic_sessions
            (session_id, harness, segment_index, model, agent, project_dir, cwd, slug, title,
             parent_session_id, summary_text, source_file, snapshot_diffs_json,
             commit_messages_json, message_count, token_count, start_time, end_time, created_at)
         VALUES (?1, 'opencode', 0, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, '', ?10, ?11, ?12, ?13, ?14, ?15, ?16)
         ON CONFLICT(session_id, harness, segment_index) DO UPDATE SET
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
    // segment_index = 0 is explicit because the v17 UNIQUE is on the triple;
    // although today only one opencode segment per session exists, pinning
    // the lookup to (session_id, harness, segment_index) matches the INSERT.
    let agentic_session_id: i64 = tx.query_row(
        "SELECT id FROM agentic_sessions
         WHERE session_id = ?1 AND harness = 'opencode' AND segment_index = 0",
        params![&s.id],
        |r| r.get(0),
    )?;

    // Enqueue (or re-pend) for the brain. The `WHERE status != 'processing'`
    // guard only prevents clobbering a row the brain is mid-claim on — it does
    // NOT make re-pending free: a re-pended `done` session is re-enriched into
    // a *new* knowledge node. That is correct only because `read_new_sessions`
    // returns a known session solely when the source's `time_updated` exceeds
    // the `end_time` stored for it (i.e. real new content). If that predicate
    // ever weakened to `>=`, an unchanged session would re-pend every tick and
    // spawn unbounded duplicate nodes.
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
    // `events_last_1h/24h` are intentionally NOT bumped — the poller re-reads
    // and re-upserts a session every time its content grows (and full-scans the
    // index for backfill), so a per-tick increment would over-count these
    // idempotent re-ingests. The daemon's `flush_events` owns those counters
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

    let known_end_times = read_known_opencode_session_end_times(&hippo_conn)?;
    let processing_ids = read_processing_opencode_session_ids(&hippo_conn)?;

    let mut new_sessions = read_new_sessions(&oc_conn, &known_end_times)?;
    // Defer any session the brain is mid-enrichment on: re-pending it would
    // no-op against the `processing` row, and advancing its watermark now would
    // strand the new content. Leaving it untouched lets the per-session diff
    // retry it once the brain releases the row.
    let before = new_sessions.len();
    new_sessions.retain(|s| !processing_ids.contains(&s.id));
    let deferred = before - new_sessions.len();
    if deferred > 0 {
        debug!(
            deferred,
            "deferred opencode sessions under active enrichment"
        );
    }
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

    for session in &new_sessions {
        debug!(id = %session.id, slug = %session.slug, "processing opencode session");

        // Each session carries its own watermark (its stored `end_time`), so a
        // failed upsert leaves that watermark behind and the session is retried
        // on the next tick — no global cursor to advance, regress, or strand a
        // mid-batch failure behind.
        match upsert_session(&hippo_conn, session) {
            Ok(()) => events_sent += 1,
            Err(e) => {
                error!(%e, id = %session.id, "Failed to write session to Hippo DB");
                errors_sent += 1;
                record_upsert_error(&hippo_conn, &e);
            }
        }
    }

    info!(events_sent, errors_sent, "opencode tick: completed");

    Ok(events_sent)
}
