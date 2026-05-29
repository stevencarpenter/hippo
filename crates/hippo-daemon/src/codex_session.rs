//! Codex CLI rollout-session poller — see
//! docs/superpowers/specs/2026-05-17-codex-ingestion-design.md.

use anyhow::{Context, Result};
use chrono::DateTime;
use hippo_core::config::HippoConfig;
use hippo_core::redaction::RedactionEngine;
use rusqlite::{Connection, OpenFlags, OptionalExtension, params};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::path::{Path, PathBuf};
use tracing::{debug, error, info, warn};
use walkdir::WalkDir;

/// 5-minute gap between user prompts marks a task boundary.
const TASK_GAP_MS: i64 = 5 * 60 * 1000;
/// Accumulated character cap before forcing a new segment.
const MAX_SEGMENT_CHARS: usize = 12_000;

/// A single tool call, summarized for enrichment. Serialized into
/// `claude_sessions.tool_calls_json`.
#[derive(Debug, Clone, Serialize)]
pub struct ToolCall {
    pub name: String,
    pub summary: String,
}

/// A parsed Codex conversation segment, upserted into `claude_sessions`.
#[derive(Debug, Clone)]
pub struct CodexSegment {
    pub session_id: String,
    pub project_dir: String,
    pub cwd: String,
    pub segment_index: i64,
    pub start_time: i64,
    pub end_time: i64,
    pub user_prompts: Vec<String>,
    pub assistant_texts: Vec<String>,
    pub tool_calls: Vec<ToolCall>,
    pub message_count: i64,
    pub source_file: String,
}

/// Parse an ISO-8601 timestamp to epoch milliseconds; 0 on any failure.
pub(crate) fn parse_ts(ts: &str) -> i64 {
    if ts.is_empty() {
        return 0;
    }
    DateTime::parse_from_rfc3339(ts)
        .map(|dt| dt.timestamp_millis())
        .unwrap_or(0)
}

/// Short human-readable summary of a tool call's argument JSON. Mirrors
/// `_tool_summary` in codex_sessions.py: prefer the most informative single
/// argument, else the first non-empty string value, else the raw string.
pub(crate) fn tool_summary(arguments: &str) -> String {
    let parsed: serde_json::Value =
        serde_json::from_str(arguments).unwrap_or(serde_json::Value::Null);
    if let Some(obj) = parsed.as_object() {
        for key in [
            "cmd", "command", "filePath", "path", "uri", "query", "pattern",
        ] {
            if let Some(v) = obj.get(key).and_then(|v| v.as_str()) {
                return v.chars().take(120).collect();
            }
        }
        for v in obj.values() {
            if let Some(s) = v.as_str()
                && !s.is_empty()
            {
                return s.chars().take(80).collect();
            }
        }
    }
    arguments.chars().take(80).collect()
}

/// Pull the actual user request out of a Codex user message, stripping the
/// Xcode-injected project-context prefix. Substring port of
/// `_extract_user_text_from_codex_message` in `codex_sessions.py`, whose
/// `_XCODE_STATUS_PATTERN` (case-insensitive regex) is:
///   `The user (?:has (?:no )?(?:code selected|file currently open)|is`
///   `currently inside this file:[^\n]*)\.?\n`
/// hippo-daemon has no `regex` dependency, so this matches the three
/// distinctive tails as substrings — derived from the regex alternatives, not
/// invented — advances past the rest of that status line, and takes the text
/// after the last marker (else the last `\n\n` paragraph), capped at 500.
pub(crate) fn extract_user_text(message: &str) -> String {
    let markers = ["code selected", "file currently open", "inside this file:"];
    let mut cut = 0usize;
    let mut found_marker = false;
    for m in markers {
        if let Some(idx) = message.rfind(m) {
            // Advance through the rest of that status line (its trailing `\n`).
            let after = idx + m.len();
            let line_end = message[after..]
                .find('\n')
                .map(|n| after + n + 1)
                .unwrap_or(message.len());
            if line_end > cut {
                cut = line_end;
                found_marker = true;
            }
        }
    }
    let text = if found_marker {
        message[cut..].trim()
    } else if let Some(idx) = message.rfind("\n\n") {
        message[idx + 2..].trim()
    } else {
        message.trim()
    };
    text.chars().take(500).collect()
}

/// Extract text from `input_text` and `output_text` content blocks only.
///
/// Mirrors the Python reference in `codex_sessions.py`, which filters to
/// `block.get("type") == "output_text"` for assistant content. The Rust
/// port serves both user (`input_text`) and assistant (`output_text`) paths
/// with a single helper, so both types are allowed. Any block whose `type`
/// is absent or is anything else (e.g. `"reasoning"`) is skipped, even if it
/// carries a `text` field, to avoid leaking non-displayable intermediate
/// reasoning into the knowledge base.
fn content_text(content: &serde_json::Value) -> String {
    content
        .as_array()
        .map(|blocks| {
            blocks
                .iter()
                .filter(|b| {
                    matches!(
                        b.get("type").and_then(|t| t.as_str()),
                        Some("input_text") | Some("output_text")
                    )
                })
                .filter_map(|b| b.get("text").and_then(|t| t.as_str()))
                .collect::<Vec<_>>()
                .join("\n")
        })
        .unwrap_or_default()
}

