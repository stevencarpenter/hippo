//! Cursor Agent CLI transcript poller — see
//! docs/superpowers/specs/2026-05-25-cursor-ingestion-design.md.
//!
//! Cursor transcripts are Anthropic-style JSONL (`{role, message:{content}}`)
//! with NO per-line timestamps and NO in-file session metadata: identity is
//! derived from the path, time from the file mtime, and segments split on
//! accumulated character count only.

use serde::Serialize;

/// A single tool call, summarized for enrichment. Serialized into
/// `claude_sessions.tool_calls_json`.
#[derive(Debug, Clone, Serialize)]
pub struct ToolCall {
    pub name: String,
    pub summary: String,
}

/// A parsed Cursor conversation segment, upserted into `claude_sessions`.
#[derive(Debug, Clone)]
pub struct CursorSegment {
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
    pub is_subagent: bool,
    pub parent_session_id: Option<String>,
}

/// Short human-readable summary of a Cursor `tool_use` block's `input` object.
/// Prefer the most informative single argument, else the first non-empty
/// string value, else the compact JSON.
pub fn tool_summary(input: &serde_json::Value) -> String {
    if let Some(obj) = input.as_object() {
        for key in [
            "command",
            "file_path",
            "path",
            "glob_pattern",
            "pattern",
            "query",
            "uri",
            "target_directory",
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tool_summary_prefers_command_then_path() {
        assert_eq!(
            tool_summary(&serde_json::json!({"command": "cargo test", "description": "x"})),
            "cargo test"
        );
        assert_eq!(
            tool_summary(&serde_json::json!({"file_path": "/tmp/x.rs"})),
            "/tmp/x.rs"
        );
        assert_eq!(
            tool_summary(&serde_json::json!({"glob_pattern": "**/*.ts"})),
            "**/*.ts"
        );
        assert_eq!(tool_summary(&serde_json::json!({})), "{}");
    }
}
