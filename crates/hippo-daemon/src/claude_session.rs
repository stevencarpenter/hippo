use std::collections::HashMap;
use std::io::{BufRead, Seek, SeekFrom};
use std::path::Path;
use std::time::Duration;

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use hippo_core::events::{
    CapturedOutput, EventEnvelope, EventPayload, GitState, ShellEvent, ShellKind,
};
use hippo_core::redaction::RedactionEngine;
use rusqlite::{Connection, params};
use tracing::{error, info, warn};
use uuid::Uuid;

use crate::commands::send_event_fire_and_forget;

/// Maximum bytes to store in CapturedOutput
const MAX_OUTPUT_BYTES: usize = 4096;

/// Gap between user prompts that forces a new segment (5 minutes in ms).
///
/// Kept in lockstep with `TASK_GAP_MS` in
/// `brain/src/hippo_brain/claude_sessions.py`. Tasks separated by more than
/// this gap are considered distinct work units and get their own enrichment.
const SEGMENT_GAP_MS: i64 = 5 * 60 * 1000;

/// Maximum accumulated free-text chars before forcing a segment split.
///
/// Matches `max_prompt_chars` default in the Python `extract_segments`.
/// Prevents runaway single segments from exceeding the enrichment model's
/// context window.
const MAX_SEGMENT_CHARS: usize = 12_000;

/// Pending tool use waiting for its result
struct PendingToolUse {
    tool_use_id: String,
    name: String,
    input: serde_json::Value,
    timestamp: DateTime<Utc>,
    session_id: String,
    cwd: String,
    git_branch: Option<String>,
    /// Resolved once at insertion via a per-session `cwd -> owner/repo`
    /// cache so we don't spawn `git` per envelope during batch imports
    /// of long sessions.
    git_repo: Option<String>,
}

/// Format a tool_use into a human-readable command string
fn format_tool_command(name: &str, input: &serde_json::Value) -> String {
    match name {
        "Bash" => input
            .get("command")
            .and_then(|v| v.as_str())
            .unwrap_or("bash")
            .to_string(),
        "Read" => {
            let path = input
                .get("file_path")
                .and_then(|v| v.as_str())
                .unwrap_or("<unknown>");
            format!("read {}", path)
        }
        "Edit" => {
            let path = input
                .get("file_path")
                .and_then(|v| v.as_str())
                .unwrap_or("<unknown>");
            format!("edit {}", path)
        }
        "Write" => {
            let path = input
                .get("file_path")
                .and_then(|v| v.as_str())
                .unwrap_or("<unknown>");
            format!("write {}", path)
        }
        "Grep" => {
            let pattern = input.get("pattern").and_then(|v| v.as_str()).unwrap_or("*");
            let path = input.get("path").and_then(|v| v.as_str()).unwrap_or(".");
            format!("grep '{}' {}", pattern, path)
        }
        "Glob" => {
            let pattern = input.get("pattern").and_then(|v| v.as_str()).unwrap_or("*");
            format!("glob '{}'", pattern)
        }
        "Agent" => {
            let desc = input
                .get("description")
                .and_then(|v| v.as_str())
                .unwrap_or("agent task");
            format!("agent: {}", desc)
        }
        "TaskCreate" => {
            let subject = input
                .get("subject")
                .and_then(|v| v.as_str())
                .unwrap_or("task");
            format!("task: {}", subject)
        }
        "TaskUpdate" => {
            let task_id = input.get("taskId").and_then(|v| v.as_str()).unwrap_or("?");
            let status = input.get("status").and_then(|v| v.as_str()).unwrap_or("?");
            format!("task-update: {} {}", task_id, status)
        }
        other => other.to_string(),
    }
}

/// Extract text content from a tool_result content field.
/// Content can be either a string or an array of content blocks.
fn extract_result_content(content: &serde_json::Value) -> Option<String> {
    content.as_str().map(str::to_string).or_else(|| {
        let parts: Vec<&str> = content
            .as_array()?
            .iter()
            .filter_map(|b| b.get("text").and_then(|t| t.as_str()))
            .collect();
        (!parts.is_empty()).then(|| parts.join("\n"))
    })
}

/// Truncate a string to at most `max_bytes` on a char boundary.
fn truncate_to_bytes(s: &str, max_bytes: usize) -> (&str, bool) {
    if s.len() <= max_bytes {
        return (s, false);
    }
    let mut end = max_bytes;
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    (&s[..end], true)
}

/// Convert a tool_use + optional tool_result into an EventEnvelope
fn build_envelope(
    pending: &PendingToolUse,
    result_content: Option<&str>,
    result_is_error: bool,
    result_timestamp: Option<DateTime<Utc>>,
    hostname: &str,
) -> EventEnvelope {
    let command = format_tool_command(&pending.name, &pending.input);

    let exit_code = if result_is_error { 1 } else { 0 };

    let duration_ms = result_timestamp
        .map(|rt| {
            let diff = rt.signed_duration_since(pending.timestamp);
            diff.num_milliseconds().max(0) as u64
        })
        .unwrap_or(0);

    let session_id = Uuid::parse_str(&pending.session_id)
        .unwrap_or_else(|_| Uuid::new_v5(&Uuid::NAMESPACE_URL, pending.session_id.as_bytes()));

    let git_state = if pending.git_repo.is_some() || pending.git_branch.is_some() {
        Some(GitState {
            repo: pending.git_repo.clone(),
            branch: pending.git_branch.clone(),
            commit: None,
            is_dirty: false,
        })
    } else {
        None
    };

    let stdout = result_content.map(|content| {
        let original_bytes = content.len();
        let (truncated_str, was_truncated) = truncate_to_bytes(content, MAX_OUTPUT_BYTES);
        CapturedOutput {
            content: truncated_str.to_string(),
            truncated: was_truncated,
            original_bytes,
        }
    });

    let envelope_id = Uuid::new_v5(&Uuid::NAMESPACE_URL, pending.tool_use_id.as_bytes());

    let event = ShellEvent {
        session_id,
        command,
        exit_code,
        duration_ms,
        cwd: pending.cwd.clone().into(),
        hostname: hostname.to_string(),
        shell: ShellKind::Unknown("claude-code".to_string()),
        stdout,
        stderr: None,
        env_snapshot: HashMap::new(),
        git_state,
        redaction_count: 0,
        tool_name: Some(pending.name.clone()),
    };

    EventEnvelope {
        envelope_id,
        producer_version: 1,
        timestamp: pending.timestamp,
        payload: EventPayload::Shell(Box::new(event)),
        probe_tag: None,
    }
}

