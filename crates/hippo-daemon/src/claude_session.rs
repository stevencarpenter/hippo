use std::collections::HashMap;
use std::io::{BufRead, Seek, SeekFrom};
use std::path::Path;
use std::time::Duration;

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use hippo_core::events::{
    CapturedOutput, EventEnvelope, EventPayload, GitState, ShellEvent, ShellKind,
};
use tracing::{error, info, warn};
use uuid::Uuid;

use crate::commands::send_event_fire_and_forget;

/// Maximum bytes to store in CapturedOutput
const MAX_OUTPUT_BYTES: usize = 4096;

/// Pending tool use waiting for its result
struct PendingToolUse {
    tool_use_id: String,
    name: String,
    input: serde_json::Value,
    timestamp: DateTime<Utc>,
    session_id: String,
    cwd: String,
    git_branch: Option<String>,
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

    let git_repo = crate::git_repo::derive_git_repo(Path::new(&pending.cwd));
    let git_state = if git_repo.is_some() || pending.git_branch.is_some() {
        Some(GitState {
            repo: git_repo,
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
    };

    EventEnvelope {
        envelope_id,
        producer_version: 1,
        timestamp: pending.timestamp,
        payload: EventPayload::Shell(Box::new(event)),
    }
}

/// Process a single JSONL line. Returns envelopes for any completed tool uses.
fn process_line(
    line: &str,
    pending: &mut HashMap<String, PendingToolUse>,
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

/// Run the importer in batch mode: read all lines, send all events, exit.
/// Returns (events_sent, errors).
pub async fn ingest_batch(
    path: &Path,
    socket_path: &Path,
    timeout_ms: u64,
) -> Result<(usize, usize)> {
    let file =
        std::fs::File::open(path).with_context(|| format!("failed to open {}", path.display()))?;
    let reader = std::io::BufReader::new(file);

    let hostname = hostname::get()
        .map(|h| h.to_string_lossy().to_string())
        .unwrap_or_else(|_| "unknown".to_string());
    let mut pending: HashMap<String, PendingToolUse> = HashMap::new();
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

        let envelopes = match process_line(&line, &mut pending, &hostname) {
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

    Ok((sent, errors))
}

/// Run the importer in tail mode: skip to end, watch for new lines.
pub async fn ingest_tail(path: &Path, socket_path: &Path, timeout_ms: u64) -> Result<()> {
    // Seek to end of file to get initial position
    let file =
        std::fs::File::open(path).with_context(|| format!("failed to open {}", path.display()))?;
    let mut position = file.metadata()?.len();
    drop(file);

    let hostname = hostname::get()
        .map(|h| h.to_string_lossy().to_string())
        .unwrap_or_else(|_| "unknown".to_string());
    info!(path = %path.display(), position, "tailing session file");

    let mut pending: HashMap<String, PendingToolUse> = HashMap::new();
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
                            let envelopes = match process_line(&line, &mut pending, &hostname) {
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
}
