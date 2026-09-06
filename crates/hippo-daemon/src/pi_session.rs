//! Pi agent session transcript poller.
//!
//! Three transcript shapes live under `~/.pi/agent/sessions/<slug>/`:
//! - main sessions (`<timestamp>_<uuid>.jsonl`) and subagent runs
//!   (`<parent-ts>_<parent-uuid>/<runId>/run-0/session.jsonl`): JSONL with a
//!   leading `{"type": "session", ...}` line (canonical id + cwd) and
//!   `{"type": "message", ...}` lines;
//! - reviewer transcripts (`subagent-artifacts/<id>_reviewer_transcript.jsonl`):
//!   `{"recordType": "message", ...}` lines with top-level `role`, `ts`,
//!   `runId`, and per-line `cwd`; tool calls are inline `toolCall` blocks
//!   (the `tool_start`/`tool_end` records duplicate them and are ignored).
//!
//! Tool calls arrive as assistant `toolCall` content blocks; their results
//! arrive as separate `toolResult` messages (outputs skipped — summaries come
//! from the call arguments, mirroring the Cursor poller). `thinking` blocks
//! are encrypted/opaque and skipped. `model_change`, `thinking_level_change`,
//! `session_info`, and `custom_message` lines carry no conversation content
//! and are skipped.
//!
//! Identity is the `session` header id when present, else the first reviewer
//! `runId`, else the filename UUID suffix. Time comes from per-line
//! timestamps (5-minute gap task boundaries, like Codex/Claude); the file
//! mtime is the fallback when a line timestamp is absent. Segments
//! additionally split on accumulated character count.

use std::path::Path;

use anyhow::{Context, Result};
use hippo_core::config::HippoConfig;
use hippo_core::redaction::RedactionEngine;
use rusqlite::{OptionalExtension, params};
use serde::Serialize;
use sha2::{Digest, Sha256};
use tracing::{debug, error, info, warn};
use walkdir::WalkDir;

/// 5-minute gap between user prompts marks a task boundary.
const TASK_GAP_MS: i64 = 5 * 60 * 1000;
const MAX_SEGMENT_CHARS: usize = 12_000;

/// A single tool call, summarized for enrichment. Serialized into
/// `agentic_sessions.tool_calls_json` (harness = 'pi').
#[derive(Debug, Clone, Serialize)]
pub struct ToolCall {
    pub name: String,
    pub summary: String,
}

/// A parsed Pi conversation segment, upserted into `agentic_sessions` (harness = 'pi').
#[derive(Debug, Clone)]
pub struct PiSegment {
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
    pub token_count: i64,
    pub source_file: String,
    pub is_subagent: bool,
    pub parent_session_id: Option<String>,
}

/// Short human-readable summary of a Pi `toolCall` block's `arguments` object.
/// Prefer the most informative single argument, else the first non-empty
/// string value, else the compact JSON.
pub(crate) fn tool_summary(input: &serde_json::Value) -> String {
    if let Some(obj) = input.as_object() {
        for key in [
            "command",
            "cmd",
            "file_path",
            "filePath",
            "path",
            "glob_pattern",
            "pattern",
            "query",
            "search",
            "tool",
            "uri",
            "target_directory",
            "server",
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
    input.to_string().chars().take(80).collect()
}

/// Parse an ISO-8601 timestamp to epoch milliseconds; 0 on any failure.
pub(crate) fn parse_ts(ts: &str) -> i64 {
    if ts.is_empty() {
        return 0;
    }
    chrono::DateTime::parse_from_rfc3339(ts)
        .map(|dt| dt.timestamp_millis())
        .unwrap_or(0)
}

/// Identity derived from a transcript's `session` header line and path.
///
/// The header's `id` is canonical when present. Otherwise the session id is the
/// UUID suffix of `<timestamp>_<uuid>.jsonl` filenames (or the portion before
/// a `_reviewer_transcript` suffix for subagent review transcripts), falling
/// back to the full file stem. `cwd` prefers the header value (ground truth),
/// then the per-line cwd, then the slug decode.
#[derive(Debug, Clone)]
pub(crate) struct PathIdentity {
    pub session_id: String,
    pub project_dir: String,
    pub cwd: String,
    pub is_subagent: bool,
    pub parent_session_id: Option<String>,
}

/// Derive the `project_dir` (last path component) for a given cwd, falling back
/// to the slug when the cwd is empty.
fn project_dir_for(cwd: &str, slug: &str) -> String {
    Path::new(cwd)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| slug.to_string())
}

/// Decode a `~/.pi/agent/sessions/<slug>/` slug into a cwd. The slug wraps the
/// `/`-separated path with `--` and maps `/` onto `-` (same convention as
/// `~/.claude/projects`). Like Cursor's slug this decode is AMBIGUOUS for
/// hyphenated path components, so it is the *fallback* only; the `session`
/// header cwd wins, then `recover_cwd_from_paths`.
fn decode_slug_to_cwd(slug: &str) -> String {
    let trimmed = slug.trim_matches('-');
    if trimmed.is_empty() {
        return String::new();
    }
    format!("/{}", trimmed.replace('-', "/"))
}

/// Nearest ancestor path component that looks like a pi project slug
/// (`--...--`). The transcript file's parent is the slug dir in the normal
/// layout; subagent artifacts nest deeper, so walk up.
fn find_slug(path: &Path) -> String {
    let mut current = path.parent();
    while let Some(dir) = current {
        if let Some(name) = dir.file_name().and_then(|n| n.to_str())
            && name.starts_with("--")
            && name.ends_with("--")
            && name.len() > 4
        {
            return name.to_string();
        }
        current = dir.parent();
    }
    String::new()
}

/// Session id candidate from a pi transcript filename.
pub(crate) fn session_id_from_filename(path: &Path) -> String {
    let stem = path
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| "pi-unknown".into());
    let candidate = stem.strip_suffix("_reviewer_transcript").unwrap_or(&stem);
    let candidate = candidate
        .rsplit('_')
        .next()
        .unwrap_or(candidate)
        .to_string();
    if uuid::Uuid::parse_str(&candidate).is_ok() {
        candidate
    } else {
        stem
    }
}