/// Process a single JSONL line. Returns envelopes for any completed tool uses.
///
/// `git_repo_cache` memoizes `cwd -> owner/repo` across a session so
/// `git config --get` runs at most once per unique cwd rather than per
/// envelope — matters for batch-importing long sessions.
fn process_line(
    line: &str,
    pending: &mut HashMap<String, PendingToolUse>,
    git_repo_cache: &mut HashMap<String, Option<String>>,
    hostname: &str,
) -> Result<Vec<EventEnvelope>> {
    let line = line.trim();
    if line.is_empty() {
        return Ok(vec![]);
    }

    let value: serde_json::Value = serde_json::from_str(line).context("invalid JSON line")?;

    let msg_type = value.get("type").and_then(|v| v.as_str()).unwrap_or("");

    // Skip non-message types
    match msg_type {
        "assistant" | "user" => {}
        _ => return Ok(vec![]),
    }

    let content_array = match value
        .get("message")
        .and_then(|m| m.get("content"))
        .and_then(|c| c.as_array())
    {
        Some(arr) => arr,
        None => return Ok(vec![]),
    };

    let mut envelopes = Vec::new();

    if msg_type == "assistant" {
        // Extract metadata from the outer object
        let timestamp_str = value
            .get("timestamp")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let timestamp = timestamp_str
            .parse::<DateTime<Utc>>()
            .unwrap_or_else(|_| Utc::now());
        let session_id = value
            .get("sessionId")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let cwd = value
            .get("cwd")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let git_branch = value
            .get("gitBranch")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());

        for block in content_array {
            if block.get("type").and_then(|v| v.as_str()) == Some("tool_use") {
                let tool_use_id = block
                    .get("id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let name = block
                    .get("name")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let input = block
                    .get("input")
                    .cloned()
                    .unwrap_or(serde_json::Value::Object(serde_json::Map::new()));

                if !tool_use_id.is_empty() {
                    let git_repo = git_repo_cache
                        .entry(cwd.clone())
                        .or_insert_with(|| crate::git_repo::derive_git_repo(Path::new(&cwd)))
                        .clone();
                    pending.insert(
                        tool_use_id.clone(),
                        PendingToolUse {
                            tool_use_id,
                            name,
                            input,
                            timestamp,
                            session_id: session_id.clone(),
                            cwd: cwd.clone(),
                            git_branch: git_branch.clone(),
                            git_repo,
                        },
                    );
                }
            }
        }
    } else if msg_type == "user" {
        // Look for tool_result blocks
        let result_timestamp_str = value
            .get("timestamp")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let result_timestamp = result_timestamp_str.parse::<DateTime<Utc>>().ok();

        for block in content_array {
            if block.get("type").and_then(|v| v.as_str()) == Some("tool_result") {
                let tool_use_id = block
                    .get("tool_use_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");

                if let Some(pending_tool) = pending.remove(tool_use_id) {
                    let is_error = block
                        .get("is_error")
                        .and_then(|v| v.as_bool())
                        .unwrap_or(false);

                    let content_val = block.get("content");
                    let content_text = content_val.and_then(extract_result_content);

                    let envelope = build_envelope(
                        &pending_tool,
                        content_text.as_deref(),
                        is_error,
                        result_timestamp,
                        hostname,
                    );
                    envelopes.push(envelope);
                }
            }
        }
    }

    Ok(envelopes)
}

/// A parsed conversation segment to upsert into the `claude_sessions` table.
///
/// Mirrors the Python `SessionSegment` in
/// `brain/src/hippo_brain/claude_sessions.py`. Populated directly from a
/// Claude Code JSONL transcript without going through the daemon socket so
/// the batch importer can survive without brain running.
#[derive(Debug, Clone)]
struct SessionSegment {
    session_id: String,
    project_dir: String,
    cwd: String,
    git_branch: Option<String>,
    segment_index: i64,
    start_time: i64,
    end_time: i64,
    user_prompts: Vec<String>,
    assistant_texts: Vec<String>,
    tool_calls: Vec<ToolCallSummary>,
    message_count: i64,
    token_count: i64,
    source_file: String,
    is_subagent: bool,
    parent_session_id: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize)]
struct ToolCallSummary {
    name: String,
    summary: String,
}

/// Describes how a JSONL path decomposes into the fields the `claude_sessions`
/// row needs. We derive these from the filesystem layout Claude Code uses:
///
/// ```text
/// <projects-root>/<project-encoded>/<session-uuid>.jsonl              (main)
/// <projects-root>/<project-encoded>/<parent-uuid>/subagents/<id>.jsonl (subagent)
/// ```
///
/// `project_dir` is the encoded project directory name (e.g.
/// `-Users-carpenter-projects-hippo`). We keep the encoded form because the
/// Python ingester uses the same value — mixing encodings between paths would
/// make dedupe-by-(session_id, segment_index) lookups miss.
struct SessionFile<'a> {
    path: &'a Path,
    project_dir: String,
    session_id: String,
    is_subagent: bool,
    parent_session_id: Option<String>,
}

impl<'a> SessionFile<'a> {
    fn from_path(path: &'a Path) -> Self {
        let session_id = path
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_string();

        // Detect `<project>/<parent-uuid>/subagents/<id>.jsonl`.
        let parent = path.parent();
        let is_subagent =
            parent.and_then(|p| p.file_name()).and_then(|n| n.to_str()) == Some("subagents");

        let (project_dir, parent_session_id) = if is_subagent {
            // parent = <project>/<parent-uuid>/subagents
            // grandparent = <project>/<parent-uuid>
            // great-grandparent = <project>
            let grandparent = parent.and_then(|p| p.parent());
            let project = grandparent.and_then(|p| p.parent());
            let parent_uuid = grandparent
                .and_then(|p| p.file_name())
                .and_then(|n| n.to_str())
                .map(|s| s.to_string());
            let project_name = project
                .and_then(|p| p.file_name())
                .and_then(|n| n.to_str())
                .unwrap_or("")
                .to_string();
            (project_name, parent_uuid)
        } else {
            // parent = <project>
            let project_name = parent
                .and_then(|p| p.file_name())
                .and_then(|n| n.to_str())
                .unwrap_or("")
                .to_string();
            (project_name, None)
        };

        Self {
            path,
            project_dir,
            session_id,
            is_subagent,
            parent_session_id,
        }
    }
}

/// Extract a concise summary from a `tool_use` content block. Port of the
/// Python `_extract_tool_summary`.
fn extract_tool_summary(block: &serde_json::Value) -> Option<ToolCallSummary> {
    let name = block.get("name").and_then(|v| v.as_str()).unwrap_or("");
    if name.is_empty() {
        return None;
    }
    let inp = block
        .get("input")
        .cloned()
        .unwrap_or(serde_json::Value::Object(Default::default()));

    let summary = match name {
        "Bash" => inp
            .get("command")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .chars()
            .take(200)
            .collect::<String>(),
        "Read" | "Write" | "Edit" => inp
            .get("file_path")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        "Grep" => {
            let pattern = inp.get("pattern").and_then(|v| v.as_str()).unwrap_or("");
            let path = inp.get("path").and_then(|v| v.as_str()).unwrap_or("");
            if path.is_empty() {
                pattern.to_string()
            } else {
                format!("{} in {}", pattern, path)
            }
        }
        "Glob" => inp
            .get("pattern")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        "Agent" => inp
            .get("description")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .chars()
            .take(100)
            .collect::<String>(),
        _ => {
            // Generic: stringify first key=value pair, trimmed to 80 chars.
            if let Some(obj) = inp.as_object()
                && let Some((k, v)) = obj.iter().next()
            {
                let v_str = match v {
                    serde_json::Value::String(s) => s.clone(),
                    other => other.to_string(),
                };
                let truncated: String = v_str.chars().take(80).collect();
                format!("{}={}", k, truncated)
            } else {
                String::new()
            }
        }
    };

    Some(ToolCallSummary {
        name: name.to_string(),
        summary,
    })
}

