# Cursor Agent Session Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest Cursor Agent CLI transcripts (`~/.cursor/projects/**/agent-transcripts/**/*.jsonl`) into hippo via a new self-contained Rust poller, landing rows in the shared `claude_sessions` table.

**Architecture:** A launchd-scheduled `hippo cursor-poll` runs `cursor_session::poll_tick`, which walks the configured Cursor project roots, parses each agent-transcript JSONL (Anthropic-style `{role, message:{content:[…]}}`) into char-bounded segments, and upserts them into `claude_sessions` (+ `claude_enrichment_queue`). The module is self-contained, modeled on `codex_session.rs`. Two Cursor-specific adaptations: transcripts have **no per-line timestamps** (segment on char-cap only; stamp time from file mtime) and **no in-file identity** (derive `session_id`/`is_subagent`/`parent_session_id`/`cwd` from the path). A per-file inode cursor in `agentic_cursor` skips unchanged files.

**Tech Stack:** Rust (edition 2024), rusqlite, serde_json, sha2, chrono, anyhow, tracing, walkdir; Python 3.14 (brain). Spec: `docs/superpowers/specs/2026-05-25-cursor-ingestion-design.md`.

---

## File Structure

- **Create** `crates/hippo-daemon/src/cursor_session.rs` — the entire poller: `CursorSegment` + `ToolCall` structs, path-identity helpers, the Anthropic-block parser + char-cap segmentation, `claude_sessions` upsert, `agentic_cursor` cursor, `source_health` bumps, `poll_tick` + `ingest_file` entry points.
- **Create** `launchd/com.hippo.cursor-session.plist` — LaunchAgent running `hippo cursor-poll`.
- **Create** `crates/hippo-daemon/tests/cursor_session.rs` — integration tests.
- **Create** `crates/hippo-daemon/tests/fixtures/cursor/transcript-main.jsonl` and `transcript-subagent.jsonl` — hand-authored synthetic fixtures.
- **Create** `crates/hippo-daemon/tests/source_audit/cursor_agent.rs` — source-audit test; declared from `tests/source_audit.rs`.
- **Modify** `crates/hippo-core/src/config.rs` — add `CursorConfig`, add `cursor` field to `HippoConfig`.
- **Modify** `crates/hippo-core/src/storage.rs` — `EXPECTED_VERSION` → 16; v15→v16 migration seeding `agentic-session-cursor`.
- **Modify** `crates/hippo-core/src/schema.sql` — add the `agentic-session-cursor` seed row + `PRAGMA user_version = 16`.
- **Modify** `brain/src/hippo_brain/schema_version.py` — `EXPECTED_SCHEMA_VERSION` → 16.
- **Modify** `brain/src/hippo_brain/server.py` — `/.cursor/` source label + queue-depth entry.
- **Modify** `crates/hippo-daemon/src/lib.rs` — `pub mod cursor_session;`.
- **Modify** `crates/hippo-daemon/src/cli.rs` — `CursorPoll` command + `IngestSource::CursorSession`.
- **Modify** `crates/hippo-daemon/src/main.rs` — module import, dispatch arms, plist install wiring.
- **Modify** `crates/hippo-daemon/src/install.rs` — `cursor_poll_interval_secs` in `PlistVars`/`detect_vars`/`render_plist`.
- **Modify** `crates/hippo-daemon/src/probe.rs` — assertion-only `probe_cursor_session` + dispatch arm.
- **Modify** `crates/hippo-daemon/src/watchdog.rs` — I-15 Cursor coverage invariant.
- **Modify** `crates/hippo-daemon/src/commands.rs` — Cursor in doctor staleness check.
- **Modify** `crates/hippo-daemon/tests/source_audit.rs` — declare the `cursor_agent` sub-module.
- **Modify** `mise.toml` — add `com.hippo.cursor-session` to start/stop loops.
- **Modify** `config/config.default.toml` — `[cursor]` section.
- **Modify** `CLAUDE.md`, `docs/capture/sources.md`, `docs/capture/test-matrix.md`, `docs/capture/architecture.md`, `docs/capture/adding-a-source.md`, `docs/schema.md`, `docs/lifecycle.md`, `README.md` — docs.