impl PathIdentity {
    /// Full constructor: `line_cwd` is the first per-line cwd seen (the
    /// reviewer shape carries cwd per line and has no header).
    fn from_parts(
        path: &Path,
        header_id: Option<&str>,
        header_cwd: &str,
        line_cwd: Option<&str>,
    ) -> Self {
        let session_id = header_id
            .filter(|s| !s.is_empty())
            .map(|s| s.to_string())
            .unwrap_or_else(|| session_id_from_filename(path));

        let components: Vec<String> = path
            .components()
            .map(|c| c.as_os_str().to_string_lossy().into_owned())
            .collect();
        let is_artifact_subagent = components
            .iter()
            .any(|c| c == "subagents" || c == "subagent-artifacts");
        // Subagent run sessions live at
        // `<slug>/<parent-ts>_<parent-uuid>/<runId>/run-0/session.jsonl`.
        let is_run_subagent = components.iter().any(|c| c == "run-0");
        let is_subagent = is_artifact_subagent || is_run_subagent;

        // Parent linkage for run sessions: the `<ts>_<uuid>` ancestor dir
        // names the parent session file whose stem carries the parent UUID.
        // Skip the file name, the `run-0` dir, and the run-id dir itself —
        // the run id is a UUID too, but it is not the parent.
        let parent_session_id = if is_run_subagent && !is_artifact_subagent {
            let mut rev = components.iter().rev();
            rev.next(); // session.jsonl
            // Consume through the run-0 dir and the run-id dir.
            let mut past_run = false;
            for c in rev.by_ref() {
                if c == "run-0" {
                    past_run = true;
                    break;
                }
            }
            if past_run {
                rev.next(); // run-id dir
            }
            rev.find_map(|c| {
                let stem_uuid = c.rsplit('_').next().unwrap_or("");
                if uuid::Uuid::parse_str(stem_uuid).is_ok() {
                    Some(stem_uuid.to_string())
                } else {
                    None
                }
            })
        } else {
            None
        };

        let slug = find_slug(path);
        let cwd = if !header_cwd.is_empty() {
            header_cwd.to_string()
        } else if let Some(lc) = line_cwd.filter(|s| !s.is_empty()) {
            lc.to_string()
        } else {
            decode_slug_to_cwd(&slug)
        };
        let project_dir = project_dir_for(&cwd, &slug);

        PathIdentity {
            session_id,
            project_dir,
            cwd,
            is_subagent,
            parent_session_id,
        }
    }
}

/// Join the `text` of every `text` block in a `content` array.
fn text_blocks(content: &serde_json::Value) -> Vec<String> {
    content
        .as_array()
        .map(|blocks| {
            blocks
                .iter()
                .filter(|b| b.get("type").and_then(|t| t.as_str()) == Some("text"))
                .filter_map(|b| {
                    b.get("text")
                        .and_then(|t| t.as_str())
                        .map(|s| s.to_string())
                })
                .collect()
        })
        .unwrap_or_default()
}

