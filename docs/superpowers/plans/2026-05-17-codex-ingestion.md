# Codex Session Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest OpenAI Codex CLI rollout sessions into hippo via a new self-contained Rust daemon poller, retiring the legacy Python ingestion script.

**Architecture:** A launchd-scheduled `hippo codex-poll` runs `codex_session::poll_tick`, which walks the configured Codex rollout roots, parses `rollout-*.jsonl` files into task-boundary segments, and upserts them into the existing segmented `claude_sessions` table (+ `claude_enrichment_queue`). The module is fully self-contained, modeled on `opencode_session.rs` — its own segment struct, parser, upsert, content-hash. A per-file mtime cursor in `agentic_cursor` skips unchanged files.

**Tech Stack:** Rust (edition 2024), rusqlite, serde_json, sha2, chrono, anyhow, tracing. Spec: `docs/superpowers/specs/2026-05-17-codex-ingestion-design.md`.

---

## File Structure

- **Create** `crates/hippo-daemon/src/codex_session.rs` — the entire poller: `CodexSegment` struct, rollout parser, segmentation, `claude_sessions` upsert, `agentic_cursor` cursor, `source_health` bumps, `poll_tick` entry point.
- **Create** `launchd/com.hippo.codex-session.plist` — LaunchAgent running `hippo codex-poll`.
- **Modify** `crates/hippo-core/src/config.rs` — add `CodexConfig`, add `codex` field to `HippoConfig`.
- **Modify** `crates/hippo-core/src/storage.rs` — v14→v15 migration seeding `source_health`.
- **Modify** `crates/hippo-core/src/schema.sql` — add the `agentic-session-codex` seed row + `PRAGMA user_version = 15`.
- **Modify** `crates/hippo-daemon/src/lib.rs` — `pub mod codex_session;`.
- **Modify** `crates/hippo-daemon/src/main.rs` — `CodexPoll` command variant, dispatch arm, plist install wiring.
- **Modify** `crates/hippo-daemon/src/install.rs` — `codex_poll_interval_secs` in `PlistVars`.
- **Modify** `crates/hippo-daemon/src/watchdog.rs` — Codex coverage invariant.
- **Modify** `crates/hippo-daemon/src/commands.rs` — add `agentic-session-codex` to doctor staleness check.
- **Modify** `mise.toml` — replace `com.hippo.xcode-codex-ingest` with `com.hippo.codex-session`.
- **Delete** `launchd/com.hippo.xcode-codex-ingest.plist`, `scripts/hippo-ingest-codex.py`, `brain/src/hippo_brain/codex_sessions.py` + its tests.
- **Create** `crates/hippo-daemon/tests/codex_session.rs` — integration tests.

A reference for parser behavior is the existing `brain/src/hippo_brain/codex_sessions.py` (the proven implementation being ported). Quote it while implementing Tasks 3–5.

---

## Task 1: Add `CodexConfig` to hippo-core

**Files:**
- Modify: `crates/hippo-core/src/config.rs` (after `OpenConfig`, ~line 527; and `HippoConfig` ~line 26)
- Test: `crates/hippo-core/src/config.rs` (inline `#[cfg(test)]`)

- [ ] **Step 1: Write the failing test**

Add to the `config.rs` test module:

```rust
#[test]
fn codex_config_defaults_are_sane() {
    let c = CodexConfig::default();
    assert!(c.enabled);
    assert_eq!(c.poll_interval_secs, 60);
    assert_eq!(c.min_idle_secs, 60);
    assert!(c.session_roots.iter().any(|p| p.ends_with(".codex/sessions")));
    assert!(c.session_roots.iter().any(|p| p.ends_with(".codex/archived_sessions")));
}

#[test]
fn hippo_config_has_codex_with_default() {
    let toml = "";
    let cfg: HippoConfig = toml::from_str(toml).unwrap();
    assert!(cfg.codex.enabled);
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cargo test -p hippo-core codex_config`
Expected: FAIL — `CodexConfig` not found.

- [ ] **Step 3: Implement `CodexConfig`**

Add after `OpenConfig`'s `Default` impl in `config.rs`:

```rust
fn default_codex_enabled() -> bool {
    true
}

fn default_codex_poll_interval_secs() -> u64 {
    60
}

fn default_codex_min_idle_secs() -> u64 {
    60
}

fn default_codex_session_roots() -> Vec<PathBuf> {
    let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("."));
    vec![
        home.join(".codex/sessions"),
        home.join(".codex/archived_sessions"),
        home.join("Library/Developer/Xcode/CodingAssistant/codex/sessions"),
    ]
}

/// Codex CLI rollout-session ingestion. The poller walks `session_roots` for
/// `rollout-*.jsonl` files and writes segmented rows into `claude_sessions`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CodexConfig {
    /// Enable Codex session ingestion. When false, `poll_tick` is a no-op.
    #[serde(default = "default_codex_enabled")]
    pub enabled: bool,
    /// Directories scanned recursively for `rollout-*.jsonl` files.
    #[serde(default = "default_codex_session_roots")]
    pub session_roots: Vec<PathBuf>,
    /// Skip files modified within this many seconds — they may be in-flight
    /// and a partial read would freeze the segment at an early state.
    #[serde(default = "default_codex_min_idle_secs")]
    pub min_idle_secs: u64,
    /// launchd StartInterval for the codex-poll job, in seconds.
    #[serde(default = "default_codex_poll_interval_secs")]
    pub poll_interval_secs: u64,
}

impl Default for CodexConfig {
    fn default() -> Self {
        Self {
            enabled: default_codex_enabled(),
            session_roots: default_codex_session_roots(),
            min_idle_secs: default_codex_min_idle_secs(),
            poll_interval_secs: default_codex_poll_interval_secs(),
        }
    }
}
```