/// Parse a Codex rollout JSONL file into task-boundary segments.
///
/// `redaction` — applied to `user_prompts`, `assistant_texts`, and each
/// tool-call summary before the values are stored. Matches the Python
/// `redact_segment_secrets` step; the builtin pattern set is shared with
/// `hippo_brain.redaction`.
pub(crate) fn extract_segments(
    path: &Path,
    redaction: &RedactionEngine,
) -> Result<Vec<CodexSegment>> {
    let raw = std::fs::read_to_string(path)
        .with_context(|| format!("read codex rollout {}", path.display()))?;
    let source_file = path.to_string_lossy().to_string();

    let mut segments: Vec<CodexSegment> = Vec::new();
    let mut current: Option<CodexSegment> = None;
    let mut current_chars: usize = 0;
    let mut last_user_ms: i64 = 0;
    let mut session_id = String::new();
    let mut session_cwd = String::new();

    for (line_idx, line) in raw.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let obj: serde_json::Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(e) => {
                // The `min_idle_secs` settle gate means the file is complete at
                // ingest time, so a parse error indicates real corruption. Warn
                // (naming file:line) instead of silently dropping content —
                // AP-11. Continue rather than `return Err`: one corrupt line
                // should not permanently wedge an otherwise-valid rollout by
                // blocking cursor advance.
                warn!(
                    "codex: skipping unparseable JSON at {}:{} ({e})",
                    path.display(),
                    line_idx + 1
                );
                continue;
            }
        };
        let entry_type = obj.get("type").and_then(|v| v.as_str()).unwrap_or("");
        let ts = parse_ts(obj.get("timestamp").and_then(|v| v.as_str()).unwrap_or(""));
        let payload = match obj.get("payload").and_then(|v| v.as_object()) {
            Some(p) => p,
            None => continue,
        };

        if entry_type == "session_meta" {
            if let Some(id) = payload.get("id").and_then(|v| v.as_str()) {
                session_id = id.to_string();
            }
            if let Some(cwd) = payload.get("cwd").and_then(|v| v.as_str()) {
                session_cwd = cwd.to_string();
            }
            continue;
        }
        if entry_type == "turn_context" {
            if let Some(cwd) = payload.get("cwd").and_then(|v| v.as_str())
                && !cwd.is_empty()
            {
                session_cwd = cwd.to_string();
                if let Some(c) = current.as_mut() {
                    c.cwd = cwd.to_string();
                }
            }
            continue;
        }

        let payload_type = payload.get("type").and_then(|v| v.as_str()).unwrap_or("");
        let role = payload.get("role").and_then(|v| v.as_str()).unwrap_or("");
        if role == "developer" {
            continue;
        }

        // --- User prompt: either event_msg/user_message or
        //     response_item/message+role=user ---
        let is_user_event = entry_type == "event_msg" && payload_type == "user_message";
        let is_user_item =
            entry_type == "response_item" && payload_type == "message" && role == "user";
        if is_user_event || is_user_item {
            let raw_msg = if is_user_event {
                payload
                    .get("message")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string()
            } else {
                content_text(payload.get("content").unwrap_or(&serde_json::Value::Null))
            };
            if raw_msg.is_empty() {
                continue;
            }
            let user_text = extract_user_text(&raw_msg);

            // Segment boundary: 5-minute gap or char cap.
            if last_user_ms > 0
                && ts > 0
                && (ts - last_user_ms > TASK_GAP_MS || current_chars > MAX_SEGMENT_CHARS)
            {
                if let Some(seg) = current.take()
                    && (!seg.user_prompts.is_empty()
                        || !seg.tool_calls.is_empty()
                        || !seg.assistant_texts.is_empty())
                {
                    segments.push(seg);
                }
                current_chars = 0;
            }

            let seg = current.get_or_insert_with(|| {
                // If session_meta was missing or malformed, fall back to the
                // file stem (e.g. "rollout-<id>"). This is unique per file and
                // deterministic, so ON CONFLICT (session_id, segment_index)
                // never collides across two different rollout files that both
                // lack session_meta.
                let effective_session_id = if session_id.is_empty() {
                    path.file_stem()
                        .map(|s| s.to_string_lossy().into_owned())
                        .unwrap_or_else(|| "codex-unknown".into())
                } else {
                    session_id.clone()
                };
                let cwd = if session_cwd.is_empty() {
                    path.parent()
                        .map(|p| p.to_string_lossy().to_string())
                        .unwrap_or_default()
                } else {
                    session_cwd.clone()
                };
                let project_dir = Path::new(&cwd)
                    .file_name()
                    .map(|n| n.to_string_lossy().to_string())
                    .unwrap_or_else(|| effective_session_id.clone());
                CodexSegment {
                    session_id: effective_session_id,
                    project_dir,
                    cwd,
                    segment_index: segments.len() as i64,
                    start_time: ts,
                    end_time: ts,
                    user_prompts: Vec::new(),
                    assistant_texts: Vec::new(),
                    tool_calls: Vec::new(),
                    message_count: 0,
                    source_file: source_file.clone(),
                }
            });
            if ts > 0 {
                last_user_ms = ts;
                seg.end_time = seg.end_time.max(ts);
            }
            seg.message_count += 1;
            if !user_text.is_empty() {
                let redacted = redaction.redact(&user_text).text;
                current_chars += redacted.chars().count();
                seg.user_prompts.push(redacted);
            }
            continue;
        }

        // Everything else only matters inside an open segment.
        let seg = match current.as_mut() {
            Some(s) => s,
            None => continue,
        };
        if ts > 0 {
            seg.end_time = seg.end_time.max(ts);
        }
        seg.message_count += 1;

        if entry_type == "response_item"
            && (payload_type == "function_call" || payload_type == "custom_tool_call")
        {
            let name = payload
                .get("name")
                .or_else(|| payload.get("tool_name"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            if name.is_empty() {
                continue;
            }
            let args = match payload.get("arguments").or_else(|| payload.get("input")) {
                Some(serde_json::Value::String(s)) => s.clone(),
                Some(other) => other.to_string(),
                None => String::new(),
            };
            let summary = redaction.redact(&tool_summary(&args)).text;
            current_chars += summary.chars().count();
            seg.tool_calls.push(ToolCall {
                name: name.to_string(),
                summary,
            });
            continue;
        }

        if entry_type == "response_item" && role == "assistant" {
            let text = content_text(payload.get("content").unwrap_or(&serde_json::Value::Null));
            if !text.is_empty() {
                let capped: String = text.chars().take(300).collect();
                let redacted = redaction.redact(&capped).text;
                current_chars += redacted.chars().count();
                seg.assistant_texts.push(redacted);
            }
        }
    }

    if let Some(seg) = current.take()
        && (!seg.user_prompts.is_empty()
            || !seg.tool_calls.is_empty()
            || !seg.assistant_texts.is_empty())
    {
        segments.push(seg);
    }
    Ok(segments)
}