/// Extract human-typed text from a `user` message, filtering out
/// system/tool-result content. Port of the Python `_extract_user_text`.
fn extract_user_text(msg: &serde_json::Value) -> Option<String> {
    // msg can be a JSON string, or a dict with `content` that is either a
    // string or an array of content blocks. Skip anything that looks like
    // Claude-Code tooling noise (wrapped in `<...>` tags).
    if let Some(s) = msg.as_str() {
        let trimmed = s.trim();
        if trimmed.is_empty() || trimmed.starts_with('<') {
            return None;
        }
        return Some(trimmed.to_string());
    }

    let content = msg.get("content")?;
    if let Some(s) = content.as_str() {
        let trimmed = s.trim();
        if trimmed.is_empty() || trimmed.starts_with('<') {
            return None;
        }
        return Some(trimmed.to_string());
    }

    if let Some(arr) = content.as_array() {
        let texts: Vec<String> = arr
            .iter()
            .filter_map(|block| {
                if block.get("type").and_then(|t| t.as_str()) == Some("text") {
                    let text = block.get("text").and_then(|t| t.as_str()).unwrap_or("");
                    let trimmed = text.trim();
                    if !trimmed.is_empty() && !trimmed.starts_with('<') {
                        Some(trimmed.to_string())
                    } else {
                        None
                    }
                } else {
                    None
                }
            })
            .collect();
        if texts.is_empty() {
            None
        } else {
            Some(texts.join("\n"))
        }
    } else {
        None
    }
}

/// Extract text excerpts and tool calls from an assistant message. Port of
/// the Python `_extract_assistant_text`.
fn extract_assistant_content(msg: &serde_json::Value) -> (Vec<String>, Vec<ToolCallSummary>) {
    let mut texts = Vec::new();
    let mut tools = Vec::new();
    let arr = match msg.get("content").and_then(|c| c.as_array()) {
        Some(a) => a,
        None => return (texts, tools),
    };
    for block in arr {
        let ty = block.get("type").and_then(|t| t.as_str()).unwrap_or("");
        if ty == "text" {
            let text = block
                .get("text")
                .and_then(|t| t.as_str())
                .unwrap_or("")
                .trim();
            if text.len() > 20 {
                // Truncate long reasoning blocks.
                let truncated: String = text.chars().take(300).collect();
                texts.push(truncated);
            }
        } else if ty == "tool_use"
            && let Some(tc) = extract_tool_summary(block)
        {
            tools.push(tc);
        }
    }
    (texts, tools)
}

/// Stream a session JSONL and split it into task-boundary segments.
///
/// This is the Rust port of `extract_segments` in
/// `brain/src/hippo_brain/claude_sessions.py`. The split rule matches the
/// Python side (5-minute gap between user prompts OR accumulated content
/// over `MAX_SEGMENT_CHARS`) so that backfills from this path produce the
/// same `claude_sessions` rows a fresh brain scan would.
///
/// `redaction` — applied to `user_prompts`, `assistant_texts`, and each
/// tool-call summary. Matches the Python `redact_segment_secrets` step; the
/// builtin pattern set is shared with `hippo_brain.redaction`.
fn extract_segments(
    session_file: &SessionFile,
    redaction: &RedactionEngine,
) -> Result<Vec<SessionSegment>> {
    let file = std::fs::File::open(session_file.path)
        .with_context(|| format!("failed to open {}", session_file.path.display()))?;
    let reader = std::io::BufReader::new(file);

    let mut segments: Vec<SessionSegment> = Vec::new();
    let mut current: Option<SessionSegment> = None;
    let mut current_chars: usize = 0;
    let mut last_user_time_ms: i64 = 0;

    for line_result in reader.lines() {
        let line = match line_result {
            Ok(l) => l,
            Err(_) => continue,
        };
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let entry: serde_json::Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(_) => continue,
        };

        let entry_type = entry.get("type").and_then(|v| v.as_str()).unwrap_or("");

        // Skip noise entries that Python filters out. Keep this list in
        // sync with the Python `extract_segments` skip list.
        if matches!(
            entry_type,
            "file-history-snapshot" | "progress" | "queue-operation" | "last-prompt"
        ) {
            continue;
        }

        let ts_ms = entry
            .get("timestamp")
            .and_then(|v| v.as_str())
            .and_then(|s| s.parse::<DateTime<Utc>>().ok())
            .map(|dt| dt.timestamp_millis())
            .unwrap_or(0);

        let cwd = entry
            .get("cwd")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let git_branch = entry
            .get("gitBranch")
            .and_then(|v| v.as_str())
            .map(String::from);

        // Initialize first segment on any meaningful entry.
        if current.is_none() && matches!(entry_type, "user" | "assistant" | "system") {
            let now_ms = if ts_ms > 0 {
                ts_ms
            } else {
                Utc::now().timestamp_millis()
            };
            current = Some(SessionSegment {
                session_id: session_file.session_id.clone(),
                project_dir: session_file.project_dir.clone(),
                cwd: cwd.clone(),
                git_branch: git_branch.clone(),
                segment_index: segments.len() as i64,
                start_time: now_ms,
                end_time: now_ms,
                user_prompts: Vec::new(),
                assistant_texts: Vec::new(),
                tool_calls: Vec::new(),
                message_count: 0,
                token_count: 0,
                source_file: session_file.path.to_string_lossy().into_owned(),
                is_subagent: session_file.is_subagent,
                parent_session_id: session_file.parent_session_id.clone(),
            });
        }

        let seg = match current.as_mut() {
            Some(s) => s,
            None => continue,
        };

        // Check task boundary on user messages.
        if entry_type == "user" && last_user_time_ms > 0 && ts_ms > 0 {
            let gap = ts_ms - last_user_time_ms;
            if gap > SEGMENT_GAP_MS || current_chars > MAX_SEGMENT_CHARS {
                let has_content = !seg.user_prompts.is_empty()
                    || !seg.tool_calls.is_empty()
                    || !seg.assistant_texts.is_empty();
                if has_content {
                    let finished = current.take().expect("seg exists");
                    let next_index = segments.len() as i64 + 1;
                    segments.push(finished);
                    current = Some(SessionSegment {
                        session_id: session_file.session_id.clone(),
                        project_dir: session_file.project_dir.clone(),
                        cwd: if cwd.is_empty() {
                            segments.last().map(|s| s.cwd.clone()).unwrap_or_default()
                        } else {
                            cwd.clone()
                        },
                        git_branch: git_branch
                            .clone()
                            .or_else(|| segments.last().and_then(|s| s.git_branch.clone())),
                        segment_index: next_index - 1,
                        start_time: ts_ms,
                        end_time: ts_ms,
                        user_prompts: Vec::new(),
                        assistant_texts: Vec::new(),
                        tool_calls: Vec::new(),
                        message_count: 0,
                        token_count: 0,
                        source_file: session_file.path.to_string_lossy().into_owned(),
                        is_subagent: session_file.is_subagent,
                        parent_session_id: session_file.parent_session_id.clone(),
                    });
                    current_chars = 0;
                }
            }
        }

        let seg = match current.as_mut() {
            Some(s) => s,
            None => continue,
        };

        if ts_ms > 0 {
            seg.end_time = seg.end_time.max(ts_ms);
        }
        seg.message_count += 1;

        if !cwd.is_empty() {
            seg.cwd = cwd;
        }

        if entry_type == "user" {
            if ts_ms > 0 {
                last_user_time_ms = ts_ms;
            }
            let msg = entry
                .get("message")
                .cloned()
                .unwrap_or_else(|| entry.get("content").cloned().unwrap_or_default());
            if let Some(text) = extract_user_text(&msg) {
                let truncated: String = text.chars().take(500).collect();
                current_chars += truncated.len();
                seg.user_prompts.push(redaction.redact(&truncated).text);
            }
            if let Some(usage) = msg.get("usage")
                && let Some(input_tokens) = usage.get("input_tokens").and_then(|v| v.as_i64())
            {
                seg.token_count += input_tokens;
            }
        } else if entry_type == "assistant" {
            let msg = entry.get("message").cloned().unwrap_or_default();
            let (texts, tools) = extract_assistant_content(&msg);
            current_chars += texts.iter().map(|t| t.len()).sum::<usize>();
            current_chars += tools.iter().map(|t| t.summary.len()).sum::<usize>();
            for t in texts {
                seg.assistant_texts.push(redaction.redact(&t).text);
            }
            for t in tools {
                seg.tool_calls.push(ToolCallSummary {
                    name: t.name,
                    summary: redaction.redact(&t.summary).text,
                });
            }
            if let Some(usage) = msg.get("usage")
                && let Some(output_tokens) = usage.get("output_tokens").and_then(|v| v.as_i64())
            {
                seg.token_count += output_tokens;
            }
        }
    }

    // Finalize trailing segment if it has any content.
    if let Some(last) = current
        && (!last.user_prompts.is_empty()
            || !last.tool_calls.is_empty()
            || !last.assistant_texts.is_empty())
    {
        segments.push(last);
    }

    Ok(segments)
}

