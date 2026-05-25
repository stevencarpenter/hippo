//! Cursor Agent CLI transcript poller — see
//! docs/superpowers/specs/2026-05-25-cursor-ingestion-design.md.
//!
//! Cursor transcripts are Anthropic-style JSONL (`{role, message:{content}}`)
//! with NO per-line timestamps and NO in-file session metadata: identity is
//! derived from the path, time from the file mtime, and segments split on
//! accumulated character count only.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use hippo_core::config::HippoConfig;
use hippo_core::redaction::RedactionEngine;
use rusqlite::{OptionalExtension, params};
use serde::Serialize;
use sha2::{Digest, Sha256};
use tracing::{debug, error, info, warn};
use walkdir::WalkDir;

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
/// carry no session id, cwd, or subagent marker inside the file. `cwd` here is
/// *provisional* — decoded from the (ambiguous) slug; `extract_segments`
/// overrides it with `recover_cwd_from_paths` once it has scanned the
/// transcript's own absolute paths.
#[derive(Debug, Clone)]
pub(crate) struct PathIdentity {
    pub session_id: String,
    pub project_dir: String,
    pub cwd: String,
    pub slug: String,
    pub is_subagent: bool,
    pub parent_session_id: Option<String>,
}

/// Derive the `project_dir` (last path component) for a given cwd, falling back
/// to the slug when the cwd is empty (ephemeral slugs).
fn project_dir_for(cwd: &str, slug: &str) -> String {
    Path::new(cwd)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| slug.to_string())
}

/// Decode a `~/.cursor/projects/<slug>/` slug into a cwd. The slug encodes a
/// path with `-` for `/` (same convention as ~/.claude/projects). Ephemeral
/// slugs (`empty-window`, all-digit ids, `var-folders-*` temp dirs) have no
/// real project path, so they decode to an empty cwd.
///
/// NOTE: this decode is AMBIGUOUS — Cursor's slug maps both `/` and any literal
/// `-` in the real path onto the same `-`, so `/Users/me/my-app` and
/// `/Users/me/my/app` both encode to `Users-me-my-app`. This heuristic always
/// splits on `-`, so it returns the wrong cwd for the (common) hyphenated
/// project dir. It is the *fallback* only; `recover_cwd_from_paths` resolves the
/// ambiguity exactly when the transcript carries an absolute path.
fn decode_slug_to_cwd(slug: &str) -> String {
    if slug == "empty-window"
        || slug.starts_with("var-folders")
        || slug.chars().all(|c| c.is_ascii_digit())
    {
        return String::new();
    }
    format!("/{}", slug.replace('-', "/"))
}

/// Whitespace-split a shell command and keep only the absolute-path-looking
/// tokens (those starting with `/`), trimming common surrounding quote/paren
/// punctuation. Cheap and good enough to surface a cwd-bearing path.
fn absolute_tokens_in_command(command: &str) -> Vec<String> {
    command
        .split_whitespace()
        .map(|tok| tok.trim_matches(|c: char| "\"'`(),;:".contains(c)))
        .filter(|tok| tok.starts_with('/'))
        .map(|tok| tok.to_string())
        .collect()
}

/// Collect candidate absolute paths from a `tool_use` block's `input` object:
/// the well-known path-bearing keys plus absolute tokens inside `command`.
/// Non-absolute values are ignored — only paths starting with `/` can carry a
/// real cwd. Appends into `out` (encounter order preserved).
fn collect_tool_paths(input: &serde_json::Value, out: &mut Vec<String>) {
    let Some(obj) = input.as_object() else {
        return;
    };
    for key in ["path", "file_path", "cwd", "target_directory"] {
        if let Some(v) = obj.get(key).and_then(|v| v.as_str())
            && v.starts_with('/')
        {
            out.push(v.to_string());
        }
    }
    if let Some(cmd) = obj.get("command").and_then(|v| v.as_str()) {
        out.extend(absolute_tokens_in_command(cmd));
    }
}