/// Build the Codex-framed enrichment digest stored in
/// `claude_sessions.summary_text` and read by the brain's enrichment loop.
pub(crate) fn build_summary_text(seg: &CodexSegment) -> String {
    // Count caps bound summary_text. The 5-min / 12k-char segmentation split
    // only fires on user-message lines, so a segment with one prompt followed
    // by thousands of tool calls would otherwise produce an unbounded digest.
    const MAX_PROMPTS: usize = 30;
    const MAX_TOOLS: usize = 60;
    const MAX_ASSISTANT: usize = 5;
    let mut lines = vec![format!("Codex session (project: {})", seg.cwd)];
    if !seg.user_prompts.is_empty() {
        lines.push(String::new());
        lines.push("User requests:".to_string());
        for (i, p) in seg.user_prompts.iter().take(MAX_PROMPTS).enumerate() {
            lines.push(format!("  {}. \"{}\"", i + 1, p));
        }
        if seg.user_prompts.len() > MAX_PROMPTS {
            lines.push(format!(
                "  … (+{} more)",
                seg.user_prompts.len() - MAX_PROMPTS
            ));
        }
    }
    if !seg.tool_calls.is_empty() {
        lines.push(String::new());
        lines.push("Work performed:".to_string());
        for tc in seg.tool_calls.iter().take(MAX_TOOLS) {
            lines.push(format!("  - {}: {}", tc.name, tc.summary));
        }
        if seg.tool_calls.len() > MAX_TOOLS {
            lines.push(format!("  … (+{} more)", seg.tool_calls.len() - MAX_TOOLS));
        }
    }
    if !seg.assistant_texts.is_empty() {
        lines.push(String::new());
        lines.push("Assistant responses (excerpts):".to_string());
        for t in seg.assistant_texts.iter().take(MAX_ASSISTANT) {
            lines.push(format!("  - \"{}\"", t));
        }
    }
    lines.join("\n")
}

/// SHA256 (lowercase hex) of enrichment-relevant content. Same construction as
/// `claude_session::compute_segment_content_hash`: tool_calls_json | "|" |
/// user_prompts_json | "|" | assistant_texts joined by "\n".
pub(crate) fn compute_content_hash(seg: &CodexSegment) -> String {
    let tool_calls_json = serde_json::to_string(&seg.tool_calls).unwrap_or_else(|_| "[]".into());
    let user_prompts_json =
        serde_json::to_string(&seg.user_prompts).unwrap_or_else(|_| "[]".into());
    let assistant_text = seg.assistant_texts.join("\n");
    let mut hasher = Sha256::new();
    hasher.update(tool_calls_json.as_bytes());
    hasher.update(b"|");
    hasher.update(user_prompts_json.as_bytes());
    hasher.update(b"|");
    hasher.update(assistant_text.as_bytes());
    hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// Decide whether a just-upserted segment should be (re-)enqueued for
/// enrichment. A direct port of `claude_session::decide_enqueue` — Codex
/// segments share `claude_enrichment_queue` with Claude, so they must share
/// its re-enrichment gate, or a resumed rollout (grown file, bumped mtime)
/// re-pends every already-enriched earlier segment on every poll.
fn decide_enqueue(
    was_insert: bool,
    current_hash: &str,
    prior_last_enriched_hash: Option<&str>,
    prior_queue_status: Option<&str>,
    prior_queue_updated_at_ms: Option<i64>,
    now_ms: i64,
) -> bool {
    if was_insert {
        return true; // new segment — always needs first enrichment
    }
    if prior_queue_status == Some("processing") {
        return false; // a worker holds it
    }
    if prior_last_enriched_hash == Some(current_hash) {
        return false; // content unchanged since last successful enrichment
    }
    if let Some(updated_at) = prior_queue_updated_at_ms
        && (now_ms - updated_at) < 300_000
    {
        return false; // 5-minute debounce
    }
    true
}

/// Upsert one segment into `agentic_sessions` and (re-)enqueue it for
/// enrichment, inside a caller-supplied transaction. Idempotent via
/// `ON CONFLICT (session_id, harness, segment_index)`. `ingest_file` (Task 7) calls
/// this directly so a whole rollout file's segments commit atomically
/// (spec §4.3, AP-1).
pub fn upsert_segment_tx(tx: &rusqlite::Transaction, seg: &CodexSegment) -> Result<()> {
    let now_ms = chrono::Utc::now().timestamp_millis();
    let tool_calls_json = serde_json::to_string(&seg.tool_calls).unwrap_or_else(|_| "[]".into());
    let user_prompts_json =
        serde_json::to_string(&seg.user_prompts).unwrap_or_else(|_| "[]".into());
    let summary_text = build_summary_text(seg);
    let content_hash = compute_content_hash(seg);

    // Read prior state BEFORE the upsert so the enqueue gate can compare the
    // new content_hash against what was last enriched. One SELECT, mirroring
    // `claude_session::insert_segments`.
    #[allow(clippy::type_complexity)]
    let prior: Option<(i64, Option<String>, Option<String>, Option<i64>)> = tx
        .query_row(
            "SELECT s.id, s.last_enriched_content_hash, q.status, q.updated_at
             FROM agentic_sessions s
             LEFT JOIN agentic_enrichment_queue q ON q.session_id = s.id
             WHERE s.session_id = ?1
               AND s.harness = 'codex'
               AND s.segment_index = ?2",
            params![seg.session_id, seg.segment_index],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .optional()?;
    let was_insert = prior.is_none();
    let prior_last_enriched_hash = prior.as_ref().and_then(|(_, h, _, _)| h.as_deref());
    let prior_queue_status = prior.as_ref().and_then(|(_, _, s, _)| s.as_deref());
    let prior_queue_updated_at_ms = prior.as_ref().and_then(|(_, _, _, u)| *u);

    tx.execute(
        "INSERT INTO agentic_sessions
            (session_id, harness, segment_index, model, agent, project_dir, cwd,
             git_branch, slug, title, parent_session_id, is_subagent, summary_text,
             tool_calls_json, user_prompts_json, source_file, snapshot_diffs_json,
             commit_messages_json, message_count, token_count, start_time, end_time,
             content_hash, created_at)
         VALUES (?1, 'codex', ?2, '', '', ?3, ?4, NULL, '', '', NULL, 0, ?5, ?6, ?7, ?8,
                 'null', '[]', ?9, 0, ?10, ?11, ?12, ?13)
         ON CONFLICT (session_id, harness, segment_index) DO UPDATE SET
             end_time          = excluded.end_time,
             summary_text      = excluded.summary_text,
             tool_calls_json   = excluded.tool_calls_json,
             user_prompts_json = excluded.user_prompts_json,
             message_count     = excluded.message_count,
             content_hash      = excluded.content_hash,
             cwd               = excluded.cwd,
             project_dir       = excluded.project_dir",
        params![
            seg.session_id,
            seg.segment_index,
            seg.project_dir,
            seg.cwd,
            summary_text,
            tool_calls_json,
            user_prompts_json,
            seg.source_file,
            seg.message_count,
            seg.start_time,
            seg.end_time,
            content_hash,
            now_ms,
        ],
    )?;

    // `INSERT … ON CONFLICT` keeps the rowid stable, so reuse the prior id on
    // an update; `last_insert_rowid()` is only meaningful for a fresh insert.
    let agentic_session_id: i64 = if was_insert {
        tx.last_insert_rowid()
    } else {
        prior.as_ref().map(|(id, _, _, _)| *id).unwrap()
    };

    // Re-pend for enrichment only on genuinely new content (decide_enqueue).
    // `last_enriched_content_hash` is written by the brain, never here. A bare
    // file-mtime bump that re-parses unchanged segments must NOT re-enqueue
    // them — the gate is what stops a resumed rollout from re-enriching every
    // earlier segment. The `WHERE … != 'processing'` clause is a second guard
    // so a concurrent worker's lock is never trampled.
    if decide_enqueue(
        was_insert,
        &content_hash,
        prior_last_enriched_hash,
        prior_queue_status,
        prior_queue_updated_at_ms,
        now_ms,
    ) {
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
            params![agentic_session_id, now_ms],
        )?;
    }
    Ok(())
}

/// Convenience wrapper: upsert one segment in its own transaction. Used by the
/// Task 6 test; `ingest_file` (Task 7) uses `upsert_segment_tx` directly.
pub fn upsert_segment(conn: &rusqlite::Connection, seg: &CodexSegment) -> Result<()> {
    let tx = conn.unchecked_transaction()?;
    upsert_segment_tx(&tx, seg)?;
    tx.commit()?;
    Ok(())
}

// ─── Codex SQLite coverage oracle ────────────────────────────────────────────

/// Coverage comparison between Codex's own SQLite state and Hippo's ingested
/// Codex rows. Rollout JSONL remains the transcript source of truth; the
/// SQLite state DB is used to prove Hippo has seen every thread Codex knows
/// about, excluding files still inside the configured idle window.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CodexCoverageReport {
    pub total_state_threads: usize,
    pub covered_threads: usize,
    pub in_flight_threads: Vec<String>,
    pub missing_rollout_threads: Vec<String>,
    pub missing_hippo_threads: Vec<String>,
    pub log_only_thread_count: usize,
}