Add the field to `HippoConfig` (next to `pub opencode: OpenConfig,`):

```rust
    #[serde(default)]
    pub codex: CodexConfig,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cargo test -p hippo-core codex_config`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-core/src/config.rs
git commit -m "feat(codex): add CodexConfig to hippo-core config"
```

---

## Task 2: Schema v15 — seed `agentic-session-codex` in `source_health`

**Files:**
- Modify: `crates/hippo-core/src/storage.rs` (`EXPECTED_VERSION` line 16; migration block after v13→v14, ~line 790)
- Modify: `crates/hippo-core/src/schema.sql` (`source_health` seed ~line 623; `PRAGMA user_version` line 634)
- Test: `crates/hippo-core/src/storage.rs` (inline `#[cfg(test)]`)

- [ ] **Step 1: Write the failing test**

```rust
#[test]
fn test_migrate_v14_to_v15_seeds_codex_source_health() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("test.db");
    {
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE source_health (
                source TEXT PRIMARY KEY,
                last_event_ts INTEGER,
                last_success_ts INTEGER,
                last_error_ts INTEGER,
                last_error_msg TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                events_last_1h INTEGER NOT NULL DEFAULT 0,
                events_last_24h INTEGER NOT NULL DEFAULT 0,
                probe_ok INTEGER,
                probe_lag_ms INTEGER,
                updated_at INTEGER NOT NULL DEFAULT 0
            );
            PRAGMA user_version = 14;",
        )
        .unwrap();
    }
    let conn = open_db(&db_path).unwrap();
    let exists: bool = conn
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM source_health WHERE source = 'agentic-session-codex')",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(exists, "v15 migration must seed agentic-session-codex");
    let v: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0)).unwrap();
    assert_eq!(v, EXPECTED_VERSION);
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cargo test -p hippo-core test_migrate_v14_to_v15`
Expected: FAIL — `EXPECTED_VERSION` is still 14, no codex row.

- [ ] **Step 3: Implement the migration**

Bump `storage.rs` line 16:

```rust
pub const EXPECTED_VERSION: i64 = 15;
```

After the v13→v14 migration block (~line 790), add:

```rust
// v14→v15: seed the source_health row for the Codex poller. The poller's
// source_health UPDATE is a silent no-op without this row. Codex writes the
// claude_sessions table, but its capture-path health key is
// `agentic-session-codex` (health-row names are decoupled from table names —
// Claude likewise writes claude_sessions but reports as agentic-session-claude).
if version < 15 {
    let has_source_health: bool = conn
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_health')",
            [],
            |r| r.get(0),
        )
        .unwrap_or(false);
    if has_source_health {
        conn.execute_batch(
            "INSERT OR IGNORE INTO source_health (source, last_event_ts, updated_at) VALUES
                ('agentic-session-codex', NULL, unixepoch('now') * 1000);",
        )?;
    }
    conn.execute_batch("PRAGMA user_version = 15;")?;
}
```

In `schema.sql`, add to the `source_health` seed `INSERT` (~line 623, after the `agentic-session-opencode` line, before `brain-preflight`):

```sql
    ('agentic-session-codex',   NULL, unixepoch('now') * 1000),
```

Change `schema.sql` line 634:

```sql
PRAGMA user_version = 15;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cargo test -p hippo-core test_migrate_v14_to_v15`
Expected: PASS.

- [ ] **Step 5: Run the full hippo-core suite to catch version-pinned tests**

Run: `cargo test -p hippo-core`
Expected: PASS. If a test asserts `user_version == 14` or `EXPECTED_VERSION == 14`, update it to 15 — that is a correct consequence of the bump, not a regression.

- [ ] **Step 6: Commit**

```bash
git add crates/hippo-core/src/storage.rs crates/hippo-core/src/schema.sql
git commit -m "feat(codex): schema v15 seeds agentic-session-codex source_health row"
```

---

## Task 3: Codex rollout parser — line types and `CodexSegment`

**Files:**
- Create: `crates/hippo-daemon/src/codex_session.rs`
- Modify: `crates/hippo-daemon/src/lib.rs` (module list, ~line 13)
- Test: inline `#[cfg(test)]` in `codex_session.rs`

Reference: `brain/src/hippo_brain/codex_sessions.py` lines 99–135 (`_parse_ts`, `_tool_summary`).

- [ ] **Step 1: Declare the module**

In `lib.rs`, add alphabetically near `claude_session`:

```rust
pub mod codex_session;
```

- [ ] **Step 2: Write the failing test**

Create `codex_session.rs` with only:

```rust
//! Codex CLI rollout-session poller — see
//! docs/superpowers/specs/2026-05-17-codex-ingestion-design.md.

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_ts_handles_iso_and_garbage() {
        assert_eq!(parse_ts("2026-04-04T07:47:59.376Z"), 1775634479376);
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cargo test -p hippo-daemon codex_session`
Expected: FAIL — `parse_ts` / `tool_summary` not defined.

- [ ] **Step 4: Implement the helpers and `CodexSegment`**

Prepend to `codex_session.rs` (above the test module):