/// Recover the *true* cwd from absolute paths found in the transcript, using
/// the slug as the disambiguation target. For each candidate path `P`, test
/// every ancestor prefix `pre` (longest first): if encoding `pre` the way
/// Cursor encodes a cwd (`pre` without its leading `/`, with `/`→`-`) equals
/// `slug`, then `pre` is the ground-truth cwd. Returns the first exact match.
///
/// This is exact and content-only (no filesystem access): the match means an
/// ancestor of a real path used in the session encodes to exactly this slug,
/// so it disambiguates `my-app` (one dir) from `my/app` (two dirs).
fn recover_cwd_from_paths(slug: &str, candidate_paths: &[String]) -> Option<String> {
    if slug.is_empty() {
        return None;
    }
    for cand in candidate_paths {
        for pre in Path::new(cand).ancestors() {
            // Skip the bare root: it strips to "" and can't match a real slug.
            let Some(rel) = pre.to_str().and_then(|s| s.strip_prefix('/')) else {
                continue;
            };
            if rel.is_empty() {
                continue;
            }
            if rel.replace('/', "-") == slug {
                return Some(pre.to_string_lossy().into_owned());
            }
        }
    }
    None
}

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
        // Provisional cwd from the ambiguous slug; extract_segments overrides
        // it once it can disambiguate against the transcript's own paths.
        let cwd = decode_slug_to_cwd(&slug);
        let project_dir = project_dir_for(&cwd, &slug);

        PathIdentity {
            session_id,
            project_dir,
            cwd,
            slug,
            is_subagent,
            parent_session_id,
        }
    }
}

/// Pull the user's request out of a text block. Cursor wraps the first user
/// turn in `<user_query>…</user_query>`; take the inner text when present,
/// else the whole block. Capped at 500 chars (Codex parity).
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
    // Absolute paths seen in tool_use inputs, used after the scan to recover the
    // true cwd from the ambiguous slug (Finding 1).
    let mut candidate_paths: Vec<String> = Vec::new();

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
                current_chars += redacted.chars().count();
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
                current_chars += redacted.chars().count();
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
                    collect_tool_paths(&input, &mut candidate_paths);
                    let summary = redaction.redact(&tool_summary(&input)).text;
                    current_chars += summary.chars().count();
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

    // Finding 1: the slug-derived cwd is ambiguous for hyphenated project dirs.
    // Now that the whole transcript has been scanned, try to recover the true
    // cwd from an absolute path whose ancestor encodes to exactly this slug. On
    // a match, override the provisional cwd/project_dir on every segment.
    if let Some(real_cwd) = recover_cwd_from_paths(&id.slug, &candidate_paths) {
        let real_project_dir = project_dir_for(&real_cwd, &id.slug);
        for seg in &mut segments {
            seg.cwd = real_cwd.clone();
            seg.project_dir = real_project_dir.clone();
        }
    }

    Ok(segments)
}

/// Build the Cursor-framed enrichment digest stored in
/// `claude_sessions.summary_text`.
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

/// Decide whether a just-upserted segment should be (re-)enqueued for
/// enrichment. Direct port of `codex_session::decide_enqueue` — Cursor shares
/// `claude_enrichment_queue`, so it must share the re-enrichment gate or a
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

/// Upsert one segment into `claude_sessions` and (re-)enqueue it, inside a
/// caller-supplied transaction. Idempotent via `ON CONFLICT (session_id,
/// segment_index)`. Unlike Codex, Cursor passes real `is_subagent` /
/// `parent_session_id` values.
pub fn upsert_segment_tx(tx: &rusqlite::Transaction, seg: &CursorSegment) -> Result<()> {
    let now_ms = chrono::Utc::now().timestamp_millis();
    let tool_calls_json = serde_json::to_string(&seg.tool_calls).unwrap_or_else(|_| "[]".into());
    let user_prompts_json =
        serde_json::to_string(&seg.user_prompts).unwrap_or_else(|_| "[]".into());
    let summary_text = build_summary_text(seg);
    let content_hash = compute_content_hash(seg);

    #[allow(clippy::type_complexity)]
    let prior: Option<(i64, Option<String>, Option<String>, Option<i64>)> = tx
        .query_row(
            "SELECT cs.id, cs.last_enriched_content_hash, ceq.status, ceq.updated_at
             FROM claude_sessions cs
             LEFT JOIN claude_enrichment_queue ceq ON ceq.claude_session_id = cs.id
             WHERE cs.session_id = ?1 AND cs.segment_index = ?2",
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
        "INSERT INTO claude_sessions
            (session_id, project_dir, cwd, git_branch, segment_index,
             start_time, end_time, summary_text, tool_calls_json,
             user_prompts_json, message_count, token_count, source_file,
             is_subagent, parent_session_id, content_hash, created_at)
         VALUES (?1, ?2, ?3, NULL, ?4, ?5, ?6, ?7, ?8, ?9, ?10, 0, ?11, ?12, ?13, ?14, ?15)
         ON CONFLICT (session_id, segment_index) DO UPDATE SET
             end_time          = excluded.end_time,
             summary_text      = excluded.summary_text,
             tool_calls_json   = excluded.tool_calls_json,
             user_prompts_json = excluded.user_prompts_json,
             message_count     = excluded.message_count,
             content_hash      = excluded.content_hash,
             cwd               = excluded.cwd,
             project_dir       = excluded.project_dir,
             is_subagent       = excluded.is_subagent,
             parent_session_id = excluded.parent_session_id",
        params![
            seg.session_id,
            seg.project_dir,
            seg.cwd,
            seg.segment_index,
            seg.start_time,
            seg.end_time,
            summary_text,
            tool_calls_json,
            user_prompts_json,
            seg.message_count,
            seg.source_file,
            is_subagent_i,
            seg.parent_session_id,
            content_hash,
            now_ms,
        ],
    )?;

    let claude_session_id: i64 = if was_insert {
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
            "INSERT INTO claude_enrichment_queue
                 (claude_session_id, status, retry_count, error_message, created_at, updated_at)
             VALUES (?1, 'pending', 0, NULL, ?2, ?2)
             ON CONFLICT(claude_session_id) DO UPDATE SET
                 status        = 'pending',
                 retry_count   = 0,
                 error_message = NULL,
                 updated_at    = excluded.updated_at
             WHERE claude_enrichment_queue.status != 'processing'",
            params![claude_session_id, now_ms],
        )?;
    }
    Ok(())
}