/// Parse a Pi session transcript JSONL into task-boundary segments.
///
/// `mtime_ms` is the fallback timestamp when a line carries no parseable
/// timestamp. `redaction` is applied to prompts, assistant text, and tool
/// summaries before they are stored.
pub(crate) fn extract_segments(
    path: &Path,
    mtime_ms: i64,
    redaction: &RedactionEngine,
) -> Result<Vec<PiSegment>> {
    let raw = std::fs::read_to_string(path)
        .with_context(|| format!("read pi transcript {}", path.display()))?;
    let source_file = path.to_string_lossy().to_string();

    // First pass: header line for canonical identity. The reviewer shape
    // has no `session` line — fall back to the first `runId` / line `cwd`.
    let mut header_id: Option<String> = None;
    let mut header_cwd = String::new();
    let mut header_ts: i64 = 0;
    let mut line_cwd: Option<String> = None;
    for line in raw.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let Ok(obj) = serde_json::from_str::<serde_json::Value>(line) else {
            continue;
        };
        if line_cwd.is_none()
            && let Some(cwd) = obj.get("cwd").and_then(|v| v.as_str())
            && !cwd.is_empty()
        {
            line_cwd = Some(cwd.to_string());
        }
        if obj.get("type").and_then(|v| v.as_str()) == Some("session") {
            header_id = obj
                .get("id")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string());
            header_cwd = obj
                .get("cwd")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            header_ts = parse_ts(obj.get("timestamp").and_then(|v| v.as_str()).unwrap_or(""));
            break;
        }
        // Reviewer shape: adopt the first runId as the session id.
        if header_id.is_none()
            && obj.get("recordType").and_then(|v| v.as_str()) == Some("message")
            && let Some(run_id) = obj.get("runId").and_then(|v| v.as_str())
            && !run_id.is_empty()
        {
            header_id = Some(run_id.to_string());
        }
        if header_id.is_some() && line_cwd.is_some() {
            break;
        }
    }
    let id = PathIdentity::from_parts(path, header_id.as_deref(), &header_cwd, line_cwd.as_deref());

    let new_segment = |index: i64, ts: i64| PiSegment {
        session_id: id.session_id.clone(),
        project_dir: id.project_dir.clone(),
        cwd: id.cwd.clone(),
        segment_index: index,
        start_time: ts,
        end_time: ts,
        user_prompts: Vec::new(),
        assistant_texts: Vec::new(),
        tool_calls: Vec::new(),
        message_count: 0,
        token_count: 0,
        source_file: source_file.clone(),
        is_subagent: id.is_subagent,
        parent_session_id: id.parent_session_id.clone(),
    };

    let mut segments: Vec<PiSegment> = Vec::new();
    let mut current: Option<PiSegment> = None;
    let mut current_chars: usize = 0;
    let mut last_user_ms: i64 = 0;

    for (line_idx, line) in raw.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let obj: serde_json::Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(e) => {
                warn!(
                    "pi: skipping unparseable JSON at {}:{} ({e})",
                    path.display(),
                    line_idx + 1
                );
                continue;
            }
        };
        // Two line shapes: main (`{"type": "message", ...}`) and reviewer
        // (`{"recordType": "message", ...}` with top-level role/ts/usage).
        // `tool_start`/`tool_end` records duplicate inline toolCalls — skip.
        let is_reviewer_message = obj.get("recordType").and_then(|v| v.as_str()) == Some("message");
        if obj.get("type").and_then(|v| v.as_str()) != Some("message") && !is_reviewer_message {
            continue;
        }
        let msg = obj
            .get("message")
            .cloned()
            .unwrap_or(serde_json::Value::Null);
        let role = if is_reviewer_message {
            obj.get("role")
                .and_then(|v| v.as_str())
                .filter(|r| !r.is_empty())
                .unwrap_or_else(|| msg.get("role").and_then(|v| v.as_str()).unwrap_or(""))
        } else {
            msg.get("role").and_then(|v| v.as_str()).unwrap_or("")
        };
        // Prefer the message-envelope timestamp (epoch ms); then the
        // reviewer's top-level `ts`; then the line ISO timestamp; then mtime.
        let ts = msg
            .get("timestamp")
            .and_then(|v| v.as_i64())
            .filter(|t| *t > 0)
            .or_else(|| obj.get("ts").and_then(|v| v.as_i64()).filter(|t| *t > 0))
            .unwrap_or_else(|| {
                let iso = obj.get("timestamp").and_then(|v| v.as_str()).unwrap_or("");
                let parsed = parse_ts(iso);
                if parsed > 0 { parsed } else { mtime_ms }
            });
        let content = msg
            .get("content")
            .cloned()
            .unwrap_or(serde_json::Value::Null);

        if role == "user" {
            let prompts: Vec<String> = text_blocks(&content)
                .iter()
                .map(|t| t.trim().chars().take(500).collect::<String>())
                .filter(|t: &String| !t.is_empty())
                .collect();
            if prompts.is_empty() {
                continue;
            }

            // Segment boundary: 5-minute gap or char cap.
            if last_user_ms > 0
                && ts > 0
                && (ts - last_user_ms > TASK_GAP_MS || current_chars > MAX_SEGMENT_CHARS)
                && let Some(seg) = current.take()
            {
                if !seg.user_prompts.is_empty()
                    || !seg.tool_calls.is_empty()
                    || !seg.assistant_texts.is_empty()
                {
                    segments.push(seg);
                }
                current_chars = 0;
            }

            let seg = current.get_or_insert_with(|| new_segment(segments.len() as i64, ts));
            if ts > 0 {
                last_user_ms = ts;
                seg.end_time = seg.end_time.max(ts);
            }
            seg.message_count += 1;
            for p in prompts {
                let redacted = redaction.redact(&p).text;
                current_chars += redacted.chars().count();
                seg.user_prompts.push(redacted);
            }
            continue;
        }

        if role == "toolResult" {
            // Outputs can be megabytes (full command output, file reads);
            // summaries come from the `toolCall` arguments. Parity with Cursor.
            continue;
        }

        if role != "assistant" {
            continue;
        }
        let seg = match current.as_mut() {
            Some(s) => s,
            None => continue,
        };
        if ts > 0 {
            seg.end_time = seg.end_time.max(ts);
        }
        seg.message_count += 1;
        // Reviewer lines carry `usage` at the top level; main-shape lines
        // carry it inside the message envelope.
        if let Some(usage) = msg.get("usage").or_else(|| obj.get("usage")) {
            let input = usage.get("input").and_then(|v| v.as_i64()).unwrap_or(0);
            let output = usage.get("output").and_then(|v| v.as_i64()).unwrap_or(0);
            seg.token_count += input + output;
        }

        let Some(blocks) = content.as_array() else {
            continue;
        };
        for b in blocks {
            match b.get("type").and_then(|t| t.as_str()) {
                Some("text") => {
                    let t = b.get("text").and_then(|v| v.as_str()).unwrap_or("");
                    let t = t.trim();
                    if t.is_empty() {
                        continue;
                    }
                    let capped: String = t.chars().take(300).collect();
                    let redacted = redaction.redact(&capped).text;
                    current_chars += redacted.chars().count();
                    seg.assistant_texts.push(redacted);
                }
                Some("toolCall") => {
                    let name = b.get("name").and_then(|v| v.as_str()).unwrap_or("");
                    if name.is_empty() {
                        continue;
                    }
                    let args = b
                        .get("arguments")
                        .cloned()
                        .unwrap_or(serde_json::Value::Null);
                    let summary = redaction.redact(&tool_summary(&args)).text;
                    current_chars += summary.chars().count();
                    seg.tool_calls.push(ToolCall {
                        name: name.to_string(),
                        summary,
                    });
                }
                // `thinking` (opaque/encrypted) and `image` carry nothing enrichable.
                _ => {}
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

    // Backfill zero start times (no parseable timestamp anywhere) from mtime so
    // freshness probes see real activity instead of epoch 0.
    if header_ts <= 0 && mtime_ms > 0 {
        for seg in &mut segments {
            if seg.start_time <= 0 {
                seg.start_time = mtime_ms;
            }
            if seg.end_time <= 0 {
                seg.end_time = mtime_ms;
            }
        }
    }

    Ok(segments)
}

/// Build the Pi-framed enrichment digest stored in
/// `agentic_sessions.summary_text` (harness = 'pi').
pub(crate) fn build_summary_text(seg: &PiSegment) -> String {
    const MAX_PROMPTS: usize = 30;
    const MAX_TOOLS: usize = 60;
    const MAX_ASSISTANT: usize = 5;
    let header = if seg.is_subagent {
        format!("Pi session (subagent, project: {})", seg.cwd)
    } else {
        format!("Pi session (project: {})", seg.cwd)
    };
    let mut lines = vec![header];
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

/// SHA256 (lowercase hex) of enrichment-relevant content: tool_calls_json |
/// user_prompts_json | assistant_texts joined by "\n". Same construction as
/// `cursor_session::compute_content_hash`.
pub(crate) fn compute_content_hash(seg: &PiSegment) -> String {
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
/// enrichment. Direct port of `cursor_session::decide_enqueue` — Pi shares
/// `agentic_enrichment_queue`, so it must share the re-enrichment gate or a
/// re-parsed (mtime-bumped) file re-pends every already-enriched segment.
fn decide_enqueue(
    was_insert: bool,
    current_hash: &str,
    prior_last_enriched_hash: Option<&str>,
    prior_queue_status: Option<&str>,
    prior_queue_updated_at_ms: Option<i64>,
    now_ms: i64,
) -> bool {
    if was_insert {
        return true;
    }
    if prior_queue_status == Some("processing") {
        return false;
    }
    if prior_last_enriched_hash == Some(current_hash) {
        return false;
    }
    if let Some(updated_at) = prior_queue_updated_at_ms
        && (now_ms - updated_at) < 300_000
    {
        return false;
    }
    true
}

/// Upsert one segment into `agentic_sessions` and (re-)enqueue it, inside a
/// caller-supplied transaction. Idempotent via `ON CONFLICT (session_id,
/// harness, segment_index)`.
pub fn upsert_segment_tx(tx: &rusqlite::Transaction<'_>, seg: &PiSegment) -> Result<()> {
    let now_ms = chrono::Utc::now().timestamp_millis();
    let tool_calls_json = serde_json::to_string(&seg.tool_calls).unwrap_or_else(|_| "[]".into());
    let user_prompts_json =
        serde_json::to_string(&seg.user_prompts).unwrap_or_else(|_| "[]".into());
    let summary_text = build_summary_text(seg);
    let content_hash = compute_content_hash(seg);

    #[allow(clippy::type_complexity)]
    let prior: Option<(i64, Option<String>, Option<String>, Option<i64>)> = tx
        .query_row(
            "SELECT s.id, s.last_enriched_content_hash, q.status, q.updated_at
             FROM agentic_sessions s
             LEFT JOIN agentic_enrichment_queue q ON q.session_id = s.id
             WHERE s.session_id = ?1
               AND s.harness = 'pi'
               AND s.segment_index = ?2",
            params![seg.session_id, seg.segment_index],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .optional()?;
    let was_insert = prior.is_none();
    let prior_last_enriched_hash = prior.as_ref().and_then(|(_, h, _, _)| h.as_deref());
    let prior_queue_status = prior.as_ref().and_then(|(_, _, s, _)| s.as_deref());
    let prior_queue_updated_at_ms = prior.as_ref().and_then(|(_, _, _, u)| *u);

    let is_subagent_i = if seg.is_subagent { 1 } else { 0 };
    tx.execute(
        "INSERT INTO agentic_sessions
            (session_id, harness, segment_index, model, agent, project_dir, cwd,
             git_branch, slug, title, parent_session_id, is_subagent, summary_text,
             tool_calls_json, user_prompts_json, source_file, snapshot_diffs_json,
             commit_messages_json, message_count, token_count, start_time, end_time,
             content_hash, created_at)
         VALUES (?1, 'pi', ?2, '', '', ?3, ?4, NULL, '', '', ?5, ?6, ?7, ?8, ?9, ?10,
                 'null', '[]', ?11, ?12, ?13, ?14, ?15, ?16)
         ON CONFLICT (session_id, harness, segment_index) DO UPDATE SET
             end_time          = excluded.end_time,
             summary_text      = excluded.summary_text,
             tool_calls_json   = excluded.tool_calls_json,
             user_prompts_json = excluded.user_prompts_json,
             message_count     = excluded.message_count,
             token_count       = excluded.token_count,
             content_hash      = excluded.content_hash,
             cwd               = excluded.cwd,
             project_dir       = excluded.project_dir,
             is_subagent       = excluded.is_subagent,
             parent_session_id = excluded.parent_session_id",
        params![
            seg.session_id,
            seg.segment_index,
            seg.project_dir,
            seg.cwd,
            seg.parent_session_id,
            is_subagent_i,
            summary_text,
            tool_calls_json,
            user_prompts_json,
            seg.source_file,
            seg.message_count,
            seg.token_count,
            seg.start_time,
            seg.end_time,
            content_hash,
            now_ms,
        ],
    )?;

    let agentic_session_id: i64 = if was_insert {
        tx.last_insert_rowid()
    } else {
        prior.as_ref().map(|(id, _, _, _)| *id).unwrap()
    };

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

/// Stable inode-keyed cursor for one transcript file. The `pi-` prefix
/// disambiguates from the other pollers' keys in `agentic_cursor`.
fn cursor_key(meta: &std::fs::Metadata) -> String {
    use std::os::unix::fs::MetadataExt;
    format!("pi-{}", meta.ino())
}

fn read_cursor(conn: &rusqlite::Connection, key: &str) -> i64 {
    conn.query_row(
        "SELECT last_seen_updated_at FROM agentic_cursor WHERE source_key = ?1",
        params![key],
        |r| r.get(0),
    )
    .unwrap_or(0)
}

/// True when the poller has already consumed this exact file version and the
/// parser intentionally produced no session row.
pub(crate) fn processed_without_segments(conn: &rusqlite::Connection, path: &Path) -> bool {
    let Ok(meta) = std::fs::metadata(path) else {
        return false;
    };
    let Some(mtime_ms) = meta
        .modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_millis() as i64)
    else {
        return false;
    };
    let key = cursor_key(&meta);
    conn.query_row(
        "SELECT last_seen_updated_at >= ?2
                AND (COALESCE(last_id, '') = '' OR ?3 = 0)
         FROM agentic_cursor WHERE source_key = ?1",
        params![key, mtime_ms, meta.len() as i64],
        |row| row.get::<_, bool>(0),
    )
    .unwrap_or(false)
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
         WHERE source = 'agentic-session-pi'",
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
         WHERE source = 'agentic-session-pi'",
        params![now, format!("{err:#}")],
    ) {
        warn!("pi source_health error update failed: {e}");
    }
}

/// True for any `*.jsonl` under the pi sessions roots. The roots themselves
/// are the `<slug>/` parents, so no path-component gate is needed; the
/// extension excludes the `.md`/`.json` subagent artifacts.
pub(crate) fn is_transcript(path: &Path) -> bool {
    path.extension().map(|e| e == "jsonl").unwrap_or(false)
}

/// One poll cycle: walk every root, ingest changed idle transcript files.
pub fn poll_tick(config: &HippoConfig) -> Result<usize> {
    if !config.pi.enabled {
        debug!("pi poll disabled by config");
        return Ok(0);
    }
    let conn = hippo_core::storage::open_db(&config.db_path())?;
    let now_ms = chrono::Utc::now().timestamp_millis();
    let min_idle_ms = config.pi.min_idle_secs as i64 * 1000;
    let redaction = crate::load_redaction_engine(config);

    let mut ingested = 0usize;
    for root in &config.pi.session_roots {
        if !root.is_dir() {
            continue;
        }
        for entry in WalkDir::new(root).into_iter().filter_map(|e| e.ok()) {
            let path = entry.path();
            if !is_transcript(path) {
                continue;
            }
            let meta = match entry.metadata() {
                Ok(m) => m,
                Err(_) => continue,
            };
            let Some(mtime_ms) = meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_millis() as i64)
            else {
                warn!(
                    "pi: unreadable mtime for {}, skipping this tick",
                    path.display()
                );
                continue;
            };
            if now_ms - mtime_ms < min_idle_ms {
                continue;
            }
            let key = cursor_key(&meta);
            if mtime_ms <= read_cursor(&conn, &key) {
                continue;
            }
            match ingest_file(&conn, path, mtime_ms, &redaction) {
                Ok((count, session_id)) => {
                    ingested += count;
                    if count > 0 {
                        bump_health_ok(&conn, mtime_ms);
                    }
                    if let Err(e) = write_cursor(&conn, &key, mtime_ms, &session_id) {
                        warn!("pi write failed for {}: {e:#}", path.display());
                    }
                }
                Err(e) => {
                    error!("pi ingest failed for {}: {e:#}", path.display());
                    record_error(&conn, &e);
                }
            }
        }
    }
    info!(ingested, "pi poll tick: completed");
    Ok(ingested)
}

/// One-shot manual import of a single Pi transcript (recovery/backfill).
pub fn ingest_one(config: &HippoConfig, path: &Path) -> Result<usize> {
    // Absolutize so the stored `source_file` matches what `poll_tick`
    // discovers via `WalkDir` (absolute paths under `session_roots`).
    let path = std::path::absolute(path)
        .with_context(|| format!("absolutize pi ingest path {}", path.display()))?;
    let conn = hippo_core::storage::open_db(&config.db_path())?;
    let meta = std::fs::metadata(&path)
        .with_context(|| format!("read metadata for pi ingest path {}", path.display()))?;
    let mtime_ms = meta
        .modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_millis() as i64)
        .unwrap_or_else(|| chrono::Utc::now().timestamp_millis());
    let redaction = crate::load_redaction_engine(config);
    let (count, session_id) = ingest_file(&conn, &path, mtime_ms, &redaction)?;
    if count > 0 {
        bump_health_ok(&conn, mtime_ms);
    }
    let key = cursor_key(&meta);
    if let Err(e) = write_cursor(&conn, &key, mtime_ms, &session_id) {
        warn!("pi write failed for {}: {e:#}", path.display());
    }
    Ok(count)
}

/// Parse one file and upsert all its segments in a single transaction.
fn ingest_file(
    conn: &rusqlite::Connection,
    path: &Path,
    mtime_ms: i64,
    redaction: &RedactionEngine,
) -> Result<(usize, String)> {
    let segments = extract_segments(path, mtime_ms, redaction)?;
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

/// Session id for a transcript file, resolved exactly the way
/// `extract_segments` resolves it (header id, else first reviewer `runId`,
/// else filename-derived). Doctor coverage uses this so the compared key can
/// never drift from the stored row key.
pub(crate) fn session_file_id(path: &Path) -> String {
    let Ok(raw) = std::fs::read_to_string(path) else {
        return session_id_from_filename(path);
    };
    for line in raw.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let Ok(obj) = serde_json::from_str::<serde_json::Value>(line) else {
            continue;
        };
        if obj.get("type").and_then(|v| v.as_str()) == Some("session")
            && let Some(id) = obj.get("id").and_then(|v| v.as_str())
            && !id.is_empty()
        {
            return id.to_string();
        }
        if obj.get("recordType").and_then(|v| v.as_str()) == Some("message")
            && let Some(run_id) = obj.get("runId").and_then(|v| v.as_str())
            && !run_id.is_empty()
        {
            return run_id.to_string();
        }
    }
    session_id_from_filename(path)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn write_transcript(dir: &Path, slug: &str, name: &str, lines: &[&str]) -> PathBuf {
        let d = dir.join(slug);
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join(name);
        std::fs::write(&p, lines.join("\n")).unwrap();
        p
    }

    fn sample_segment() -> PiSegment {
        PiSegment {
            session_id: "s1".into(),
            project_dir: "proj".into(),
            cwd: "/work/proj".into(),
            segment_index: 0,
            start_time: 1_775_634_000_000,
            end_time: 1_775_634_500_000,
            user_prompts: vec!["fix the bug".into()],
            assistant_texts: vec!["done".into()],
            tool_calls: vec![ToolCall {
                name: "bash".into(),
                summary: "cargo test".into(),
            }],
            message_count: 3,
            token_count: 42,
            source_file:
                "/Users/x/.pi/agent/sessions/--Users-x-proj--/2026-01-01T00-00-00-000Z_s1.jsonl"
                    .into(),
            is_subagent: false,
            parent_session_id: None,
        }
    }

    #[test]
    fn summary_text_includes_prompts_tools_and_project() {
        let s = build_summary_text(&sample_segment());
        assert!(s.contains("Pi session"));
        assert!(s.contains("/work/proj"));
        assert!(s.contains("fix the bug"));
        assert!(s.contains("bash"));
        assert!(s.contains("cargo test"));
    }

    #[test]
    fn content_hash_is_stable_and_changes_with_content() {
        let a = compute_content_hash(&sample_segment());
        assert_eq!(a, compute_content_hash(&sample_segment()));
        let mut changed = sample_segment();
        changed.user_prompts = vec!["different".into()];
        assert_ne!(a, compute_content_hash(&changed));
        assert_eq!(a.len(), 64);
    }

    #[test]
    fn tool_summary_prefers_command_then_path() {
        assert_eq!(
            tool_summary(&serde_json::json!({"command": "cargo test"})),
            "cargo test"
        );
        assert_eq!(
            tool_summary(&serde_json::json!({"path": "/tmp/x.rs"})),
            "/tmp/x.rs"
        );
        assert_eq!(
            tool_summary(&serde_json::json!({"server": "hippo", "search": "ask lessons"})),
            "ask lessons"
        );
        assert_eq!(tool_summary(&serde_json::json!({})), "{}");
    }

    #[test]
    fn session_id_from_filename_handles_all_shapes() {
        let uuid = "01a0756f-4443-70dc-9ff9-96af11f97cc1";
        let name1 = format!("/s/--x--/2026-09-06T06-37-01-379Z_{uuid}.jsonl");
        let p = Path::new(&name1);
        assert_eq!(session_id_from_filename(p), uuid);
        // Subagent review transcript: strip the suffix, keep the id.
        let rid = "250b4a64-9fdf-4e02-b51d-707dfee030a5";
        let name2 = format!("/s/--x--/subagent-artifacts/{rid}_reviewer_transcript.jsonl");
        let p2 = Path::new(&name2);
        assert_eq!(session_id_from_filename(p2), rid);
        // No underscore: full stem.
        let p3 = Path::new("/s/--x--/plainname.jsonl");
        assert_eq!(session_id_from_filename(p3), "plainname");
    }

    #[test]
    fn identity_prefers_header_cwd_over_slug() {
        let p = Path::new("/Users/me/.pi/agent/sessions/--Users-me-my-app--/t_uuid-x.jsonl");
        let id = PathIdentity::from_parts(p, Some("sess-1"), "/Users/me/my-app", None);
        assert_eq!(id.session_id, "sess-1");
        assert_eq!(id.cwd, "/Users/me/my-app");
        assert_eq!(id.project_dir, "my-app");
        assert!(!id.is_subagent);
    }

    #[test]
    fn extract_segments_parses_basic_transcript() {
        let tmp = tempfile::tempdir().unwrap();
        let p = write_transcript(
            tmp.path(),
            "--Users-me-proj--",
            "2026-09-06T06-37-01-379Z_abc.jsonl",
            &[
                r#"{"type":"session","version":3,"id":"sess-9","timestamp":"2026-09-06T06:37:01.379Z","cwd":"/Users/me/proj"}"#,
                r#"{"type":"message","id":"m1","timestamp":"2026-09-06T06:39:40.751Z","message":{"role":"user","content":[{"type":"text","text":"fix the build"}],"timestamp":1788676780747}}"#,
                r#"{"type":"message","id":"m2","timestamp":"2026-09-06T06:39:44.858Z","message":{"role":"assistant","content":[{"type":"toolCall","id":"c1","name":"bash","arguments":{"command":"cargo build"}}],"usage":{"input":10,"output":5}},"timestamp":1788676784858}"#,
                r#"{"type":"message","id":"m3","timestamp":"2026-09-06T06:39:44.890Z","message":{"role":"toolResult","toolCallId":"c1","toolName":"bash","content":[{"type":"text","text":"error: build failed"}],"isError":true,"timestamp":1788676784890}}"#,
            ],
        );
        let segs = extract_segments(&p, 1_788_676_785_000, &RedactionEngine::builtin()).unwrap();
        assert_eq!(segs.len(), 1);
        let s = &segs[0];
        assert_eq!(s.session_id, "sess-9");
        assert_eq!(s.cwd, "/Users/me/proj");
        assert_eq!(s.user_prompts, vec!["fix the build".to_string()]);
        assert_eq!(s.tool_calls.len(), 1);
        assert_eq!(s.tool_calls[0].name, "bash");
        assert_eq!(s.tool_calls[0].summary, "cargo build");
        // toolResult outputs are not stored.
        assert!(s.assistant_texts.is_empty());
        assert_eq!(s.token_count, 15);
        assert_eq!(s.message_count, 2);
        assert!(s.start_time > 0 && s.end_time >= s.start_time);
    }

    #[test]
    fn extract_segments_splits_on_five_minute_gap() {
        let tmp = tempfile::tempdir().unwrap();
        let p = write_transcript(
            tmp.path(),
            "--Users-me-proj--",
            "2026-09-06T00-00-00-000Z_gap.jsonl",
            &[
                r#"{"type":"session","version":3,"id":"gap-1","timestamp":"2026-09-06T00:00:00.000Z","cwd":"/Users/me/proj"}"#,
                r#"{"type":"message","id":"m1","timestamp":"2026-09-06T00:00:01.000Z","message":{"role":"user","content":[{"type":"text","text":"first"}],"timestamp":1000}}"#,
                r#"{"type":"message","id":"m2","timestamp":"2026-09-06T00:10:01.000Z","message":{"role":"user","content":[{"type":"text","text":"second"}],"timestamp":601000}}"#,
            ],
        );
        let segs = extract_segments(&p, 601_000, &RedactionEngine::builtin()).unwrap();
        assert_eq!(segs.len(), 2, "10-minute gap must split the session");
        assert_eq!(segs[1].segment_index, 1);
    }

    #[test]
    fn extract_segments_skips_thinking_and_model_lines() {
        let tmp = tempfile::tempdir().unwrap();
        let p = write_transcript(
            tmp.path(),
            "--Users-me-proj--",
            "2026-09-06T00-00-00-000Z_noise.jsonl",
            &[
                r#"{"type":"session","version":3,"id":"n-1","timestamp":"2026-09-06T00:00:00.000Z","cwd":"/Users/me/proj"}"#,
                r#"{"type":"model_change","id":"a","timestamp":"2026-09-06T00:00:01.000Z","provider":"x","modelId":"y"}"#,
                r#"{"type":"message","id":"m1","timestamp":"2026-09-06T00:00:02.000Z","message":{"role":"user","content":[{"type":"text","text":"hi"}],"timestamp":2000}}"#,
                r#"{"type":"message","id":"m2","timestamp":"2026-09-06T00:00:03.000Z","message":{"role":"assistant","content":[{"type":"thinking","thinking":"secret"},{"type":"text","text":"hello there, this is a long enough reply"}],"timestamp":3000}}"#,
                r#"{"type":"custom_message","customType":"subagent-notify","content":"noise","id":"c1","timestamp":"2026-09-06T00:00:04.000Z"}"#,
            ],
        );
        let segs = extract_segments(&p, 4_000, &RedactionEngine::builtin()).unwrap();
        assert_eq!(segs.len(), 1);
        assert_eq!(segs[0].tool_calls.len(), 0);
        assert_eq!(segs[0].assistant_texts.len(), 1);
        assert!(!segs[0].assistant_texts[0].contains("secret"));
    }

    #[test]
    fn extract_segments_warns_and_continues_on_unparseable_line() {
        let tmp = tempfile::tempdir().unwrap();
        let p = write_transcript(
            tmp.path(),
            "--Users-me-proj--",
            "2026-09-06T00-00-00-000Z_bad.jsonl",
            &[
                r#"{"type":"session","version":3,"id":"b-1","timestamp":"2026-09-06T00:00:00.000Z","cwd":"/Users/me/proj"}"#,
                r#"{"type":"message","id":"m1","timestamp":"2026-09-06T00:00:02.000Z","message":{"role":"user","content":[{"type":"text","text":"fix it"}],"timestamp":2000}}"#,
                r#"{this is not { valid json"#,
                r#"{"type":"message","id":"m2","timestamp":"2026-09-06T00:00:03.000Z","message":{"role":"assistant","content":[{"type":"text","text":"on it, working through the details now"}],"timestamp":3000}}"#,
            ],
        );
        let segs = extract_segments(&p, 4_000, &RedactionEngine::builtin()).unwrap();
        assert_eq!(segs.len(), 1);
        assert_eq!(segs[0].user_prompts, vec!["fix it".to_string()]);
    }

    #[test]
    fn extract_segments_parses_committed_fixture() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures/pi/session-basic.jsonl");
        let segs = extract_segments(&path, 1_788_676_785_100, &RedactionEngine::builtin()).unwrap();
        assert_eq!(segs.len(), 1);
        let s = &segs[0];
        assert_eq!(s.session_id, "pi-fixture-session-1");
        assert_eq!(s.user_prompts, vec!["fix the failing build".to_string()]);
        assert_eq!(s.tool_calls.len(), 2);
        assert_eq!(s.tool_calls[0].name, "bash");
        assert_eq!(s.tool_calls[0].summary, "cargo build");
        assert_eq!(s.tool_calls[1].summary, "/Users/me/proj/src/main.rs");
        assert!(
            s.assistant_texts
                .iter()
                .any(|t| t.contains("type mismatch"))
        );
    }

    #[test]
    fn extract_segments_parses_reviewer_transcript() {
        let tmp = tempfile::tempdir().unwrap();
        let subdir = tmp
            .path()
            .join("--Users-me-dotfiles--")
            .join("subagent-artifacts");
        std::fs::create_dir_all(&subdir).unwrap();
        let rp = subdir.join("run-abc_reviewer_transcript.jsonl");
        std::fs::write(
            &rp,
            [
                r#"{"version":1,"recordType":"message","runId":"run-abc","cwd":"/Users/me/.dotfiles","ts":1788677483387,"timestamp":"2026-09-06T06:51:23.387Z","role":"user","message":{"role":"user","content":[{"type":"text","text":"review these files"}]}}"#,
                r#"{"version":1,"recordType":"message","runId":"run-abc","cwd":"/Users/me/.dotfiles","ts":1788677484228,"timestamp":"2026-09-06T06:51:24.228Z","role":"assistant","usage":{"input":100,"output":20},"message":{"role":"assistant","content":[{"type":"toolCall","id":"c1","name":"read","arguments":{"path":"/Users/me/.dotfiles/Justfile"}}]}}"#,
                r#"{"version":1,"recordType":"tool_start","runId":"run-abc","ts":1788677485000,"toolName":"read","argsPayload":"{\"path\": \"/x\"}"}"#,
                r#"{"version":1,"recordType":"tool_end","runId":"run-abc","ts":1788677485100,"toolName":"read","isError":false}"#,
            ]
            .join("\n"),
        )
        .unwrap();
        let segs = extract_segments(&rp, 1_788_677_485_100, &RedactionEngine::builtin()).unwrap();
        assert_eq!(segs.len(), 1);
        let s = &segs[0];
        assert_eq!(s.session_id, "run-abc");
        assert_eq!(s.cwd, "/Users/me/.dotfiles");
        assert!(s.is_subagent, "reviewer transcripts are subagent runs");
        assert_eq!(s.user_prompts, vec!["review these files".to_string()]);
        // Inline toolCall counted once; tool_start/tool_end ignored.
        assert_eq!(s.tool_calls.len(), 1);
        assert_eq!(s.tool_calls[0].summary, "/Users/me/.dotfiles/Justfile");
        assert_eq!(s.token_count, 120);
        assert_eq!(session_file_id(&rp), "run-abc");
    }

    #[test]
    fn run_session_identity_links_parent() {
        let tmp = tempfile::tempdir().unwrap();
        let parent = "2026-09-06T01-45-56-952Z_01a07464-c7d8-7017-b4e3-87df8e31b863";
        let dir = tmp
            .path()
            .join("--Users-me-dotfiles--")
            .join(parent)
            .join("4156bb1b-94b1-4b3b-a9bd-4cc4f87e0f37")
            .join("run-0");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("session.jsonl");
        std::fs::write(
            &p,
            r#"{"type":"session","version":3,"id":"01a074eb-bb34-7418-addc-990702ffc012","timestamp":"2026-09-06T04:13:21.076Z","cwd":"/Users/me/.dotfiles"}"#,
        )
        .unwrap();
        let id = PathIdentity::from_parts(
            &p,
            Some("01a074eb-bb34-7418-addc-990702ffc012"),
            "/Users/me/.dotfiles",
            None,
        );
        assert!(id.is_subagent);
        assert_eq!(
            id.parent_session_id.as_deref(),
            Some("01a07464-c7d8-7017-b4e3-87df8e31b863")
        );
        // Header id wins over the constant `session` stem.
        assert_eq!(session_file_id(&p), "01a074eb-bb34-7418-addc-990702ffc012");
    }

    #[test]
    fn decide_enqueue_gates_on_content_change() {
        assert!(decide_enqueue(true, "h1", None, None, None, 1_000));
        assert!(!decide_enqueue(false, "h1", Some("h1"), None, None, 1_000));
        assert!(!decide_enqueue(
            false,
            "h2",
            Some("h1"),
            Some("processing"),
            None,
            1_000
        ));
        assert!(decide_enqueue(
            false,
            "h2",
            Some("h1"),
            Some("failed"),
            Some(0),
            400_000
        ));
    }
}