#[derive(Debug, Clone)]
struct CodexStateThread {
    id: String,
    rollout_path: PathBuf,
}

fn open_readonly_sqlite(path: &Path) -> Result<Connection> {
    let conn = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .with_context(|| format!("failed to open SQLite DB read-only at {}", path.display()))?;
    conn.pragma_update(None, "busy_timeout", "5000")
        .context("failed to set busy_timeout=5000")?;
    conn.pragma_update(None, "foreign_keys", "ON")
        .context("failed to set foreign_keys=ON")?;
    Ok(conn)
}

fn sqlite_table_exists(conn: &Connection, table_name: &str) -> Result<bool> {
    Ok(conn.query_row(
        "SELECT EXISTS(
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = ?1
        )",
        params![table_name],
        |row| row.get(0),
    )?)
}

fn read_codex_state_threads(conn: &Connection) -> Result<Vec<CodexStateThread>> {
    let mut stmt = conn.prepare("SELECT id, rollout_path FROM threads ORDER BY id ASC")?;
    stmt.query_map([], |row| {
        let id: String = row.get(0)?;
        let rollout_path: String = row.get(1)?;
        Ok(CodexStateThread {
            id,
            rollout_path: PathBuf::from(rollout_path),
        })
    })?
    .collect::<std::result::Result<Vec<_>, _>>()
    .map_err(Into::into)
}

fn read_hippo_session_ids(conn: &Connection) -> Result<HashSet<String>> {
    let mut stmt = conn.prepare(
        "SELECT DISTINCT session_id
         FROM agentic_sessions
         WHERE harness = 'codex'
           AND probe_tag IS NULL",
    )?;
    stmt.query_map([], |row| row.get::<_, String>(0))?
        .collect::<std::result::Result<HashSet<_>, _>>()
        .map_err(Into::into)
}

fn read_log_thread_ids(conn: &Connection) -> Result<HashSet<String>> {
    if !sqlite_table_exists(conn, "logs")? {
        return Ok(HashSet::new());
    }
    let mut stmt = conn.prepare(
        "SELECT DISTINCT thread_id
         FROM logs
         WHERE thread_id IS NOT NULL AND thread_id != ''",
    )?;
    stmt.query_map([], |row| row.get::<_, String>(0))?
        .collect::<std::result::Result<HashSet<_>, _>>()
        .map_err(Into::into)
}

fn is_file_in_idle_window(path: &Path, min_idle_secs: u64) -> bool {
    let Ok(meta) = std::fs::metadata(path) else {
        return false;
    };
    let Ok(modified) = meta.modified() else {
        return false;
    };
    match modified.elapsed() {
        Ok(age) => age < std::time::Duration::from_secs(min_idle_secs),
        Err(_) => true,
    }
}