```rust
use anyhow::{Context, Result};
use chrono::DateTime;
use serde::Serialize;
use std::path::{Path, PathBuf};

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
    let parsed: serde_json::Value = serde_json::from_str(arguments).unwrap_or(serde_json::Value::Null);
    if let Some(obj) = parsed.as_object() {
        for key in ["cmd", "command", "filePath", "path", "uri", "query", "pattern"] {
            if let Some(v) = obj.get(key).and_then(|v| v.as_str()) {
                return v.chars().take(120).collect();
            }
        }
        for v in obj.values() {
            if let Some(s) = v.as_str() {
                if !s.is_empty() {
                    return s.chars().take(80).collect();
                }
            }
        }
    }
    arguments.chars().take(80).collect()
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cargo test -p hippo-daemon codex_session`
Expected: PASS. (Verify the `parse_ts` epoch value against the test — adjust the expected constant if your timezone math differs; `1775634479376` is `2026-04-04T07:47:59.376Z` in UTC.)

- [ ] **Step 6: Commit**

```bash
git add crates/hippo-daemon/src/lib.rs crates/hippo-daemon/src/codex_session.rs
git commit -m "feat(codex): scaffold codex_session module with rollout helpers"
```

---

## Task 4: Codex rollout parser — segment extraction

**Files:**
- Modify: `crates/hippo-daemon/src/codex_session.rs`
- Test: inline `#[cfg(test)]`

Reference: `codex_sessions.py` `extract_codex_segments` (lines 138–303). The Rust port must handle user prompts appearing as **either** `event_msg`/`user_message` **or** `response_item`/`message` with `role == "user"` (the standalone CLI and Xcode-embedded Codex differ — see spec §4.4).

- [ ] **Step 1: Write the failing test**

Add fixture-driven tests. Create a **synthetic, hand-authored** fixture committed to the repo at `crates/hippo-daemon/tests/fixtures/codex/rollout-cli.jsonl`. **Do not copy a real session file** — hippo is a public repository, and a real rollout contains the owner's actual prompts, code, and filesystem paths. Hand-author a small fixture that exercises the line types the parser handles (`session_meta`, `event_msg`/`user_message`, `response_item` function call + assistant message):

```jsonl
{"timestamp":"2026-04-04T07:47:59.376Z","type":"session_meta","payload":{"id":"019d5776-0000-7a03-8832-synthfixture0","timestamp":"2026-04-04T07:47:55.190Z","cwd":"/Users/dev/proj","originator":"Codex Desktop","cli_version":"0.0.0-test"}}
{"timestamp":"2026-04-04T07:48:00.000Z","type":"event_msg","payload":{"type":"user_message","message":"add a unit test for the parser"}}
{"timestamp":"2026-04-04T07:48:02.000Z","type":"response_item","payload":{"type":"function_call","name":"shell","arguments":"{\"command\":\"cargo test\"}"}}
{"timestamp":"2026-04-04T07:48:05.000Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Added the test and it passes."}]}}
```

```rust
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
    assert!(segs[0].user_prompts.iter().any(|p| p.contains("hello codex")));
}

#[test]
fn extract_user_text_strips_xcode_status_prefix() {
    // Faithful to codex_sessions.py _XCODE_STATUS_PATTERN: the real user text
    // follows the last "The user ... " status line.
    let msg = "Project structure:\n  src/\nThe user has no code selected.\nrefactor the parser";
    assert_eq!(extract_user_text(msg), "refactor the parser");
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cargo test -p hippo-daemon codex_session`
Expected: FAIL — `extract_segments` not defined.

- [ ] **Step 3: Implement `extract_segments`**

Add to `codex_session.rs`:

```rust
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
fn extract_user_text(message: &str) -> String {
    let markers = ["code selected", "file currently open", "inside this file:"];
    let mut cut = 0usize;
    for m in markers {
        if let Some(idx) = message.rfind(m) {
            // Advance through the rest of that status line (its trailing `\n`).
            let after = idx + m.len();
            let line_end = message[after..]
                .find('\n')
                .map(|n| after + n + 1)
                .unwrap_or(message.len());
            cut = cut.max(line_end);
        }
    }
    let candidate = message[cut..].trim();
    let text = if !candidate.is_empty() {
        candidate
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
            if let Some(cwd) = payload.get("cwd").and_then(|v| v.as_str()) {
                if !cwd.is_empty() {
                    session_cwd = cwd.to_string();
                    if let Some(c) = current.as_mut() {
                        c.cwd = cwd.to_string();
                    }
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
                payload.get("message").and_then(|v| v.as_str()).unwrap_or("").to_string()
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
                if let Some(seg) = current.take() {
                    if !seg.user_prompts.is_empty()
                        || !seg.tool_calls.is_empty()
                        || !seg.assistant_texts.is_empty()
                    {
                        segments.push(seg);
                    }
                }
                current_chars = 0;
            }

            let seg = current.get_or_insert_with(|| {
                let cwd = if session_cwd.is_empty() {
                    path.parent().map(|p| p.to_string_lossy().to_string()).unwrap_or_default()
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
            seg.tool_calls.push(ToolCall { name: name.to_string(), summary });
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

    if let Some(seg) = current.take() {
        if !seg.user_prompts.is_empty()
            || !seg.tool_calls.is_empty()
            || !seg.assistant_texts.is_empty()
        {
            segments.push(seg);
        }
    }
    Ok(segments)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cargo test -p hippo-daemon codex_session`
Expected: PASS (all five tests). If `extract_segments_parses_committed_cli_fixture` fails, the real rollout format is authoritative — diff the synthetic fixture against a real `~/.codex/sessions/` rollout locally (do not commit the real one), adjust the parser to match observed line shapes, and document any deviation from the Python reference.

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-daemon/src/codex_session.rs crates/hippo-daemon/tests/fixtures/codex/
git commit -m "feat(codex): parse rollout JSONL into task-boundary segments"
```

---

## Task 5: `summary_text` and `content_hash`

**Files:**
- Modify: `crates/hippo-daemon/src/codex_session.rs`
- Test: inline `#[cfg(test)]`