**Deliberately NOT mirrored from Codex:** the legacy-Python retirement (Cursor has none) and the `check_codex_coverage` / `check_codex_state_coverage` oracle (it cross-checks Codex's `state_5.sqlite`; Cursor exposes no equivalent state DB we parse).

---

## Task 1: Add `CursorConfig` to hippo-core

**Files:**
- Modify: `crates/hippo-core/src/config.rs`

- [ ] **Step 1: Write the failing tests**

Add to the `#[cfg(test)] mod tests` block in `crates/hippo-core/src/config.rs`:

```rust
    #[test]
    fn cursor_config_defaults_are_sane() {
        let c = CursorConfig::default();
        assert!(c.enabled);
        assert_eq!(c.poll_interval_secs, 60);
        assert_eq!(c.min_idle_secs, 60);
        assert!(
            c.session_roots
                .iter()
                .any(|p| p.ends_with(".cursor/projects"))
        );
    }

    #[test]
    fn hippo_config_has_cursor_with_default() {
        let toml = "";
        let cfg: HippoConfig = toml::from_str(toml).unwrap();
        assert!(cfg.cursor.enabled);
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cargo test -p hippo-core cursor_config_defaults_are_sane hippo_config_has_cursor_with_default`
Expected: FAIL — `cannot find type CursorConfig` / `no field cursor`.

- [ ] **Step 3: Add the `cursor` field to `HippoConfig`**

In `crates/hippo-core/src/config.rs`, in the `HippoConfig` struct, immediately after the `codex` field (currently lines 27-28):

```rust
    #[serde(default)]
    pub cursor: CursorConfig,
```

- [ ] **Step 4: Add `CursorConfig` + defaults**

In `crates/hippo-core/src/config.rs`, immediately after the `CodexConfig` `Default` impl (after line 624), add:

```rust
fn default_cursor_enabled() -> bool {
    true
}

fn default_cursor_poll_interval_secs() -> u64 {
    60
}

fn default_cursor_min_idle_secs() -> u64 {
    60
}

fn default_cursor_session_roots() -> Vec<PathBuf> {
    let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("."));
    vec![home.join(".cursor/projects")]
}

/// Cursor Agent CLI transcript ingestion. The poller walks `session_roots`
/// for `agent-transcripts/**/*.jsonl` files (main + subagents) and writes
/// segmented rows into `claude_sessions`, distinguished by the `.cursor/`
/// path stored in the `source_file` column.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CursorConfig {
    /// Enable Cursor session ingestion. When false, `poll_tick` is a no-op.
    #[serde(default = "default_cursor_enabled")]
    pub enabled: bool,
    /// Directories scanned recursively for `agent-transcripts/**/*.jsonl`.
    #[serde(default = "default_cursor_session_roots")]
    pub session_roots: Vec<PathBuf>,
    /// Skip files modified within this many seconds — they may be in-flight
    /// and a partial read would freeze the segment at an early state.
    #[serde(default = "default_cursor_min_idle_secs")]
    pub min_idle_secs: u64,
    /// launchd StartInterval for the cursor-poll job, in seconds.
    #[serde(default = "default_cursor_poll_interval_secs")]
    pub poll_interval_secs: u64,
}

impl Default for CursorConfig {
    fn default() -> Self {
        Self {
            enabled: default_cursor_enabled(),
            session_roots: default_cursor_session_roots(),
            min_idle_secs: default_cursor_min_idle_secs(),
            poll_interval_secs: default_cursor_poll_interval_secs(),
        }
    }
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cargo test -p hippo-core cursor_config_defaults_are_sane hippo_config_has_cursor_with_default`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add crates/hippo-core/src/config.rs
git commit -m "feat(cursor): add CursorConfig to hippo-core"
```

---

## Task 2: Schema v16 — seed `agentic-session-cursor`

**Files:**
- Modify: `crates/hippo-core/src/storage.rs`
- Modify: `crates/hippo-core/src/schema.sql`
- Modify: `brain/src/hippo_brain/schema_version.py`

- [ ] **Step 1: Write the failing test**

Add to the `#[cfg(test)] mod tests` block in `crates/hippo-core/src/storage.rs` (mirror `test_migrate_v14_to_v15_seeds_codex_source_health` at lines 3520-3560 — note the full 14-column `source_health` DDL):

```rust
    #[test]
    fn test_migrate_v15_to_v16_seeds_cursor_source_health() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("test.db");
        {
            let conn = rusqlite::Connection::open(&db_path).unwrap();
            conn.execute_batch(
                "CREATE TABLE source_health (
                    source                 TEXT PRIMARY KEY,
                    last_event_ts          INTEGER,
                    last_success_ts        INTEGER,
                    last_error_ts          INTEGER,
                    last_error_msg         TEXT,
                    consecutive_failures   INTEGER NOT NULL DEFAULT 0,
                    events_last_1h         INTEGER NOT NULL DEFAULT 0,
                    events_last_24h        INTEGER NOT NULL DEFAULT 0,
                    expected_min_per_hour  INTEGER,
                    probe_ok               INTEGER,
                    probe_lag_ms           INTEGER,
                    probe_last_run_ts      INTEGER,
                    last_heartbeat_ts      INTEGER,
                    updated_at             INTEGER NOT NULL DEFAULT 0
                );
                PRAGMA user_version = 15;",
            )
            .unwrap();
        }
        let conn = open_db(&db_path).unwrap();
        let exists: bool = conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM source_health WHERE source = 'agentic-session-cursor')",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(exists, "v16 migration must seed agentic-session-cursor");
        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, EXPECTED_VERSION);
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cargo test -p hippo-core test_migrate_v15_to_v16_seeds_cursor_source_health`
Expected: FAIL — the row is absent and `EXPECTED_VERSION` is still 15, so the migration bails with a version-mismatch error or the assert fails.

- [ ] **Step 3: Bump `EXPECTED_VERSION`**

In `crates/hippo-core/src/storage.rs` line 16, change:

```rust
pub const EXPECTED_VERSION: i64 = 15;
```

to:

```rust
pub const EXPECTED_VERSION: i64 = 16;
```

- [ ] **Step 4: Add the v15→v16 migration block**

In `crates/hippo-core/src/storage.rs`, immediately after the v14→v15 block (which ends with `conn.execute_batch("PRAGMA user_version = 15;")?;` around line 986, just before the `} else if version != 0 && version != EXPECTED_VERSION {` arm), insert a new block. The guard widens to `(1..16)` to match the existing `(1..15)` style:

```rust
    // v15→v16: seed the source_health row for the Cursor poller. The poller's
    // source_health UPDATE is a silent no-op without this row. Cursor writes
    // the claude_sessions table, but its capture-path health key is
    // `agentic-session-cursor`: source_health keys identify the capture path,
    // not the destination table — like agentic-session-codex.
    if (1..16).contains(&version) {
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
                    ('agentic-session-cursor', NULL, unixepoch('now') * 1000);",
            )?;
        }
        conn.execute_batch("PRAGMA user_version = 16;")?;
    } else if version != 0 && version != EXPECTED_VERSION {
```

Note: you are REPLACING the existing `} else if version != 0 …` line with the block above (which ends in that same `} else if …` opener). Keep the body of that else-if arm unchanged.

- [ ] **Step 5: Update `schema.sql`**

In `crates/hippo-core/src/schema.sql`, in the agentic-source seed block (lines 622-631), add the cursor row after the codex row, and update the trailing pragma (line 638):

```sql
INSERT OR IGNORE INTO source_health (source, last_event_ts, updated_at) VALUES
    ('agentic-session-claude',  (SELECT MAX(start_time) FROM agentic_sessions WHERE harness = 'claude-code'), unixepoch('now') * 1000),
    ('agentic-session-opencode', NULL, unixepoch('now') * 1000),
    ('agentic-session-codex',   NULL, unixepoch('now') * 1000),
    ('agentic-session-cursor',  NULL, unixepoch('now') * 1000),
    -- brain-preflight: the brain's enrichment loop writes here every cycle
    -- with the inference-backend reachability result. Watchdog I-12 reads
    -- consecutive_failures to alarm when preflight has been stuck failing.
    ('brain-preflight',          NULL, unixepoch('now') * 1000);
```

And change the final line from `PRAGMA user_version = 15;` to:

```sql
PRAGMA user_version = 16;
```

- [ ] **Step 6: Bump the brain schema version in lockstep**

In `brain/src/hippo_brain/schema_version.py`, change `EXPECTED_SCHEMA_VERSION: int = 15` to `16`, and append a docstring line after the `v14→v15` note:

```python
v15→v16 seeds the `agentic-session-cursor` row in `source_health`
so the Cursor poller's health UPDATE is not a silent no-op.
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cargo test -p hippo-core test_migrate_v15_to_v16_seeds_cursor_source_health && cargo test -p hippo-core schema`
Expected: PASS, including the existing schema/migration tests that assert `PRAGMA user_version` equals `EXPECTED_VERSION` (now 16).

- [ ] **Step 8: Verify brain/daemon agreement**

Run: `cargo test -p hippo-core && uv run --project brain pytest brain/tests -k schema -q`
Expected: PASS — daemon `EXPECTED_VERSION` and brain `EXPECTED_SCHEMA_VERSION` both 16.

- [ ] **Step 9: Commit**

```bash
git add crates/hippo-core/src/storage.rs crates/hippo-core/src/schema.sql brain/src/hippo_brain/schema_version.py
git commit -m "feat(cursor): schema v16 seeds agentic-session-cursor source_health"
```

---

## Task 3: Cursor module scaffolding — `CursorSegment`, `ToolCall`, `tool_summary`

**Files:**
- Modify: `crates/hippo-daemon/src/lib.rs`
- Create: `crates/hippo-daemon/src/cursor_session.rs`

- [ ] **Step 1: Declare the module**

In `crates/hippo-daemon/src/lib.rs`, add `cursor_session` to the module list (alphabetical, next to `codex_session`):

```rust
pub mod cursor_session;
```

- [ ] **Step 2: Write the failing test**

Create `crates/hippo-daemon/src/cursor_session.rs` with the structs, the `tool_summary` helper, and a test:

```rust
//! Cursor Agent CLI transcript poller — see
//! docs/superpowers/specs/2026-05-25-cursor-ingestion-design.md.
//!
//! Cursor transcripts are Anthropic-style JSONL (`{role, message:{content}}`)
//! with NO per-line timestamps and NO in-file session metadata: identity is
//! derived from the path, time from the file mtime, and segments split on
//! accumulated character count only.

use anyhow::{Context, Result};
use hippo_core::config::HippoConfig;
use hippo_core::redaction::RedactionEngine;
use rusqlite::{OptionalExtension, params};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use tracing::{debug, error, info, warn};
use walkdir::WalkDir;

/// Accumulated character cap before forcing a new segment. There is no
/// time-gap rule for Cursor — transcripts carry no per-line timestamps — so
/// this char cap is the ONLY segment boundary.
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
```

- [ ] **Step 3: Run test to verify it passes (compiles + green)**

Run: `cargo test -p hippo-daemon cursor_session::tests::tool_summary_prefers_command_then_path`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add crates/hippo-daemon/src/lib.rs crates/hippo-daemon/src/cursor_session.rs
git commit -m "feat(cursor): scaffold cursor_session module with CursorSegment + tool_summary"
```

---

## Task 4: Path-identity helpers

**Files:**
- Modify: `crates/hippo-daemon/src/cursor_session.rs`

- [ ] **Step 1: Write the failing tests**

Add to the `tests` module in `crates/hippo-daemon/src/cursor_session.rs`:

```rust
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
        let p = Path::new(
            "/Users/me/.cursor/projects/empty-window/agent-transcripts/x/x.jsonl",
        );
        let id = PathIdentity::from_path(p);
        assert_eq!(id.cwd, "");
        let p2 = Path::new(
            "/Users/me/.cursor/projects/1779680566655/agent-transcripts/y/y.jsonl",
        );
        assert_eq!(PathIdentity::from_path(p2).cwd, "");
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cargo test -p hippo-daemon cursor_session::tests::identity_`
Expected: FAIL — `cannot find type PathIdentity`.

- [ ] **Step 3: Implement `PathIdentity`**

Add to `crates/hippo-daemon/src/cursor_session.rs` (above the `tests` module):

```rust
/// Identity derived entirely from a transcript's path. Cursor transcripts
/// carry no session id, cwd, or subagent marker inside the file.
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
fn decode_slug_to_cwd(slug: &str) -> String {
    if slug == "empty-window"
        || slug.starts_with("var-folders")
        || slug.chars().all(|c| c.is_ascii_digit())
    {
        return String::new();
    }
    format!("/{}", slug.replace('-', "/"))
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

        // Subagent iff the immediate parent directory is `subagents`.
        let is_subagent = path
            .parent()
            .and_then(|p| p.file_name())
            .map(|n| n == "subagents")
            .unwrap_or(false);

        // parent_session_id = the <uuid> dir that encloses agent-transcripts/<uuid>/…
        // For a subagent: .../agent-transcripts/<uuid>/subagents/<sub>.jsonl → grandparent.
        let parent_session_id = if is_subagent {
            path.parent()
                .and_then(|p| p.parent())
                .and_then(|p| p.file_name())
                .map(|n| n.to_string_lossy().into_owned())
        } else {
            None
        };

        // slug = the component immediately before "agent-transcripts".
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cargo test -p hippo-daemon cursor_session::tests::identity_`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-daemon/src/cursor_session.rs
git commit -m "feat(cursor): derive session identity from transcript path"
```

---

## Task 5: Transcript parser — `extract_segments` (timestamp-free)

**Files:**
- Modify: `crates/hippo-daemon/src/cursor_session.rs`
- Create: `crates/hippo-daemon/tests/fixtures/cursor/transcript-main.jsonl`
- Create: `crates/hippo-daemon/tests/fixtures/cursor/transcript-subagent.jsonl`

- [ ] **Step 1: Create the synthetic fixtures**

Create `crates/hippo-daemon/tests/fixtures/cursor/transcript-main.jsonl` (hand-authored; never a real transcript):

```jsonl
{"role":"user","message":{"content":[{"type":"text","text":"<user_query>\nfix the failing build\n</user_query>"}]}}
{"role":"assistant","message":{"content":[{"type":"text","text":"Checking the build now."},{"type":"tool_use","name":"Shell","input":{"command":"cargo build","description":"build"}}]}}
{"role":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":"error[E0382]"}]}}
{"role":"assistant","message":{"content":[{"type":"text","text":"Found the borrow error; fixing it.\n\n[REDACTED]"},{"type":"tool_use","name":"Read","input":{"path":"/proj/src/main.rs"}}]}}
```

Create `crates/hippo-daemon/tests/fixtures/cursor/transcript-subagent.jsonl`:

```jsonl
{"role":"user","message":{"content":[{"type":"text","text":"<user_query>\nYou are PR agent 1. Merge PR 42.\n</user_query>"}]}}
{"role":"assistant","message":{"content":[{"type":"text","text":"Checking out PR 42."},{"type":"tool_use","name":"Shell","input":{"command":"gh pr checkout 42"}}]}}
```

- [ ] **Step 2: Write the failing tests**

Add to the `tests` module in `crates/hippo-daemon/src/cursor_session.rs`:

```rust
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
        // <user_query> wrapper stripped.
        assert_eq!(s.user_prompts, vec!["fix the failing build".to_string()]);
        // tool_result block is NOT a user prompt.
        assert_eq!(s.user_prompts.len(), 1);
        // assistant text + tool calls captured.
        assert!(s.assistant_texts.iter().any(|t| t.contains("build")));
        assert_eq!(s.tool_calls.len(), 2);
        assert_eq!(s.tool_calls[0].name, "Shell");
        assert_eq!(s.tool_calls[0].summary, "cargo build");
        // time stamped from the passed-in mtime.
        assert_eq!(s.start_time, 1_775_000_000_000);
        assert_eq!(s.end_time, 1_775_000_000_000);
    }

    #[test]
    fn extract_segments_subagent_identity() {
        // Place the subagent fixture under a synthetic subagents/ path.
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join(
            "Users-me-projects-foo/agent-transcripts/parent-1/subagents",
        );
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
        // Many user turns, no timestamps anywhere -> must still split on the
        // char cap alone. NOTE: extract_user_text caps each prompt at 500
        // chars, so a segment holds ~24 prompts (24×500 ≈ 12000) before the
        // next user turn forces a split. 40 turns therefore yields >1 segment.
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cargo test -p hippo-daemon cursor_session::tests::extract_segments_`
Expected: FAIL — `cannot find function extract_segments`.

- [ ] **Step 4: Implement `extract_user_text`, `content_text`, and `extract_segments`**

Add to `crates/hippo-daemon/src/cursor_session.rs` (above the `tests` module):

```rust
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
                .filter_map(|b| b.get("text").and_then(|t| t.as_str()).map(|s| s.to_string()))
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
            // Real user prompts are `text` blocks; `tool_result` blocks are
            // tool output returned to the model, not user intent — skip them.
            let prompts: Vec<String> = text_blocks(&content, "text")
                .iter()
                .map(|t| extract_user_text(t))
                .filter(|t| !t.is_empty())
                .collect();
            if prompts.is_empty() {
                continue;
            }

            // Segment boundary: char cap only (no timestamps exist).
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

        // Assistant turns only matter inside an open segment.
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cargo test -p hippo-daemon cursor_session::tests::extract_segments_`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add crates/hippo-daemon/src/cursor_session.rs crates/hippo-daemon/tests/fixtures/cursor/
git commit -m "feat(cursor): char-cap segmentation parser for Anthropic-style transcripts"
```

---

## Task 6: `build_summary_text` + `compute_content_hash`

**Files:**
- Modify: `crates/hippo-daemon/src/cursor_session.rs`

- [ ] **Step 1: Write the failing tests**

Add to the `tests` module:

```rust
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cargo test -p hippo-daemon cursor_session::tests::summary_text_includes cursor_session::tests::content_hash_is_stable`
Expected: FAIL — functions not found.

- [ ] **Step 3: Implement both functions**

Add to `crates/hippo-daemon/src/cursor_session.rs`:

```rust
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
    hasher.finalize().iter().map(|b| format!("{b:02x}")).collect()
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cargo test -p hippo-daemon cursor_session::tests::summary_text_includes cursor_session::tests::content_hash_is_stable`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-daemon/src/cursor_session.rs
git commit -m "feat(cursor): summary_text digest + content_hash"
```

---

## Task 7: Upsert into `claude_sessions` + enqueue

**Files:**
- Modify: `crates/hippo-daemon/src/cursor_session.rs`
- Create: `crates/hippo-daemon/tests/cursor_session.rs`

- [ ] **Step 1: Make the segment types public**

In `crates/hippo-daemon/src/cursor_session.rs`, the `CursorSegment` and `ToolCall` structs are already `pub`. Confirm they are reachable from integration tests (`pub struct`). No change if already `pub`.

- [ ] **Step 2: Write the failing integration test**

Create `crates/hippo-daemon/tests/cursor_session.rs`:

```rust
use hippo_core::storage::open_db;
use tempfile::TempDir;

fn seg(session_id: &str, is_subagent: bool, parent: Option<&str>) -> hippo_daemon::cursor_session::CursorSegment {
    hippo_daemon::cursor_session::CursorSegment {
        session_id: session_id.into(),
        project_dir: "foo".into(),
        cwd: "/work/foo".into(),
        segment_index: 0,
        start_time: 1_775_634_000_000,
        end_time: 1_775_634_000_000,
        user_prompts: vec!["do a thing".into()],
        assistant_texts: vec![],
        tool_calls: vec![],
        message_count: 1,
        source_file: format!(
            "/Users/x/.cursor/projects/Users-x-projects-foo/agent-transcripts/{session_id}/{session_id}.jsonl"
        ),
        is_subagent,
        parent_session_id: parent.map(|s| s.to_string()),
    }
}

#[test]
fn upsert_writes_claude_session_and_enqueues() {
    let tmp = TempDir::new().unwrap();
    let conn = open_db(&tmp.path().join("hippo.db")).unwrap();
    let s = seg("cur-1", false, None);
    hippo_daemon::cursor_session::upsert_segment(&conn, &s).unwrap();

    let (cnt, src): (i64, String) = conn
        .query_row(
            "SELECT COUNT(*), MAX(source_file) FROM claude_sessions WHERE session_id = 'cur-1'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(cnt, 1);
    assert!(src.contains("/.cursor/"));

    let queued: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM claude_enrichment_queue q
             JOIN claude_sessions s ON s.id = q.claude_session_id
             WHERE s.session_id = 'cur-1' AND q.status = 'pending'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(queued, 1);

    // Idempotent re-upsert.
    hippo_daemon::cursor_session::upsert_segment(&conn, &s).unwrap();
    let cnt2: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM claude_sessions WHERE session_id = 'cur-1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(cnt2, 1, "re-upsert must not duplicate");
}

#[test]
fn upsert_subagent_records_parent_link() {
    let tmp = TempDir::new().unwrap();
    let conn = open_db(&tmp.path().join("hippo.db")).unwrap();
    let s = seg("sub-1", true, Some("parent-1"));
    hippo_daemon::cursor_session::upsert_segment(&conn, &s).unwrap();

    let (is_sub, parent): (i64, Option<String>) = conn
        .query_row(
            "SELECT is_subagent, parent_session_id FROM claude_sessions WHERE session_id = 'sub-1'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(is_sub, 1);
    assert_eq!(parent.as_deref(), Some("parent-1"));
}
```

Add an inline unit test for the gate to the `tests` module in `cursor_session.rs`:

```rust
    #[test]
    fn decide_enqueue_gates_on_content_change() {
        // new insert -> enqueue
        assert!(decide_enqueue(true, "h1", None, None, None, 1_000));
        // unchanged content -> skip
        assert!(!decide_enqueue(false, "h1", Some("h1"), None, None, 1_000));
        // processing -> skip
        assert!(!decide_enqueue(false, "h2", Some("h1"), Some("processing"), None, 1_000));
        // changed content past debounce -> enqueue
        assert!(decide_enqueue(false, "h2", Some("h1"), Some("failed"), Some(0), 400_000));
    }
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cargo test -p hippo-daemon --test cursor_session && cargo test -p hippo-daemon cursor_session::tests::decide_enqueue_gates`
Expected: FAIL — `upsert_segment` / `decide_enqueue` not found.

- [ ] **Step 4: Implement `decide_enqueue`, `upsert_segment_tx`, `upsert_segment`**

Add to `crates/hippo-daemon/src/cursor_session.rs`:

```rust
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cargo test -p hippo-daemon --test cursor_session && cargo test -p hippo-daemon cursor_session::tests::decide_enqueue_gates`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add crates/hippo-daemon/src/cursor_session.rs crates/hippo-daemon/tests/cursor_session.rs
git commit -m "feat(cursor): upsert segments into claude_sessions with subagent links"
```

---

## Task 8: `poll_tick` — file walk, cursor, source_health

**Files:**
- Modify: `crates/hippo-daemon/src/cursor_session.rs`
- Modify: `crates/hippo-daemon/tests/cursor_session.rs`
- Modify: `crates/hippo-daemon/Cargo.toml` (dev-dependency `filetime`, if not already present)

- [ ] **Step 1: Ensure `filetime` dev-dependency**

In `crates/hippo-daemon/Cargo.toml`, under `[dev-dependencies]`, confirm `filetime` is listed (Codex's tests already use it). If missing, add:

```toml
filetime = "0.2"
```

- [ ] **Step 2: Write the failing integration tests**

Add to `crates/hippo-daemon/tests/cursor_session.rs`:

```rust
fn write_transcript(root: &std::path::Path, slug: &str, uuid: &str, prompt: &str) -> std::path::PathBuf {
    let dir = root.join(slug).join("agent-transcripts").join(uuid);
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join(format!("{uuid}.jsonl"));
    let line = format!(
        r#"{{"role":"user","message":{{"content":[{{"type":"text","text":"<user_query>\n{prompt}\n</user_query>"}}]}}}}"#
    );
    std::fs::write(&p, line).unwrap();
    p
}

#[test]
fn poll_tick_ingests_idle_files_and_advances_cursor() {
    let tmp = TempDir::new().unwrap();
    let roots = tmp.path().join("projects");
    let f = write_transcript(&roots, "Users-x-projects-foo", "sess-1", "hello cursor");
    let old = std::time::SystemTime::now() - std::time::Duration::from_secs(3600);
    filetime::set_file_mtime(&f, filetime::FileTime::from_system_time(old)).unwrap();

    let data_dir = tmp.path().join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    let config = hippo_daemon::cursor_session::test_config(&data_dir, std::slice::from_ref(&roots));
    let _ = open_db(&config.db_path()).unwrap();

    assert_eq!(hippo_daemon::cursor_session::poll_tick(&config).unwrap(), 1);
    assert_eq!(
        hippo_daemon::cursor_session::poll_tick(&config).unwrap(),
        0,
        "unchanged file must be skipped via cursor"
    );

    let conn = open_db(&config.db_path()).unwrap();
    let health: i64 = conn
        .query_row(
            "SELECT last_success_ts FROM source_health WHERE source = 'agentic-session-cursor'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(health > 0, "source_health must be bumped");
}

#[test]
fn poll_tick_skips_in_flight_files() {
    let tmp = TempDir::new().unwrap();
    let roots = tmp.path().join("projects");
    write_transcript(&roots, "Users-x-projects-foo", "fresh", "in flight"); // mtime = now
    let data_dir = tmp.path().join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    let config = hippo_daemon::cursor_session::test_config(&data_dir, &[roots]);
    let _ = open_db(&config.db_path()).unwrap();
    assert_eq!(hippo_daemon::cursor_session::poll_tick(&config).unwrap(), 0);
}

#[test]
fn poll_tick_returns_zero_when_disabled() {
    let tmp = TempDir::new().unwrap();
    let data_dir = tmp.path().join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    let mut config = hippo_daemon::cursor_session::test_config(&data_dir, &[]);
    config.cursor.enabled = false;
    assert_eq!(hippo_daemon::cursor_session::poll_tick(&config).unwrap(), 0);
}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cargo test -p hippo-daemon --test cursor_session poll_tick`
Expected: FAIL — `poll_tick` / `test_config` not found.

- [ ] **Step 4: Implement the cursor, health, and poll functions**

Add to `crates/hippo-daemon/src/cursor_session.rs`:

```rust
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
            let mtime_ms = meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_millis() as i64)
                .unwrap_or(0);
            if now_ms - mtime_ms < min_idle_ms {
                continue; // in-flight
            }
            let key = cursor_key(&meta);
            if mtime_ms <= read_cursor(&conn, &key) {
                continue; // unchanged
            }
            match ingest_file(&conn, path, mtime_ms) {
                Ok((count, session_id)) => {
                    ingested += count;
                    if count > 0 {
                        bump_health_ok(&conn, mtime_ms);
                    }
                    if let Err(e) = write_cursor(&conn, &key, mtime_ms, &session_id) {
                        warn!("cursor cursor write failed for {}: {e:#}", path.display());
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cargo test -p hippo-daemon --test cursor_session`
Expected: PASS (all cursor_session integration tests).

- [ ] **Step 6: Add the source-audit test**

Create `crates/hippo-daemon/tests/source_audit/cursor_agent.rs`:

```rust
//! Source #9 — Cursor Agent CLI transcripts.
//!
//! Drives a real transcript through the production `poll_tick` path and
//! asserts a `claude_sessions` row lands and `source_health` is updated.

use hippo_core::storage::open_db;
use tempfile::TempDir;

#[test]
fn cursor_agent_transcript_lands_row_and_bumps_health() {
    let tmp = TempDir::new().unwrap();
    let roots = tmp.path().join("projects");
    let dir = roots
        .join("Users-x-projects-foo")
        .join("agent-transcripts")
        .join("sess-audit");
    std::fs::create_dir_all(&dir).unwrap();
    let f = dir.join("sess-audit.jsonl");
    std::fs::write(
        &f,
        r#"{"role":"user","message":{"content":[{"type":"text","text":"<user_query>\naudit\n</user_query>"}]}}"#,
    )
    .unwrap();
    let old = std::time::SystemTime::now() - std::time::Duration::from_secs(3600);
    filetime::set_file_mtime(&f, filetime::FileTime::from_system_time(old)).unwrap();

    let data_dir = tmp.path().join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    let config = hippo_daemon::cursor_session::test_config(&data_dir, std::slice::from_ref(&roots));
    let _ = open_db(&config.db_path()).unwrap();

    let n = hippo_daemon::cursor_session::poll_tick(&config).unwrap();
    assert_eq!(n, 1, "Cursor transcript must produce one claude_sessions row");

    let conn = open_db(&config.db_path()).unwrap();
    let rows: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM claude_sessions WHERE session_id = 'sess-audit' AND source_file LIKE '%/.cursor/%'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(rows, 1);
}
```

Declare it in `crates/hippo-daemon/tests/source_audit.rs`, after the `xcode_codingassistant` declaration:

```rust
#[path = "source_audit/cursor_agent.rs"]
mod cursor_agent;
```

- [ ] **Step 7: Run the source-audit test**

Run: `cargo test -p hippo-daemon --test source_audit cursor_agent`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add crates/hippo-daemon/src/cursor_session.rs crates/hippo-daemon/tests/cursor_session.rs crates/hippo-daemon/tests/source_audit.rs crates/hippo-daemon/tests/source_audit/cursor_agent.rs crates/hippo-daemon/Cargo.toml
git commit -m "feat(cursor): poll_tick with inode cursor, source_health, source-audit test"
```

---

## Task 9: CLI — `hippo cursor-poll` + `hippo ingest cursor-session`

**Files:**
- Modify: `crates/hippo-daemon/src/cli.rs`
- Modify: `crates/hippo-daemon/src/main.rs`

- [ ] **Step 1: Add the `CursorPoll` command variant**

In `crates/hippo-daemon/src/cli.rs`, in the `Commands` enum, after the `CodexPoll` variant (around line 137):

```rust
    /// Poll Cursor Agent CLI transcript files and ingest new sessions.
    CursorPoll,
```

- [ ] **Step 2: Add the `CursorSession` ingest variant**

In `crates/hippo-daemon/src/cli.rs`, in the `IngestSource` enum (after `ClaudeSession`, around line 304):

```rust
    /// Import a Cursor Agent transcript JSONL file
    CursorSession {
        /// Path to the JSONL transcript file
        path: String,
        /// Wait up to N seconds for the file to appear before importing (default: 0 = no wait)
        #[arg(long, default_value_t = 0)]
        wait_for_file: u64,
    },
```

- [ ] **Step 3: Import the module in main.rs**

In `crates/hippo-daemon/src/main.rs` lines 5-6, add `cursor_session` to the `use` list (alphabetical):

```rust
    backfill, claude_session, codex_session, commands, cursor_session, daemon, gh_api, gh_poll,
    opencode_session, watch_claude_sessions,
```

- [ ] **Step 4: Add the `CursorPoll` dispatch arm**

In `crates/hippo-daemon/src/main.rs`, immediately after the `Commands::CodexPoll => …` arm (around line 1109):

```rust
        Commands::CursorPoll => match cursor_session::poll_tick(&config) {
            Ok(n) => tracing::info!(ingested = n, "cursor poll: completed"),
            Err(e) => {
                eprintln!("Error running cursor poll: {e:#}");
                std::process::exit(1);
            }
        },
```

- [ ] **Step 5: Add the `CursorSession` ingest dispatch arm**

In `crates/hippo-daemon/src/main.rs`, inside the `Commands::Ingest { source } => match source { … }` block, after the `IngestSource::ClaudeSession { … }` arm (around line 973), add an arm that mirrors it but routes to a one-shot cursor ingest. Since `cursor_session::ingest_file` is private and takes a connection, expose a small public one-shot wrapper. First add to `crates/hippo-daemon/src/cursor_session.rs`:

```rust
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
```

Then the dispatch arm in `main.rs`:

```rust
            IngestSource::CursorSession {
                path,
                wait_for_file,
            } => {
                let path = std::path::Path::new(&path);
                if !path.exists() {
                    if wait_for_file > 0 {
                        let deadline = std::time::Instant::now()
                            + std::time::Duration::from_secs(wait_for_file);
                        eprint!("Waiting for {}...", path.display());
                        while !path.exists() {
                            if std::time::Instant::now() >= deadline {
                                eprintln!("\nFile not found after {}s: {}", wait_for_file, path.display());
                                std::process::exit(1);
                            }
                            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                        }
                        eprintln!(" found.");
                    } else {
                        eprintln!("File not found: {}", path.display());
                        std::process::exit(1);
                    }
                }
                match cursor_session::ingest_one(&config, path) {
                    Ok(n) => println!("Cursor import complete: {n} segments ingested"),
                    Err(e) => {
                        eprintln!("Error importing cursor session: {e:#}");
                        std::process::exit(1);
                    }
                }
            }
```

- [ ] **Step 6: Build and smoke-test the CLI**

Run: `cargo build -p hippo-daemon`
Expected: compiles clean.

Run: `cargo run -p hippo-daemon -- cursor-poll`
Expected: exits 0; logs `cursor poll tick: completed`. (Ingests any real idle transcripts on this machine.)

- [ ] **Step 7: Commit**

```bash
git add crates/hippo-daemon/src/cli.rs crates/hippo-daemon/src/main.rs crates/hippo-daemon/src/cursor_session.rs
git commit -m "feat(cursor): hippo cursor-poll + hippo ingest cursor-session"
```

---

## Task 10: launchd job `com.hippo.cursor-session`

**Files:**
- Create: `launchd/com.hippo.cursor-session.plist`
- Modify: `crates/hippo-daemon/src/install.rs`
- Modify: `crates/hippo-daemon/src/main.rs`
- Modify: `mise.toml`

- [ ] **Step 1: Create the plist**

Create `launchd/com.hippo.cursor-session.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hippo.cursor-session</string>
    <key>ProgramArguments</key>
    <array>
        <string>__HIPPO_BIN__</string>
        <string>cursor-poll</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>__HOME__</string>
        <key>PATH</key>
        <string>__PATH__</string>
    </dict>
    <key>StartInterval</key><integer>__CURSOR_POLL_INTERVAL_SECS__</integer>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>RunAtLoad</key><false/>
    <key>WatchPaths</key>
    <array>
        <string>__HOME__/.cursor/projects</string>
    </array>
    <key>StandardOutPath</key>
    <string>__DATA_DIR__/cursor-session.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>__DATA_DIR__/cursor-session.stderr.log</string>
    <key>WorkingDirectory</key>
    <string>__HOME__</string>
</dict>
</plist>
```

- [ ] **Step 2: Plumb `cursor_poll_interval_secs` through `install.rs`**

In `crates/hippo-daemon/src/install.rs`, in `render_plist` (after the `__CODEX_POLL_INTERVAL_SECS__` replacement, line 25-28):

```rust
        .replace(
            "__CURSOR_POLL_INTERVAL_SECS__",
            &vars.cursor_poll_interval_secs.to_string(),
        )
```

In the `PlistVars` struct, after `codex_poll_interval_secs: u64,`:

```rust
    pub cursor_poll_interval_secs: u64,
```

In `detect_vars`, after the `codex_poll_interval_secs` binding:

```rust
    let cursor_poll_interval_secs = cfg
        .as_ref()
        .map(|c| c.cursor.poll_interval_secs)
        .unwrap_or(60);
```

And in the returned `PlistVars { … }` literal, after `codex_poll_interval_secs,`:

```rust
        cursor_poll_interval_secs,
```

- [ ] **Step 3: Wire the plist into the install/teardown flow in main.rs**

In `crates/hippo-daemon/src/main.rs`, in the `DaemonAction::Install` arm:

(a) After `let codex_session_was_loaded = install::service_is_loaded("com.hippo.codex-session");` (line ~250):

```rust
                let cursor_session_was_loaded =
                    install::service_is_loaded("com.hippo.cursor-session");
```

(b) Add it to the `stack_was_active` OR-chain (line ~258):

```rust
                    || codex_session_was_loaded
                    || cursor_session_was_loaded;
```

(c) After the `if codex_session_was_loaded { … "Stopped codex-session" }` teardown block (line ~334):

```rust
                if cursor_session_was_loaded {
                    install::service_bootout(
                        &domain,
                        &launch_agents.join("com.hippo.cursor-session.plist"),
                    );
                    println!("  Stopped cursor-session");
                }
```

(d) Add the template include after `let codex_session_template = include_str!(…);` (line ~345):

```rust
                let cursor_session_template =
                    include_str!("../../../launchd/com.hippo.cursor-session.plist");
```

(e) After the codex `install_plist`/`remove_plist` gate (line ~378), add the cursor gate:

```rust
                let cursor_session_installed = if config.cursor.enabled {
                    install::install_plist(
                        "com.hippo.cursor-session",
                        cursor_session_template,
                        &vars,
                        force,
                    )?;
                    true
                } else {
                    println!("  (cursor source disabled; skipping cursor-session plist)");
                    install::remove_plist("com.hippo.cursor-session")?;
                    false
                };
```

(f) After `let codex_session_started = …;` (line ~485):

```rust
                let cursor_session_started = install::should_start_optional_poll_agent(
                    cursor_session_installed,
                    cursor_session_was_loaded,
                    stack_was_active,
                );
```

(g) In the `support_agents` array (after the `codex-session` tuple, line ~530):

```rust
                        (
                            "cursor-session",
                            "com.hippo.cursor-session.plist",
                            cursor_session_started,
                        ),
```

(h) In the `needs_manual_start` expression (after the codex line ~558):

```rust
                    || (cursor_session_installed && !cursor_session_started)
```

(i) In the "Load with:" hint block (after the codex hint, line ~588):

```rust
                    if cursor_session_installed && !cursor_session_started {
                        println!(
                            "  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hippo.cursor-session.plist"
                        );
                    }
```

- [ ] **Step 4: Add to mise start/stop loops**

In `mise.toml`, in BOTH the `[tasks.start]` and `[tasks.stop]` `for label in \` loops, add a line after `com.hippo.codex-session`:

```bash
  com.hippo.codex-session \
  com.hippo.cursor-session
```

(The last item in each loop has no trailing backslash; ensure `com.hippo.codex-session` now ends with ` \` and `com.hippo.cursor-session` is the new final entry.)

- [ ] **Step 5: Build to verify wiring compiles**

Run: `cargo build -p hippo-daemon`
Expected: compiles clean (the `include_str!` resolves the new plist; `PlistVars` has the new field everywhere).

- [ ] **Step 6: Commit**

```bash
git add launchd/com.hippo.cursor-session.plist crates/hippo-daemon/src/install.rs crates/hippo-daemon/src/main.rs mise.toml
git commit -m "feat(cursor): com.hippo.cursor-session launchd job + install wiring"
```

---

## Task 11: Doctor + watchdog coverage

**Files:**
- Modify: `crates/hippo-daemon/src/watchdog.rs`
- Modify: `crates/hippo-daemon/src/commands.rs`

- [ ] **Step 1: Write the failing watchdog tests**

In `crates/hippo-daemon/src/watchdog.rs` test module, after the I-13 tests (line ~1851):

```rust
    // ── I-15 (cursor coverage proxy) ───────────────────────────────────────

    #[test]
    fn i15_cursor_alarms_on_repeated_failures() {
        let row = SourceHealthRow {
            last_event_ts: Some(NOW - 10_000),
            consecutive_failures: 4,
            ..blank_row("agentic-session-cursor")
        };
        let rows = vec![row];
        let v = check_i15_cursor_coverage_proxy(&by_source(&rows), NOW);
        assert!(v.is_some());
        assert_eq!(v.unwrap().invariant_id, "I-15");
    }

    #[test]
    fn i15_cursor_suppressed_when_failures_low() {
        let row = SourceHealthRow {
            last_event_ts: Some(NOW - 600_000),
            consecutive_failures: 2,
            ..blank_row("agentic-session-cursor")
        };
        let rows = vec![row];
        assert!(check_i15_cursor_coverage_proxy(&by_source(&rows), NOW).is_none());
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cargo test -p hippo-daemon watchdog i15_cursor`
Expected: FAIL — `check_i15_cursor_coverage_proxy` not found.

- [ ] **Step 3: Implement the I-15 invariant**

In `crates/hippo-daemon/src/watchdog.rs`, after `check_i13_codex_coverage_proxy` (line ~687):

```rust
/// I-15: Cursor-session coverage proxy. Mirrors I-13: alarm when the Cursor
/// poller has failed repeatedly. Full freshness coverage is the doctor's job.
pub fn check_i15_cursor_coverage_proxy(
    by_source: &std::collections::HashMap<&str, &SourceHealthRow>,
    now_ms: i64,
) -> Option<InvariantViolation> {
    let row = by_source.get("agentic-session-cursor")?;
    if row.consecutive_failures > 3 {
        let age_ms = coverage_proxy_since_ms(row, now_ms);
        return Some(InvariantViolation {
            invariant_id: "I-15".to_string(),
            source: "agentic-session-cursor".to_string(),
            since_ms: age_ms,
            details: json!({
                "consecutive_failures": row.consecutive_failures,
                "note": "proxy predicate; full freshness check lives in hippo doctor",
            }),
        });
    }
    None
}
```

- [ ] **Step 4: Register I-15 in `check_invariants`**

In `crates/hippo-daemon/src/watchdog.rs`, in `check_invariants`, after the I-13 registration block (line ~489):

```rust
    // I-15: Cursor-session coverage proxy.
    if !bench_paused && let Some(v) = check_i15_cursor_coverage_proxy(&by_source, now_ms) {
        violations.push(v);
    }
```

- [ ] **Step 5: Run watchdog tests to verify they pass**

Run: `cargo test -p hippo-daemon watchdog i15_cursor`
Expected: PASS.

> Ordering note: the `SuppressionSignals` struct must gain its new fields (Step 6) before the `signals()` test helper references them (Step 7) and before the new tests use the 5-arg helper (Step 8). Follow the step order below.

- [ ] **Step 6: Extend `SuppressionSignals` and the doctor logic**

In `crates/hippo-daemon/src/commands.rs`:

(a) In the `SuppressionSignals` struct (line ~2014), after `codex_sessions_recent: bool,`:

```rust
    /// At least one Cursor agent-transcript `.jsonl` exists under the roots.
    cursor_sessions_exist: bool,
    /// At least one Cursor agent-transcript `.jsonl` changed within 10 minutes.
    cursor_sessions_recent: bool,
```

(b) In `check_source_staleness`, add `'agentic-session-cursor'` to the `WHERE source IN (...)` list (line 1657) and to the `all_sources` array (line ~1843):

```rust
         WHERE source IN ('shell', 'browser', 'agentic-session-claude', 'claude-tool', 'agentic-session-opencode', 'agentic-session-codex', 'agentic-session-cursor') \
```

```rust
    let all_sources = [
        "agentic-session-codex",
        "agentic-session-cursor",
        "agentic-session-opencode",
        "browser",
        "agentic-session-claude",
        "claude-tool",
        "shell",
    ];
```

(c) Add a `cursor_session_state` closure modeled on `codex_session_state` (after it, line ~1773). It walks `cfg.cursor.session_roots` for `**/agent-transcripts/**/*.jsonl`:

```rust
    let cursor_session_state = || -> (bool, bool) {
        let Ok(cfg) = doctor_config.as_ref() else {
            return (false, false);
        };
        let idle_cutoff = std::time::SystemTime::now()
            .checked_sub(std::time::Duration::from_secs(IDLE_WINDOW_SECS))
            .unwrap_or(std::time::UNIX_EPOCH);
        let mut any_exist = false;
        for root in &cfg.cursor.session_roots {
            if !root.is_dir() {
                continue;
            }
            for entry in WalkDir::new(root).into_iter().filter_map(|e| e.ok()) {
                let path = entry.path();
                let is_jsonl = path.extension().map(|e| e == "jsonl").unwrap_or(false);
                let under = path.components().any(|c| c.as_os_str() == "agent-transcripts");
                if !(is_jsonl && under) {
                    continue;
                }
                any_exist = true;
                if let Ok(meta) = entry.metadata()
                    && let Ok(modified) = meta.modified()
                    && modified > idle_cutoff
                {
                    return (true, true);
                }
            }
        }
        (any_exist, false)
    };
```

(d) Compute the signals and add them to the `suppression_env` literal (line ~1832):

```rust
    let (cursor_sessions_do_exist, cursor_sessions_are_recent) = cursor_session_state();
```

```rust
        codex_sessions_exist: codex_sessions_do_exist,
        codex_sessions_recent: codex_sessions_are_recent,
        cursor_sessions_exist: cursor_sessions_do_exist,
        cursor_sessions_recent: cursor_sessions_are_recent,
    };
```

(e) Add suppression-reason arms in `source_staleness_suppression_reason` (after the codex arms, line ~2059):

```rust
        "agentic-session-cursor" if !signals.cursor_sessions_exist => {
            Some("no Cursor sessions found")
        }
        "agentic-session-cursor" if !signals.cursor_sessions_recent => Some("Cursor sessions idle"),
```

(f) Add a threshold arm in `source_staleness_thresholds_for` by extending the codex/opencode or-pattern (line ~2091):

```rust
        "agentic-session-opencode" | "agentic-session-codex" | "agentic-session-cursor" => {
            SourceStalenessThresholds {
                warn_secs: 300,
                fail_secs: 3600,
            }
        }
```

- [ ] **Step 7: Extend the `signals()` test helper**

In `crates/hippo-daemon/src/commands.rs` (line ~3749), the `signals()` test helper currently takes 3 args. Now that `SuppressionSignals` has the two cursor fields (Step 6a), change the helper signature/body to set them, and update its existing call sites:

```rust
    fn signals(
        recent_claude_session: bool,
        codex_sessions_exist: bool,
        codex_sessions_recent: bool,
        cursor_sessions_exist: bool,
        cursor_sessions_recent: bool,
    ) -> SuppressionSignals {
        SuppressionSignals {
            probe_ok: None,
            firefox_running: false,
            recent_claude_session,
            opencode_db_recent: false,
            codex_sessions_exist,
            codex_sessions_recent,
            cursor_sessions_exist,
            cursor_sessions_recent,
        }
    }
```

Then update each existing `signals(a, b, c)` call in this test module to `signals(a, b, c, false, false)` (the claude and codex staleness tests at lines ~3760-3847).

- [ ] **Step 8: Add the cursor doctor tests**

In `crates/hippo-daemon/src/commands.rs` test module, after the codex staleness tests (line ~3847):

```rust
    #[test]
    fn test_cursor_staleness_suppressed_when_no_sessions_exist() {
        assert_eq!(
            classify_source_staleness(
                "agentic-session-cursor",
                2 * 3600,
                signals(false, false, false, false, false),
            ),
            SourceStalenessStatus::Suppressed("no Cursor sessions found"),
        );
    }

    #[test]
    fn test_cursor_staleness_alarms_when_files_fresh_but_health_stale() {
        assert_eq!(
            classify_source_staleness(
                "agentic-session-cursor",
                2 * 3600,
                signals(false, false, false, true, true),
            ),
            SourceStalenessStatus::Fail,
        );
    }
```

- [ ] **Step 9: Run the full doctor + watchdog test suites**

Run: `cargo test -p hippo-daemon watchdog && cargo test -p hippo-daemon commands`
Expected: PASS — including the updated `signals()` call sites and the new cursor tests.

- [ ] **Step 10: Commit**

```bash
git add crates/hippo-daemon/src/watchdog.rs crates/hippo-daemon/src/commands.rs
git commit -m "feat(cursor): doctor staleness coverage + watchdog I-15 proxy"
```

---

## Task 12: Probe (assertion-only)

**Files:**
- Modify: `crates/hippo-daemon/src/probe.rs`

- [ ] **Step 1: Implement `probe_cursor_session`**

In `crates/hippo-daemon/src/probe.rs`, after `probe_claude_session` (line ~282), add an assertion-only probe. Note the Cursor-specific timing guard: only assert on files idle long enough that the 60s poller should have ingested them (avoids spurious fails on in-flight files):

```rust
/// Cursor-session probe: assertion-based, mirrors `probe_claude_session`.
///
/// For every `~/.cursor/projects/**/agent-transcripts/**/*.jsonl` whose mtime
/// falls in the window [now-5min, now-2*min_idle], assert a `claude_sessions`
/// row exists with that `source_file`. Files newer than 2*min_idle are skipped
/// — the 60 s poller has not necessarily ingested them yet, so asserting on
/// them would spuriously fail.
fn probe_cursor_session(config: &HippoConfig) -> Result<(bool, Option<i64>)> {
    let now_ms = chrono::Utc::now().timestamp_millis();
    let window_ms: i64 = 5 * 60 * 1000;
    let settle_ms: i64 = (config.cursor.min_idle_secs as i64 * 2) * 1000;

    let roots = &config.cursor.session_roots;
    let mut recent: Vec<(std::path::PathBuf, i64)> = Vec::new();
    for root in roots {
        if !root.is_dir() {
            continue;
        }
        for entry in walkdir::WalkDir::new(root).into_iter().filter_map(|e| e.ok()) {
            let path = entry.path();
            let is_jsonl = path.extension().map(|e| e == "jsonl").unwrap_or(false);
            let under = path.components().any(|c| c.as_os_str() == "agent-transcripts");
            if !(is_jsonl && under) {
                continue;
            }
            let Some(mtime_ms) = entry.metadata().ok().and_then(|m| {
                m.modified().ok().and_then(|t| {
                    t.duration_since(std::time::UNIX_EPOCH).ok().map(|d| d.as_millis() as i64)
                })
            }) else {
                continue;
            };
            let age = now_ms - mtime_ms;
            if age >= settle_ms && age <= window_ms {
                recent.push((path.to_path_buf(), mtime_ms));
            }
        }
    }

    if recent.is_empty() {
        info!("cursor-session probe: no settled recent transcripts — trivial pass");
        return Ok((true, None));
    }

    let db = storage::open_db(&config.db_path()).context("cannot open DB for cursor-session probe")?;
    let mut all_ok = true;
    let mut latest_lag: Option<i64> = None;
    for (path, mtime_ms) in &recent {
        let path_str = path.to_string_lossy();
        let count: i64 = db
            .query_row(
                "SELECT COUNT(*) FROM claude_sessions
                 WHERE source_file = ?1 AND probe_tag IS NULL AND end_time >= ?2",
                rusqlite::params![path_str.as_ref(), mtime_ms - window_ms],
                |row| row.get(0),
            )
            .with_context(|| format!("failed to query claude_sessions for {}", path_str))?;
        if count == 0 {
            warn!("cursor-session probe: no row for {}", path_str);
            all_ok = false;
        } else {
            let lag = now_ms - mtime_ms;
            latest_lag = Some(latest_lag.map_or(lag, |p: i64| p.max(lag)));
        }
    }
    Ok((all_ok, latest_lag))
}
```

- [ ] **Step 2: Wire it into `run()`**

In `crates/hippo-daemon/src/probe.rs`, in `run()`, after the `agentic-session-claude` arm (line ~80), add:

```rust
    if run_all || source == Some("agentic-session-cursor") {
        match probe_cursor_session(config) {
            Ok((ok, lag)) => {
                println!(
                    "[probe] agentic-session-cursor: {} (lag={}ms)",
                    if ok { "OK" } else { "FAIL" },
                    lag.map(|l| l.to_string()).as_deref().unwrap_or("N/A")
                );
                write_probe_result(config, "agentic-session-cursor", ok, lag)?;
            }
            Err(e) => {
                warn!("agentic-session-cursor probe error: {e:#}");
                println!("[probe] agentic-session-cursor: ERROR — {e:#}");
                write_probe_result(config, "agentic-session-cursor", false, None)?;
            }
        }
    }
```

And update the unknown-source `matches!` guard (line ~100) to include the new source:

```rust
    if let Some(s) = source
        && !matches!(
            s,
            "shell" | "claude-tool" | "agentic-session-claude" | "browser" | "agentic-session-cursor"
        )
    {
        anyhow::bail!(
            "unknown probe source '{}'; valid: shell, claude-tool, agentic-session-claude, browser, agentic-session-cursor",
            s
        );
    }
```

- [ ] **Step 3: Write a probe test**

Add a test in the `crates/hippo-daemon/src/probe.rs` test module (or a `tests/` integration file if probes are tested there — match the existing location). Minimal assertion-only happy path:

```rust
    #[test]
    fn cursor_probe_trivial_pass_when_no_transcripts() {
        let tmp = tempfile::tempdir().unwrap();
        let data = tmp.path().join("data");
        std::fs::create_dir_all(&data).unwrap();
        let mut config = hippo_core::config::HippoConfig::default();
        config.storage.data_dir = data;
        config.cursor.session_roots = vec![tmp.path().join("nonexistent")];
        let (ok, lag) = super::probe_cursor_session(&config).unwrap();
        assert!(ok);
        assert_eq!(lag, None);
    }
```

- [ ] **Step 4: Run the probe tests**

Run: `cargo test -p hippo-daemon probe cursor`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-daemon/src/probe.rs
git commit -m "feat(cursor): assertion-only cursor-session probe with poll-settle guard"
```

---

## Task 13: Brain source label

**Files:**
- Modify: `brain/src/hippo_brain/server.py`
- Modify: `brain/tests/test_server_extended.py`

- [ ] **Step 1: Write the failing test**

In `brain/tests/test_server_extended.py`, add a test mirroring the codex label test:

```python
def test_source_label_for_cursor_segments():
    from hippo_brain.server import _source_label_for_claude_segments

    cursor_segs = [
        {"source_file": "/Users/me/.cursor/projects/p/agent-transcripts/s/s.jsonl"}
    ]
    assert _source_label_for_claude_segments(cursor_segs) == "cursor"

    mixed = [
        {"source_file": "/Users/me/.cursor/projects/p/agent-transcripts/s/s.jsonl"},
        {"source_file": "/Users/me/.claude/projects/p/s.jsonl"},
    ]
    assert _source_label_for_claude_segments(mixed) == "claude"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project brain pytest brain/tests/test_server_extended.py::test_source_label_for_cursor_segments -q`
Expected: FAIL — returns `"claude"` for the cursor-only case.

- [ ] **Step 3: Add the cursor label logic**

In `brain/src/hippo_brain/server.py`, after the `CODEX_SOURCE_SQL` constant (line ~123) add:

```python
CURSOR_SOURCE_SQL = "s.source_file LIKE '%/.cursor/%'"
```

After `_is_codex_source_file` (line ~128) add:

```python
def _is_cursor_source_file(source_file: str | None) -> bool:
    return bool(source_file and "/.cursor/" in source_file)
```

Update `_source_label_for_claude_segments` (line ~134) so cursor is checked before the claude fallback:

```python
def _source_label_for_claude_segments(segments: list[dict]) -> str:
    if segments and all(_is_codex_source_file(seg.get("source_file")) for seg in segments):
        return "codex"
    if segments and all(_is_cursor_source_file(seg.get("source_file")) for seg in segments):
        return "cursor"
    return "claude"
```

- [ ] **Step 4: Add cursor to queue-depth metrics**

In `brain/src/hippo_brain/server.py`, in `_collect_queue_depths` (line ~140), add a `cursor` query and exclude cursor rows from the `claude` count so they are not double-counted:

```python
        "claude": f"""
            SELECT COUNT(*)
            FROM claude_enrichment_queue q
            JOIN claude_sessions s ON q.claude_session_id = s.id
            WHERE q.status = ?
              AND s.probe_tag IS NULL
              AND NOT ({CODEX_SOURCE_SQL})
              AND NOT ({CURSOR_SOURCE_SQL})
        """,
        "cursor": f"""
            SELECT COUNT(*)
            FROM claude_enrichment_queue q
            JOIN claude_sessions s ON q.claude_session_id = s.id
            WHERE q.status = ?
              AND s.probe_tag IS NULL
              AND ({CURSOR_SOURCE_SQL})
        """,
```

- [ ] **Step 5: Run brain tests + lint**

Run: `uv run --project brain pytest brain/tests/test_server_extended.py -q && uv run --project brain ruff check brain/`
Expected: PASS, lint clean.

- [ ] **Step 6: Commit**

```bash
git add brain/src/hippo_brain/server.py brain/tests/test_server_extended.py
git commit -m "feat(cursor): label cursor knowledge nodes + queue-depth metric"
```

---

## Task 14: config template, docs, and full verification

**Files:**
- Modify: `config/config.default.toml`
- Modify: `CLAUDE.md`, `docs/capture/sources.md`, `docs/capture/test-matrix.md`, `docs/capture/architecture.md`, `docs/capture/adding-a-source.md`, `docs/schema.md`, `docs/lifecycle.md`, `README.md`

- [ ] **Step 1: Add the `[cursor]` config section**

In `config/config.default.toml`, after the `[codex]` section (line ~217):

```toml
[cursor]
# Cursor Agent CLI transcript ingestion.
#
# When enabled, `hippo cursor-poll` (invoked every 60 s by the
# `com.hippo.cursor-session` LaunchAgent) walks `session_roots` for
# `agent-transcripts/**/*.jsonl` files (main sessions + subagents), parses each
# into char-bounded segments, and writes them as segmented rows in
# `claude_sessions` — distinguished by the `.cursor/` path stored in the
# `source_file` column. Subagents are ingested as their own sessions with a
# parent_session_id link. The brain enriches these alongside Claude Code data.
#
# Cursor transcripts carry no per-line timestamps, so segments are bounded by
# accumulated character count and time-stamped from the file mtime.
#
# `session_roots` defaults to:
#   ~/.cursor/projects
#
# To verify:
#   1. Ensure `~/.cursor/projects` contains agent-transcripts/**/*.jsonl files
#   2. Run once: `hippo cursor-poll`
#   3. Verify: `hippo doctor` shows healthy `agentic-session-cursor` source

enabled = true
# min_idle_secs = 60
# poll_interval_secs = 60
# session_roots = []  # leave commented out; Rust default supplies ~/.cursor/projects
```

- [ ] **Step 2: Add the CLAUDE.md section**

In `CLAUDE.md`, after the "Codex Session Ingestion" section, add a "Cursor Session Ingestion" subsection describing: `cursor_session::poll_tick` in `crates/hippo-daemon/src/cursor_session.rs`, launchd `com.hippo.cursor-session` (`StartInterval` 60s), the `[cursor]` config, the `agentic-session-cursor` health key, that rows land in `claude_sessions` distinguished by the `.cursor/` `source_file` path, that subagents are ingested with `is_subagent=1` + `parent_session_id`, the timestamp-free char-cap segmentation, and the v16 migration. Mirror the structure and length of the existing Codex section.

- [ ] **Step 3: Update the capture docs**

- `docs/capture/sources.md` — add row "9 | Cursor Agent transcripts | `com.hippo.cursor-session` → `hippo cursor-poll` → `cursor_session::poll_tick` walks `~/.cursor/projects/**/agent-transcripts/**/*.jsonl`, upserts into `claude_sessions`; health key `agentic-session-cursor` | `claude_sessions` (shared), `claude_enrichment_queue` | I-15 + I-2 (shared) | assertion-only probe | healthy".
- `docs/capture/test-matrix.md` — add F-rows: cursor event landed (`source_audit::cursor_agent`), source_health bumped (`poll_tick_ingests_idle_files_and_advances_cursor`), probe assertion (`cursor_probe_trivial_pass_when_no_transcripts` + live), probe rows absent from user queries (shared `probe_tag IS NULL` filter — AP-6).
- `docs/capture/architecture.md` — add I-15 to the invariant table: "**I-15** Cursor-session coverage | If `agentic-session-cursor.consecutive_failures > 3`, the Cursor poller is actively broken. | proxy | Bench pause window. | Watchdog alarm + doctor `[!!] agentic-session-cursor events`." Add `agentic-session-cursor` to the `source_health.source` enumerations.
- `docs/capture/adding-a-source.md` — change the Cursor row in the "Want to capture" table from "Not yet" to "Covered — `com.hippo.cursor-session` Rust poller (`cursor_session.rs`); see `docs/superpowers/specs/2026-05-25-cursor-ingestion-design.md`".
- `docs/schema.md` — add a v16 changelog row mirroring the v15 row: "**v16** | Cursor session ingestion capture-health. | No new tables. Seeds `source_health` row `agentic-session-cursor` (NULL `last_event_ts`) via `INSERT OR IGNORE`, then `PRAGMA user_version = 16`. | The Cursor poller records capture health under `agentic-session-cursor`; Cursor segments land in `claude_sessions` distinguished by their `.cursor/` `source_file` path. See `docs/superpowers/specs/2026-05-25-cursor-ingestion-design.md`."
- `docs/lifecycle.md` — add a sentence next to the Codex line: "Cursor Agent transcripts are ingested by `hippo cursor-poll` (`com.hippo.cursor-session`), whose `cursor_session::poll_tick` parses the Anthropic-style transcript shape and writes segmented rows through the same `claude_sessions` table, sharing `claude_enrichment_queue`. Capture-health is keyed `agentic-session-cursor`."
- `README.md` — if the "Why hippo" / sources list enumerates Claude/Codex/opencode, add Cursor.

- [ ] **Step 4: Run full-workspace verification**

Run each and confirm clean:

```bash
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
uv run --project brain pytest brain/tests -q
uv run --project brain ruff check brain/
uv run --project brain ruff format --check brain/
```

Expected: all green. Fix any failures before proceeding.

- [ ] **Step 5: Live smoke test**

```bash
cargo build --release
./target/release/hippo cursor-poll
./target/release/hippo doctor
```

Expected: `cursor-poll` exits 0 and ingests any idle transcripts; `hippo doctor` shows an `agentic-session-cursor events` line (`[OK]`/`[WW]`/`[--]` depending on activity, not `[!!]`). If you have real Cursor agent sessions, confirm rows: `sqlite3 ~/.local/share/hippo/hippo.db "SELECT COUNT(*) FROM claude_sessions WHERE source_file LIKE '%/.cursor/%';"`.

- [ ] **Step 6: Commit**

```bash
git add config/config.default.toml CLAUDE.md docs/ README.md
git commit -m "docs(cursor): config template, CLAUDE.md, capture docs for Cursor source"
```

---

## Self-Review notes (spec coverage map)

- Spec §2 (on-disk format) → Tasks 4, 5 (path identity + Anthropic-block parser, fixtures).
- Spec §3 (scope/decisions: CLI transcripts, poller, subagents) → Tasks 5 (subagent identity), 7 (subagent columns), 8 (poller).
- Spec §4.1 discovery → Task 8 (`is_transcript` + walk).
- Spec §4.2 path identity → Task 4.
- Spec §4.3 block parser → Task 5.
- Spec §4.4 timestamp-free segmentation → Task 5 (`extract_segments_splits_on_char_cap_without_timestamps`).
- Spec §4.5 resume cursor (`cursor-agent-<inode>`) → Task 8.
- Spec §4.6 upsert & enqueue → Task 7.
- Spec §5 integration (config, CLI, launchd, schema v16, doctor, watchdog, probe) → Tasks 1, 2, 9, 10, 11, 12.
- Spec §6 brain label → Task 13.
- Spec §7 error handling & crash safety → Task 8 (record_error, cursor-advances-last, zero-segment behavior).
- Spec §8 testing → Tasks 5–8, 11–13 (+ source_audit in Task 8).
- Spec §9 out of scope (no vscdb, no oracle) → honored (not implemented).
- Spec §10 unification follow-up → `source_file` `/.cursor/` derivation lands in Task 13 (mirrors codex), absorbable by the future migration.