/// Compare `~/.codex/state_5.sqlite` thread rows against Hippo's Codex
/// ingestion rows. `logs_2.sqlite` is intentionally diagnostic-only: it is
/// counted as log-only coverage but never treated as a transcript source.
pub fn check_codex_coverage(
    hippo_conn: &Connection,
    state_db_path: &Path,
    logs_db_path: Option<&Path>,
    min_idle_secs: u64,
) -> Result<CodexCoverageReport> {
    let state_conn = open_readonly_sqlite(state_db_path)?;
    let state_threads = read_codex_state_threads(&state_conn)?;
    let hippo_ids = read_hippo_session_ids(hippo_conn)?;

    let mut state_ids = HashSet::new();
    let mut covered_threads = 0usize;
    let mut in_flight_threads = Vec::new();
    let mut missing_rollout_threads = Vec::new();
    let mut missing_hippo_threads = Vec::new();

    for thread in &state_threads {
        state_ids.insert(thread.id.clone());
        if hippo_ids.contains(&thread.id) {
            covered_threads += 1;
            continue;
        }
        if !thread.rollout_path.exists() {
            missing_rollout_threads.push(thread.id.clone());
            continue;
        }
        if is_file_in_idle_window(&thread.rollout_path, min_idle_secs) {
            in_flight_threads.push(thread.id.clone());
            continue;
        }
        missing_hippo_threads.push(thread.id.clone());
    }

    let log_only_thread_count = match logs_db_path.filter(|path| path.exists()) {
        Some(path) => {
            let logs_conn = open_readonly_sqlite(path)?;
            read_log_thread_ids(&logs_conn)?
                .difference(&state_ids)
                .filter(|id| !hippo_ids.contains(*id))
                .count()
        }
        None => 0,
    };

    in_flight_threads.sort();
    missing_rollout_threads.sort();
    missing_hippo_threads.sort();

    Ok(CodexCoverageReport {
        total_state_threads: state_threads.len(),
        covered_threads,
        in_flight_threads,
        missing_rollout_threads,
        missing_hippo_threads,
        log_only_thread_count,
    })
}

// ─── poll_tick: file walk, cursor, source_health ─────────────────────────────

/// Stable inode-keyed cursor key for one rollout file. Inode survives the
/// `mv` Codex performs on archival, so archived files aren't re-parsed.
/// APFS inode numbers are 64-bit and monotonically assigned; they are not
/// realistically reused within a host's lifetime, so the cursor key stays
/// stable across the archival `mv` with no risk of a stale row aliasing a
/// different file. `ino()` is available on every Unix target via
/// `MetadataExt` — no per-OS `cfg` split is needed.
fn cursor_key(meta: &std::fs::Metadata) -> String {
    use std::os::unix::fs::MetadataExt;
    format!("codex-{}", meta.ino())
}

fn read_cursor(conn: &rusqlite::Connection, key: &str) -> i64 {
    conn.query_row(
        "SELECT last_seen_updated_at FROM agentic_cursor WHERE source_key = ?1",
        params![key],
        |r| r.get(0),
    )
    .unwrap_or(0)
}

fn write_cursor(
    conn: &rusqlite::Connection,
    key: &str,
    mtime_ms: i64,
    session_id: &str,
) -> Result<()> {
    let now = chrono::Utc::now().timestamp_millis();
    conn.execute(
        "INSERT INTO agentic_cursor (source_key, last_seen_updated_at, last_id, updated_at)
         VALUES (?1, ?2, ?3, ?4)
         ON CONFLICT(source_key) DO UPDATE SET
             last_seen_updated_at = excluded.last_seen_updated_at,
             last_id              = excluded.last_id,
             updated_at           = excluded.updated_at",
        params![key, mtime_ms, session_id, now],
    )?;
    Ok(())
}

fn bump_health_ok(conn: &rusqlite::Connection, last_event_ms: i64) {
    let now = chrono::Utc::now().timestamp_millis();
    let _ = conn.execute(
        "UPDATE source_health
         SET last_event_ts        = MAX(COALESCE(last_event_ts, 0), ?1),
             last_success_ts      = ?2,
             consecutive_failures = 0,
             updated_at           = ?2
         WHERE source = 'agentic-session-codex'",
        params![last_event_ms, now],
    );
}

fn record_error(conn: &rusqlite::Connection, err: &anyhow::Error) {
    let now = chrono::Utc::now().timestamp_millis();
    if let Err(e) = conn.execute(
        "UPDATE source_health
         SET last_error_ts        = ?1,
             last_error_msg       = ?2,
             consecutive_failures = consecutive_failures + 1,
             updated_at           = ?1
         WHERE source = 'agentic-session-codex'",
        params![now, format!("{err:#}")],
    ) {
        warn!("codex source_health error update failed: {e}");
    }
}

/// One poll cycle: walk every root, ingest changed idle rollout files.
pub fn poll_tick(config: &HippoConfig) -> Result<usize> {
    if !config.codex.enabled {
        debug!("codex poll disabled by config");
        return Ok(0);
    }
    let conn = hippo_core::storage::open_db(&config.db_path())?;
    let now_ms = chrono::Utc::now().timestamp_millis();
    let min_idle_ms = config.codex.min_idle_secs as i64 * 1000;
    let redaction = crate::load_redaction_engine(config);

    let mut ingested = 0usize;
    for root in &config.codex.session_roots {
        if !root.is_dir() {
            continue;
        }
        for entry in WalkDir::new(root).into_iter().filter_map(|e| e.ok()) {
            let path = entry.path();
            let is_rollout = path.extension().map(|e| e == "jsonl").unwrap_or(false)
                && path
                    .file_name()
                    .and_then(|n| n.to_str())
                    .map(|n| n.starts_with("rollout-"))
                    .unwrap_or(false);
            if !is_rollout {
                continue;
            }
            let meta = match entry.metadata() {
                Ok(m) => m,
                Err(_) => continue,
            };
            // Finding 3: an unreadable mtime would otherwise fall back to 0 and
            // be silently skipped forever (mtime_ms <= cursor). Warn (naming the
            // file) instead of skipping silently, then skip this tick.
            let Some(mtime_ms) = meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_millis() as i64)
            else {
                warn!(
                    "codex: unreadable mtime for {}, skipping this tick",
                    path.display()
                );
                continue;
            };
            // Skip in-flight files (avoid partial reads).
            if now_ms - mtime_ms < min_idle_ms {
                continue;
            }
            let key = cursor_key(&meta);
            if mtime_ms <= read_cursor(&conn, &key) {
                continue; // unchanged since last successful parse
            }
            match ingest_file(&conn, path, &redaction) {
                Ok((count, session_id)) => {
                    ingested += count;
                    // Only bump health when real segments landed. A rollout
                    // that parsed to zero segments (e.g. a `session_meta`-only
                    // file with no user messages) captured nothing, so
                    // `last_event_ts` must not move — mirroring how a poll
                    // that finds no files at all never bumps health. The
                    // cursor still advances so the empty file is not
                    // re-parsed every tick.
                    if count > 0 {
                        bump_health_ok(&conn, mtime_ms);
                    }
                    if let Err(e) = write_cursor(&conn, &key, mtime_ms, &session_id) {
                        warn!("codex cursor write failed for {}: {e:#}", path.display());
                    }
                }
                Err(e) => {
                    error!("codex ingest failed for {}: {e:#}", path.display());
                    record_error(&conn, &e);
                }
            }
        }
    }
    info!(ingested, "codex poll tick: completed");
    Ok(ingested)
}