/// Convenience wrapper: upsert one segment in its own transaction.
pub fn upsert_segment(conn: &rusqlite::Connection, seg: &CursorSegment) -> Result<()> {
    let tx = conn.unchecked_transaction()?;
    upsert_segment_tx(&tx, seg)?;
    tx.commit()?;
    Ok(())
}

/// Stable inode-keyed cursor for one transcript file. The `cursor-agent-`
/// prefix disambiguates from the `agentic_cursor` table's own name. Inode
/// survives a project-dir rename, so a renamed project's files aren't
/// re-parsed.
fn cursor_key(meta: &std::fs::Metadata) -> String {
    use std::os::unix::fs::MetadataExt;
    format!("cursor-agent-{}", meta.ino())
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
         WHERE source = 'agentic-session-cursor'",
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
         WHERE source = 'agentic-session-cursor'",
        params![now, format!("{err:#}")],
    ) {
        warn!("cursor source_health error update failed: {e}");
    }
}

/// True for `**/agent-transcripts/**/*.jsonl` (main + subagents).
fn is_transcript(path: &Path) -> bool {
    let is_jsonl = path.extension().map(|e| e == "jsonl").unwrap_or(false);
    let under_transcripts = path
        .components()
        .any(|c| c.as_os_str() == "agent-transcripts");
    is_jsonl && under_transcripts
}

/// One poll cycle: walk every root, ingest changed idle transcript files.
pub fn poll_tick(config: &HippoConfig) -> Result<usize> {
    if !config.cursor.enabled {
        debug!("cursor poll disabled by config");
        return Ok(0);
    }
    let conn = hippo_core::storage::open_db(&config.db_path())?;
    let now_ms = chrono::Utc::now().timestamp_millis();
    let min_idle_ms = config.cursor.min_idle_secs as i64 * 1000;

    let mut ingested = 0usize;
    for root in &config.cursor.session_roots {
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
                    "cursor: unreadable mtime for {}, skipping this tick",
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
            match ingest_file(&conn, path, mtime_ms) {
                Ok((count, session_id)) => {
                    ingested += count;
                    if count > 0 {
                        bump_health_ok(&conn, mtime_ms);
                    }
                    if let Err(e) = write_cursor(&conn, &key, mtime_ms, &session_id) {
                        warn!("cursor write failed for {}: {e:#}", path.display());
                    }
                }
                Err(e) => {
                    error!("cursor ingest failed for {}: {e:#}", path.display());
                    record_error(&conn, &e);
                }
            }
        }
    }
    info!(ingested, "cursor poll tick: completed");
    Ok(ingested)
}

/// One-shot manual import of a single Cursor transcript (recovery/backfill).
/// Mirrors the `hippo ingest cursor-session <path>` entry point.
pub fn ingest_one(config: &HippoConfig, path: &Path) -> Result<usize> {
    let conn = hippo_core::storage::open_db(&config.db_path())?;
    let mtime_ms = std::fs::metadata(path)
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_millis() as i64)
        .unwrap_or_else(|| chrono::Utc::now().timestamp_millis());
    let (count, _) = ingest_file(&conn, path, mtime_ms)?;
    if count > 0 {
        bump_health_ok(&conn, mtime_ms);
    }
    Ok(count)
}

