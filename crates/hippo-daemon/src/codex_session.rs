//! Codex CLI rollout-session poller — see
//! docs/superpowers/specs/2026-05-17-codex-ingestion-design.md.

use anyhow::{Context, Result};
use chrono::DateTime;
use serde::Serialize;
use std::path::Path;

/// 5-minute gap between user prompts marks a task boundary.
const TASK_GAP_MS: i64 = 5 * 60 * 1000;
/// Accumulated character cap before forcing a new segment.
const MAX_SEGMENT_CHARS: usize = 12_000;

/// A single tool call, summarized for enrichment. Serialized into
/// `claude_sessions.tool_calls_json`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct ToolCall {
    pub(crate) name: String,
    pub(crate) summary: String,
}

/// A parsed Codex conversation segment, upserted into `claude_sessions`.
#[allow(dead_code)] // fields consumed in Task 7
#[derive(Debug, Clone)]
pub(crate) struct CodexSegment {
    pub(crate) session_id: String,
    pub(crate) project_dir: String,
    pub(crate) cwd: String,
    pub(crate) segment_index: i64,
    pub(crate) start_time: i64,
    pub(crate) end_time: i64,
    pub(crate) user_prompts: Vec<String>,
    pub(crate) assistant_texts: Vec<String>,
    pub(crate) tool_calls: Vec<ToolCall>,
    pub(crate) message_count: i64,
    pub(crate) source_file: String,
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
#[allow(dead_code)] // consumed in Task 7
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

/// Extract input_text/output_text from a content-block array.
fn content_text(content: &serde_json::Value) -> String {
    content
        .as_array()
        .map(|blocks| {
            blocks
                .iter()
                .filter_map(|b| b.get("text").and_then(|t| t.as_str()))
                .collect::<Vec<_>>()
                .join("\n")
        })
        .unwrap_or_default()
}

/// Parse a Codex rollout JSONL file into task-boundary segments.
#[allow(dead_code)] // consumed in Task 7
pub(crate) fn extract_segments(path: &Path) -> Result<Vec<CodexSegment>> {
    let raw = std::fs::read_to_string(path)
        .with_context(|| format!("read codex rollout {}", path.display()))?;
    let source_file = path.to_string_lossy().to_string();

    let mut segments: Vec<CodexSegment> = Vec::new();
    let mut current: Option<CodexSegment> = None;
    let mut current_chars: usize = 0;
    let mut last_user_ms: i64 = 0;
    let mut session_id = String::new();
    let mut session_cwd = String::new();

    for line in raw.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let obj: serde_json::Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(_) => continue,
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
                    .unwrap_or_else(|| session_id.clone());
                CodexSegment {
                    session_id: session_id.clone(),
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
                current_chars += user_text.len();
                seg.user_prompts.push(user_text);
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
            let summary = tool_summary(&args);
            current_chars += summary.len();
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
                current_chars += capped.len();
                seg.assistant_texts.push(capped);
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

#[cfg(test)]
mod tests {
    use super::*;

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
        let segs = extract_segments(&path).expect("parse");
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
        let segs = extract_segments(&p).unwrap();
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
        let segs = extract_segments(&p).unwrap();
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
        let segs = extract_segments(&p).unwrap();
        assert_eq!(
            segs.len(),
            2,
            "accumulated chars over MAX_SEGMENT_CHARS must split the session"
        );
        assert_eq!(segs[1].segment_index, 1);
    }
}