/// Parse one file and upsert all its segments in a single transaction.
fn ingest_file(
    conn: &rusqlite::Connection,
    path: &Path,
    redaction: &RedactionEngine,
) -> Result<(usize, String)> {
    let segments = extract_segments(path, redaction)?;
    if segments.is_empty() {
        return Ok((0, String::new()));
    }
    let session_id = segments[0].session_id.clone();
    let tx = conn.unchecked_transaction()?;
    for seg in &segments {
        upsert_segment_tx(&tx, seg)?;
    }
    tx.commit()?;
    Ok((segments.len(), session_id))
}

/// Test-only constructor for a `HippoConfig` pointed at a temp data dir.
/// `pub` so the crate-external integration test can reach it, but
/// `#[doc(hidden)]` because it is test scaffolding, not public API.
#[doc(hidden)]
pub fn test_config(data_dir: &Path, roots: &[PathBuf]) -> HippoConfig {
    let mut cfg = HippoConfig::default();
    cfg.storage.data_dir = data_dir.to_path_buf();
    cfg.codex.session_roots = roots.to_vec();
    cfg.codex.min_idle_secs = 60;
    cfg
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_segment() -> CodexSegment {
        CodexSegment {
            session_id: "s1".into(),
            project_dir: "proj".into(),
            cwd: "/work/proj".into(),
            segment_index: 0,
            start_time: 1_775_634_000_000,
            end_time: 1_775_634_500_000,
            user_prompts: vec!["fix the bug".into()],
            assistant_texts: vec!["done".into()],
            tool_calls: vec![ToolCall {
                name: "shell".into(),
                summary: "cargo test".into(),
            }],
            message_count: 3,
            source_file: "/Users/x/.codex/sessions/2026/04/04/rollout-s1.jsonl".into(),
        }
    }

    #[test]
    fn summary_text_includes_prompts_tools_and_project() {
        let s = build_summary_text(&sample_segment());
        assert!(s.contains("Codex session"));
        assert!(s.contains("proj"));
        assert!(s.contains("fix the bug"));
        assert!(s.contains("shell"));
        assert!(s.contains("cargo test"));
    }

    #[test]
    fn summary_text_emits_overflow_line_past_prompt_cap() {
        let mut seg = sample_segment();
        seg.user_prompts = (0..31).map(|i| format!("prompt {i}")).collect();
        let s = build_summary_text(&seg);
        assert!(
            s.contains("… (+1 more)"),
            "31 prompts past MAX_PROMPTS=30 must show overflow line"
        );
    }

    #[test]
    fn content_hash_is_stable_and_changes_with_content() {
        let a = compute_content_hash(&sample_segment());
        let b = compute_content_hash(&sample_segment());
        assert_eq!(a, b);
        let mut changed = sample_segment();
        changed.user_prompts = vec!["different".into()];
        assert_ne!(a, compute_content_hash(&changed));
        assert_eq!(a.len(), 64); // SHA256 hex
    }

    #[test]
    fn parse_ts_handles_iso_and_garbage() {
        assert_eq!(parse_ts("2026-04-04T07:47:59.376Z"), 1775288879376);
        assert_eq!(parse_ts(""), 0);
        assert_eq!(parse_ts("not-a-date"), 0);
    }

    #[test]
    fn tool_summary_prefers_command_args() {
        assert_eq!(tool_summary(r#"{"command":"ls -la"}"#), "ls -la");
        assert_eq!(tool_summary(r#"{"path":"/tmp/x"}"#), "/tmp/x");
        assert_eq!(tool_summary("not json"), "not json");
        assert_eq!(tool_summary(""), "");
    }

    #[test]
    fn extract_segments_parses_committed_cli_fixture() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures/codex/rollout-cli.jsonl");
        let segs = extract_segments(&path, &RedactionEngine::builtin()).expect("parse");
        assert!(!segs.is_empty(), "expected at least one segment");
        let s = &segs[0];
        assert!(!s.session_id.is_empty());
        assert!(!s.cwd.is_empty());
        assert_eq!(s.segment_index, 0);
        assert!(s.start_time > 0);
        assert!(s.message_count > 0);
    }

    #[test]
    fn extract_segments_splits_on_five_minute_gap() {
        // Two user prompts 10 minutes apart -> two segments.
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("rollout-x.jsonl");
        let lines = [
            r#"{"timestamp":"2026-04-04T00:00:00.000Z","type":"session_meta","payload":{"id":"abc","timestamp":"2026-04-04T00:00:00.000Z","cwd":"/proj"}}"#,
            r#"{"timestamp":"2026-04-04T00:00:01.000Z","type":"event_msg","payload":{"type":"user_message","message":"first request"}}"#,
            r#"{"timestamp":"2026-04-04T00:10:01.000Z","type":"event_msg","payload":{"type":"user_message","message":"second request"}}"#,
        ];
        std::fs::write(&p, lines.join("\n")).unwrap();
        let segs = extract_segments(&p, &RedactionEngine::builtin()).unwrap();
        assert_eq!(segs.len(), 2, "10-minute gap must split the session");
        assert_eq!(segs[1].segment_index, 1);
    }

    #[test]
    fn extract_segments_handles_response_item_user_role() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("rollout-y.jsonl");
        let lines = [
            r#"{"timestamp":"2026-04-04T00:00:00.000Z","type":"session_meta","payload":{"id":"def","cwd":"/proj"}}"#,
            r#"{"timestamp":"2026-04-04T00:00:01.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello codex"}]}}"#,
        ];
        std::fs::write(&p, lines.join("\n")).unwrap();
        let segs = extract_segments(&p, &RedactionEngine::builtin()).unwrap();
        assert_eq!(segs.len(), 1);
        assert!(
            segs[0]
                .user_prompts
                .iter()
                .any(|p| p.contains("hello codex"))
        );
    }

    #[test]
    fn extract_user_text_strips_xcode_status_prefix() {
        // Faithful to codex_sessions.py _XCODE_STATUS_PATTERN: the real user text
        // follows the last "The user ... " status line.
        let msg = "Project structure:\n  src/\nThe user has no code selected.\nrefactor the parser";
        assert_eq!(extract_user_text(msg), "refactor the parser");
    }

    #[test]
    fn extract_user_text_uses_last_paragraph_when_no_marker() {
        // No Xcode status marker -> fall back to the last `\n\n` paragraph,
        // matching the Python reference (and this function's doc comment).
        let msg = "some preamble context\n\nthe actual request";
        assert_eq!(extract_user_text(msg), "the actual request");
    }

    #[test]
    fn extract_segments_splits_on_max_segment_chars() {
        // Two user prompts close in time (no 5-minute gap) with enough
        // accumulated tool-call summary between them to exceed
        // MAX_SEGMENT_CHARS -> the char-cap branch of the boundary OR splits.
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("rollout-cap.jsonl");
        let mut lines: Vec<String> = Vec::new();
        lines.push(
            r#"{"timestamp":"2026-04-04T00:00:00.000Z","type":"session_meta","payload":{"id":"cap","timestamp":"2026-04-04T00:00:00.000Z","cwd":"/proj"}}"#
                .to_string(),
        );
        lines.push(
            r#"{"timestamp":"2026-04-04T00:00:01.000Z","type":"event_msg","payload":{"type":"user_message","message":"first request"}}"#
                .to_string(),
        );
        // Each function_call's `command` is 120 chars; tool_summary caps at 120,
        // so every call adds 120 to current_chars. 120 calls = 14_400 > 12_000.
        let long_cmd = "x".repeat(120);
        for i in 0..120 {
            lines.push(format!(
                r#"{{"timestamp":"2026-04-04T00:00:0{}.000Z","type":"response_item","payload":{{"type":"function_call","name":"shell","arguments":"{{\"command\":\"{}\"}}"}}}}"#,
                2 + (i % 8),
                long_cmd,
            ));
        }
        // Second prompt is only seconds after the first -> no time-gap split;
        // the split must come purely from the char cap.
        lines.push(
            r#"{"timestamp":"2026-04-04T00:00:30.000Z","type":"event_msg","payload":{"type":"user_message","message":"second request"}}"#
                .to_string(),
        );
        std::fs::write(&p, lines.join("\n")).unwrap();
        let segs = extract_segments(&p, &RedactionEngine::builtin()).unwrap();
        assert_eq!(
            segs.len(),
            2,
            "accumulated chars over MAX_SEGMENT_CHARS must split the session"
        );
        assert_eq!(segs[1].segment_index, 1);
    }

    /// `content_text` must only include blocks whose type is `input_text` or
    /// `output_text`. Blocks with any other type (e.g. `"reasoning"`) are
    /// skipped even when they carry a `text` field, so non-displayable
    /// intermediate reasoning never leaks into the knowledge base.
    #[test]
    fn content_text_filters_to_text_bearing_block_types() {
        let content = serde_json::json!([
            {"type": "output_text", "text": "answer from assistant"},
            {"type": "reasoning", "text": "internal thought that must not appear"},
            {"type": "input_text", "text": "user input text"},
            {"type": "image_url", "url": "https://example.com/img.png"},
        ]);
        let result = content_text(&content);
        assert!(
            result.contains("answer from assistant"),
            "output_text block must be included"
        );
        assert!(
            result.contains("user input text"),
            "input_text block must be included"
        );
        assert!(
            !result.contains("internal thought"),
            "reasoning block must NOT be included"
        );
    }

    /// Regression guard: a rollout with no `session_meta` line must produce a
    /// non-empty `session_id` equal to the file stem, not the empty string.
    /// An empty session_id would collide in ON CONFLICT (session_id, segment_index)
    /// across any two files that both lack session_meta.
    #[test]
    fn extract_segments_falls_back_to_file_stem_when_no_session_meta() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("rollout-nosessionmeta.jsonl");
        // No session_meta line at all — only a user message.
        let lines = [
            r#"{"timestamp":"2026-04-04T00:00:01.000Z","type":"event_msg","payload":{"type":"user_message","message":"hello without meta"}}"#,
        ];
        std::fs::write(&p, lines.join("\n")).unwrap();
        let segs = extract_segments(&p, &RedactionEngine::builtin()).unwrap();
        assert_eq!(segs.len(), 1);
        let sid = &segs[0].session_id;
        assert!(
            !sid.is_empty(),
            "session_id must be non-empty when session_meta is absent"
        );
        assert_eq!(
            sid, "rollout-nosessionmeta",
            "session_id must be the file stem when session_meta is absent"
        );
    }

    /// Regression guard: user prompts must NOT contain the raw secret after
    /// `extract_segments` runs with the builtin redaction engine.
    ///
    /// Pattern used: `AKIAIOSFODNN7EXAMPLE` — a canonical AWS access key
    /// caught by the `aws_access_key` builtin rule (verified by
    /// `hippo_core::redaction` tests).  The resulting user_prompts entry must
    /// contain `[REDACTED]` and must not contain the raw key.
    #[test]
    fn extract_segments_redacts_secret_in_user_prompt() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("rollout-sec.jsonl");
        // Embed an AWS-key-shaped value that the builtin engine matches.
        let secret = "AKIAIOSFODNN7EXAMPLE";
        let message = format!("my key is {secret}");
        let lines = [
            r#"{"timestamp":"2026-04-04T00:00:00.000Z","type":"session_meta","payload":{"id":"sec","cwd":"/proj"}}"#.to_string(),
            format!(
                r#"{{"timestamp":"2026-04-04T00:00:01.000Z","type":"event_msg","payload":{{"type":"user_message","message":"{message}"}}}}"#
            ),
        ];
        std::fs::write(&p, lines.join("\n")).unwrap();
        let segs = extract_segments(&p, &RedactionEngine::builtin()).unwrap();
        assert_eq!(segs.len(), 1);
        let prompts = &segs[0].user_prompts;
        assert!(!prompts.is_empty(), "expected at least one user prompt");
        for prompt in prompts {
            assert!(
                !prompt.contains(secret),
                "raw secret must not appear in user_prompts; got: {prompt:?}"
            );
            assert!(
                prompt.contains("[REDACTED]"),
                "expected [REDACTED] placeholder in user_prompts; got: {prompt:?}"
            );
        }
    }

    #[test]
    fn decide_enqueue_gates_on_content_change() {
        let now = 2_000_000_000_000;
        let stale = now - 600_000; // 10 min ago — past the 5-min debounce
        // New segment — always enqueued.
        assert!(decide_enqueue(true, "h1", None, None, None, now));
        // A worker holds the row — never trample it.
        assert!(!decide_enqueue(
            false,
            "h1",
            None,
            Some("processing"),
            Some(stale),
            now
        ));
        // Content unchanged since last enrichment — skip.
        assert!(!decide_enqueue(
            false,
            "h1",
            Some("h1"),
            Some("done"),
            Some(stale),
            now
        ));
        // Content changed — re-enqueue.
        assert!(decide_enqueue(
            false,
            "h2",
            Some("h1"),
            Some("done"),
            Some(stale),
            now
        ));
        // Changed, but a re-pend already landed inside the debounce window — skip.
        assert!(!decide_enqueue(
            false,
            "h2",
            Some("h1"),
            Some("done"),
            Some(now - 1_000),
            now
        ));
        // Content changed, no prior queue row at all — must enqueue.
        assert!(decide_enqueue(false, "h2", Some("h1"), None, None, now));
    }

    #[test]
    fn read_hippo_session_ids_excludes_non_codex_and_probe_rows() {
        let tmp = tempfile::tempdir().unwrap();
        let db_path = tmp.path().join("hippo.db");
        let conn = hippo_core::storage::open_db(&db_path).unwrap();

        // Insert a real Codex row (should be included).
        let codex_seg = CodexSegment {
            session_id: "codex-covered".into(),
            project_dir: "proj".into(),
            cwd: "/work/proj".into(),
            segment_index: 0,
            start_time: 1_775_634_000_000,
            end_time: 1_775_634_500_000,
            user_prompts: vec!["codex prompt".into()],
            assistant_texts: vec![],
            tool_calls: vec![],
            message_count: 1,
            source_file: "/Users/me/.codex/sessions/rollout-x.jsonl".into(),
        };
        upsert_segment(&conn, &codex_seg).unwrap();

        // Non-Codex row in agentic_sessions with claude-code harness
        // (must be excluded: harness != 'codex').
        conn.execute(
            "INSERT INTO agentic_sessions
                 (session_id, harness, segment_index, project_dir, cwd,
                  summary_text, tool_calls_json, user_prompts_json,
                  message_count, source_file, is_subagent, start_time,
                  end_time, created_at)
             VALUES ('claude-session', 'claude-code', 0, 'proj', '/work',
                     'summary', '[]', '[]', 1,
                     '/Users/me/.claude/projects/abc/session.jsonl',
                     0, 1_775_634_000_000, 1_775_634_500_000, 1_775_634_000_000)",
            [],
        )
        .unwrap();

        // Codex-harness row WITH probe_tag set in agentic_sessions
        // (must be excluded: probe_tag IS NOT NULL despite harness = 'codex').
        conn.execute(
            "INSERT INTO agentic_sessions
                 (session_id, harness, segment_index, project_dir, cwd,
                  summary_text, tool_calls_json, user_prompts_json,
                  message_count, source_file, is_subagent, probe_tag,
                  start_time, end_time, created_at)
             VALUES ('codex-probe', 'codex', 0, 'proj', '/work',
                     'summary', '[]', '[]', 1,
                     '/Users/me/.codex/sessions/rollout-probe.jsonl',
                     0, 'test-probe', 1_775_634_000_000, 1_775_634_500_000,
                     1_775_634_000_000)",
            [],
        )
        .unwrap();

        let ids = read_hippo_session_ids(&conn).unwrap();
        assert!(
            ids.contains("codex-covered"),
            "Codex row must be included; got {ids:?}"
        );
        assert!(
            !ids.contains("claude-session"),
            "non-Codex .claude/ row must be excluded; got {ids:?}"
        );
        assert!(
            !ids.contains("codex-probe"),
            "probe row must be excluded; got {ids:?}"
        );
    }

    /// Round-3 #1: a single corrupted JSONL line must NOT wedge the rollout.
    /// The bad line is skipped (warn logged) and surrounding valid lines still
    /// produce a segment. Mirrors cursor_session for parser-class parity.
    #[test]
    fn extract_segments_warns_and_continues_on_unparseable_line() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("rollout-bad.jsonl");
        let lines = [
            r#"{"timestamp":"2026-04-04T00:00:00.000Z","type":"session_meta","payload":{"id":"abc","timestamp":"2026-04-04T00:00:00.000Z","cwd":"/proj"}}"#,
            r#"{this is not { valid json"#,
            r#"{"timestamp":"2026-04-04T00:00:01.000Z","type":"event_msg","payload":{"type":"user_message","message":"first request"}}"#,
        ];
        std::fs::write(&p, lines.join("\n")).unwrap();
        let segs = extract_segments(&p, &RedactionEngine::builtin()).unwrap();
        assert!(
            !segs.is_empty(),
            "valid lines must still yield a segment despite the corrupt middle line"
        );
    }
}
