//! Codex CLI rollout-session poller — see
//! docs/superpowers/specs/2026-05-17-codex-ingestion-design.md.

use chrono::DateTime;
use serde::Serialize;

/// 5-minute gap between user prompts marks a task boundary.
#[allow(dead_code)] // consumed in Task 4+
const TASK_GAP_MS: i64 = 5 * 60 * 1000;
/// Accumulated character cap before forcing a new segment.
#[allow(dead_code)] // consumed in Task 4+
const MAX_SEGMENT_CHARS: usize = 12_000;

/// A single tool call, summarized for enrichment. Serialized into
/// `claude_sessions.tool_calls_json`.
#[allow(dead_code)] // consumed in Task 4+
#[derive(Debug, Clone, Serialize)]
pub(crate) struct ToolCall {
    pub(crate) name: String,
    pub(crate) summary: String,
}

/// A parsed Codex conversation segment, upserted into `claude_sessions`.
#[allow(dead_code)] // consumed in Task 4+
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
#[allow(dead_code)] // consumed in Task 4+
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
#[allow(dead_code)] // consumed in Task 4+
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
}