/// Build the human-readable `summary_text` column shipped to enrichment.
///
/// Match the Python `build_claude_enrichment_prompt` shape: a labeled header
/// line, optional timestamp range, a bullet-list of user prompts, a
/// bullet-list of tool calls, and a joined assistant-text blob. The brain
/// consumes this text directly as the LLM prompt so the format needs to
/// stay stable across Rust and Python writers.
fn build_summary_text(seg: &SessionSegment) -> String {
    let mut out = String::new();
    let branch = seg.git_branch.as_deref().unwrap_or("unknown");
    out.push_str(&format!(
        "Claude Code session segment (project: {}, branch: {})\n",
        seg.cwd, branch
    ));
    if seg.start_time > 0 && seg.end_time > 0 {
        let start = DateTime::<Utc>::from_timestamp_millis(seg.start_time)
            .map(|d| d.format("%Y-%m-%d %H:%M").to_string())
            .unwrap_or_default();
        let end = DateTime::<Utc>::from_timestamp_millis(seg.end_time)
            .map(|d| d.format("%H:%M").to_string())
            .unwrap_or_default();
        out.push_str(&format!("Time: {} - {}\n", start, end));
    }
    if !seg.user_prompts.is_empty() {
        out.push_str("\nUser prompts:\n");
        for p in &seg.user_prompts {
            out.push_str(&format!("- {}\n", p));
        }
    }
    if !seg.tool_calls.is_empty() {
        out.push_str(&format!("\nTool calls ({}):\n", seg.tool_calls.len()));
        for t in &seg.tool_calls {
            out.push_str(&format!("- {}: {}\n", t.name, t.summary));
        }
    }
    if !seg.assistant_texts.is_empty() {
        out.push_str("\nAssistant reasoning:\n");
        out.push_str(&seg.assistant_texts.join("\n\n"));
        out.push('\n');
    }
    out
}

/// Insert extracted segments into `claude_sessions` + enqueue them for
/// enrichment. Returns `(inserted, skipped)`.
///
/// `INSERT OR IGNORE` handles re-imports idempotently against the
/// `UNIQUE (session_id, segment_index)` constraint — same semantics as the
/// Python `insert_segment` which swallows `UNIQUE constraint` errors.
fn insert_segments(conn: &Connection, segments: &[SessionSegment]) -> Result<(usize, usize)> {
    let mut inserted = 0usize;
    let mut skipped = 0usize;
    let now_ms = Utc::now().timestamp_millis();

    for seg in segments {
        let summary_text = build_summary_text(seg);
        let tool_calls_json =
            serde_json::to_string(&seg.tool_calls).unwrap_or_else(|_| "[]".into());
        let user_prompts_json =
            serde_json::to_string(&seg.user_prompts).unwrap_or_else(|_| "[]".into());
        let is_subagent_int: i64 = if seg.is_subagent { 1 } else { 0 };

        match conn.execute(
            "INSERT OR IGNORE INTO claude_sessions
                (session_id, project_dir, cwd, git_branch, segment_index,
                 start_time, end_time, summary_text, tool_calls_json,
                 user_prompts_json, message_count, token_count, source_file,
                 is_subagent, parent_session_id, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16)",
            params![
                seg.session_id,
                seg.project_dir,
                seg.cwd,
                seg.git_branch,
                seg.segment_index,
                seg.start_time,
                seg.end_time,
                summary_text,
                tool_calls_json,
                user_prompts_json,
                seg.message_count,
                seg.token_count,
                seg.source_file,
                is_subagent_int,
                seg.parent_session_id,
                now_ms,
            ],
        )? {
            0 => {
                // UNIQUE (session_id, segment_index) collided — already ingested.
                skipped += 1;
                continue;
            }
            _ => {
                let claude_session_id = conn.last_insert_rowid();
                // Enqueue for enrichment. OR IGNORE so a replay doesn't trip the
                // UNIQUE (claude_session_id) constraint — belt-and-suspenders since
                // the parent INSERT was also OR IGNORE.
                conn.execute(
                    "INSERT OR IGNORE INTO claude_enrichment_queue (claude_session_id, created_at)
                     VALUES (?1, ?2)",
                    params![claude_session_id, now_ms],
                )?;

                // Update source_health for claude-session on success.
                let seg_ts = seg.end_time;
                match conn.execute(
                    "UPDATE source_health
                     SET last_event_ts        = MAX(COALESCE(last_event_ts, 0), ?1),
                         last_success_ts      = ?2,
                         events_last_1h       = events_last_1h + 1,
                         events_last_24h      = events_last_24h + 1,
                         consecutive_failures = 0,
                         updated_at           = ?2
                     WHERE source = 'claude-session'",
                    rusqlite::params![seg_ts, now_ms],
                ) {
                    Err(e) if !crate::is_missing_source_health_table_error(&e) => {
                        warn!("source_health session update failed: {e}");
                    }
                    _ => {}
                }

                inserted += 1;
            }
        }
    }

    Ok((inserted, skipped))
}