- [ ] **Step 1: Write the failing test**

```rust
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
        tool_calls: vec![ToolCall { name: "shell".into(), summary: "cargo test".into() }],
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
fn content_hash_is_stable_and_changes_with_content() {
    let a = compute_content_hash(&sample_segment());
    let b = compute_content_hash(&sample_segment());
    assert_eq!(a, b);
    let mut changed = sample_segment();
    changed.user_prompts = vec!["different".into()];
    assert_ne!(a, compute_content_hash(&changed));
    assert_eq!(a.len(), 64); // SHA256 hex
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cargo test -p hippo-daemon codex_session`
Expected: FAIL — `build_summary_text` / `compute_content_hash` not defined.

- [ ] **Step 3: Implement**

Add `use sha2::{Digest, Sha256};` to the imports, then:

```rust
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
            lines.push(format!("  … (+{} more)", seg.user_prompts.len() - MAX_PROMPTS));
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
    hasher.finalize().iter().map(|b| format!("{b:02x}")).collect()
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cargo test -p hippo-daemon codex_session`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-daemon/src/codex_session.rs
git commit -m "feat(codex): build enrichment summary_text and content_hash"
```

---

## Task 6: Upsert a segment into `claude_sessions` + `claude_enrichment_queue`

**Files:**
- Modify: `crates/hippo-daemon/src/codex_session.rs`
- Test: `crates/hippo-daemon/tests/codex_session.rs` (create)

- [ ] **Step 1: Write the failing test**

Create `crates/hippo-daemon/tests/codex_session.rs`:

```rust
use hippo_core::storage::open_db;
use tempfile::TempDir;

