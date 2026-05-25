//! Cursor Agent CLI transcript poller — see
//! docs/superpowers/specs/2026-05-25-cursor-ingestion-design.md.
//!
//! Cursor transcripts are Anthropic-style JSONL (`{role, message:{content}}`)
//! with NO per-line timestamps and NO in-file session metadata: identity is
//! derived from the path, time from the file mtime, and segments split on
//! accumulated character count only.

use std::path::Path;

use anyhow::{Context, Result};
use hippo_core::redaction::RedactionEngine;
use serde::Serialize;
use sha2::{Digest, Sha256};

// live once Task 8 wires poll_tick → ingest_file
#[allow(dead_code)]
const MAX_SEGMENT_CHARS: usize = 12_000;

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
// live once Task 8 wires poll_tick → ingest_file
#[allow(dead_code)]
pub(crate) fn tool_summary(input: &serde_json::Value) -> String {
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
// live once Task 8 wires poll_tick → ingest_file
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
// live once Task 8 wires poll_tick → ingest_file
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

// live once Task 8 wires poll_tick → ingest_file
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

/// Pull the user's request out of a text block. Cursor wraps the first user
/// turn in `<user_query>…</user_query>`; take the inner text when present,
/// else the whole block. Capped at 500 chars (Codex parity).
// live once Task 8 wires poll_tick → ingest_file
#[allow(dead_code)]
pub(crate) fn extract_user_text(text: &str) -> String {
    let inner = match (text.find("<user_query>"), text.find("</user_query>")) {
        (Some(start), Some(end)) if end > start => {
            let from = start + "<user_query>".len();
            &text[from..end]
        }
        _ => text,
    };
    inner.trim().chars().take(500).collect()
}

/// Join the `text` of every block of the given `kind` in a `content` array.
// live once Task 8 wires poll_tick → ingest_file
#[allow(dead_code)]
fn text_blocks(content: &serde_json::Value, kind: &str) -> Vec<String> {
    content
        .as_array()
        .map(|blocks| {
            blocks
                .iter()
                .filter(|b| b.get("type").and_then(|t| t.as_str()) == Some(kind))
                .filter_map(|b| {
                    b.get("text")
                        .and_then(|t| t.as_str())
                        .map(|s| s.to_string())
                })
                .collect()
        })
        .unwrap_or_default()
}

/// Parse a Cursor agent-transcript JSONL into char-bounded segments.
///
/// `mtime_ms` stamps every segment's start/end time — Cursor transcripts have
/// no per-line timestamps. `redaction` is applied to prompts, assistant text,
/// and tool summaries before they are stored.
// live once Task 8 wires poll_tick → ingest_file
#[allow(dead_code)]
pub(crate) fn extract_segments(
    path: &Path,
    mtime_ms: i64,
    redaction: &RedactionEngine,
) -> Result<Vec<CursorSegment>> {
    let raw = std::fs::read_to_string(path)
        .with_context(|| format!("read cursor transcript {}", path.display()))?;
    let source_file = path.to_string_lossy().to_string();
    let id = PathIdentity::from_path(path);

    let new_segment = |index: i64| CursorSegment {
        session_id: id.session_id.clone(),
        project_dir: id.project_dir.clone(),
        cwd: id.cwd.clone(),
        segment_index: index,
        start_time: mtime_ms,
        end_time: mtime_ms,
        user_prompts: Vec::new(),
        assistant_texts: Vec::new(),
        tool_calls: Vec::new(),
        message_count: 0,
        source_file: source_file.clone(),
        is_subagent: id.is_subagent,
        parent_session_id: id.parent_session_id.clone(),
    };

    let mut segments: Vec<CursorSegment> = Vec::new();
    let mut current: Option<CursorSegment> = None;
    let mut current_chars: usize = 0;

    for line in raw.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let obj: serde_json::Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(_) => continue,
        };
        let role = obj.get("role").and_then(|v| v.as_str()).unwrap_or("");
        let content = obj
            .get("message")
            .and_then(|m| m.get("content"))
            .cloned()
            .unwrap_or(serde_json::Value::Null);

        if role == "user" {
            let prompts: Vec<String> = text_blocks(&content, "text")
                .iter()
                .map(|t| extract_user_text(t))
                .filter(|t| !t.is_empty())
                .collect();
            if prompts.is_empty() {
                continue;
            }

            if current_chars > MAX_SEGMENT_CHARS
                && let Some(seg) = current.take()
            {
                segments.push(seg);
                current_chars = 0;
            }

            let seg = current.get_or_insert_with(|| new_segment(segments.len() as i64));
            seg.message_count += 1;
            for p in prompts {
                let redacted = redaction.redact(&p).text;
                current_chars += redacted.len();
                seg.user_prompts.push(redacted);
            }
            continue;
        }

        let seg = match current.as_mut() {
            Some(s) => s,
            None => continue,
        };
        seg.message_count += 1;

        if role == "assistant" {
            for t in text_blocks(&content, "text") {
                let t = t.trim_end_matches("[REDACTED]").trim();
                if t.is_empty() {
                    continue;
                }
                let capped: String = t.chars().take(300).collect();
                let redacted = redaction.redact(&capped).text;
                current_chars += redacted.len();
                seg.assistant_texts.push(redacted);
            }
            if let Some(blocks) = content.as_array() {
                for b in blocks {
                    if b.get("type").and_then(|t| t.as_str()) != Some("tool_use") {
                        continue;
                    }
                    let name = b.get("name").and_then(|v| v.as_str()).unwrap_or("");
                    if name.is_empty() {
                        continue;
                    }
                    let input = b.get("input").cloned().unwrap_or(serde_json::Value::Null);
                    let summary = redaction.redact(&tool_summary(&input)).text;
                    current_chars += summary.len();
                    seg.tool_calls.push(ToolCall {
                        name: name.to_string(),
                        summary,
                    });
                }
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

/// Build the Cursor-framed enrichment digest stored in
/// `claude_sessions.summary_text`.
// live once Task 7 wires upsert_segment_tx
#[allow(dead_code)]
pub(crate) fn build_summary_text(seg: &CursorSegment) -> String {
    const MAX_PROMPTS: usize = 30;
    const MAX_TOOLS: usize = 60;
    const MAX_ASSISTANT: usize = 5;
    let header = if seg.is_subagent {
        format!("Cursor session (subagent, project: {})", seg.cwd)
    } else {
        format!("Cursor session (project: {})", seg.cwd)
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
/// `codex_session::compute_content_hash`.
// live once Task 7 wires upsert_segment_tx
#[allow(dead_code)]
pub(crate) fn compute_content_hash(seg: &CursorSegment) -> String {
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

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_segment() -> CursorSegment {
        CursorSegment {
            session_id: "s1".into(),
            project_dir: "proj".into(),
            cwd: "/work/proj".into(),
            segment_index: 0,
            start_time: 1_775_634_000_000,
            end_time: 1_775_634_500_000,
            user_prompts: vec!["fix the bug".into()],
            assistant_texts: vec!["done".into()],
            tool_calls: vec![ToolCall {
                name: "Shell".into(),
                summary: "cargo test".into(),
            }],
            message_count: 3,
            source_file: "/Users/x/.cursor/projects/p/agent-transcripts/s1/s1.jsonl".into(),
            is_subagent: false,
            parent_session_id: None,
        }
    }

    #[test]
    fn summary_text_includes_prompts_tools_and_project() {
        let s = build_summary_text(&sample_segment());
        assert!(s.contains("Cursor session"));
        assert!(s.contains("/work/proj"));
        assert!(s.contains("fix the bug"));
        assert!(s.contains("Shell"));
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

    fn fixture(name: &str) -> std::path::PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures/cursor")
            .join(name)
    }

    #[test]
    fn extract_segments_parses_main_fixture() {
        let p = fixture("transcript-main.jsonl");
        let segs = extract_segments(&p, 1_775_000_000_000, &RedactionEngine::builtin()).unwrap();
        assert_eq!(segs.len(), 1);
        let s = &segs[0];
        assert_eq!(s.session_id, "transcript-main");
        assert!(!s.is_subagent);
        assert_eq!(s.user_prompts, vec!["fix the failing build".to_string()]);
        assert_eq!(s.user_prompts.len(), 1);
        assert!(s.assistant_texts.iter().any(|t| t.contains("build")));
        assert_eq!(s.tool_calls.len(), 2);
        assert_eq!(s.tool_calls[0].name, "Shell");
        assert_eq!(s.tool_calls[0].summary, "cargo build");
        assert_eq!(s.start_time, 1_775_000_000_000);
        assert_eq!(s.end_time, 1_775_000_000_000);
    }

    #[test]
    fn extract_segments_subagent_identity() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp
            .path()
            .join("Users-me-projects-foo/agent-transcripts/parent-1/subagents");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("sub-1.jsonl");
        std::fs::copy(fixture("transcript-subagent.jsonl"), &p).unwrap();
        let segs = extract_segments(&p, 1_775_000_000_000, &RedactionEngine::builtin()).unwrap();
        assert_eq!(segs.len(), 1);
        assert!(segs[0].is_subagent);
        assert_eq!(segs[0].parent_session_id.as_deref(), Some("parent-1"));
        assert_eq!(segs[0].session_id, "sub-1");
    }

    #[test]
    fn extract_segments_splits_on_char_cap_without_timestamps() {
        // extract_user_text caps each prompt at 500 chars, so a segment holds
        // ~24 prompts (~12000 chars) before the next user turn forces a split.
        // 40 turns therefore yields >1 segment — proving the split works with
        // NO timestamps anywhere.
        let tmp = tempfile::tempdir().unwrap();
        let p = tmp.path().join("big.jsonl");
        let big = "x".repeat(600); // capped to 500 by extract_user_text
        let mut lines = Vec::new();
        for _ in 0..40 {
            lines.push(format!(
                r#"{{"role":"user","message":{{"content":[{{"type":"text","text":"{big}"}}]}}}}"#
            ));
        }
        std::fs::write(&p, lines.join("\n")).unwrap();
        let segs = extract_segments(&p, 1_000, &RedactionEngine::builtin()).unwrap();
        assert!(
            segs.len() > 1,
            "40 user turns (~500 chars each) must split despite no timestamps, got {}",
            segs.len()
        );
        assert_eq!(segs[1].segment_index, 1);
    }
}