/// Extract segments from a JSONL session file and insert them into an existing
/// connection. Called by the FS watcher (which owns its own connection) to avoid
/// opening a second writer. Returns `(inserted, skipped, errors)`.
pub fn ingest_session_file(conn: &Connection, path: &Path) -> (usize, usize, usize) {
    let redaction = RedactionEngine::builtin();
    let session_file = SessionFile::from_path(path);

    let segments = match extract_segments(&session_file, &redaction) {
        Ok(s) => s,
        Err(e) => {
            warn!(path = %path.display(), %e, "failed to extract session segments");
            return (0, 0, 1);
        }
    };

    if segments.is_empty() {
        return (0, 0, 0);
    }

    match insert_segments(conn, &segments) {
        Ok((inserted, skipped)) => (inserted, skipped, 0),
        Err(e) => {
            error!(%e, "failed to insert session segments");
            (0, 0, 1)
        }
    }
}

fn write_session_segments(db_path: &Path, path: &Path) -> (usize, usize, usize) {
    let redaction = RedactionEngine::builtin();
    let session_file = SessionFile::from_path(path);

    let segments = match extract_segments(&session_file, &redaction) {
        Ok(s) => s,
        Err(e) => {
            warn!(path = %path.display(), %e, "failed to extract session segments");
            return (0, 0, 1);
        }
    };

    if segments.is_empty() {
        return (0, 0, 0);
    }

    let conn = match hippo_core::storage::open_db(db_path) {
        Ok(c) => c,
        Err(e) => {
            error!(db = %db_path.display(), %e, "failed to open DB for segment write");
            return (0, 0, 1);
        }
    };

    match insert_segments(&conn, &segments) {
        Ok((inserted, skipped)) => (inserted, skipped, 0),
        Err(e) => {
            error!(%e, "failed to insert claude_sessions segments");
            (0, 0, 1)
        }
    }
}

/// Run the importer in batch mode: read all lines, send all events, exit.
/// Returns (events_sent, errors).
pub async fn ingest_batch(
    path: &Path,
    socket_path: &Path,
    timeout_ms: u64,
    db_path: &Path,
) -> Result<(usize, usize)> {
    // path is a Claude session JSONL supplied by the user via `hippo ingest
    // claude-session --batch`. This is a local CLI operating on the user's
    // own machine — no privilege boundary, no untrusted network input.
    // nosemgrep: rust.actix.path-traversal.tainted-path.tainted-path
    let file =
        std::fs::File::open(path).with_context(|| format!("failed to open {}", path.display()))?;
    let reader = std::io::BufReader::new(file);

    let hostname = hostname::get()
        .map(|h| h.to_string_lossy().to_string())
        .unwrap_or_else(|_| "unknown".to_string());
    let mut pending: HashMap<String, PendingToolUse> = HashMap::new();
    let mut git_repo_cache: HashMap<String, Option<String>> = HashMap::new();
    let mut sent = 0usize;
    let mut errors = 0usize;
    let mut line_num = 0usize;

    for line_result in reader.lines() {
        line_num += 1;
        let line = match line_result {
            Ok(l) => l,
            Err(e) => {
                warn!(line_num, %e, "failed to read line");
                errors += 1;
                continue;
            }
        };

        let envelopes = match process_line(&line, &mut pending, &mut git_repo_cache, &hostname) {
            Ok(envs) => envs,
            Err(e) => {
                warn!(line_num, %e, "skipping line");
                errors += 1;
                continue;
            }
        };

        for envelope in envelopes {
            match send_event_fire_and_forget(socket_path, &envelope, timeout_ms).await {
                Ok(()) => {
                    sent += 1;
                    if sent.is_multiple_of(50) {
                        info!(sent, "batch progress");
                    }
                }
                Err(e) => {
                    error!(%e, "failed to send event");
                    errors += 1;
                }
            }
        }
    }

    // Flush any pending tool_uses that never got results
    let orphans: Vec<PendingToolUse> = pending.into_values().collect();
    for orphan in orphans {
        let envelope = build_envelope(&orphan, None, false, None, &hostname);
        match send_event_fire_and_forget(socket_path, &envelope, timeout_ms).await {
            Ok(()) => {
                sent += 1;
                if sent.is_multiple_of(50) {
                    info!(sent, "batch progress");
                }
            }
            Err(e) => {
                error!(%e, "failed to send orphan event");
                errors += 1;
            }
        }
    }

    // Second pass: extract conversation segments and upsert into
    // `claude_sessions`. This is the population path that was missing in
    // #58 — tool-call events were flowing into `events` but the session
    // rows needed for enrichment never got written. We go direct to SQLite
    // (instead of through the daemon socket) because segments don't map
    // onto the existing event wire protocol and the daemon shares the DB.
    let (segments_inserted, segments_skipped, segment_errors) =
        write_session_segments(db_path, path);
    if segments_inserted > 0 || segments_skipped > 0 || segment_errors > 0 {
        info!(
            segments_inserted,
            segments_skipped, segment_errors, "claude_sessions segments upserted"
        );
    }
    errors += segment_errors;

    Ok((sent, errors))
}