#[test]
fn upsert_writes_claude_session_and_enqueues() {
    let tmp = TempDir::new().unwrap();
    let db_path = tmp.path().join("hippo.db");
    let conn = open_db(&db_path).unwrap();

    let seg = hippo_daemon::codex_session::CodexSegment {
        session_id: "codex-1".into(),
        project_dir: "proj".into(),
        cwd: "/work/proj".into(),
        segment_index: 0,
        start_time: 1_775_634_000_000,
        end_time: 1_775_634_500_000,
        user_prompts: vec!["do a thing".into()],
        assistant_texts: vec![],
        tool_calls: vec![],
        message_count: 1,
        source_file: "/Users/x/.codex/sessions/2026/04/04/rollout-codex-1.jsonl".into(),
    };
    hippo_daemon::codex_session::upsert_segment(&conn, &seg).unwrap();

    let (cnt, src): (i64, String) = conn
        .query_row(
            "SELECT COUNT(*), MAX(source_file) FROM claude_sessions WHERE session_id = 'codex-1'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(cnt, 1);
    assert!(src.contains("/.codex/"));

    let queued: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM claude_enrichment_queue q
             JOIN claude_sessions s ON s.id = q.claude_session_id
             WHERE s.session_id = 'codex-1' AND q.status = 'pending'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(queued, 1);

    // Idempotent re-upsert: no duplicate row.
    hippo_daemon::codex_session::upsert_segment(&conn, &seg).unwrap();
    let cnt2: i64 = conn
        .query_row("SELECT COUNT(*) FROM claude_sessions WHERE session_id = 'codex-1'", [], |r| r.get(0))
        .unwrap();
    assert_eq!(cnt2, 1, "re-upsert must not duplicate");
}
```

`CodexSegment`, `ToolCall`, and `upsert_segment` must be reachable from the integration test, so change their visibility from `pub(crate)` to `pub` in `codex_session.rs`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cargo test -p hippo-daemon --test codex_session`
Expected: FAIL — `upsert_segment` not defined.

- [ ] **Step 3: Implement `upsert_segment`**

Add `use rusqlite::params;` to imports, then:

```rust
/// Upsert one segment into `claude_sessions` and (re-)enqueue it for
/// enrichment, inside a caller-supplied transaction. Idempotent via
/// `ON CONFLICT (session_id, segment_index)`. `ingest_file` (Task 7) calls
/// this directly so a whole rollout file's segments commit atomically
/// (spec §4.3, AP-1).
pub fn upsert_segment_tx(tx: &rusqlite::Transaction, seg: &CodexSegment) -> Result<()> {
    let now_ms = chrono::Utc::now().timestamp_millis();
    let tool_calls_json = serde_json::to_string(&seg.tool_calls).unwrap_or_else(|_| "[]".into());
    let user_prompts_json =
        serde_json::to_string(&seg.user_prompts).unwrap_or_else(|_| "[]".into());
    let summary_text = build_summary_text(seg);
    let content_hash = compute_content_hash(seg);

    tx.execute(
        "INSERT INTO claude_sessions
            (session_id, project_dir, cwd, git_branch, segment_index,
             start_time, end_time, summary_text, tool_calls_json,
             user_prompts_json, message_count, token_count, source_file,
             is_subagent, parent_session_id, content_hash, created_at)
         VALUES (?1, ?2, ?3, NULL, ?4, ?5, ?6, ?7, ?8, ?9, ?10, 0, ?11, 0, NULL, ?12, ?13)
         ON CONFLICT (session_id, segment_index) DO UPDATE SET
             end_time          = excluded.end_time,
             summary_text      = excluded.summary_text,
             tool_calls_json   = excluded.tool_calls_json,
             user_prompts_json = excluded.user_prompts_json,
             message_count     = excluded.message_count,
             content_hash      = excluded.content_hash",
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
            content_hash,
            now_ms,
        ],
    )?;

    let claude_session_id: i64 = tx.query_row(
        "SELECT id FROM claude_sessions WHERE session_id = ?1 AND segment_index = ?2",
        params![seg.session_id, seg.segment_index],
        |r| r.get(0),
    )?;

    // Re-pend for enrichment unless a worker currently holds it.
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cargo test -p hippo-daemon --test codex_session`
Expected: PASS.

> Note: if `claude_enrichment_queue` columns differ from `(claude_session_id, status, retry_count, error_message, created_at, updated_at)`, match the actual schema in `crates/hippo-core/src/schema.sql` — the statement above is copied verbatim from `claude_session.rs:1083`.

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-daemon/src/codex_session.rs crates/hippo-daemon/tests/codex_session.rs
git commit -m "feat(codex): upsert segments into claude_sessions"
```

---

## Task 7: `poll_tick` — file walk, cursor, source_health

**Files:**
- Modify: `crates/hippo-daemon/src/codex_session.rs`
- Test: `crates/hippo-daemon/tests/codex_session.rs`

- [ ] **Step 1: Write the failing test**

```rust
fn write_rollout(dir: &std::path::Path, id: &str, prompt: &str) -> std::path::PathBuf {
    let p = dir.join(format!("rollout-{id}.jsonl"));
    let lines = [
        format!(r#"{{"timestamp":"2026-04-04T00:00:00.000Z","type":"session_meta","payload":{{"id":"{id}","cwd":"/proj"}}}}"#),
        format!(r#"{{"timestamp":"2026-04-04T00:00:01.000Z","type":"event_msg","payload":{{"type":"user_message","message":"{prompt}"}}}}"#),
    ];
    std::fs::write(&p, lines.join("\n")).unwrap();
    p
}

#[test]
fn poll_tick_ingests_idle_files_and_advances_cursor() {
    let tmp = TempDir::new().unwrap();
    let roots = tmp.path().join("sessions");
    std::fs::create_dir_all(&roots).unwrap();
    let f = write_rollout(&roots, "p1", "hello");
    // Backdate mtime so the file is "idle".
    let old = std::time::SystemTime::now() - std::time::Duration::from_secs(3600);
    filetime::set_file_mtime(&f, filetime::FileTime::from_system_time(old)).unwrap();

    let data_dir = tmp.path().join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    let config = hippo_daemon::codex_session::test_config(&data_dir, &[roots.clone()]);
    let _ = open_db(&config.db_path()).unwrap();

    let n = hippo_daemon::codex_session::poll_tick(&config).unwrap();
    assert_eq!(n, 1, "one new segment ingested");

    // Second tick: file unchanged -> cursor skip, zero new.
    let n2 = hippo_daemon::codex_session::poll_tick(&config).unwrap();
    assert_eq!(n2, 0, "unchanged file must be skipped");

    let conn = open_db(&config.db_path()).unwrap();
    let health: i64 = conn
        .query_row(
            "SELECT last_success_ts FROM source_health WHERE source = 'agentic-session-codex'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(health > 0, "source_health must be bumped");
}

#[test]
fn poll_tick_skips_in_flight_files() {
    let tmp = TempDir::new().unwrap();
    let roots = tmp.path().join("sessions");
    std::fs::create_dir_all(&roots).unwrap();
    write_rollout(&roots, "fresh", "in flight"); // mtime = now
    let data_dir = tmp.path().join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    let config = hippo_daemon::codex_session::test_config(&data_dir, &[roots]);
    let _ = open_db(&config.db_path()).unwrap();
    let n = hippo_daemon::codex_session::poll_tick(&config).unwrap();
    assert_eq!(n, 0, "files within min_idle_secs are skipped");
}
```

Add `filetime` as a dev-dependency in `crates/hippo-daemon/Cargo.toml` under `[dev-dependencies]` if not present: `filetime = "0.2"`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cargo test -p hippo-daemon --test codex_session`
Expected: FAIL — `poll_tick` / `test_config` not defined.

- [ ] **Step 3: Implement `poll_tick` and the cursor**

Add to `codex_session.rs`. The walk uses `walkdir` (already a hippo-daemon dependency):

```rust
use hippo_core::config::HippoConfig;
use tracing::{debug, error, info, warn};
use walkdir::WalkDir;

/// Stable inode-keyed cursor key for one rollout file. Inode survives the
/// `mv` Codex performs on archival, so archived files aren't re-parsed.
/// `ino()` is available on every Unix target via `MetadataExt` — no per-OS
/// `cfg` split is needed.
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

fn write_cursor(conn: &rusqlite::Connection, key: &str, mtime_ms: i64, session_id: &str) -> Result<()> {
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

    let mut ingested = 0usize;
    for root in &config.codex.session_roots {
        if !root.is_dir() {
            continue;
        }
        for entry in WalkDir::new(root).into_iter().filter_map(|e| e.ok()) {
            let path = entry.path();
            let is_rollout = path.extension().map(|e| e == "jsonl").unwrap_or(false)
                && path.file_name().and_then(|n| n.to_str())
                    .map(|n| n.starts_with("rollout-")).unwrap_or(false);
            if !is_rollout {
                continue;
            }
            let meta = match entry.metadata() {
                Ok(m) => m,
                Err(_) => continue,
            };
            let mtime_ms = meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_millis() as i64)
                .unwrap_or(0);
            // Skip in-flight files (avoid partial reads).
            if now_ms - mtime_ms < min_idle_ms {
                continue;
            }
            let key = cursor_key(&meta);
            if mtime_ms <= read_cursor(&conn, &key) {
                continue; // unchanged since last successful parse
            }
            match ingest_file(&conn, path) {
                Ok((count, session_id)) => {
                    ingested += count;
                    bump_health_ok(&conn, mtime_ms);
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
fn ingest_file(conn: &rusqlite::Connection, path: &Path) -> Result<(usize, String)> {
    let segments = extract_segments(path)?;
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
pub fn test_config(data_dir: &Path, roots: &[PathBuf]) -> HippoConfig {
    let mut cfg = HippoConfig::default();
    cfg.storage.data_dir = data_dir.to_path_buf();
    cfg.codex.session_roots = roots.to_vec();
    cfg.codex.min_idle_secs = 60;
    cfg
}
```

`ingest_file` calls `upsert_segment_tx` (defined in Task 6) so a whole rollout
file's segments commit in one transaction (spec §4.3, AP-1).

- [ ] **Step 4: Run test to verify it passes**

Run: `cargo test -p hippo-daemon --test codex_session`
Expected: PASS (all tests).

- [ ] **Step 5: Run clippy and fmt**

Run: `cargo clippy -p hippo-daemon --all-targets -- -D warnings && cargo fmt --check`
Expected: clean. Fix any lint before committing.

- [ ] **Step 6: Commit**

```bash
git add crates/hippo-daemon/src/codex_session.rs crates/hippo-daemon/tests/codex_session.rs crates/hippo-daemon/Cargo.toml
git commit -m "feat(codex): poll_tick walks roots, ingests idle files, tracks cursor"
```

---

## Task 8: CLI command `hippo codex-poll`

**Files:**
- Modify: `crates/hippo-daemon/src/main.rs` (`Commands` enum; dispatch arm near `OpencodePoll` ~line 1042)
- Test: manual (Step 4)

- [ ] **Step 1: Add the command variant**

In the `Commands` enum in `main.rs`, next to `OpencodePoll`:

```rust
    /// Poll Codex CLI rollout files and ingest new sessions.
    CodexPoll,
```

- [ ] **Step 2: Add the dispatch arm**

Next to the `Commands::OpencodePoll` arm:

```rust
Commands::CodexPoll => {
    match codex_session::poll_tick(&config) {
        Ok(n) => tracing::info!(ingested = n, "codex poll: completed"),
        Err(e) => {
            eprintln!("Error running codex poll: {e:#}");
            std::process::exit(1);
        }
    }
}
```

Ensure `use hippo_daemon::codex_session;` (or the crate-local equivalent) is in scope, matching how `opencode_session` is imported in `main.rs`.

- [ ] **Step 3: Build**

Run: `cargo build -p hippo-daemon`
Expected: compiles clean.

- [ ] **Step 4: Verify the command exists**

Run: `cargo run -p hippo-daemon -- codex-poll`
Expected: runs a poll tick against the real config; exits 0; logs `codex poll: completed`.

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-daemon/src/main.rs
git commit -m "feat(codex): add hippo codex-poll CLI command"
```

---

## Task 9: launchd job — `com.hippo.codex-session`

**Files:**
- Create: `launchd/com.hippo.codex-session.plist`
- Delete: `launchd/com.hippo.xcode-codex-ingest.plist`
- Modify: `crates/hippo-daemon/src/install.rs` (`PlistVars`, `detect_vars`)
- Modify: `crates/hippo-daemon/src/main.rs` (install/bootout wiring)

- [ ] **Step 1: Create the new plist**

`launchd/com.hippo.codex-session.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hippo.codex-session</string>
    <key>ProgramArguments</key>
    <array>
        <string>__HIPPO_BIN__</string>
        <string>codex-poll</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>__HOME__</string>
        <key>PATH</key>
        <string>__PATH__</string>
    </dict>
    <key>StartInterval</key><integer>__CODEX_POLL_INTERVAL_SECS__</integer>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>RunAtLoad</key><false/>
    <key>WatchPaths</key>
    <array>
        <string>__HOME__/.codex/sessions</string>
        <string>__HOME__/Library/Developer/Xcode/CodingAssistant/codex/sessions</string>
    </array>
    <key>StandardOutPath</key>
    <string>__DATA_DIR__/codex-session.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>__DATA_DIR__/codex-session.stderr.log</string>
    <key>WorkingDirectory</key>
    <string>__HOME__</string>
</dict>
</plist>
```

- [ ] **Step 2: Delete the legacy plist**

```bash
git rm launchd/com.hippo.xcode-codex-ingest.plist
```

- [ ] **Step 3: Add the plist variable**

In `install.rs`, add to `PlistVars`:

```rust
    pub codex_poll_interval_secs: u64,
```

In `detect_vars`, after `opencode_poll_interval_secs`:

```rust
    let codex_poll_interval_secs = cfg
        .as_ref()
        .map(|c| c.codex.poll_interval_secs)
        .unwrap_or(60);
```

Add `codex_poll_interval_secs,` to the returned `PlistVars { ... }`.

In `render_plist`, add to the chain:

```rust
        .replace(
            "__CODEX_POLL_INTERVAL_SECS__",
            &vars.codex_poll_interval_secs.to_string(),
        )
```

- [ ] **Step 4: Update main.rs install wiring**

In `main.rs`, replace the `xcode_codex_template` block:

```rust
                let codex_session_template =
                    include_str!("../../../launchd/com.hippo.codex-session.plist");
```

Replace the `install::install_plist("com.hippo.xcode-codex-ingest", ...)` call with:

```rust
                if config.codex.enabled {
                    install::install_plist(
                        "com.hippo.codex-session",
                        codex_session_template,
                        &vars,
                        force,
                    )?;
                } else {
                    install::remove_plist("com.hippo.codex-session")?;
                }
```

Add a bootout for the old + new label near the other `service_bootout` calls so reinstalls cleanly replace the renamed job:

```rust
                install::service_bootout(
                    &domain,
                    &launch_agents.join("com.hippo.xcode-codex-ingest.plist"),
                );
                install::service_bootout(
                    &domain,
                    &launch_agents.join("com.hippo.codex-session.plist"),
                );
```

- [ ] **Step 5: Build**

Run: `cargo build -p hippo-daemon`
Expected: compiles clean (the `include_str!` will fail loudly if the plist path is wrong).

- [ ] **Step 6: Commit**

```bash
git add launchd/com.hippo.codex-session.plist crates/hippo-daemon/src/install.rs crates/hippo-daemon/src/main.rs
git rm launchd/com.hippo.xcode-codex-ingest.plist
git commit -m "feat(codex): replace xcode-codex-ingest launchd job with codex-session poller"
```

---

## Task 10: Doctor + watchdog coverage

**Files:**
- Modify: `crates/hippo-daemon/src/commands.rs` (`check_source_staleness` ~line 1647)
- Modify: `crates/hippo-daemon/src/watchdog.rs` (after `check_i11_opencode_coverage_proxy` ~line 646)
- Test: inline `#[cfg(test)]` in `watchdog.rs`

- [ ] **Step 1: Update the doctor staleness check**

Two edits in `commands.rs`. First, change the `check_source_staleness` `WHERE source IN (...)` list (~line 1647) to include codex:

```rust
         WHERE source IN ('shell', 'browser', 'claude-session', 'claude-tool', 'agentic-session-opencode', 'agentic-session-codex') \
```

Second, extend the `agentic-session-opencode` arm of `source_staleness_thresholds_for` (~line 1925) to an or-pattern covering Codex. The Codex poller is interval-driven like opencode and needs the same lenient `fail_secs` (not the 1800 s `_` default); an or-pattern (rather than a second identical arm) avoids a `clippy::match_same_arms` warning that would fail the `-D warnings` build:

```rust
        "agentic-session-opencode" | "agentic-session-codex" => SourceStalenessThresholds {
            warn_secs: 300,
            fail_secs: 3600,
        },
```

Scope note: opencode additionally *suppresses* idle-source warnings inside `check_source_staleness` (see the "idle DBs are suppressed below" comment at `commands.rs:1924`). A Codex equivalent — suppressing the warning when no `session_roots` file changed recently — is **out of scope** for this feature; without it a Codex-idle day yields a `[WW]` warning, never a `[!!]` failure. State this in issue #154 rather than silently absorbing it.

- [ ] **Step 2: Write the failing watchdog test**

In `watchdog.rs` test module:

```rust
#[test]
fn i13_codex_alarms_on_repeated_failures() {
    let now = 1_000_000_000_000;
    let row = SourceHealthRow {
        source: "agentic-session-codex".to_string(),
        last_event_ts: Some(now - 10_000),
        consecutive_failures: 4,
        ..SourceHealthRow::default()
    };
    let mut map = std::collections::HashMap::new();
    map.insert("agentic-session-codex", &row);
    let v = check_i13_codex_coverage_proxy(&map, now);
    assert!(v.is_some());
    assert_eq!(v.unwrap().invariant_id, "I-13");
}
```

(Adjust `SourceHealthRow` construction to match its actual fields/`Default` — copy the shape used by the existing I-11 opencode test in this file.)

- [ ] **Step 3: Run test to verify it fails**

Run: `cargo test -p hippo-daemon i13_codex`
Expected: FAIL — `check_i13_codex_coverage_proxy` not defined.

- [ ] **Step 4: Implement the invariant**

In `watchdog.rs`, after `check_i11_opencode_coverage_proxy`:

```rust
/// I-13: Codex-session coverage proxy. Mirrors I-11: alarm when the Codex
/// poller has failed repeatedly. Full freshness coverage is the doctor's job.
pub fn check_i13_codex_coverage_proxy(
    by_source: &std::collections::HashMap<&str, &SourceHealthRow>,
    now_ms: i64,
) -> Option<InvariantViolation> {
    let row = by_source.get("agentic-session-codex")?;
    let last_event = row.last_event_ts?;
    if row.consecutive_failures > 3 {
        return Some(InvariantViolation {
            invariant_id: "I-13".to_string(),
            source: "agentic-session-codex".to_string(),
            since_ms: now_ms - last_event,
            details: json!({
                "consecutive_failures": row.consecutive_failures,
                "note": "proxy predicate; full freshness check lives in hippo doctor",
            }),
        });
    }
    None
}
```

Wire `check_i13_codex_coverage_proxy` into the watchdog's invariant-evaluation loop wherever `check_i11_opencode_coverage_proxy` is called (search the file for `i11`). The highest existing invariant is `I-12`, so `I-13` is the correct next ID.

- [ ] **Step 5: Run tests + clippy**

Run: `cargo test -p hippo-daemon watchdog && cargo clippy -p hippo-daemon --all-targets -- -D warnings`
Expected: PASS, clean.

- [ ] **Step 6: Commit**

```bash
git add crates/hippo-daemon/src/commands.rs crates/hippo-daemon/src/watchdog.rs
git commit -m "feat(codex): doctor staleness + watchdog coverage for agentic-session-codex"
```

---

## Task 11: Retire the legacy Python path + mise.toml

**Files:**
- Delete: `scripts/hippo-ingest-codex.py`, `brain/src/hippo_brain/codex_sessions.py`, `brain/tests/test_codex_sessions.py` (if present)
- Modify: `brain/src/hippo_brain/claude_sessions.py` (`source == "codex"` branch ~line 457; the `source` field ~line 89)
- Modify: `mise.toml` (lines ~826, ~854)

- [ ] **Step 1: Delete the legacy ingester files**

```bash
git rm scripts/hippo-ingest-codex.py brain/src/hippo_brain/codex_sessions.py
git rm brain/tests/test_codex_sessions.py   # only if it exists
```

- [ ] **Step 2: Remove dead Codex branches from `claude_sessions.py`**

Grep first: `grep -rn "codex\|build_codex" brain/src/hippo_brain/`. Remove the `if segment.source == "codex":` branch in `insert_segment` and the now-unused `source` dataclass field / `build_codex_enrichment_summary` import. If `source` has no remaining readers, delete the field; if other code reads it, leave the field but drop the codex-specific branch. Verify nothing else imports `codex_sessions`:

Run: `grep -rn "codex_sessions\|hippo-ingest-codex" brain/ scripts/ launchd/`
Expected: no results after edits.

- [ ] **Step 3: Update mise.toml**

In `mise.toml`, replace `com.hippo.xcode-codex-ingest` with `com.hippo.codex-session` in both the `start` and `stop` task launchd label lists (~lines 826, 854).

- [ ] **Step 4: Run the brain test suite**

Run: `uv run --project brain pytest brain/tests -q`
Expected: PASS. If a test referenced `codex_sessions`, it was deleted in Step 1; if a test referenced the removed `insert_segment` branch, update it to reflect Codex no longer flowing through Python.

- [ ] **Step 5: Lint brain**

Run: `uv run --project brain ruff check brain/ && uv run --project brain ruff format --check brain/`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add -A scripts/ brain/ mise.toml
git commit -m "chore(codex): retire legacy Python codex ingestion script"
```

---

## Task 12: config.toml template + full verification

**Files:**
- Modify: `config/config.toml` (or the default config template — locate via `grep -rn "\[opencode\]" config/`)
- Modify: `CLAUDE.md` (Claude Session Ingestion / sources section — add a Codex paragraph)

- [ ] **Step 1: Add the `[codex]` section to the default config template**

After the `[opencode]` section in the default `config.toml`:

```toml
[codex]
# Ingest OpenAI Codex CLI rollout sessions into claude_sessions.
enabled = true
# Directories scanned recursively for rollout-*.jsonl files.
session_roots = [
    "~/.codex/sessions",
    "~/.codex/archived_sessions",
    "~/Library/Developer/Xcode/CodingAssistant/codex/sessions",
]
# Skip files modified within this many seconds (avoid partial reads).
min_idle_secs = 60
# launchd StartInterval for the codex-poll job.
poll_interval_secs = 60
```

> If the template does not expand `~`, write absolute paths or omit `session_roots` entirely so the Rust `default_codex_session_roots()` supplies them. Confirm how `[opencode] db_path` handles `~` in the same file and match it.

- [ ] **Step 2: Document the source in CLAUDE.md**

Add a short "Codex Session Ingestion" subsection near "Claude Session Ingestion" in `CLAUDE.md`, describing: `hippo codex-poll`, the `com.hippo.codex-session` launchd job, that Codex writes segmented rows to `claude_sessions`, and a pointer to the spec.

- [ ] **Step 3: Full workspace verification**

Run each, expect clean:

```bash
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
uv run --project brain pytest brain/tests -q
uv run --project brain ruff check brain/
```

- [ ] **Step 4: Live smoke test**

```bash
cargo build --release
./target/release/hippo codex-poll
./target/release/hippo doctor
```

Expected: `codex-poll` ingests real sessions (check `sqlite3 ~/.local/share/hippo/hippo.db "SELECT COUNT(*) FROM claude_sessions WHERE source_file LIKE '%/.codex/%'"` returns > 0); `doctor` shows an `agentic-session-codex` line.

- [ ] **Step 5: Commit**

```bash
git add config/ CLAUDE.md
git commit -m "docs(codex): add [codex] config section and CLAUDE.md notes"
```

---

## Self-Review notes

- **Spec coverage:** §3 data model → Task 6; §4.2 segmentation → Task 4; §4.3 poller → Task 7; §4.4 parser (both user-message shapes) → Task 4; §4.5 cursor → Task 7; §5 config → Tasks 1, 12; §5 CLI → Task 8; §5 launchd → Task 9; §5 schema v15 → Task 2; §5 doctor/watchdog → Task 10; §6 retire legacy → Task 11; §7 harness derivation → no code (migration concern, documented in spec). §8 tests → Tasks 3–7, 10.
- **Open verification carried into tasks:** spec's "does brain enrichment need Codex-specific framing" — Task 11 Step 4 surfaces it via the brain test run; framing is baked into `summary_text` by the Rust poller (Task 5), so no brain change is expected.
- **Type consistency:** `CodexSegment`, `ToolCall`, `extract_segments`, `build_summary_text`, `compute_content_hash`, `upsert_segment`/`upsert_segment_tx`, `poll_tick`, `test_config` are used consistently across Tasks 3–8.
