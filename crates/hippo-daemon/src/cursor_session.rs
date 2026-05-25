//! Cursor Agent CLI transcript poller — see
//! docs/superpowers/specs/2026-05-25-cursor-ingestion-design.md.
//!
//! Cursor transcripts are Anthropic-style JSONL (`{role, message:{content}}`)
//! with NO per-line timestamps and NO in-file session metadata: identity is
//! derived from the path, time from the file mtime, and segments split on
//! accumulated character count only.

use std::path::Path;

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

/// Identity derived entirely from a transcript's path. Cursor transcripts
/// carry no session id, cwd, or subagent marker inside the file.
// dead_code allow removed in Task 5 (extract_segments becomes the caller)
#[allow(dead_code)]
#[derive(Debug, Clone)]
pub(crate) struct PathIdentity {
    pub session_id: String,
    pub project_dir: String,
    pub cwd: String,
    pub is_subagent: bool,
    pub parent_session_id: Option<String>,
}

/// Decode a `~/.cursor/projects/<slug>/` slug into a cwd. The slug encodes a
/// path with `-` for `/` (same convention as ~/.claude/projects). Ephemeral
/// slugs (`empty-window`, all-digit ids, `var-folders-*` temp dirs) have no
/// real project path, so they decode to an empty cwd.
// dead_code allow removed in Task 5 (extract_segments becomes the caller)
#[allow(dead_code)]
fn decode_slug_to_cwd(slug: &str) -> String {
    if slug == "empty-window"
        || slug.starts_with("var-folders")
        || slug.chars().all(|c| c.is_ascii_digit())
    {
        return String::new();
    }
    format!("/{}", slug.replace('-', "/"))
}

// dead_code allow removed in Task 5 (extract_segments becomes the caller)
#[allow(dead_code)]
impl PathIdentity {
    pub(crate) fn from_path(path: &Path) -> Self {
        let comps: Vec<String> = path
            .components()
            .map(|c| c.as_os_str().to_string_lossy().into_owned())
            .collect();

        let session_id = path
            .file_stem()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_else(|| "cursor-unknown".into());

        let is_subagent = path
            .parent()
            .and_then(|p| p.file_name())
            .map(|n| n == "subagents")
            .unwrap_or(false);

        let parent_session_id = if is_subagent {
            path.parent()
                .and_then(|p| p.parent())
                .and_then(|p| p.file_name())
                .map(|n| n.to_string_lossy().into_owned())
        } else {
            None
        };

        let slug = comps
            .iter()
            .position(|c| c == "agent-transcripts")
            .and_then(|i| i.checked_sub(1))
            .and_then(|i| comps.get(i))
            .cloned()
            .unwrap_or_default();
        let cwd = decode_slug_to_cwd(&slug);
        let project_dir = Path::new(&cwd)
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_else(|| slug.clone());

        PathIdentity {
            session_id,
            project_dir,
            cwd,
            is_subagent,
            parent_session_id,
        }
    }
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

    #[test]
    fn identity_main_transcript() {
        let p = Path::new(
            "/Users/me/.cursor/projects/Users-me-projects-foo/agent-transcripts/abc-123/abc-123.jsonl",
        );
        let id = PathIdentity::from_path(p);
        assert_eq!(id.session_id, "abc-123");
        assert!(!id.is_subagent);
        assert_eq!(id.parent_session_id, None);
        assert_eq!(id.cwd, "/Users/me/projects/foo");
        assert_eq!(id.project_dir, "foo");
    }

    #[test]
    fn identity_subagent_transcript() {
        let p = Path::new(
            "/Users/me/.cursor/projects/Users-me-projects-foo/agent-transcripts/abc-123/subagents/sub-9.jsonl",
        );
        let id = PathIdentity::from_path(p);
        assert_eq!(id.session_id, "sub-9");
        assert!(id.is_subagent);
        assert_eq!(id.parent_session_id.as_deref(), Some("abc-123"));
        assert_eq!(id.cwd, "/Users/me/projects/foo");
    }

    #[test]
    fn identity_ephemeral_slug_has_empty_cwd() {
        let p = Path::new("/Users/me/.cursor/projects/empty-window/agent-transcripts/x/x.jsonl");
        let id = PathIdentity::from_path(p);
        assert_eq!(id.cwd, "");
        let p2 = Path::new("/Users/me/.cursor/projects/1779680566655/agent-transcripts/y/y.jsonl");
        assert_eq!(PathIdentity::from_path(p2).cwd, "");
    }
}