/// Run the importer in tail mode: skip to end, watch for new lines.
pub async fn ingest_tail(path: &Path, socket_path: &Path, timeout_ms: u64) -> Result<()> {
    // path is a Claude session JSONL supplied by the user via `hippo ingest
    // claude-session --follow`. Local CLI on the user's own machine — no
    // privilege boundary.
    // nosemgrep: rust.actix.path-traversal.tainted-path.tainted-path
    let file =
        std::fs::File::open(path).with_context(|| format!("failed to open {}", path.display()))?;
    let mut position = file.metadata()?.len();
    drop(file);

    let hostname = hostname::get()
        .map(|h| h.to_string_lossy().to_string())
        .unwrap_or_else(|_| "unknown".to_string());
    info!(path = %path.display(), position, "tailing session file");

    let mut pending: HashMap<String, PendingToolUse> = HashMap::new();
    let mut git_repo_cache: HashMap<String, Option<String>> = HashMap::new();
    let mut total_sent = 0usize;
    let mut total_errors = 0usize;
    let watch_pid = std::env::var("HIPPO_WATCH_PID")
        .ok()
        .and_then(|s| s.parse::<u32>().ok());

    loop {
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {
                info!("shutting down (ctrl+c)");
                break;
            }
            _ = tokio::time::sleep(Duration::from_secs(1)) => {
                // Single process-death check per tick
                let process_dead = watch_pid.is_some_and(|pid| unsafe {
                    let ret = libc::kill(pid as i32, 0);
                    ret == -1 && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
                });

                if process_dead {
                    info!(watch_pid, "watched process exited, performing final drain");
                }

                // Read new lines from current position
                let mut file = match std::fs::File::open(path) {
                    Ok(f) => f,
                    Err(e) => {
                        warn!(%e, "failed to open file");
                        if process_dead { break; }
                        continue;
                    }
                };

                let file_len = file.metadata().map(|m| m.len()).unwrap_or(0);
                if file_len < position {
                    warn!("file truncated, resetting position");
                    position = 0;
                }

                if file_len == position {
                    if process_dead {
                        info!("drain complete, exiting");
                        break;
                    }
                    continue;
                }

                if let Err(e) = file.seek(SeekFrom::Start(position)) {
                    warn!(%e, "failed to seek");
                    if process_dead { break; }
                    continue;
                }

                let reader = std::io::BufReader::new(file);
                let mut batch_sent = 0usize;

                for line_result in reader.lines() {
                    match line_result {
                        Ok(line) => {
                            let envelopes = match process_line(&line, &mut pending, &mut git_repo_cache, &hostname) {
                                Ok(envs) => envs,
                                Err(e) => {
                                    warn!(%e, "skipping line");
                                    total_errors += 1;
                                    // Advance past this line even on parse error
                                    position += line.len() as u64 + 1;
                                    continue;
                                }
                            };

                            for envelope in envelopes {
                                match send_event_fire_and_forget(socket_path, &envelope, timeout_ms).await {
                                    Ok(()) => {
                                        total_sent += 1;
                                        batch_sent += 1;
                                    }
                                    Err(e) => {
                                        error!(%e, "failed to send event");
                                        total_errors += 1;
                                    }
                                }
                            }

                            // Advance position past this line (+1 for newline)
                            position += line.len() as u64 + 1;
                        }
                        Err(e) => {
                            warn!(%e, "error reading line, stopping at current position");
                            total_errors += 1;
                            break; // Don't advance past unread data
                        }
                    }
                }

                if batch_sent > 0 {
                    info!(batch_sent, total_sent, total_errors, "sent events");
                }

                // Break AFTER final read if process is dead
                if process_dead {
                    info!(batch_sent, "final drain complete, exiting");
                    break;
                }
            }
        }
    }

    // Flush any pending tool_uses
    let orphans: Vec<PendingToolUse> = pending.into_values().collect();
    if !orphans.is_empty() {
        info!(count = orphans.len(), "flushing pending tool uses");
        for orphan in orphans {
            let envelope = build_envelope(&orphan, None, false, None, &hostname);
            match send_event_fire_and_forget(socket_path, &envelope, timeout_ms).await {
                Ok(()) => total_sent += 1,
                Err(e) => {
                    error!(%e, "failed to send orphan event");
                    total_errors += 1;
                }
            }
        }
    }

    info!(total_sent, total_errors, "session tail complete");

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use hippo_core::events::EventPayload;

    /// Test wrapper — stubs out the per-session git_repo cache so individual
    /// cases can stay focused. Cache correctness is covered separately.
    fn process_line(
        line: &str,
        pending: &mut HashMap<String, PendingToolUse>,
        hostname: &str,
    ) -> Result<Vec<EventEnvelope>> {
        let mut cache: HashMap<String, Option<String>> = HashMap::new();
        super::process_line(line, pending, &mut cache, hostname)
    }

    #[test]
    fn test_format_tool_command_bash() {
        let input = serde_json::json!({"command": "cargo test -p hippo-core"});
        assert_eq!(
            format_tool_command("Bash", &input),
            "cargo test -p hippo-core"
        );
    }

    #[test]
    fn test_format_tool_command_read() {
        let input = serde_json::json!({"file_path": "/foo/bar.rs"});
        assert_eq!(format_tool_command("Read", &input), "read /foo/bar.rs");
    }

    #[test]
    fn test_format_tool_command_edit() {
        let input = serde_json::json!({"file_path": "/foo/bar.rs"});
        assert_eq!(format_tool_command("Edit", &input), "edit /foo/bar.rs");
    }

    #[test]
    fn test_format_tool_command_write() {
        let input = serde_json::json!({"file_path": "/foo/bar.rs"});
        assert_eq!(format_tool_command("Write", &input), "write /foo/bar.rs");
    }

    #[test]
    fn test_format_tool_command_grep() {
        let input = serde_json::json!({"pattern": "TODO", "path": "src/"});
        assert_eq!(format_tool_command("Grep", &input), "grep 'TODO' src/");
    }

    #[test]
    fn test_format_tool_command_glob() {
        let input = serde_json::json!({"pattern": "**/*.rs"});
        assert_eq!(format_tool_command("Glob", &input), "glob '**/*.rs'");
    }

    #[test]
    fn test_format_tool_command_agent() {
        let input = serde_json::json!({"description": "find all TODO comments"});
        assert_eq!(
            format_tool_command("Agent", &input),
            "agent: find all TODO comments"
        );
    }

    #[test]
    fn test_format_tool_command_task_create() {
        let input = serde_json::json!({"subject": "fix the bug"});
        assert_eq!(
            format_tool_command("TaskCreate", &input),
            "task: fix the bug"
        );
    }

    #[test]
    fn test_format_tool_command_task_update() {
        let input = serde_json::json!({"taskId": "42", "status": "completed"});
        assert_eq!(
            format_tool_command("TaskUpdate", &input),
            "task-update: 42 completed"
        );
    }

    #[test]
    fn test_format_tool_command_unknown() {
        let input = serde_json::json!({});
        assert_eq!(format_tool_command("SomeTool", &input), "SomeTool");
    }

    #[test]
    fn test_extract_result_content_string() {
        let val = serde_json::json!("hello world");
        assert_eq!(
            extract_result_content(&val),
            Some("hello world".to_string())
        );
    }

    #[test]
    fn test_extract_result_content_array() {
        let val = serde_json::json!([
            {"type": "text", "text": "line one"},
            {"type": "text", "text": "line two"}
        ]);
        assert_eq!(
            extract_result_content(&val),
            Some("line one\nline two".to_string())
        );
    }

    #[test]
    fn test_extract_result_content_null() {
        let val = serde_json::json!(null);
        assert_eq!(extract_result_content(&val), None);
    }

    #[test]
    fn test_truncate_to_bytes() {
        let s = "hello world";
        let (truncated, was) = truncate_to_bytes(s, 5);
        assert_eq!(truncated, "hello");
        assert!(was);

        let (full, was) = truncate_to_bytes(s, 100);
        assert_eq!(full, "hello world");
        assert!(!was);
    }

    #[test]
    fn test_truncate_to_bytes_multibyte() {
        // Each emoji is 4 bytes
        let s = "\u{1F600}\u{1F601}\u{1F602}"; // 12 bytes
        let (truncated, was) = truncate_to_bytes(s, 5);
        // Should truncate to the last valid char boundary at or before 5
        assert_eq!(truncated, "\u{1F600}"); // 4 bytes
        assert!(was);
    }

    #[test]
    fn test_process_line_skips_empty() {
        let mut pending = HashMap::new();
        let result = process_line("", &mut pending, "test-host").unwrap();
        assert!(result.is_empty());
    }

    #[test]
    fn test_process_line_skips_system_type() {
        let mut pending = HashMap::new();
        let line = r#"{"type": "system", "message": {"role": "system", "content": "hi"}}"#;
        let result = process_line(line, &mut pending, "test-host").unwrap();
        assert!(result.is_empty());
    }

    #[test]
    fn test_process_line_assistant_creates_pending() {
        let mut pending = HashMap::new();
        let line = r#"{
            "type": "assistant",
            "uuid": "fed2b3ee-0000-0000-0000-000000000000",
            "timestamp": "2026-03-28T11:33:45.146Z",
            "sessionId": "dcb2cf8e-0000-0000-0000-000000000000",
            "cwd": "/home/user/projects/hippo",
            "gitBranch": "main",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me read the file."},
                    {"type": "tool_use", "id": "toolu_01KjQTmoDtKU3G", "name": "Read", "input": {"file_path": "/foo/bar.rs"}}
                ]
            }
        }"#;
        let result = process_line(line, &mut pending, "test-host").unwrap();
        assert!(result.is_empty());
        assert!(pending.contains_key("toolu_01KjQTmoDtKU3G"));
        let p = &pending["toolu_01KjQTmoDtKU3G"];
        assert_eq!(p.name, "Read");
        assert_eq!(p.cwd, "/home/user/projects/hippo");
        assert_eq!(p.git_branch.as_deref(), Some("main"));
    }

    #[test]
    fn test_process_line_tool_result_completes_pending() {
        let mut pending = HashMap::new();

        // First: assistant message with tool_use
        let assistant_line = r#"{
            "type": "assistant",
            "timestamp": "2026-03-28T11:33:45.000Z",
            "sessionId": "dcb2cf8e-0000-0000-0000-000000000000",
            "cwd": "/projects/hippo",
            "gitBranch": "main",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_abc123", "name": "Bash", "input": {"command": "cargo build"}}
                ]
            }
        }"#;
        process_line(assistant_line, &mut pending, "test-host").unwrap();
        assert_eq!(pending.len(), 1);

        // Second: user message with tool_result
        let user_line = r#"{
            "type": "user",
            "timestamp": "2026-03-28T11:33:47.500Z",
            "sessionId": "dcb2cf8e-0000-0000-0000-000000000000",
            "cwd": "/projects/hippo",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_abc123", "content": "Compiling hippo v0.1.0\n    Finished `dev` profile"}
                ]
            }
        }"#;
        let envelopes = process_line(user_line, &mut pending, "test-host").unwrap();
        assert_eq!(envelopes.len(), 1);
        assert!(pending.is_empty());

        let env = &envelopes[0];
        match &env.payload {
            EventPayload::Shell(shell) => {
                assert_eq!(shell.command, "cargo build");
                assert_eq!(shell.exit_code, 0);
                assert_eq!(shell.duration_ms, 2500);
                assert_eq!(shell.cwd.to_str().unwrap(), "/projects/hippo");
                assert!(matches!(shell.shell, ShellKind::Unknown(ref s) if s == "claude-code"));
                assert!(shell.stdout.is_some());
                let stdout = shell.stdout.as_ref().unwrap();
                assert!(stdout.content.contains("Compiling hippo"));
                assert!(!stdout.truncated);
                assert_eq!(
                    shell.git_state.as_ref().unwrap().branch.as_deref(),
                    Some("main")
                );
            }
            other => panic!("expected Shell payload, got {:?}", other),
        }
    }

    #[test]
    fn test_process_line_tool_result_with_error() {
        let mut pending = HashMap::new();

        let assistant_line = r#"{
            "type": "assistant",
            "timestamp": "2026-03-28T11:00:00.000Z",
            "sessionId": "aaa-bbb",
            "cwd": "/tmp",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_err1", "name": "Bash", "input": {"command": "false"}}
                ]
            }
        }"#;
        process_line(assistant_line, &mut pending, "test-host").unwrap();

        let user_line = r#"{
            "type": "user",
            "timestamp": "2026-03-28T11:00:01.000Z",
            "sessionId": "aaa-bbb",
            "cwd": "/tmp",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_err1", "is_error": true, "content": "command failed"}
                ]
            }
        }"#;
        let envelopes = process_line(user_line, &mut pending, "test-host").unwrap();
        assert_eq!(envelopes.len(), 1);

        match &envelopes[0].payload {
            EventPayload::Shell(shell) => {
                assert_eq!(shell.exit_code, 1);
                assert_eq!(shell.duration_ms, 1000);
            }
            other => panic!("expected Shell payload, got {:?}", other),
        }
    }

    #[test]
    fn test_process_line_array_content_in_tool_result() {
        let mut pending = HashMap::new();

        let assistant_line = r#"{
            "type": "assistant",
            "timestamp": "2026-03-28T12:00:00.000Z",
            "sessionId": "sess1",
            "cwd": "/tmp",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_arr1", "name": "Read", "input": {"file_path": "/tmp/test.txt"}}
                ]
            }
        }"#;
        process_line(assistant_line, &mut pending, "test-host").unwrap();

        let user_line = r#"{
            "type": "user",
            "timestamp": "2026-03-28T12:00:00.500Z",
            "sessionId": "sess1",
            "cwd": "/tmp",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_arr1", "content": [{"type": "text", "text": "file contents here"}]}
                ]
            }
        }"#;
        let envelopes = process_line(user_line, &mut pending, "test-host").unwrap();
        assert_eq!(envelopes.len(), 1);

        match &envelopes[0].payload {
            EventPayload::Shell(shell) => {
                assert_eq!(shell.command, "read /tmp/test.txt");
                let stdout = shell.stdout.as_ref().unwrap();
                assert_eq!(stdout.content, "file contents here");
            }
            other => panic!("expected Shell payload, got {:?}", other),
        }
    }

    #[test]
    fn test_deterministic_envelope_id() {
        let mut pending1 = HashMap::new();
        let mut pending2 = HashMap::new();

        let line = r#"{
            "type": "assistant",
            "timestamp": "2026-03-28T12:00:00.000Z",
            "sessionId": "sess1",
            "cwd": "/tmp",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_deterministic", "name": "Bash", "input": {"command": "echo hi"}}
                ]
            }
        }"#;
        process_line(line, &mut pending1, "test-host").unwrap();
        process_line(line, &mut pending2, "test-host").unwrap();

        let result_line = r#"{
            "type": "user",
            "timestamp": "2026-03-28T12:00:01.000Z",
            "sessionId": "sess1",
            "cwd": "/tmp",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_deterministic", "content": "hi"}
                ]
            }
        }"#;

        let envs1 = process_line(result_line, &mut pending1, "test-host").unwrap();
        let envs2 = process_line(result_line, &mut pending2, "test-host").unwrap();

        assert_eq!(envs1[0].envelope_id, envs2[0].envelope_id);
    }

    #[test]
    fn test_process_line_malformed_json() {
        let mut pending = HashMap::new();
        let result = process_line("not valid json {{{", &mut pending, "test-host");
        assert!(result.is_err());
    }

    #[test]
    fn test_process_line_skips_no_message_field() {
        let mut pending = HashMap::new();
        let line = r#"{"type": "assistant", "timestamp": "2026-03-28T12:00:00Z"}"#;
        let result = process_line(line, &mut pending, "test-host").unwrap();
        assert!(result.is_empty());
    }

    #[test]
    fn test_build_envelope_no_result() {
        let pending = PendingToolUse {
            tool_use_id: "toolu_orphan".to_string(),
            name: "Bash".to_string(),
            input: serde_json::json!({"command": "ls"}),
            timestamp: "2026-03-28T12:00:00Z".parse().unwrap(),
            session_id: "dcb2cf8e-0000-0000-0000-000000000000".to_string(),
            cwd: "/tmp".to_string(),
            git_branch: None,
            git_repo: None,
        };

        let envelope = build_envelope(&pending, None, false, None, "test-host");
        match &envelope.payload {
            EventPayload::Shell(shell) => {
                assert_eq!(shell.command, "ls");
                assert_eq!(shell.duration_ms, 0);
                assert!(shell.stdout.is_none());
                assert!(shell.git_state.is_none());
            }
            other => panic!("expected Shell payload, got {:?}", other),
        }
    }

    #[test]
    fn test_output_truncation() {
        let long_content = "x".repeat(5000);
        let pending = PendingToolUse {
            tool_use_id: "toolu_trunc".to_string(),
            name: "Bash".to_string(),
            input: serde_json::json!({"command": "cat big"}),
            timestamp: "2026-03-28T12:00:00Z".parse().unwrap(),
            session_id: "dcb2cf8e-0000-0000-0000-000000000000".to_string(),
            cwd: "/tmp".to_string(),
            git_branch: None,
            git_repo: None,
        };

        let envelope = build_envelope(&pending, Some(&long_content), false, None, "test-host");
        match &envelope.payload {
            EventPayload::Shell(shell) => {
                let stdout = shell.stdout.as_ref().unwrap();
                assert!(stdout.truncated);
                assert_eq!(stdout.content.len(), MAX_OUTPUT_BYTES);
                assert_eq!(stdout.original_bytes, 5000);
            }
            other => panic!("expected Shell payload, got {:?}", other),
        }
    }

    #[test]
    fn test_multiple_tool_uses_in_single_message() {
        let mut pending = HashMap::new();

        // Assistant message with two parallel tool_use blocks
        let assistant_line = r#"{
            "type": "assistant",
            "timestamp": "2026-03-28T12:00:00.000Z",
            "sessionId": "sess1",
            "cwd": "/projects/hippo",
            "gitBranch": "main",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Running tests and clippy in parallel."},
                    {"type": "tool_use", "id": "toolu_parallel1", "name": "Bash", "input": {"command": "cargo test"}},
                    {"type": "tool_use", "id": "toolu_parallel2", "name": "Bash", "input": {"command": "cargo clippy"}}
                ]
            }
        }"#;
        let result = process_line(assistant_line, &mut pending, "test-host").unwrap();
        assert!(result.is_empty(), "tool_use blocks should be pending");
        assert_eq!(pending.len(), 2);
        assert!(pending.contains_key("toolu_parallel1"));
        assert!(pending.contains_key("toolu_parallel2"));

        // User message with both tool_results
        let user_line = r#"{
            "type": "user",
            "timestamp": "2026-03-28T12:00:03.000Z",
            "sessionId": "sess1",
            "cwd": "/projects/hippo",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_parallel1", "content": "test result: ok. 62 passed"},
                    {"type": "tool_result", "tool_use_id": "toolu_parallel2", "content": "Finished dev profile"}
                ]
            }
        }"#;
        let envelopes = process_line(user_line, &mut pending, "test-host").unwrap();
        assert_eq!(envelopes.len(), 2);
        assert!(pending.is_empty());

        let commands: Vec<&str> = envelopes
            .iter()
            .map(|e| match &e.payload {
                EventPayload::Shell(s) => s.command.as_str(),
                _ => panic!("expected Shell"),
            })
            .collect();
        assert!(commands.contains(&"cargo test"));
        assert!(commands.contains(&"cargo clippy"));
    }

    #[test]
    fn envelope_populates_git_repo_from_cwd() {
        // Real git worktree with a github origin — build_envelope should pull
        // `owner/repo` through the derive helper into git_state.repo.
        let tmp = tempfile::tempdir().unwrap();
        let cwd = tmp.path();
        let status = std::process::Command::new("git")
            .arg("-C")
            .arg(cwd)
            .args(["init", "--quiet", "-b", "main"])
            .status()
            .unwrap();
        assert!(status.success());
        let status = std::process::Command::new("git")
            .arg("-C")
            .arg(cwd)
            .args([
                "remote",
                "add",
                "origin",
                "git@github.com:sjcarpenter/hippo.git",
            ])
            .status()
            .unwrap();
        assert!(status.success());

        let mut pending = HashMap::new();
        let assistant_line = format!(
            r#"{{
                "type": "assistant",
                "timestamp": "2026-03-28T12:00:00.000Z",
                "sessionId": "sess-git",
                "cwd": "{}",
                "gitBranch": "main",
                "message": {{
                    "role": "assistant",
                    "content": [
                        {{"type": "tool_use", "id": "toolu_git", "name": "Bash", "input": {{"command": "echo hi"}}}}
                    ]
                }}
            }}"#,
            cwd.display()
        );
        process_line(&assistant_line, &mut pending, "test-host").unwrap();

        let user_line = format!(
            r#"{{
                "type": "user",
                "timestamp": "2026-03-28T12:00:01.000Z",
                "sessionId": "sess-git",
                "cwd": "{}",
                "message": {{
                    "role": "user",
                    "content": [
                        {{"type": "tool_result", "tool_use_id": "toolu_git", "content": "hi"}}
                    ]
                }}
            }}"#,
            cwd.display()
        );
        let envelopes = process_line(&user_line, &mut pending, "test-host").unwrap();
        assert_eq!(envelopes.len(), 1);

        match &envelopes[0].payload {
            EventPayload::Shell(shell) => {
                let repo = shell.git_state.as_ref().and_then(|g| g.repo.as_deref());
                assert_eq!(repo, Some("sjcarpenter/hippo"));
            }
            other => panic!("expected Shell payload, got {:?}", other),
        }
    }

    #[test]
    fn git_repo_cache_shared_across_envelopes() {
        // Same cwd reused across many tool_uses should only populate the
        // cache once — a regression here means we're re-spawning `git` per
        // envelope during batch imports.
        let tmp = tempfile::tempdir().unwrap();
        let cwd = tmp.path();
        let status = std::process::Command::new("git")
            .arg("-C")
            .arg(cwd)
            .args(["init", "--quiet", "-b", "main"])
            .status()
            .unwrap();
        assert!(status.success());

        let mut pending = HashMap::new();
        let mut cache: HashMap<String, Option<String>> = HashMap::new();
        for i in 0..3 {
            let line = format!(
                r#"{{
                    "type": "assistant",
                    "timestamp": "2026-03-28T12:00:00.000Z",
                    "sessionId": "sess-cache",
                    "cwd": "{cwd}",
                    "message": {{
                        "role": "assistant",
                        "content": [
                            {{"type": "tool_use", "id": "toolu_{i}", "name": "Bash", "input": {{"command": "echo"}}}}
                        ]
                    }}
                }}"#,
                cwd = cwd.display(),
                i = i
            );
            super::process_line(&line, &mut pending, &mut cache, "test-host").unwrap();
        }
        assert_eq!(cache.len(), 1, "cwd should be cached once across envelopes");
    }
}