/// Parse one file and upsert all its segments in a single transaction.
fn ingest_file(conn: &rusqlite::Connection, path: &Path, mtime_ms: i64) -> Result<(usize, String)> {
    let redaction = RedactionEngine::builtin();
    let segments = extract_segments(path, mtime_ms, &redaction)?;
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
#[doc(hidden)]
pub fn test_config(data_dir: &Path, roots: &[PathBuf]) -> HippoConfig {
    let mut cfg = HippoConfig::default();
    cfg.storage.data_dir = data_dir.to_path_buf();
    cfg.cursor.session_roots = roots.to_vec();
    cfg.cursor.min_idle_secs = 60;
    cfg
}

#[cfg(test)]
mod tests {
    use super::*;

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
        // ~25 prompts push current_chars past the 12_000 cap; the next user
        // turn then splits. 40 turns therefore yields >1 segment — proving the
        // split works with NO timestamps anywhere.
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

    // ── Finding 1: recover_cwd_from_paths disambiguates the slug ──────────────

    #[test]
    fn recover_cwd_prefers_hyphenated_dir_over_split() {
        // Slug is ambiguous: it could be /Users/me/projects/my-app (one dir) or
        // /Users/me/projects/my/app (two dirs). A real path under my-app must
        // resolve it to the hyphenated form.
        let slug = "Users-me-projects-my-app";
        let paths = vec!["/Users/me/projects/my-app/src/main.rs".to_string()];
        assert_eq!(
            recover_cwd_from_paths(slug, &paths).as_deref(),
            Some("/Users/me/projects/my-app")
        );
    }

    #[test]
    fn recover_cwd_returns_none_when_no_path_matches_slug() {
        let slug = "Users-me-projects-my-app";
        // An unrelated absolute path can't disambiguate this slug.
        let paths = vec!["/tmp/scratch/file.txt".to_string()];
        assert_eq!(recover_cwd_from_paths(slug, &paths), None);
        // No candidate paths at all -> None (fall back to decode heuristic).
        assert_eq!(recover_cwd_from_paths(slug, &[]), None);
    }

    #[test]
    fn recover_cwd_uses_first_matching_candidate() {
        let slug = "Users-me-projects-my-app";
        let paths = vec![
            "/elsewhere/file".to_string(),
            "/Users/me/projects/my-app/a.rs".to_string(),
            "/Users/me/projects/my-app/deeper/b.rs".to_string(),
        ];
        assert_eq!(
            recover_cwd_from_paths(slug, &paths).as_deref(),
            Some("/Users/me/projects/my-app")
        );
    }

    #[test]
    fn extract_segments_recovers_hyphenated_cwd_from_tool_path() {
        // Slug `Users-me-projects-my-app` would WRONGLY decode to
        // `/Users/me/projects/my/app`; a tool_use `path` under the real dir must
        // correct it to `/Users/me/projects/my-app`.
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp
            .path()
            .join("Users-me-projects-my-app/agent-transcripts/sess-1");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("sess-1.jsonl");
        let lines = [
            r#"{"role":"user","message":{"content":[{"type":"text","text":"<user_query>fix it</user_query>"}]}}"#,
            r#"{"role":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"path":"/Users/me/projects/my-app/src/main.rs"}}]}}"#,
        ];
        std::fs::write(&p, lines.join("\n")).unwrap();
        let segs = extract_segments(&p, 1_775_000_000_000, &RedactionEngine::builtin()).unwrap();
        assert_eq!(segs.len(), 1);
        assert_eq!(
            segs[0].cwd, "/Users/me/projects/my-app",
            "tool path must recover the hyphenated cwd, not the slug-split one"
        );
        assert_eq!(
            segs[0].project_dir, "my-app",
            "project_dir must match the corrected cwd"
        );
    }

    #[test]
    fn extract_segments_recovers_cwd_from_absolute_command_token() {
        // The cwd-bearing path arrives only inside a shell `command` string.
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp
            .path()
            .join("Users-me-projects-my-app/agent-transcripts/sess-2");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("sess-2.jsonl");
        let lines = [
            r#"{"role":"user","message":{"content":[{"type":"text","text":"<user_query>build</user_query>"}]}}"#,
            r#"{"role":"assistant","message":{"content":[{"type":"tool_use","name":"Shell","input":{"command":"cargo build --manifest-path /Users/me/projects/my-app/Cargo.toml"}}]}}"#,
        ];
        std::fs::write(&p, lines.join("\n")).unwrap();
        let segs = extract_segments(&p, 1_775_000_000_000, &RedactionEngine::builtin()).unwrap();
        assert_eq!(segs.len(), 1);
        assert_eq!(segs[0].cwd, "/Users/me/projects/my-app");
        assert_eq!(segs[0].project_dir, "my-app");
    }

    #[test]
    fn extract_segments_falls_back_to_slug_decode_without_tool_path() {
        // No tool path disambiguates the slug -> keep the decode heuristic. With
        // a non-hyphenated project dir the heuristic is already correct.
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp
            .path()
            .join("Users-me-projects-foo/agent-transcripts/sess-3");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("sess-3.jsonl");
        let lines = [
            r#"{"role":"user","message":{"content":[{"type":"text","text":"<user_query>hi</user_query>"}]}}"#,
            r#"{"role":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}"#,
        ];
        std::fs::write(&p, lines.join("\n")).unwrap();
        let segs = extract_segments(&p, 1_775_000_000_000, &RedactionEngine::builtin()).unwrap();
        assert_eq!(segs.len(), 1);
        assert_eq!(
            segs[0].cwd, "/Users/me/projects/foo",
            "no tool path -> slug decode heuristic stands"
        );
        assert_eq!(segs[0].project_dir, "foo");
    }

    // ── Finding 2: char cap counts chars, not bytes ──────────────────────────

    #[test]
    fn extract_segments_does_not_split_multibyte_under_char_cap() {
        // A single segment whose UTF-8 BYTE length exceeds MAX_SEGMENT_CHARS but
        // whose CHARACTER count is well under it must NOT split. Each "🦛" is 4
        // bytes but 1 char. extract_user_text caps a prompt at 500 chars, so we
        // spread the content across several user turns close together: byte-len
        // > 12_000, char-count < 12_000.
        let tmp = tempfile::tempdir().unwrap();
        let p = tmp.path().join("multibyte.jsonl");
        // 5 turns × 400 hippos = 2_000 chars (< 12_000) but 8_000 bytes; add
        // more turns to push bytes past 12_000 while chars stay under.
        // 10 turns × 400 = 4_000 chars, 16_000 bytes.
        let hippos = "🦛".repeat(400); // 400 chars, 1_600 bytes, under the 500 cap
        let mut lines = Vec::new();
        for _ in 0..10 {
            lines.push(format!(
                r#"{{"role":"user","message":{{"content":[{{"type":"text","text":"{hippos}"}}]}}}}"#
            ));
        }
        let raw = lines.join("\n");
        std::fs::write(&p, &raw).unwrap();

        let segs = extract_segments(&p, 1_000, &RedactionEngine::builtin()).unwrap();
        // Sanity: total stored prompt bytes exceed the cap, chars do not.
        let total_chars: usize = segs
            .iter()
            .flat_map(|s| s.user_prompts.iter())
            .map(|p| p.chars().count())
            .sum();
        let total_bytes: usize = segs
            .iter()
            .flat_map(|s| s.user_prompts.iter())
            .map(|p| p.len())
            .sum();
        assert!(
            total_bytes > MAX_SEGMENT_CHARS,
            "fixture must exceed cap in bytes ({total_bytes} <= {MAX_SEGMENT_CHARS})"
        );
        assert!(
            total_chars < MAX_SEGMENT_CHARS,
            "fixture must stay under cap in chars ({total_chars} >= {MAX_SEGMENT_CHARS})"
        );
        assert_eq!(
            segs.len(),
            1,
            "multibyte content under the CHAR cap must stay one segment (byte-len counting split it ~3x early), got {}",
            segs.len()
        );
    }

    #[test]
    fn collect_tool_paths_gathers_keys_and_command_tokens() {
        let mut out = Vec::new();
        collect_tool_paths(
            &serde_json::json!({"path": "/a/b", "file_path": "/c/d", "cwd": "/e"}),
            &mut out,
        );
        assert!(out.contains(&"/a/b".to_string()));
        assert!(out.contains(&"/c/d".to_string()));
        assert!(out.contains(&"/e".to_string()));

        let mut out2 = Vec::new();
        collect_tool_paths(
            &serde_json::json!({"command": "ls -la /Users/me/x && cat relative.txt"}),
            &mut out2,
        );
        assert!(
            out2.contains(&"/Users/me/x".to_string()),
            "absolute token from command must be collected; got {out2:?}"
        );
        assert!(
            !out2.iter().any(|p| p.contains("relative.txt")),
            "relative tokens must be ignored; got {out2:?}"
        );

        // Non-absolute values for path keys are ignored.
        let mut out3 = Vec::new();
        collect_tool_paths(&serde_json::json!({"path": "relative/x"}), &mut out3);
        assert!(out3.is_empty(), "relative path value must be ignored");
    }
}
