# Agentic Session Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `ShellKind::Unknown("claude-code")` hack with a first-class `AgenticToolCall` event payload; migrate Claude onto it; add opencode (live SQLite polling) and Codex (historical JSONL batch) as producers.

**Architecture:** New `AgenticToolCall` variant in `hippo-core::events` with `Harness` enum and per-harness readers. opencode uses a daemon-side SQLite poller gated by `PRAGMA data_version`. Codex is a batch CLI importer. Schema migrates v5→v6, renaming `claude_sessions` → `agentic_sessions` with harness/model/provider/agent/effort/tokens/cost columns. Brain side renames module and dispatches per harness.

**Tech Stack:** Rust 2024 (hippo-core, hippo-daemon), Python 3.14 (brain, uv), rusqlite, serde/serde_json, chrono, uuid, tokio, tracing, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-04-17-opencode-ingestion-and-agentic-labeling-design.md`

**Pre-flight:**
- Worktree: agree with user on whether to use a dedicated worktree before starting. Recommended: `~/projects/hippo-agentic` worktree on branch `agentic-ingestion`.
- Before every `cargo test`, `cargo clippy`, or Python test, run from repo root unless noted.
- Use `cargo test -p <crate>` scoped by crate where possible to keep iteration fast.

---

## Phase 1: Foundation — `AgenticToolCall` types and renderer

### Task 1.1: Scaffold the agentic module in `hippo-core`

**Files:**
- Create: `crates/hippo-core/src/agentic/mod.rs`
- Create: `crates/hippo-core/src/agentic/types.rs`
- Modify: `crates/hippo-core/src/lib.rs`

- [ ] **Step 1: Create `crates/hippo-core/src/agentic/mod.rs`**

```rust
//! Shared types and helpers for agentic (coding-assistant) tool calls.
//!
//! Producers: Claude Code JSONL ingester, opencode SQLite poller, Codex batch
//! importer. Downstream consumers: the daemon event pipeline, the brain
//! enrichment module, and MCP query tools.

pub mod render;
pub mod types;

pub use render::render_command;
pub use types::{AgenticStatus, AgenticToolCall, Harness, TokenUsage};
```

- [ ] **Step 2: Create `crates/hippo-core/src/agentic/types.rs`**

```rust
use std::path::PathBuf;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::events::{CapturedOutput, GitState};

/// Which coding harness produced this tool call.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", content = "name", rename_all = "kebab-case")]
pub enum Harness {
    ClaudeCode,
    Opencode,
    Codex,
    Unknown(String),
}

impl Harness {
    /// Stable string form for DB storage and search filters.
    pub fn as_db_str(&self) -> &str {
        match self {
            Self::ClaudeCode => "claude-code",
            Self::Opencode => "opencode",
            Self::Codex => "codex",
            Self::Unknown(s) => s,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum AgenticStatus {
    Ok,
    Error,
    Orphaned,
}

impl AgenticStatus {
    pub fn as_db_str(&self) -> &str {
        match self {
            Self::Ok => "ok",
            Self::Error => "error",
            Self::Orphaned => "orphaned",
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct TokenUsage {
    pub input: u64,
    pub output: u64,
    pub reasoning: u64,
    pub cache_read: u64,
    pub cache_write: u64,
}

/// A single tool call emitted by an agentic coding harness.
///
/// Pairs an assistant-side tool invocation with its result. Orphaned calls
/// (no result observed) are emitted with `status = Orphaned` at source EOF.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgenticToolCall {
    pub session_id: Uuid,
    pub parent_session_id: Option<Uuid>,
    pub harness: Harness,
    pub harness_version: Option<String>,
    pub model: String,
    pub provider: Option<String>,
    pub agent: Option<String>,
    pub effort: Option<String>,
    pub tool_name: String,
    pub tool_input: serde_json::Value,
    pub command: String,
    pub tool_output: Option<CapturedOutput>,
    pub status: AgenticStatus,
    pub duration_ms: u64,
    pub started_at: DateTime<Utc>,
    pub cwd: PathBuf,
    pub hostname: String,
    pub git_state: Option<GitState>,
    pub tokens: Option<TokenUsage>,
    pub cost_usd: Option<f64>,
    pub redaction_count: u32,
}
```

- [ ] **Step 3: Export from `crates/hippo-core/src/lib.rs`**

Find the existing module declarations (should be near the top). Add:

```rust
pub mod agentic;
```

Keep alphabetical with existing `pub mod` lines.

- [ ] **Step 4: Verify it builds**

Run: `cargo build -p hippo-core`
Expected: PASS (no warnings, no errors).

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-core/src/agentic/mod.rs crates/hippo-core/src/agentic/types.rs crates/hippo-core/src/lib.rs
git commit -m "feat(hippo-core): scaffold agentic module with AgenticToolCall type"
```

### Task 1.2: Round-trip JSON tests for the new types

**Files:**
- Create: `crates/hippo-core/tests/agentic_types.rs`

- [ ] **Step 1: Write the failing test file**

```rust
use std::path::PathBuf;

use chrono::Utc;
use hippo_core::agentic::{AgenticStatus, AgenticToolCall, Harness, TokenUsage};
use hippo_core::events::{CapturedOutput, GitState};
use uuid::Uuid;

fn sample() -> AgenticToolCall {
    AgenticToolCall {
        session_id: Uuid::new_v4(),
        parent_session_id: None,
        harness: Harness::ClaudeCode,
        harness_version: None,
        model: "claude-opus-4-7".into(),
        provider: Some("anthropic".into()),
        agent: None,
        effort: None,
        tool_name: "Bash".into(),
        tool_input: serde_json::json!({"command": "cargo test"}),
        command: "cargo test".into(),
        tool_output: Some(CapturedOutput {
            content: "ok".into(),
            truncated: false,
            original_bytes: 2,
        }),
        status: AgenticStatus::Ok,
        duration_ms: 1234,
        started_at: Utc::now(),
        cwd: PathBuf::from("/tmp"),
        hostname: "host".into(),
        git_state: Some(GitState {
            repo: None,
            branch: Some("main".into()),
            commit: None,
            is_dirty: false,
        }),
        tokens: Some(TokenUsage {
            input: 10,
            output: 5,
            reasoning: 0,
            cache_read: 0,
            cache_write: 0,
        }),
        cost_usd: None,
        redaction_count: 0,
    }
}

#[test]
fn roundtrip_json() {
    let call = sample();
    let json = serde_json::to_string(&call).unwrap();
    let parsed: AgenticToolCall = serde_json::from_str(&json).unwrap();
    assert_eq!(parsed.tool_name, "Bash");
    assert_eq!(parsed.command, "cargo test");
    assert_eq!(parsed.model, "claude-opus-4-7");
    assert_eq!(parsed.status, AgenticStatus::Ok);
}

#[test]
fn harness_unknown_roundtrip() {
    let call = AgenticToolCall {
        harness: Harness::Unknown("aider".into()),
        ..sample()
    };
    let json = serde_json::to_string(&call).unwrap();
    let parsed: AgenticToolCall = serde_json::from_str(&json).unwrap();
    assert_eq!(parsed.harness, Harness::Unknown("aider".into()));
}

#[test]
fn harness_as_db_str() {
    assert_eq!(Harness::ClaudeCode.as_db_str(), "claude-code");
    assert_eq!(Harness::Opencode.as_db_str(), "opencode");
    assert_eq!(Harness::Codex.as_db_str(), "codex");
    assert_eq!(Harness::Unknown("x".into()).as_db_str(), "x");
}

#[test]
fn status_as_db_str() {
    assert_eq!(AgenticStatus::Ok.as_db_str(), "ok");
    assert_eq!(AgenticStatus::Error.as_db_str(), "error");
    assert_eq!(AgenticStatus::Orphaned.as_db_str(), "orphaned");
}
```

- [ ] **Step 2: Run the tests to confirm they pass**

Run: `cargo test -p hippo-core --test agentic_types`
Expected: 4 tests pass.

- [ ] **Step 3: Commit**

```bash
git add crates/hippo-core/tests/agentic_types.rs
git commit -m "test(hippo-core): round-trip and db-str assertions for AgenticToolCall"
```

### Task 1.3: Shared command renderer

**Files:**
- Create: `crates/hippo-core/src/agentic/render.rs`
- Create: `crates/hippo-core/tests/agentic_render.rs`

- [ ] **Step 1: Write the failing test file**

```rust
use hippo_core::agentic::render::render_command;
use serde_json::json;

#[test]
fn bash_renders_command_verbatim() {
    let input = json!({"command": "cargo test -p hippo-core"});
    assert_eq!(render_command("Bash", &input), "cargo test -p hippo-core");
}

#[test]
fn read_renders_path() {
    assert_eq!(
        render_command("Read", &json!({"file_path": "/foo/bar.rs"})),
        "read /foo/bar.rs"
    );
}

#[test]
fn edit_renders_path() {
    assert_eq!(
        render_command("Edit", &json!({"file_path": "/foo/bar.rs"})),
        "edit /foo/bar.rs"
    );
}

#[test]
fn write_renders_path() {
    assert_eq!(
        render_command("Write", &json!({"file_path": "/foo/bar.rs"})),
        "write /foo/bar.rs"
    );
}

#[test]
fn grep_renders_pattern_and_path() {
    assert_eq!(
        render_command("Grep", &json!({"pattern": "TODO", "path": "src/"})),
        "grep 'TODO' src/"
    );
}

#[test]
fn glob_renders_pattern() {
    assert_eq!(
        render_command("Glob", &json!({"pattern": "**/*.rs"})),
        "glob '**/*.rs'"
    );
}

#[test]
fn agent_renders_description() {
    assert_eq!(
        render_command("Agent", &json!({"description": "find TODOs"})),
        "agent: find TODOs"
    );
}

#[test]
fn task_create_renders_subject() {
    assert_eq!(
        render_command("TaskCreate", &json!({"subject": "fix bug"})),
        "task: fix bug"
    );
}

#[test]
fn task_update_renders_id_and_status() {
    assert_eq!(
        render_command("TaskUpdate", &json!({"taskId": "42", "status": "completed"})),
        "task-update: 42 completed"
    );
}

#[test]
fn exec_command_renders_cmd() {
    // Codex shape
    assert_eq!(
        render_command(
            "exec_command",
            &json!({"cmd": "ls /tmp", "workdir": "/tmp"})
        ),
        "ls /tmp"
    );
}

#[test]
fn skill_renders_name() {
    // opencode shape
    assert_eq!(
        render_command("skill", &json!({"name": "brainstorming"})),
        "skill: brainstorming"
    );
}

#[test]
fn unknown_tool_returns_name() {
    assert_eq!(render_command("MadeUp", &json!({})), "MadeUp");
}
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `cargo test -p hippo-core --test agentic_render`
Expected: FAIL (module `render_command` does not exist yet).

- [ ] **Step 3: Implement `crates/hippo-core/src/agentic/render.rs`**

```rust
//! Render a structured tool-call `input` into a human-readable command string.
//!
//! Shared across all agentic harnesses so Claude's `Bash` call and Codex's
//! `exec_command` produce the same display shape for downstream greps.

use serde_json::Value;

pub fn render_command(tool_name: &str, input: &Value) -> String {
    match tool_name {
        "Bash" => input
            .get("command")
            .and_then(Value::as_str)
            .unwrap_or("bash")
            .to_string(),
        "exec_command" => input
            .get("cmd")
            .and_then(Value::as_str)
            .unwrap_or("exec")
            .to_string(),
        "Read" => format!("read {}", str_field(input, "file_path", "<unknown>")),
        "Edit" => format!("edit {}", str_field(input, "file_path", "<unknown>")),
        "Write" => format!("write {}", str_field(input, "file_path", "<unknown>")),
        "Grep" => format!(
            "grep '{}' {}",
            str_field(input, "pattern", "*"),
            str_field(input, "path", ".")
        ),
        "Glob" => format!("glob '{}'", str_field(input, "pattern", "*")),
        "Agent" => format!("agent: {}", str_field(input, "description", "agent task")),
        "TaskCreate" => format!("task: {}", str_field(input, "subject", "task")),
        "TaskUpdate" => format!(
            "task-update: {} {}",
            str_field(input, "taskId", "?"),
            str_field(input, "status", "?")
        ),
        "skill" => format!("skill: {}", str_field(input, "name", "?")),
        other => other.to_string(),
    }
}

fn str_field<'a>(input: &'a Value, key: &str, default: &'a str) -> &'a str {
    input.get(key).and_then(Value::as_str).unwrap_or(default)
}
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `cargo test -p hippo-core --test agentic_render`
Expected: 12 tests pass.

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-core/src/agentic/render.rs crates/hippo-core/tests/agentic_render.rs
git commit -m "feat(hippo-core): shared agentic command renderer"
```

### Task 1.4: Wire `AgenticToolCall` into `EventPayload`

**Files:**
- Modify: `crates/hippo-core/src/events.rs`
- Create: `crates/hippo-core/tests/agentic_envelope.rs`

- [ ] **Step 1: Write the failing envelope test**

`crates/hippo-core/tests/agentic_envelope.rs`:

```rust
use std::path::PathBuf;

use chrono::Utc;
use hippo_core::agentic::{AgenticStatus, AgenticToolCall, Harness};
use hippo_core::events::{EventEnvelope, EventPayload};
use uuid::Uuid;

fn sample_call() -> AgenticToolCall {
    AgenticToolCall {
        session_id: Uuid::new_v4(),
        parent_session_id: None,
        harness: Harness::ClaudeCode,
        harness_version: None,
        model: "claude-opus-4-7".into(),
        provider: Some("anthropic".into()),
        agent: None,
        effort: None,
        tool_name: "Bash".into(),
        tool_input: serde_json::json!({"command": "ls"}),
        command: "ls".into(),
        tool_output: None,
        status: AgenticStatus::Ok,
        duration_ms: 0,
        started_at: Utc::now(),
        cwd: PathBuf::from("/tmp"),
        hostname: "h".into(),
        git_state: None,
        tokens: None,
        cost_usd: None,
        redaction_count: 0,
    }
}

#[test]
fn envelope_roundtrip_adjacently_tagged() {
    let envelope = EventEnvelope {
        envelope_id: Uuid::new_v4(),
        producer_version: 1,
        timestamp: Utc::now(),
        payload: EventPayload::AgenticToolCall(Box::new(sample_call())),
    };
    let json = serde_json::to_string(&envelope).unwrap();
    let parsed: EventEnvelope = serde_json::from_str(&json).unwrap();
    match parsed.payload {
        EventPayload::AgenticToolCall(c) => assert_eq!(c.command, "ls"),
        _ => panic!("expected AgenticToolCall payload"),
    }

    let v: serde_json::Value = serde_json::to_value(&envelope).unwrap();
    assert_eq!(v["payload"]["type"], "AgenticToolCall");
    assert!(v["payload"]["data"].is_object());
}
```

- [ ] **Step 2: Run test to verify failure**

Run: `cargo test -p hippo-core --test agentic_envelope`
Expected: FAIL (`AgenticToolCall` variant does not exist on `EventPayload`).

- [ ] **Step 3: Add the variant to `crates/hippo-core/src/events.rs`**

Find the `pub enum EventPayload { ... }` block (around line 16). Add the new variant before `Raw`:

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", content = "data")]
pub enum EventPayload {
    Shell(Box<ShellEvent>),
    FsChange(FsChangeEvent),
    IdeAction(IdeActionEvent),
    Browser(Box<BrowserEvent>),
    AgenticToolCall(Box<crate::agentic::AgenticToolCall>),
    Raw(serde_json::Value),
}
```

- [ ] **Step 4: Run the envelope test**

Run: `cargo test -p hippo-core --test agentic_envelope`
Expected: PASS.

- [ ] **Step 5: Run full crate tests to catch exhaustive-match breakage**

Run: `cargo test -p hippo-core`
Expected: PASS. If any `match payload` elsewhere now fails non-exhaustively, the compile error will surface the file and line — add an `EventPayload::AgenticToolCall(_)` arm that does the obvious thing (usually treat as a Shell-like event for storage, or unreachable at this stage).

- [ ] **Step 6: Commit**

```bash
git add crates/hippo-core/src/events.rs crates/hippo-core/tests/agentic_envelope.rs
git commit -m "feat(hippo-core): add AgenticToolCall variant to EventPayload"
```

---

## Phase 2: Schema migration v5 → v6

### Task 2.1: Write the v6 migration SQL and update the schema file

**Files:**
- Modify: `crates/hippo-core/src/schema.sql`
- Modify: `crates/hippo-core/src/storage.rs`

- [ ] **Step 1: Append v6 tables to `crates/hippo-core/src/schema.sql`**

At the **end** of the file, replace the trailing `PRAGMA user_version = 5;` with the v6 block below. Keep the v5 content unchanged above it.

```sql
-- ─── v6: agentic sessions (claude, opencode, codex) ─────────────────
-- Rename Claude-specific tables to harness-agnostic form. Full migration
-- (ALTER TABLE ... RENAME) is performed in storage.rs for existing DBs;
-- fresh DBs skip the rename and create the renamed tables directly.

CREATE TABLE IF NOT EXISTS agentic_sessions (
    id                 INTEGER PRIMARY KEY,
    session_id         TEXT NOT NULL,
    project_dir        TEXT NOT NULL,
    cwd                TEXT NOT NULL,
    git_branch         TEXT,
    segment_index      INTEGER NOT NULL,
    start_time         INTEGER NOT NULL,
    end_time           INTEGER NOT NULL,
    summary_text       TEXT NOT NULL,
    tool_calls_json    TEXT,
    user_prompts_json  TEXT,
    message_count      INTEGER NOT NULL,
    token_count        INTEGER,
    source_file        TEXT NOT NULL,
    is_subagent        INTEGER NOT NULL DEFAULT 0,
    parent_session_id  TEXT,
    harness            TEXT NOT NULL DEFAULT 'claude-code',
    harness_version    TEXT,
    model              TEXT,
    provider           TEXT,
    agent              TEXT,
    effort             TEXT,
    tokens_input       INTEGER,
    tokens_output      INTEGER,
    tokens_reasoning   INTEGER,
    tokens_cache_read  INTEGER,
    tokens_cache_write INTEGER,
    cost_usd           REAL,
    enriched           INTEGER NOT NULL DEFAULT 0,
    created_at         INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    UNIQUE (session_id, segment_index)
);

CREATE TABLE IF NOT EXISTS knowledge_node_agentic_sessions (
    knowledge_node_id  INTEGER NOT NULL REFERENCES knowledge_nodes(id),
    agentic_session_id INTEGER NOT NULL REFERENCES agentic_sessions(id),
    PRIMARY KEY (knowledge_node_id, agentic_session_id)
);

CREATE TABLE IF NOT EXISTS agentic_enrichment_queue (
    id                 INTEGER PRIMARY KEY,
    agentic_session_id INTEGER NOT NULL UNIQUE REFERENCES agentic_sessions(id),
    status             TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'done', 'failed', 'skipped')),
    priority           INTEGER NOT NULL DEFAULT 5,
    retry_count        INTEGER NOT NULL DEFAULT 0,
    max_retries        INTEGER NOT NULL DEFAULT 5,
    error_message      TEXT,
    locked_at          INTEGER,
    locked_by          TEXT,
    created_at         INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    updated_at         INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
);

-- Cursor for live SQLite pollers (opencode today; future live sources).
-- `source_key` is the inode as TEXT for rename-safety.
CREATE TABLE IF NOT EXISTS agentic_cursor (
    harness            TEXT NOT NULL,
    source_key         TEXT NOT NULL,
    last_time_created  INTEGER NOT NULL,
    last_id            TEXT NOT NULL,
    updated_at         INTEGER NOT NULL,
    PRIMARY KEY (harness, source_key)
);

CREATE INDEX IF NOT EXISTS idx_agentic_sessions_cwd ON agentic_sessions(cwd);
CREATE INDEX IF NOT EXISTS idx_agentic_sessions_session ON agentic_sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_agentic_sessions_harness ON agentic_sessions(harness);
CREATE INDEX IF NOT EXISTS idx_agentic_sessions_model ON agentic_sessions(model);
CREATE INDEX IF NOT EXISTS idx_agentic_queue_pending ON agentic_enrichment_queue(status, priority)
    WHERE status = 'pending';

-- Remove v5 Claude-specific artifacts from the fresh-install path. Existing
-- DBs retain them only if the v5→v6 migration in storage.rs decides to keep
-- them as legacy names (it does not — see migration block).
DROP TABLE IF EXISTS claude_sessions;
DROP TABLE IF EXISTS knowledge_node_claude_sessions;
DROP TABLE IF EXISTS claude_enrichment_queue;

PRAGMA user_version = 6;
```

Leave the existing v5 `claude_sessions` CREATE statements in place above — they will be renamed by the storage.rs migration; a fresh install hits the v6 block and the v5 Claude tables are replaced/dropped by the DROP statements at the end.

Wait — on a fresh install, `schema.sql` is applied top-to-bottom in one batch. The v5 `claude_sessions` CREATE runs first, then the v6 block creates `agentic_sessions` and drops the Claude ones. Net result: fresh DBs have only the v6 tables. That's what we want.

- [ ] **Step 2: Add the v5→v6 migration branch to `crates/hippo-core/src/storage.rs`**

Locate the chain of `if version < N` blocks (there's one ending with `PRAGMA user_version = 5;` around line 249). Immediately after the v5 block closes, add:

```rust
if version < 6 {
    conn.execute_batch(
        "-- v5 → v6: rename claude_* tables to agentic_*, add harness-labeling columns
         ALTER TABLE claude_sessions RENAME TO agentic_sessions;
         ALTER TABLE claude_enrichment_queue RENAME TO agentic_enrichment_queue;
         ALTER TABLE claude_enrichment_queue RENAME COLUMN claude_session_id TO agentic_session_id;
         ALTER TABLE knowledge_node_claude_sessions RENAME TO knowledge_node_agentic_sessions;
         ALTER TABLE knowledge_node_agentic_sessions RENAME COLUMN claude_session_id TO agentic_session_id;

         ALTER TABLE agentic_sessions ADD COLUMN harness TEXT NOT NULL DEFAULT 'claude-code';
         ALTER TABLE agentic_sessions ADD COLUMN harness_version TEXT;
         ALTER TABLE agentic_sessions ADD COLUMN model TEXT;
         ALTER TABLE agentic_sessions ADD COLUMN provider TEXT;
         ALTER TABLE agentic_sessions ADD COLUMN agent TEXT;
         ALTER TABLE agentic_sessions ADD COLUMN effort TEXT;
         ALTER TABLE agentic_sessions ADD COLUMN tokens_input INTEGER;
         ALTER TABLE agentic_sessions ADD COLUMN tokens_output INTEGER;
         ALTER TABLE agentic_sessions ADD COLUMN tokens_reasoning INTEGER;
         ALTER TABLE agentic_sessions ADD COLUMN tokens_cache_read INTEGER;
         ALTER TABLE agentic_sessions ADD COLUMN tokens_cache_write INTEGER;
         ALTER TABLE agentic_sessions ADD COLUMN cost_usd REAL;

         CREATE INDEX IF NOT EXISTS idx_agentic_sessions_harness ON agentic_sessions(harness);
         CREATE INDEX IF NOT EXISTS idx_agentic_sessions_model ON agentic_sessions(model);

         CREATE TABLE IF NOT EXISTS agentic_cursor (
             harness            TEXT NOT NULL,
             source_key         TEXT NOT NULL,
             last_time_created  INTEGER NOT NULL,
             last_id            TEXT NOT NULL,
             updated_at         INTEGER NOT NULL,
             PRIMARY KEY (harness, source_key)
         );

         -- Existing indexes on claude_sessions got renamed with the table, but
         -- the index names still reference 'claude'; fix them for clarity.
         DROP INDEX IF EXISTS idx_claude_sessions_cwd;
         DROP INDEX IF EXISTS idx_claude_sessions_session;
         DROP INDEX IF EXISTS idx_claude_queue_pending;
         CREATE INDEX IF NOT EXISTS idx_agentic_sessions_cwd ON agentic_sessions(cwd);
         CREATE INDEX IF NOT EXISTS idx_agentic_sessions_session ON agentic_sessions(session_id);
         CREATE INDEX IF NOT EXISTS idx_agentic_queue_pending ON agentic_enrichment_queue(status, priority)
             WHERE status = 'pending';

         PRAGMA user_version = 6;",
    )?;
}
```

Then find the `EXPECTED_VERSION` const (search for it in the same file) and change it from `5` to `6`.

- [ ] **Step 3: Build and let existing tests find breakage**

Run: `cargo build -p hippo-core`
Expected: PASS.

- [ ] **Step 4: Run existing storage tests**

Run: `cargo test -p hippo-core storage`
Expected: some tests may fail because they pin v5. That's next task's problem — but if anything fails at build-time, fix it here.

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-core/src/schema.sql crates/hippo-core/src/storage.rs
git commit -m "feat(hippo-core): schema v6 migration (claude_* → agentic_*)"
```

### Task 2.2: Golden migration test

**Files:**
- Create: `crates/hippo-core/tests/schema_v6_migration.rs`
- Reference: `crates/hippo-core/tests/fixtures/schema_v4.sql` (existing)
- Reference: `crates/hippo-core/tests/schema_v5_migration.rs` (existing)

- [ ] **Step 1: Peek at the existing v5 migration test to copy its style**

Run: `cat crates/hippo-core/tests/schema_v5_migration.rs`
Note the pattern used. Your v6 test should follow the same layout (build v5 DB, call `storage::open_with_migrations`, assert).

- [ ] **Step 2: Write the failing v6 migration test**

`crates/hippo-core/tests/schema_v6_migration.rs`:

```rust
//! v5 → v6 migration: claude_* tables become agentic_* with new columns and
//! a default harness value of 'claude-code' for existing rows.

use rusqlite::Connection;

fn seed_v5(conn: &Connection) {
    // Load the v4 fixture first (creates tables up to v4), then simulate v5
    // by running the v4→v5 migration inline via the real helper.
    let v4_sql = include_str!("fixtures/schema_v4.sql");
    conn.execute_batch(v4_sql).unwrap();
    // Running migrations up to v5 via the library puts us at the pre-v6 state.
    let version: i64 = conn
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap();
    assert_eq!(version, 4, "fixture must start at v4");

    // Manually run v4→v5 here so this test owns the seed and doesn't depend
    // on migrate() behavior for the pre-state.
    // Copy the v5 block from storage.rs verbatim here... OR reuse helper:
    hippo_core::storage::run_migrations_for_tests(conn, 5).unwrap();
    let version: i64 = conn
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap();
    assert_eq!(version, 5);

    // Insert a claude_sessions row + queue + FK link to knowledge_nodes.
    conn.execute(
        "INSERT INTO knowledge_nodes (uuid, content, embed_text) VALUES ('kn-uuid', '{}', 'e')",
        [],
    )
    .unwrap();
    let kn_id: i64 = conn
        .query_row("SELECT id FROM knowledge_nodes LIMIT 1", [], |r| r.get(0))
        .unwrap();

    conn.execute(
        "INSERT INTO claude_sessions
            (session_id, project_dir, cwd, segment_index, start_time, end_time,
             summary_text, message_count, source_file)
         VALUES ('sess-x', 'proj', '/tmp', 0, 100, 200, 'summary', 3, '/tmp/s.jsonl')",
        [],
    )
    .unwrap();
    let cs_id: i64 = conn
        .query_row("SELECT id FROM claude_sessions LIMIT 1", [], |r| r.get(0))
        .unwrap();

    conn.execute(
        "INSERT INTO claude_enrichment_queue (claude_session_id) VALUES (?1)",
        [cs_id],
    )
    .unwrap();
    conn.execute(
        "INSERT INTO knowledge_node_claude_sessions (knowledge_node_id, claude_session_id)
         VALUES (?1, ?2)",
        [kn_id, cs_id],
    )
    .unwrap();
}

#[test]
fn migration_preserves_data_and_applies_defaults() {
    let conn = Connection::open_in_memory().unwrap();
    seed_v5(&conn);

    hippo_core::storage::run_migrations_for_tests(&conn, 6).unwrap();

    let version: i64 = conn
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap();
    assert_eq!(version, 6);

    // Row preserved under new table name
    let session_id: String = conn
        .query_row(
            "SELECT session_id FROM agentic_sessions WHERE project_dir = 'proj'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(session_id, "sess-x");

    // Default harness applied
    let harness: String = conn
        .query_row(
            "SELECT harness FROM agentic_sessions WHERE session_id = 'sess-x'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(harness, "claude-code");

    // Queue FK renamed and preserved
    let cnt: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM agentic_enrichment_queue WHERE agentic_session_id IS NOT NULL",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(cnt, 1);

    // Cursor table exists and is empty
    let cnt: i64 = conn
        .query_row("SELECT COUNT(*) FROM agentic_cursor", [], |r| r.get(0))
        .unwrap();
    assert_eq!(cnt, 0);

    // Old tables gone
    let has_old: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='claude_sessions'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(has_old, 0);
}
```

- [ ] **Step 3: Expose `run_migrations_for_tests` if missing**

Open `crates/hippo-core/src/storage.rs`. If a public test helper like `run_migrations_for_tests` does not already exist, add one that runs the migration chain up to a requested target version:

```rust
/// Test-only hook that runs the migration chain up to `target` and stops.
/// Keeps test setup from having to copy migration SQL.
#[cfg(any(test, feature = "test-utils"))]
pub fn run_migrations_for_tests(conn: &rusqlite::Connection, target: i64) -> anyhow::Result<()> {
    // Delegate to the actual migration function in this file, reusing the
    // same SQL; caller is responsible for seeding any prior state.
    let current: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
    // Re-run the existing migrate_schema() helper that implements the chain,
    // then cap the final user_version if we overshot (we won't, because
    // migrate_schema goes exactly to EXPECTED_VERSION).
    let _ = current;
    let _ = target;
    // Simplest: copy the relevant if-blocks to run incrementally. For this
    // plan we assume `migrate_schema()` is available and bumps to EXPECTED_VERSION.
    crate::storage::migrate_schema(conn)?;
    Ok(())
}
```

If `migrate_schema` is not `pub(crate)`, make it so. If it does not exist as a standalone function, refactor the migration chain in `storage.rs` into a private helper function `migrate_schema(&Connection) -> Result<()>` before adding the test hook — reuse the existing code, don't duplicate SQL.

- [ ] **Step 4: Run the migration test**

Run: `cargo test -p hippo-core --test schema_v6_migration`
Expected: PASS.

- [ ] **Step 5: Run full hippo-core tests to verify no regressions**

Run: `cargo test -p hippo-core`
Expected: all green. Fix any v5-pinned tests to expect v6 where appropriate.

- [ ] **Step 6: Commit**

```bash
git add crates/hippo-core/tests/schema_v6_migration.rs crates/hippo-core/src/storage.rs
git commit -m "test(hippo-core): golden test for v5→v6 agentic schema migration"
```

---

## Phase 3: Claude migration onto `AgenticToolCall`

### Task 3.1: Migrate `claude_session.rs` to emit `AgenticToolCall`

**Files:**
- Modify: `crates/hippo-daemon/src/claude_session.rs`
- Reference existing tests in that file (lines 527-996).

- [ ] **Step 1: Update the existing tests first (TDD: change expectations, watch them fail, then fix code)**

In `crates/hippo-daemon/src/claude_session.rs`, find every test that matches on `EventPayload::Shell(shell)` (there are several — `test_process_line_tool_result_completes_pending`, `test_process_line_tool_result_with_error`, `test_process_line_array_content_in_tool_result`, `test_build_envelope_no_result`, `test_output_truncation`, `test_multiple_tool_uses_in_single_message`). Update each to match `EventPayload::AgenticToolCall(call)` and assert the new field shapes:

```rust
match &env.payload {
    EventPayload::AgenticToolCall(call) => {
        assert_eq!(call.command, "cargo build");
        assert_eq!(call.status, AgenticStatus::Ok);
        assert_eq!(call.duration_ms, 2500);
        assert_eq!(call.cwd.to_str().unwrap(), "/projects/hippo");
        assert_eq!(call.harness, Harness::ClaudeCode);
        assert_eq!(call.provider.as_deref(), Some("anthropic"));
        assert_eq!(call.model, "claude-opus-4-7");  // derived from test fixture
        assert!(call.tool_output.is_some());
        let out = call.tool_output.as_ref().unwrap();
        assert!(out.content.contains("Compiling hippo"));
        assert!(!out.truncated);
        assert_eq!(call.git_state.as_ref().unwrap().branch.as_deref(), Some("main"));
    }
    other => panic!("expected AgenticToolCall payload, got {:?}", other),
}
```

Add `"message": { "model": "claude-opus-4-7", ... }` to the assistant-line fixtures in the tests that don't already have it. Keep existing tests that don't reference the payload (e.g., `test_format_tool_command_*`) — move them to `render.rs` in hippo-core if they're not already, or delete them from here if they're duplicated (the renderer tests in Task 1.3 already cover them).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cargo test -p hippo-daemon claude_session`
Expected: FAIL (code still emits `Shell`).

- [ ] **Step 3: Rewrite `process_line`, `build_envelope`, `PendingToolUse`**

Replace `PendingToolUse` so it carries the fields we now need at pairing time:

```rust
struct PendingToolUse {
    tool_use_id: String,
    name: String,
    input: serde_json::Value,
    timestamp: DateTime<Utc>,
    session_id: String,
    cwd: String,
    git_branch: Option<String>,
    model: String,                  // from the assistant message that made the call
    is_subagent: bool,
    parent_session_id: Option<String>,
    usage_input_tokens: u64,
    usage_output_tokens: u64,
    usage_cache_read_tokens: u64,
    usage_cache_creation_tokens: u64,
}
```

Update `process_line` to extract `model` from the assistant message's `message.model` field, and `usage.*` from `message.usage`. For subagent detection, accept an `is_subagent` / `parent_session_id` pair as parameters to `process_line` — pass them from the caller based on the file path (the daemon's tailer sees just one file at a time).

Rewrite `build_envelope` to build an `AgenticToolCall` instead of a `ShellEvent`:

```rust
fn build_envelope(
    pending: &PendingToolUse,
    result_content: Option<&str>,
    result_is_error: bool,
    result_timestamp: Option<DateTime<Utc>>,
    hostname: &str,
) -> EventEnvelope {
    let command = hippo_core::agentic::render_command(&pending.name, &pending.input);
    let status = match (result_is_error, result_timestamp) {
        (true, _) => AgenticStatus::Error,
        (false, Some(_)) => AgenticStatus::Ok,
        (false, None) => AgenticStatus::Orphaned,
    };

    let duration_ms = result_timestamp
        .map(|rt| rt.signed_duration_since(pending.timestamp).num_milliseconds().max(0) as u64)
        .unwrap_or(0);

    let session_id = Uuid::parse_str(&pending.session_id)
        .unwrap_or_else(|_| Uuid::new_v5(&Uuid::NAMESPACE_URL, pending.session_id.as_bytes()));
    let parent_session_id = pending.parent_session_id.as_deref().map(|s| {
        Uuid::parse_str(s).unwrap_or_else(|_| Uuid::new_v5(&Uuid::NAMESPACE_URL, s.as_bytes()))
    });

    let git_state = pending.git_branch.as_ref().map(|branch| GitState {
        repo: None,
        branch: Some(branch.clone()),
        commit: None,
        is_dirty: false,
    });

    let tool_output = result_content.map(|content| {
        let original_bytes = content.len();
        let (truncated_str, was_truncated) = truncate_to_bytes(content, MAX_OUTPUT_BYTES);
        CapturedOutput {
            content: truncated_str.to_string(),
            truncated: was_truncated,
            original_bytes,
        }
    });

    let tokens = Some(TokenUsage {
        input: pending.usage_input_tokens,
        output: pending.usage_output_tokens,
        reasoning: 0,
        cache_read: pending.usage_cache_read_tokens,
        cache_write: pending.usage_cache_creation_tokens,
    });

    let agent = if pending.is_subagent {
        Some("subagent".to_string())
    } else {
        None
    };

    let envelope_id = Uuid::new_v5(&Uuid::NAMESPACE_URL, pending.tool_use_id.as_bytes());

    let call = AgenticToolCall {
        session_id,
        parent_session_id,
        harness: Harness::ClaudeCode,
        harness_version: None,
        model: pending.model.clone(),
        provider: Some("anthropic".to_string()),
        agent,
        effort: None,
        tool_name: pending.name.clone(),
        tool_input: pending.input.clone(),
        command,
        tool_output,
        status,
        duration_ms,
        started_at: pending.timestamp,
        cwd: PathBuf::from(&pending.cwd),
        hostname: hostname.to_string(),
        git_state,
        tokens,
        cost_usd: None,
        redaction_count: 0,
    };

    EventEnvelope {
        envelope_id,
        producer_version: 1,
        timestamp: pending.timestamp,
        payload: EventPayload::AgenticToolCall(Box::new(call)),
    }
}
```

Remove the local `format_tool_command` function — it's now in `hippo_core::agentic::render`. Update imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cargo test -p hippo-daemon claude_session`
Expected: PASS.

- [ ] **Step 5: Run clippy**

Run: `cargo clippy -p hippo-daemon --all-targets -- -D warnings`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add crates/hippo-daemon/src/claude_session.rs
git commit -m "refactor(hippo-daemon): emit AgenticToolCall for Claude sessions"
```

### Task 3.2: Teach the daemon's storage layer to persist `AgenticToolCall`

**Files:**
- Modify: `crates/hippo-daemon/src/daemon.rs` (or wherever `handle_envelope` / event writes live — locate via `grep "EventPayload::Shell" crates/hippo-daemon/src`)

- [ ] **Step 1: Locate the payload dispatch**

Run: `grep -n "EventPayload::" crates/hippo-daemon/src/*.rs`
Identify the function that writes received envelopes to SQLite.

- [ ] **Step 2: Add an `EventPayload::AgenticToolCall` arm**

For v1 we persist agentic tool calls into the same `events` table as shell events, since the spec's storage already has a unified `events` row shape and the brain reads the source JSONL/DB directly for enrichment anyway. Map fields:

```rust
EventPayload::AgenticToolCall(call) => {
    // Store as an event row with command, stdout (tool_output), status, etc.
    // harness/model/etc. are NOT in the events table — they live only in the
    // AgenticToolCall envelope until the brain enriches them into
    // agentic_sessions. v1 scope: keep storage minimal and let the brain
    // re-read the source to build segments.
    insert_event_row(
        conn,
        envelope_id,
        &call.command,
        call.tool_output.as_ref().map(|o| o.content.as_str()),
        None,  // stderr
        match call.status {
            AgenticStatus::Ok => Some(0),
            AgenticStatus::Error => Some(1),
            AgenticStatus::Orphaned => None,
        },
        call.duration_ms,
        &call.cwd,
        &call.hostname,
        // Use the harness name in the shell column so existing queries keep working.
        call.harness.as_db_str(),
        call.git_state.as_ref(),
        call.redaction_count,
    )?;
}
```

Exact function name `insert_event_row` is illustrative — adapt to whatever helper actually exists.

- [ ] **Step 3: Run daemon tests**

Run: `cargo test -p hippo-daemon`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add crates/hippo-daemon/src/daemon.rs
git commit -m "feat(hippo-daemon): persist AgenticToolCall into events table"
```

### Task 3.3: Brain-side module rename (Claude → agentic)

**Files:**
- Rename: `brain/src/hippo_brain/claude_sessions.py` → `brain/src/hippo_brain/agentic_sessions.py`
- Modify: `brain/src/hippo_brain/enrichment.py` (imports)
- Modify: `brain/src/hippo_brain/mcp_server.py` (imports) — locate actual importers via grep
- Rename: `brain/tests/test_claude_sessions.py` → `brain/tests/test_agentic_sessions.py`

- [ ] **Step 1: Rename module file**

```bash
git mv brain/src/hippo_brain/claude_sessions.py brain/src/hippo_brain/agentic_sessions.py
git mv brain/tests/test_claude_sessions.py brain/tests/test_agentic_sessions.py
```

- [ ] **Step 2: Inside `agentic_sessions.py`, update DB table references**

Replace every occurrence of these identifiers in the file:

| Old | New |
|---|---|
| `claude_sessions` | `agentic_sessions` |
| `claude_enrichment_queue` | `agentic_enrichment_queue` |
| `claude_session_id` | `agentic_session_id` |
| `knowledge_node_claude_sessions` | `knowledge_node_agentic_sessions` |
| `ensure_claude_tables` | `ensure_agentic_tables` |
| `claim_pending_claude_segments` | `claim_pending_agentic_segments` |
| `write_claude_knowledge_node` | `write_agentic_knowledge_node` |
| `mark_claude_queue_failed` | `mark_agentic_queue_failed` |
| `build_claude_enrichment_prompt` | `build_agentic_enrichment_prompt` |

Additionally, update `ensure_agentic_tables` so that instead of asserting `user_version >= 3`, it now asserts `user_version >= 6`, and it issues the v6 CREATE TABLE statements (agentic_sessions, agentic_enrichment_queue, knowledge_node_agentic_sessions) as the Python-side fallback. Keep this helper for robustness but it's now a no-op when the Rust migration has already run.

- [ ] **Step 3: Extend `SessionSegment` with harness fields**

In `agentic_sessions.py`:

```python
@dataclass
class SessionSegment:
    session_id: str
    project_dir: str
    cwd: str
    git_branch: str | None
    segment_index: int
    start_time: int
    end_time: int
    user_prompts: list[str] = field(default_factory=list)
    assistant_texts: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    message_count: int = 0
    token_count: int = 0
    source_file: str = ""
    is_subagent: bool = False
    parent_session_id: str | None = None
    # NEW:
    harness: str = "claude-code"
    harness_version: str | None = None
    model: str | None = None
    provider: str | None = None
    agent: str | None = None
    effort: str | None = None
```

Update `extract_segments` for Claude so it populates `model` from the assistant message's `message.model` field.

Rename `iter_session_files` to `iter_claude_session_files` — reserving the unprefixed name for a dispatcher added in Phase 4/5.

- [ ] **Step 4: Add `insert_segment` support for harness fields**

Update the INSERT in `insert_segment` to write `harness`, `harness_version`, `model`, `provider`, `agent`, `effort` columns. Claude path supplies `harness='claude-code'`.

- [ ] **Step 5: Fix all call sites**

Run: `grep -rn "claude_sessions\|hippo_brain.claude" brain/src brain/tests --include="*.py"`
Fix each import and reference.

- [ ] **Step 6: Update tests to match new names + column expectations**

Inside the renamed `test_agentic_sessions.py`, apply the same rename list; update SQL assertions to check `agentic_sessions` and the new columns.

- [ ] **Step 7: Run brain tests**

Run: `uv run --project brain --extra dev pytest brain/tests/test_agentic_sessions.py -v`
Expected: PASS.

Run: `uv run --project brain --extra dev pytest brain/tests -v`
Expected: all PASS.

- [ ] **Step 8: Ruff**

Run: `uv run --project brain --extra dev ruff check brain/ && uv run --project brain --extra dev ruff format --check brain/`
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add brain/
git commit -m "refactor(brain): rename Claude session module to agentic_sessions"
```

---

## Phase 4: opencode live poller

### Task 4.1: Pin opencode schema and test fixtures

**Files:**
- Create: `crates/hippo-daemon/src/opencode_testdata.rs`

- [ ] **Step 1: Create the pinned opencode schema module**

This is a Rust module (not a SQL file) so it can be embedded and referenced from tests without a fixtures directory. The schema here must exactly match what's currently in `~/.local/share/opencode/opencode.db` — the spec confirmed the shape.

```rust
//! Pinned opencode SQLite schema for tests. Copied verbatim from
//! `sqlite3 opencode.db .schema` on 2026-04-17. Regenerate and review the
//! diff if opencode upgrades its schema.
//!
//! This module is `cfg(test)` + referenced from test binaries as
//! `#[path = "..."]` imports, keeping it out of release builds.

#![allow(dead_code)]

pub const OPENCODE_SCHEMA_PINNED_2026_04_17: &str = r#"
CREATE TABLE project (
    id text PRIMARY KEY,
    worktree text NOT NULL,
    vcs text,
    name text,
    icon_url text,
    icon_color text,
    time_created integer NOT NULL,
    time_updated integer NOT NULL,
    time_initialized integer,
    sandboxes text NOT NULL,
    commands text
);
CREATE TABLE session (
    id text PRIMARY KEY,
    project_id text NOT NULL,
    parent_id text,
    slug text NOT NULL,
    directory text NOT NULL,
    title text NOT NULL,
    version text NOT NULL,
    share_url text,
    summary_additions integer,
    summary_deletions integer,
    summary_files integer,
    summary_diffs text,
    revert text,
    permission text,
    time_created integer NOT NULL,
    time_updated integer NOT NULL,
    time_compacting integer,
    time_archived integer,
    workspace_id text
);
CREATE TABLE message (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    time_created integer NOT NULL,
    time_updated integer NOT NULL,
    data text NOT NULL
);
CREATE TABLE part (
    id text PRIMARY KEY,
    message_id text NOT NULL,
    session_id text NOT NULL,
    time_created integer NOT NULL,
    time_updated integer NOT NULL,
    data text NOT NULL
);
"#;

/// Insert a realistic tool-part row. Returns the part id.
pub fn insert_tool_part(
    conn: &rusqlite::Connection,
    session_id: &str,
    message_id: &str,
    part_id: &str,
    time_created: i64,
    time_updated: i64,
    tool: &str,
    status: &str,
    input: serde_json::Value,
    output: &str,
) -> rusqlite::Result<()> {
    let data = serde_json::json!({
        "type": "tool",
        "callID": format!("tooluse_{}", part_id),
        "tool": tool,
        "state": {
            "status": status,
            "input": input,
            "output": output,
        }
    })
    .to_string();

    conn.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        rusqlite::params![part_id, message_id, session_id, time_created, time_updated, data],
    )?;
    Ok(())
}

pub fn insert_session(
    conn: &rusqlite::Connection,
    id: &str,
    project_id: &str,
    directory: &str,
    version: &str,
    parent_id: Option<&str>,
) -> rusqlite::Result<()> {
    conn.execute(
        "INSERT INTO session (id, project_id, parent_id, slug, directory, title, version,
             sandboxes, time_created, time_updated)
         VALUES (?1, ?2, ?3, 'slug', ?4, 'title', ?5, '[]', 0, 0)",
        rusqlite::params![id, project_id, parent_id, directory, version],
    )?;
    Ok(())
}

pub fn insert_assistant_message(
    conn: &rusqlite::Connection,
    id: &str,
    session_id: &str,
    time_created: i64,
    model_id: &str,
    provider_id: &str,
    agent: &str,
) -> rusqlite::Result<()> {
    let data = serde_json::json!({
        "role": "assistant",
        "mode": agent,
        "agent": agent,
        "path": { "cwd": "/tmp" },
        "modelID": model_id,
        "providerID": provider_id,
        "tokens": {
            "input": 100, "output": 50, "reasoning": 10,
            "cache": { "read": 0, "write": 0 }
        },
        "cost": 0.001,
        "time": { "created": time_created, "completed": time_created + 1000 }
    })
    .to_string();
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data)
         VALUES (?1, ?2, ?3, ?3, ?4)",
        rusqlite::params![id, session_id, time_created, data],
    )?;
    Ok(())
}
```

- [ ] **Step 2: Wire it into the daemon crate as a conditional module**

In `crates/hippo-daemon/src/lib.rs`, add:

```rust
#[cfg(test)]
pub(crate) mod opencode_testdata;
```

- [ ] **Step 3: Build**

Run: `cargo build -p hippo-daemon --tests`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add crates/hippo-daemon/src/opencode_testdata.rs crates/hippo-daemon/src/lib.rs
git commit -m "test(hippo-daemon): pin opencode schema + seed helpers"
```

### Task 4.2: Write the opencode part-extraction logic (tests first)

**Files:**
- Create: `crates/hippo-daemon/src/opencode_session.rs`
- Modify: `crates/hippo-daemon/src/lib.rs`

- [ ] **Step 1: Write the failing tests**

Create `crates/hippo-daemon/src/opencode_session.rs`:

```rust
//! opencode live session ingester: polls opencode's SQLite DB for new `tool`
//! parts and emits `AgenticToolCall` envelopes.

use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, Result};
use chrono::{DateTime, TimeZone, Utc};
use hippo_core::agentic::{AgenticStatus, AgenticToolCall, Harness, TokenUsage};
use hippo_core::events::{CapturedOutput, EventEnvelope, EventPayload};
use rusqlite::{Connection, OpenFlags};
use tracing::{debug, error, info, warn};
use uuid::Uuid;

const MAX_OUTPUT_BYTES: usize = 4096;

/// Cursor into opencode's event stream: we only consider rows strictly after
/// (last_time_created, last_id) to achieve exactly-once-ish semantics.
#[derive(Debug, Clone, Default)]
pub struct Cursor {
    pub last_time_created: i64,
    pub last_id: String,
}

/// A row joined from opencode's part + message + session tables.
#[derive(Debug)]
pub(crate) struct JoinedPartRow {
    pub part_id: String,
    pub part_time_created: i64,
    pub part_time_updated: i64,
    pub part_data: serde_json::Value,
    pub msg_data: serde_json::Value,
    pub session_id: String,
    pub directory: String,
    pub version: String,
    pub parent_id: Option<String>,
}

/// Convert a joined row into an `AgenticToolCall` envelope. Returns Ok(None)
/// for non-tool part types (caller can skip).
pub(crate) fn build_envelope_from_row(
    row: &JoinedPartRow,
    hostname: &str,
) -> Result<Option<EventEnvelope>> {
    let part_type = row
        .part_data
        .get("type")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if part_type != "tool" {
        return Ok(None);
    }

    let tool_name = row
        .part_data
        .get("tool")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();

    let state = row.part_data.get("state");
    let tool_input = state
        .and_then(|s| s.get("input"))
        .cloned()
        .unwrap_or_else(|| serde_json::json!({}));
    let raw_output = state
        .and_then(|s| s.get("output"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let status_str = state
        .and_then(|s| s.get("status"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let status = match status_str {
        "completed" => AgenticStatus::Ok,
        "error" => AgenticStatus::Error,
        _ => AgenticStatus::Orphaned,
    };

    let command = hippo_core::agentic::render_command(&tool_name, &tool_input);

    let (truncated, out) = truncate_to_bytes(raw_output, MAX_OUTPUT_BYTES);
    let tool_output = if raw_output.is_empty() {
        None
    } else {
        Some(CapturedOutput {
            content: out.to_string(),
            truncated,
            original_bytes: raw_output.len(),
        })
    };

    let duration_ms = (row.part_time_updated - row.part_time_created).max(0) as u64;

    let session_id = Uuid::parse_str(&row.session_id)
        .unwrap_or_else(|_| Uuid::new_v5(&Uuid::NAMESPACE_URL, row.session_id.as_bytes()));
    let parent_session_id = row.parent_id.as_deref().map(|s| {
        Uuid::parse_str(s).unwrap_or_else(|_| Uuid::new_v5(&Uuid::NAMESPACE_URL, s.as_bytes()))
    });

    let model = row
        .msg_data
        .get("modelID")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let provider = row
        .msg_data
        .get("providerID")
        .and_then(|v| v.as_str())
        .map(String::from);
    let agent = row
        .msg_data
        .get("agent")
        .or_else(|| row.msg_data.get("mode"))
        .and_then(|v| v.as_str())
        .map(String::from);

    let tokens = row.msg_data.get("tokens").map(|t| TokenUsage {
        input: t.get("input").and_then(|v| v.as_u64()).unwrap_or(0),
        output: t.get("output").and_then(|v| v.as_u64()).unwrap_or(0),
        reasoning: t.get("reasoning").and_then(|v| v.as_u64()).unwrap_or(0),
        cache_read: t
            .pointer("/cache/read")
            .and_then(|v| v.as_u64())
            .unwrap_or(0),
        cache_write: t
            .pointer("/cache/write")
            .and_then(|v| v.as_u64())
            .unwrap_or(0),
    });

    let cost_usd = row.msg_data.get("cost").and_then(|v| v.as_f64());

    let cwd = row
        .msg_data
        .pointer("/path/cwd")
        .and_then(|v| v.as_str())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(&row.directory));

    let started_at: DateTime<Utc> = Utc
        .timestamp_millis_opt(row.part_time_created)
        .single()
        .unwrap_or_else(Utc::now);

    let envelope_id = Uuid::new_v5(&Uuid::NAMESPACE_URL, row.part_id.as_bytes());

    let call = AgenticToolCall {
        session_id,
        parent_session_id,
        harness: Harness::Opencode,
        harness_version: Some(row.version.clone()),
        model,
        provider,
        agent,
        effort: None,
        tool_name,
        tool_input,
        command,
        tool_output,
        status,
        duration_ms,
        started_at,
        cwd,
        hostname: hostname.to_string(),
        git_state: None,
        tokens,
        cost_usd,
        redaction_count: 0,
    };

    Ok(Some(EventEnvelope {
        envelope_id,
        producer_version: 1,
        timestamp: started_at,
        payload: EventPayload::AgenticToolCall(Box::new(call)),
    }))
}

fn truncate_to_bytes(s: &str, max_bytes: usize) -> (bool, &str) {
    if s.len() <= max_bytes {
        return (false, s);
    }
    let mut end = max_bytes;
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    (true, &s[..end])
}

// Poller loop and cursor persistence come in Task 4.3.

#[cfg(test)]
mod tests {
    use super::*;
    use crate::opencode_testdata::{
        insert_assistant_message, insert_session, insert_tool_part,
        OPENCODE_SCHEMA_PINNED_2026_04_17,
    };
    use serde_json::json;

    fn fresh_opencode_db() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch(OPENCODE_SCHEMA_PINNED_2026_04_17).unwrap();
        c
    }

    fn row_for(
        part_data: serde_json::Value,
        msg_data: serde_json::Value,
    ) -> JoinedPartRow {
        JoinedPartRow {
            part_id: "part-abc".into(),
            part_time_created: 1_000_000,
            part_time_updated: 1_001_500,
            part_data,
            msg_data,
            session_id: "ses_xyz".into(),
            directory: "/home/me/proj".into(),
            version: "1.4.6".into(),
            parent_id: None,
        }
    }

    #[test]
    fn tool_part_completed_maps_to_ok_call() {
        let row = row_for(
            json!({
                "type": "tool",
                "tool": "Bash",
                "callID": "c1",
                "state": {
                    "status": "completed",
                    "input": {"command": "ls /tmp"},
                    "output": "file1\nfile2"
                }
            }),
            json!({
                "modelID": "claude-opus-4-7",
                "providerID": "anthropic",
                "agent": "build",
                "tokens": {"input": 10, "output": 5, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                "cost": 0.001,
                "path": {"cwd": "/home/me/proj"}
            }),
        );

        let env = build_envelope_from_row(&row, "host").unwrap().unwrap();
        match env.payload {
            EventPayload::AgenticToolCall(call) => {
                assert_eq!(call.harness, Harness::Opencode);
                assert_eq!(call.harness_version.as_deref(), Some("1.4.6"));
                assert_eq!(call.model, "claude-opus-4-7");
                assert_eq!(call.provider.as_deref(), Some("anthropic"));
                assert_eq!(call.agent.as_deref(), Some("build"));
                assert_eq!(call.command, "ls /tmp");
                assert_eq!(call.status, AgenticStatus::Ok);
                assert_eq!(call.duration_ms, 1500);
                assert_eq!(
                    call.tool_output.as_ref().unwrap().content,
                    "file1\nfile2"
                );
                assert_eq!(call.tokens.as_ref().unwrap().input, 10);
                assert_eq!(call.cost_usd, Some(0.001));
            }
            _ => panic!("expected AgenticToolCall"),
        }
    }

    #[test]
    fn tool_part_error_maps_to_error_status() {
        let row = row_for(
            json!({
                "type": "tool", "tool": "Bash", "callID": "c1",
                "state": { "status": "error", "input": {"command": "false"}, "output": "boom" }
            }),
            json!({"modelID": "m", "providerID": "p"}),
        );
        let env = build_envelope_from_row(&row, "h").unwrap().unwrap();
        match env.payload {
            EventPayload::AgenticToolCall(c) => assert_eq!(c.status, AgenticStatus::Error),
            _ => panic!(),
        }
    }

    #[test]
    fn non_tool_part_returns_none() {
        let row = row_for(json!({"type": "text", "text": "hi"}), json!({}));
        assert!(build_envelope_from_row(&row, "h").unwrap().is_none());
    }

    #[test]
    fn large_output_truncated() {
        let big = "x".repeat(5000);
        let row = row_for(
            json!({
                "type": "tool", "tool": "Read", "callID": "c",
                "state": { "status": "completed", "input": {"file_path": "/f"}, "output": big }
            }),
            json!({"modelID": "m"}),
        );
        let env = build_envelope_from_row(&row, "h").unwrap().unwrap();
        if let EventPayload::AgenticToolCall(c) = env.payload {
            let out = c.tool_output.unwrap();
            assert!(out.truncated);
            assert_eq!(out.content.len(), MAX_OUTPUT_BYTES);
            assert_eq!(out.original_bytes, 5000);
        } else { panic!(); }
    }

    #[test]
    fn deterministic_envelope_id_across_runs() {
        let row = row_for(
            json!({"type": "tool", "tool": "Bash", "callID": "c",
                   "state": {"status": "completed", "input": {"command": "x"}, "output": ""}}),
            json!({"modelID": "m"}),
        );
        let e1 = build_envelope_from_row(&row, "h").unwrap().unwrap();
        let e2 = build_envelope_from_row(&row, "h").unwrap().unwrap();
        assert_eq!(e1.envelope_id, e2.envelope_id);
    }

    #[test]
    fn live_db_can_be_seeded_and_read() {
        let c = fresh_opencode_db();
        insert_session(&c, "ses_abc", "proj1", "/home/me", "1.4.6", None).unwrap();
        insert_assistant_message(
            &c, "msg1", "ses_abc", 1_000_000,
            "claude-opus-4-7", "anthropic", "build",
        ).unwrap();
        insert_tool_part(
            &c, "ses_abc", "msg1", "part-1",
            1_000_100, 1_000_500,
            "Bash", "completed",
            json!({"command": "echo hi"}),
            "hi\n",
        ).unwrap();

        let cnt: i64 = c
            .query_row("SELECT COUNT(*) FROM part", [], |r| r.get(0))
            .unwrap();
        assert_eq!(cnt, 1);
    }
}
```

- [ ] **Step 2: Wire the module into the crate**

In `crates/hippo-daemon/src/lib.rs`, add next to the other modules:

```rust
pub mod opencode_session;
```

- [ ] **Step 3: Run tests**

Run: `cargo test -p hippo-daemon --lib opencode_session`
Expected: 6 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add crates/hippo-daemon/src/opencode_session.rs crates/hippo-daemon/src/lib.rs
git commit -m "feat(hippo-daemon): opencode part → AgenticToolCall mapping"
```

### Task 4.3: Cursor persistence + `data_version` gate

**Files:**
- Modify: `crates/hippo-daemon/src/opencode_session.rs`

- [ ] **Step 1: Write tests for cursor advancement and data_version gating**

Append to the `tests` module in `opencode_session.rs`:

```rust
#[test]
fn fetch_rows_respects_cursor_tie_breaking() {
    let c = fresh_opencode_db();
    insert_session(&c, "s", "p", "/d", "1.4.6", None).unwrap();
    insert_assistant_message(&c, "m", "s", 100, "model", "prov", "build").unwrap();
    // Two parts at the same time_created:
    insert_tool_part(&c, "s", "m", "p-a", 100, 100, "Bash", "completed",
        json!({"command": "a"}), "").unwrap();
    insert_tool_part(&c, "s", "m", "p-b", 100, 100, "Bash", "completed",
        json!({"command": "b"}), "").unwrap();

    let cursor = Cursor { last_time_created: 0, last_id: String::new() };
    let rows = fetch_new_rows(&c, &cursor, 10).unwrap();
    assert_eq!(rows.len(), 2);

    // After advancing past the first row, only the second should come back.
    let advanced = Cursor {
        last_time_created: rows[0].part_time_created,
        last_id: rows[0].part_id.clone(),
    };
    let rows2 = fetch_new_rows(&c, &advanced, 10).unwrap();
    assert_eq!(rows2.len(), 1);
    assert_eq!(rows2[0].part_id, "p-b");
}

#[test]
fn data_version_short_circuits_unchanged_db() {
    let c = fresh_opencode_db();
    let v1 = read_data_version(&c).unwrap();
    let v2 = read_data_version(&c).unwrap();
    assert_eq!(v1, v2);

    insert_session(&c, "s", "p", "/d", "1.4.6", None).unwrap();
    let v3 = read_data_version(&c).unwrap();
    assert_ne!(v2, v3);
}
```

- [ ] **Step 2: Run tests to verify failure**

Run: `cargo test -p hippo-daemon --lib opencode_session::tests::fetch_rows_respects_cursor_tie_breaking`
Expected: FAIL (functions don't exist yet).

- [ ] **Step 3: Implement `fetch_new_rows` and `read_data_version`**

Add to `opencode_session.rs`:

```rust
pub(crate) fn read_data_version(conn: &Connection) -> Result<i64> {
    Ok(conn.query_row("PRAGMA data_version", [], |r| r.get(0))?)
}

pub(crate) fn fetch_new_rows(
    conn: &Connection,
    cursor: &Cursor,
    limit: u32,
) -> Result<Vec<JoinedPartRow>> {
    let mut stmt = conn.prepare(
        "SELECT p.id, p.time_created, p.time_updated, p.data,
                m.data, s.id, s.directory, s.version, s.parent_id
           FROM part p
           JOIN message m ON p.message_id = m.id
           JOIN session s ON p.session_id = s.id
          WHERE (p.time_created > ?1)
             OR (p.time_created = ?1 AND p.id > ?2)
          ORDER BY p.time_created ASC, p.id ASC
          LIMIT ?3",
    )?;
    let rows = stmt
        .query_map(
            rusqlite::params![cursor.last_time_created, cursor.last_id, limit],
            |r| {
                let part_data: String = r.get(3)?;
                let msg_data: String = r.get(4)?;
                Ok(JoinedPartRow {
                    part_id: r.get(0)?,
                    part_time_created: r.get(1)?,
                    part_time_updated: r.get(2)?,
                    part_data: serde_json::from_str(&part_data).unwrap_or(serde_json::Value::Null),
                    msg_data: serde_json::from_str(&msg_data).unwrap_or(serde_json::Value::Null),
                    session_id: r.get(5)?,
                    directory: r.get(6)?,
                    version: r.get(7)?,
                    parent_id: r.get(8)?,
                })
            },
        )?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    Ok(rows)
}
```

- [ ] **Step 4: Run tests**

Run: `cargo test -p hippo-daemon --lib opencode_session`
Expected: all 8 tests PASS.

- [ ] **Step 5: Implement cursor persistence against hippo's own DB**

Add:

```rust
pub fn load_cursor(hippo_conn: &Connection, source_key: &str) -> Result<Cursor> {
    let mut stmt = hippo_conn.prepare(
        "SELECT last_time_created, last_id
           FROM agentic_cursor
          WHERE harness = 'opencode' AND source_key = ?1",
    )?;
    let row = stmt.query_row([source_key], |r| Ok(Cursor {
        last_time_created: r.get(0)?,
        last_id: r.get(1)?,
    }));
    match row {
        Ok(c) => Ok(c),
        Err(rusqlite::Error::QueryReturnedNoRows) => Ok(Cursor::default()),
        Err(e) => Err(e.into()),
    }
}

pub fn save_cursor(hippo_conn: &Connection, source_key: &str, cursor: &Cursor) -> Result<()> {
    let now = chrono::Utc::now().timestamp_millis();
    hippo_conn.execute(
        "INSERT INTO agentic_cursor (harness, source_key, last_time_created, last_id, updated_at)
             VALUES ('opencode', ?1, ?2, ?3, ?4)
         ON CONFLICT(harness, source_key) DO UPDATE
             SET last_time_created = excluded.last_time_created,
                 last_id = excluded.last_id,
                 updated_at = excluded.updated_at",
        rusqlite::params![source_key, cursor.last_time_created, cursor.last_id, now],
    )?;
    Ok(())
}
```

- [ ] **Step 6: Add test for cursor persistence**

Append:

```rust
#[test]
fn cursor_roundtrips_through_hippo_db() {
    let c = Connection::open_in_memory().unwrap();
    c.execute_batch(
        "CREATE TABLE agentic_cursor (
             harness TEXT NOT NULL,
             source_key TEXT NOT NULL,
             last_time_created INTEGER NOT NULL,
             last_id TEXT NOT NULL,
             updated_at INTEGER NOT NULL,
             PRIMARY KEY (harness, source_key)
         );",
    ).unwrap();

    let saved = Cursor { last_time_created: 12345, last_id: "p-42".into() };
    save_cursor(&c, "inode-99", &saved).unwrap();
    let loaded = load_cursor(&c, "inode-99").unwrap();
    assert_eq!(loaded.last_time_created, 12345);
    assert_eq!(loaded.last_id, "p-42");

    // Missing source → default.
    let missing = load_cursor(&c, "other").unwrap();
    assert_eq!(missing.last_time_created, 0);
    assert_eq!(missing.last_id, "");
}
```

- [ ] **Step 7: Run tests + clippy**

Run: `cargo test -p hippo-daemon --lib opencode_session`
Run: `cargo clippy -p hippo-daemon --all-targets -- -D warnings`
Both: PASS.

- [ ] **Step 8: Commit**

```bash
git add crates/hippo-daemon/src/opencode_session.rs
git commit -m "feat(hippo-daemon): opencode cursor + data_version gating"
```

### Task 4.4: Poller loop and daemon wiring

**Files:**
- Modify: `crates/hippo-daemon/src/opencode_session.rs`
- Modify: `crates/hippo-daemon/src/daemon.rs`
- Modify: `crates/hippo-core/src/config.rs` (or wherever config structs live — locate with `grep "pub struct.*Config" crates/hippo-core/src`)

- [ ] **Step 1: Extend config**

Locate the main config struct (likely named `Config` or similar in `hippo-core/src/config.rs`). Add:

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct OpencodeConfig {
    pub enabled: bool,
    pub db_path: Option<PathBuf>,
    pub poll_interval_ms: u64,
}

impl Default for OpencodeConfig {
    fn default() -> Self {
        Self { enabled: false, db_path: None, poll_interval_ms: 1000 }
    }
}

// In the root Config struct, add:
#[serde(default)]
pub opencode: OpencodeConfig,
```

Default path resolution lives in a helper:

```rust
impl OpencodeConfig {
    pub fn resolved_db_path(&self) -> PathBuf {
        if let Some(p) = &self.db_path { return p.clone(); }
        let base = std::env::var_os("XDG_DATA_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                let home = std::env::var_os("HOME").unwrap_or_default();
                PathBuf::from(home).join(".local/share")
            });
        base.join("opencode").join("opencode.db")
    }
}
```

- [ ] **Step 2: Add poller loop to `opencode_session.rs`**

```rust
use crate::commands::send_event_fire_and_forget;

pub async fn run_poller(
    opencode_db_path: PathBuf,
    hippo_db_path: PathBuf,
    socket_path: PathBuf,
    poll_interval_ms: u64,
    send_timeout_ms: u64,
) -> Result<()> {
    let source_key = inode_source_key(&opencode_db_path)?;
    let hostname = hostname::get()
        .map(|h| h.to_string_lossy().to_string())
        .unwrap_or_else(|_| "unknown".to_string());

    info!(
        path = %opencode_db_path.display(),
        source_key,
        "opencode poller starting"
    );

    let ocode = Connection::open_with_flags(
        &opencode_db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .with_context(|| format!("open opencode db {}", opencode_db_path.display()))?;
    ocode.busy_timeout(Duration::from_secs(5))?;

    let hippo = Connection::open(&hippo_db_path)
        .with_context(|| format!("open hippo db {}", hippo_db_path.display()))?;

    let mut cursor = load_cursor(&hippo, &source_key)?;
    let mut last_data_version = 0i64;

    loop {
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {
                info!("opencode poller shutting down");
                break;
            }
            _ = tokio::time::sleep(Duration::from_millis(poll_interval_ms)) => {
                let dv = match read_data_version(&ocode) {
                    Ok(v) => v,
                    Err(e) => { warn!(%e, "data_version read failed"); continue; }
                };
                if dv == last_data_version {
                    continue;
                }
                last_data_version = dv;

                let rows = match fetch_new_rows(&ocode, &cursor, 500) {
                    Ok(r) => r,
                    Err(e) => { error!(%e, "fetch_new_rows failed"); continue; }
                };
                if rows.is_empty() { continue; }

                let mut sent = 0usize;
                for row in &rows {
                    match build_envelope_from_row(row, &hostname) {
                        Ok(Some(env)) => {
                            if let Err(e) = send_event_fire_and_forget(
                                &socket_path, &env, send_timeout_ms
                            ).await {
                                error!(%e, "failed to send opencode envelope");
                            } else {
                                sent += 1;
                            }
                        }
                        Ok(None) => {} // non-tool part
                        Err(e) => warn!(%e, "skip row"),
                    }
                }

                if let Some(last) = rows.last() {
                    cursor.last_time_created = last.part_time_created;
                    cursor.last_id = last.part_id.clone();
                    if let Err(e) = save_cursor(&hippo, &source_key, &cursor) {
                        error!(%e, "cursor save failed");
                    }
                }

                if sent > 0 {
                    debug!(sent, total_rows = rows.len(), "opencode poll tick");
                }
            }
        }
    }
    Ok(())
}

fn inode_source_key(path: &Path) -> Result<String> {
    use std::os::unix::fs::MetadataExt;
    let meta = std::fs::metadata(path)
        .with_context(|| format!("stat {}", path.display()))?;
    Ok(meta.ino().to_string())
}
```

- [ ] **Step 3: Spawn the poller from `daemon.rs`**

Locate the daemon's `run` function (where browser/gh tasks are spawned — `grep -n "tokio::spawn" crates/hippo-daemon/src/daemon.rs`). Add a conditional spawn when `config.opencode.enabled`:

```rust
if config.opencode.enabled {
    let db = config.opencode.resolved_db_path();
    let hippo_db = /* existing hippo db path */.clone();
    let socket = /* socket path */.clone();
    let poll_ms = config.opencode.poll_interval_ms;
    let send_timeout = /* existing send_timeout_ms */;
    tokio::spawn(async move {
        if let Err(e) = crate::opencode_session::run_poller(
            db, hippo_db, socket, poll_ms, send_timeout,
        ).await {
            tracing::error!(%e, "opencode poller exited");
        }
    });
}
```

Use real identifiers from the surrounding code; don't add new ones.

- [ ] **Step 4: Default config template**

Update the default config TOML template at `config/config.toml` (or wherever it lives — `grep -rn "\[browser\]" config/`). Append:

```toml
[opencode]
enabled = false
# db_path = "~/.local/share/opencode/opencode.db"
poll_interval_ms = 1000
```

- [ ] **Step 5: Build full workspace + clippy**

Run: `cargo build --workspace`
Run: `cargo clippy --workspace --all-targets -- -D warnings`
Both: PASS.

- [ ] **Step 6: Run full test suite**

Run: `cargo test --workspace`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add crates/
git commit -m "feat(hippo-daemon): opencode poller with data_version gating"
```

### Task 4.5: Brain-side opencode reader

**Files:**
- Create: `brain/src/hippo_brain/opencode_reader.py`
- Modify: `brain/src/hippo_brain/agentic_sessions.py`
- Create: `brain/tests/test_opencode_reader.py`

- [ ] **Step 1: Write the failing test**

`brain/tests/test_opencode_reader.py`:

```python
import json
import sqlite3

from hippo_brain.opencode_reader import iter_opencode_segments

OPENCODE_SCHEMA = """
CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT, vcs TEXT, name TEXT,
    icon_url TEXT, icon_color TEXT, time_created INTEGER, time_updated INTEGER,
    time_initialized INTEGER, sandboxes TEXT, commands TEXT);
CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT,
    slug TEXT, directory TEXT, title TEXT, version TEXT, share_url TEXT,
    summary_additions INTEGER, summary_deletions INTEGER, summary_files INTEGER,
    summary_diffs TEXT, revert TEXT, permission TEXT,
    time_created INTEGER, time_updated INTEGER, time_compacting INTEGER,
    time_archived INTEGER, workspace_id TEXT);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,
    time_created INTEGER, time_updated INTEGER, data TEXT);
CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
    time_created INTEGER, time_updated INTEGER, data TEXT);
"""

def seed(conn):
    conn.executescript(OPENCODE_SCHEMA)
    conn.execute(
        "INSERT INTO session VALUES ('ses1','p1',NULL,'slug','/home/p','t','1.4.6',"
        "NULL,NULL,NULL,NULL,NULL,NULL,NULL,1000,2000,NULL,NULL,NULL)"
    )
    conn.execute(
        "INSERT INTO message VALUES ('m1','ses1',1000,1000,?)",
        (json.dumps({
            "role": "assistant", "modelID": "claude-opus-4-7",
            "providerID": "anthropic", "agent": "build",
            "tokens": {"input": 10, "output": 5, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            "path": {"cwd": "/home/p"},
        }),),
    )
    conn.execute(
        "INSERT INTO part VALUES ('pA','m1','ses1',1100,1200,?)",
        (json.dumps({
            "type": "tool", "tool": "Bash", "callID": "c1",
            "state": {"status": "completed", "input": {"command": "ls"}, "output": "f1"},
        }),),
    )
    conn.execute(
        "INSERT INTO part VALUES ('pB','m1','ses1',1300,1400,?)",
        (json.dumps({
            "type": "text", "text": "Let me run ls."
        }),),
    )
    conn.commit()


def test_segments_include_tool_and_text_parts():
    conn = sqlite3.connect(":memory:")
    seed(conn)
    segments = list(iter_opencode_segments(conn))
    assert len(segments) == 1
    seg = segments[0]
    assert seg.harness == "opencode"
    assert seg.model == "claude-opus-4-7"
    assert seg.provider == "anthropic"
    assert seg.agent == "build"
    assert seg.harness_version == "1.4.6"
    assert seg.cwd == "/home/p"
    assert len(seg.tool_calls) == 1
    assert seg.tool_calls[0]["name"] == "Bash"
    assert "ls" in seg.tool_calls[0]["summary"]
    assert any("ls" in t for t in seg.assistant_texts)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --project brain --extra dev pytest brain/tests/test_opencode_reader.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `brain/src/hippo_brain/opencode_reader.py`**

```python
"""Read opencode SQLite to produce SessionSegment objects for enrichment."""

from __future__ import annotations

import json
import sqlite3
from typing import Iterator

from hippo_brain.agentic_sessions import SessionSegment, TASK_GAP_MS

# Same 5-minute gap segmentation rule as Claude; applied below.


def iter_opencode_segments(conn: sqlite3.Connection) -> Iterator[SessionSegment]:
    """Yield SessionSegment per opencode session, splitting at 5-min user gaps.

    Expects conn to be opened against opencode.db (read-only is fine).
    """
    sessions = conn.execute(
        "SELECT id, directory, version, parent_id FROM session ORDER BY time_created ASC"
    ).fetchall()

    for session_id, directory, version, parent_id in sessions:
        yield from _segments_for_session(
            conn, session_id, directory, version, parent_id
        )


def _segments_for_session(
    conn: sqlite3.Connection,
    session_id: str,
    directory: str,
    version: str,
    parent_id: str | None,
) -> Iterator[SessionSegment]:
    rows = conn.execute(
        """SELECT p.id, p.time_created, p.data, m.data
             FROM part p
             JOIN message m ON p.message_id = m.id
            WHERE p.session_id = ?
            ORDER BY p.time_created ASC, p.id ASC""",
        (session_id,),
    ).fetchall()

    if not rows:
        return

    current: SessionSegment | None = None
    last_user_time = 0
    idx = 0
    # opencode has no explicit "user text" part distinct from text parts; for
    # segmentation we use the time_created of the first tool/text part in a
    # message group as a proxy for the user's action boundary.

    for part_id, ts, part_json, msg_json in rows:
        part = json.loads(part_json)
        msg = json.loads(msg_json)
        model_id = msg.get("modelID", "")
        provider_id = msg.get("providerID")
        agent = msg.get("agent") or msg.get("mode")
        msg_cwd = msg.get("path", {}).get("cwd", directory)

        if current is None:
            current = _new_segment(
                session_id, directory, version, parent_id,
                model_id, provider_id, agent, msg_cwd, idx, ts,
            )
            idx += 1

        # 5-min gap boundary (only when we see a new message_id start — proxy).
        if ts - current.end_time > TASK_GAP_MS and (current.tool_calls or current.assistant_texts):
            yield current
            current = _new_segment(
                session_id, directory, version, parent_id,
                model_id, provider_id, agent, msg_cwd, idx, ts,
            )
            idx += 1

        current.end_time = max(current.end_time, ts)
        current.message_count += 1
        if msg_cwd:
            current.cwd = msg_cwd

        ptype = part.get("type")
        if ptype == "tool":
            state = part.get("state", {})
            current.tool_calls.append({
                "name": part.get("tool", ""),
                "summary": _summarize_tool(part.get("tool", ""), state.get("input", {})),
            })
        elif ptype == "text":
            text = (part.get("text") or "").strip()
            if text and len(text) > 20:
                current.assistant_texts.append(text[:300])

        last_user_time = ts  # noqa: F841 — retained for future fine-grained segmentation

    if current and (current.tool_calls or current.assistant_texts):
        yield current


def _new_segment(
    session_id: str,
    directory: str,
    version: str,
    parent_id: str | None,
    model: str,
    provider: str | None,
    agent: str | None,
    cwd: str,
    idx: int,
    start_ts: int,
) -> SessionSegment:
    return SessionSegment(
        session_id=session_id,
        project_dir=directory,
        cwd=cwd or directory,
        git_branch=None,
        segment_index=idx,
        start_time=start_ts,
        end_time=start_ts,
        source_file=f"opencode://{session_id}",
        is_subagent=parent_id is not None,
        parent_session_id=parent_id,
        harness="opencode",
        harness_version=version,
        model=model or None,
        provider=provider,
        agent=agent,
        effort=None,
    )


def _summarize_tool(name: str, inp: dict) -> str:
    if name == "Bash":
        return (inp.get("command") or "")[:200]
    if name in ("Read", "Write", "Edit"):
        return inp.get("file_path") or ""
    if name == "Grep":
        return inp.get("pattern") or ""
    if name == "Glob":
        return inp.get("pattern") or ""
    if name == "skill":
        return inp.get("name") or ""
    # fallback: first key
    for k, v in inp.items():
        return f"{k}={str(v)[:80]}"
    return ""
```

- [ ] **Step 4: Run tests**

Run: `uv run --project brain --extra dev pytest brain/tests/test_opencode_reader.py -v`
Expected: PASS.

- [ ] **Step 5: Ruff**

Run: `uv run --project brain --extra dev ruff check brain/ && uv run --project brain --extra dev ruff format --check brain/`
Expected: clean (if not, run `ruff format brain/`).

- [ ] **Step 6: Commit**

```bash
git add brain/
git commit -m "feat(brain): opencode SQLite reader → SessionSegment"
```

### Task 4.6: Dispatcher for `iter_session_files` across harnesses

**Files:**
- Modify: `brain/src/hippo_brain/agentic_sessions.py`
- Modify: `brain/src/hippo_brain/enrichment.py` (or wherever discovery is triggered — grep)

- [ ] **Step 1: Add the dispatcher**

In `agentic_sessions.py`, add a top-level function:

```python
from pathlib import Path
import sqlite3
from collections.abc import Iterator


def iter_all_agentic_segments(
    *,
    claude_projects_dir: Path | None,
    opencode_db_path: Path | None,
    # codex_root: Path | None — added in Phase 5
) -> Iterator[SessionSegment]:
    """Yield segments from every enabled agentic source in stable order.

    Order: claude files (as today), then opencode sessions. Codex is appended
    in Phase 5.
    """
    if claude_projects_dir is not None:
        for sf in iter_claude_session_files(claude_projects_dir):
            yield from extract_segments(sf)

    if opencode_db_path is not None and opencode_db_path.exists():
        from hippo_brain.opencode_reader import iter_opencode_segments
        with sqlite3.connect(f"file:{opencode_db_path}?mode=ro", uri=True) as conn:
            yield from iter_opencode_segments(conn)
```

- [ ] **Step 2: Update enrichment entrypoint to call the dispatcher**

In `enrichment.py` (or whichever module orchestrates discovery → insert_segment), replace the Claude-only call with the dispatcher:

```python
for seg in iter_all_agentic_segments(
    claude_projects_dir=settings.claude_projects_dir,
    opencode_db_path=settings.opencode_db_path if settings.opencode_enabled else None,
):
    insert_segment(conn, seg)
```

Expose `opencode_db_path` and `opencode_enabled` through the existing settings loader.

- [ ] **Step 3: Run all brain tests**

Run: `uv run --project brain --extra dev pytest brain/tests -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add brain/
git commit -m "feat(brain): multi-harness segment dispatcher"
```

---

## Phase 5: Codex historical batch importer

### Task 5.1: Codex JSONL parser (daemon side)

**Files:**
- Create: `crates/hippo-daemon/src/codex_session.rs`
- Modify: `crates/hippo-daemon/src/lib.rs`

- [ ] **Step 1: Write the failing tests**

`crates/hippo-daemon/src/codex_session.rs`:

```rust
//! Codex rollout JSONL batch importer.
//!
//! Input: `~/.codex/archived_sessions/rollout-*.jsonl` (and
//! `~/.codex/sessions/YYYY/MM/DD/*.jsonl`). Each file starts with a
//! `session_meta` line, followed by `response_item` lines whose payloads pair
//! `function_call`/`custom_tool_call` with matching `_output` by `call_id`.

use std::collections::HashMap;
use std::io::BufRead;
use std::path::Path;

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use hippo_core::agentic::{AgenticStatus, AgenticToolCall, Harness, TokenUsage};
use hippo_core::events::{CapturedOutput, EventEnvelope, EventPayload};
use tracing::warn;
use uuid::Uuid;

const MAX_OUTPUT_BYTES: usize = 4096;

#[derive(Debug, Clone)]
struct SessionMeta {
    id: String,
    cwd: String,
    cli_version: Option<String>,
    originator: Option<String>,
    model_provider: Option<String>,
    model: Option<String>,
    effort: Option<String>,
}

#[derive(Debug)]
struct PendingCall {
    call_id: String,
    name: String,
    arguments: serde_json::Value,
    timestamp: DateTime<Utc>,
}

pub fn parse_file(path: &Path, hostname: &str) -> Result<Vec<EventEnvelope>> {
    let file = std::fs::File::open(path)
        .with_context(|| format!("open {}", path.display()))?;
    let reader = std::io::BufReader::new(file);
    parse_lines(reader, hostname)
}

pub fn parse_lines<R: BufRead>(reader: R, hostname: &str) -> Result<Vec<EventEnvelope>> {
    let mut meta: Option<SessionMeta> = None;
    let mut pending: HashMap<String, PendingCall> = HashMap::new();
    let mut envelopes: Vec<EventEnvelope> = Vec::new();

    for line in reader.lines() {
        let line = line?;
        let line = line.trim();
        if line.is_empty() { continue; }

        let value: serde_json::Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(e) => { warn!(%e, "skip invalid JSON line"); continue; }
        };

        let ty = value.get("type").and_then(|v| v.as_str()).unwrap_or("");
        match ty {
            "session_meta" => meta = Some(read_meta(&value)),
            "turn_context" => {
                if let Some(m) = meta.as_mut() {
                    if let Some(model) = value.pointer("/payload/model").and_then(|v| v.as_str()) {
                        m.model = Some(model.to_string());
                    }
                    if let Some(eff) = value.pointer("/payload/effort").and_then(|v| v.as_str()) {
                        m.effort = Some(eff.to_string());
                    }
                }
            }
            "response_item" => {
                let Some(meta_ref) = meta.as_ref() else { continue; };
                let ts = value.get("timestamp").and_then(|v| v.as_str())
                    .and_then(|s| s.parse::<DateTime<Utc>>().ok())
                    .unwrap_or_else(Utc::now);

                let payload_ty = value.pointer("/payload/type").and_then(|v| v.as_str()).unwrap_or("");
                match payload_ty {
                    "function_call" | "custom_tool_call" => {
                        let call_id = value.pointer("/payload/call_id")
                            .and_then(|v| v.as_str()).unwrap_or("").to_string();
                        if call_id.is_empty() { continue; }
                        let name = value.pointer("/payload/name")
                            .and_then(|v| v.as_str()).unwrap_or("").to_string();
                        let args_str = value.pointer("/payload/arguments")
                            .and_then(|v| v.as_str()).unwrap_or("{}");
                        let arguments: serde_json::Value =
                            serde_json::from_str(args_str).unwrap_or(serde_json::json!({}));
                        pending.insert(call_id.clone(), PendingCall {
                            call_id, name, arguments, timestamp: ts,
                        });
                    }
                    "function_call_output" | "custom_tool_call_output" => {
                        let call_id = value.pointer("/payload/call_id")
                            .and_then(|v| v.as_str()).unwrap_or("");
                        if let Some(pc) = pending.remove(call_id) {
                            let output_raw = value.pointer("/payload/output");
                            let (output_text, status) = interpret_output(output_raw);
                            envelopes.push(build_envelope(meta_ref, &pc, output_text.as_deref(),
                                                          status, Some(ts), hostname));
                        }
                    }
                    _ => {}
                }
            }
            _ => {}
        }
    }

    // Orphans
    if let Some(m) = meta.as_ref() {
        for (_, pc) in pending.drain() {
            envelopes.push(build_envelope(m, &pc, None, AgenticStatus::Orphaned, None, hostname));
        }
    }
    Ok(envelopes)
}

fn read_meta(v: &serde_json::Value) -> SessionMeta {
    SessionMeta {
        id: v.pointer("/payload/id").and_then(|x| x.as_str()).unwrap_or("").to_string(),
        cwd: v.pointer("/payload/cwd").and_then(|x| x.as_str()).unwrap_or("").to_string(),
        cli_version: v.pointer("/payload/cli_version").and_then(|x| x.as_str()).map(String::from),
        originator: v.pointer("/payload/originator").and_then(|x| x.as_str()).map(String::from),
        model_provider: v.pointer("/payload/model_provider").and_then(|x| x.as_str()).map(String::from),
        model: None,
        effort: None,
    }
}

fn interpret_output(output: Option<&serde_json::Value>) -> (Option<String>, AgenticStatus) {
    let Some(out) = output else { return (None, AgenticStatus::Ok); };

    if let Some(s) = out.as_str() {
        let status = if s.starts_with("Error:") || s.starts_with("error:") {
            AgenticStatus::Error
        } else {
            AgenticStatus::Ok
        };
        return (Some(s.to_string()), status);
    }
    if let Some(exit) = out.get("exit_code").and_then(|x| x.as_i64()) {
        let text = out.get("output").and_then(|v| v.as_str()).map(String::from);
        let status = if exit == 0 { AgenticStatus::Ok } else { AgenticStatus::Error };
        return (text, status);
    }
    (Some(out.to_string()), AgenticStatus::Ok)
}

fn build_envelope(
    meta: &SessionMeta,
    pc: &PendingCall,
    result: Option<&str>,
    status: AgenticStatus,
    result_ts: Option<DateTime<Utc>>,
    hostname: &str,
) -> EventEnvelope {
    let command = hippo_core::agentic::render_command(&pc.name, &pc.arguments);
    let duration_ms = result_ts.map(|rt| {
        rt.signed_duration_since(pc.timestamp).num_milliseconds().max(0) as u64
    }).unwrap_or(0);

    let session_id = Uuid::parse_str(&meta.id)
        .unwrap_or_else(|_| Uuid::new_v5(&Uuid::NAMESPACE_URL, meta.id.as_bytes()));

    let tool_output = result.map(|content| {
        let original_bytes = content.len();
        let mut end = MAX_OUTPUT_BYTES.min(content.len());
        while end > 0 && !content.is_char_boundary(end) { end -= 1; }
        CapturedOutput {
            content: content[..end].to_string(),
            truncated: content.len() > MAX_OUTPUT_BYTES,
            original_bytes,
        }
    });

    let envelope_id = Uuid::new_v5(&Uuid::NAMESPACE_URL, pc.call_id.as_bytes());

    let call = AgenticToolCall {
        session_id,
        parent_session_id: None,
        harness: Harness::Codex,
        harness_version: meta.cli_version.clone(),
        model: meta.model.clone().unwrap_or_else(|| "gpt-5".to_string()),
        provider: meta.model_provider.clone(),
        agent: meta.originator.clone(),
        effort: meta.effort.clone(),
        tool_name: pc.name.clone(),
        tool_input: pc.arguments.clone(),
        command,
        tool_output,
        status,
        duration_ms,
        started_at: pc.timestamp,
        cwd: std::path::PathBuf::from(&meta.cwd),
        hostname: hostname.to_string(),
        git_state: None,
        tokens: Some(TokenUsage::default()),
        cost_usd: None,
        redaction_count: 0,
    };

    EventEnvelope {
        envelope_id,
        producer_version: 1,
        timestamp: pc.timestamp,
        payload: EventPayload::AgenticToolCall(Box::new(call)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    const MINIMAL_ROLLOUT: &str = r#"{"timestamp":"2026-04-02T15:04:38.787Z","type":"session_meta","payload":{"id":"019d4eb9-9681-76b1-872d-8afd65144b78","cwd":"/proj","cli_version":"0.118.0","originator":"Codex CLI","model_provider":"openai"}}
{"timestamp":"2026-04-02T15:04:39.000Z","type":"turn_context","payload":{"model":"gpt-5","effort":"high"}}
{"timestamp":"2026-04-02T15:04:40.000Z","type":"response_item","payload":{"type":"function_call","name":"exec_command","arguments":"{\"cmd\":\"ls /tmp\"}","call_id":"call_1"}}
{"timestamp":"2026-04-02T15:04:41.500Z","type":"response_item","payload":{"type":"function_call_output","call_id":"call_1","output":{"exit_code":0,"output":"file1\nfile2"}}}
"#;

    #[test]
    fn parses_single_paired_call() {
        let envs = parse_lines(Cursor::new(MINIMAL_ROLLOUT), "host").unwrap();
        assert_eq!(envs.len(), 1);
        match &envs[0].payload {
            EventPayload::AgenticToolCall(c) => {
                assert_eq!(c.harness, Harness::Codex);
                assert_eq!(c.model, "gpt-5");
                assert_eq!(c.provider.as_deref(), Some("openai"));
                assert_eq!(c.agent.as_deref(), Some("Codex CLI"));
                assert_eq!(c.effort.as_deref(), Some("high"));
                assert_eq!(c.harness_version.as_deref(), Some("0.118.0"));
                assert_eq!(c.command, "ls /tmp");
                assert_eq!(c.duration_ms, 1500);
                assert_eq!(c.status, AgenticStatus::Ok);
                assert!(c.tool_output.as_ref().unwrap().content.contains("file1"));
            }
            _ => panic!(),
        }
    }

    #[test]
    fn orphan_call_emitted_at_eof() {
        let rollout = r#"{"type":"session_meta","payload":{"id":"s","cwd":"/"}}
{"timestamp":"2026-04-02T15:04:40.000Z","type":"response_item","payload":{"type":"function_call","name":"x","arguments":"{}","call_id":"orphan"}}
"#;
        let envs = parse_lines(Cursor::new(rollout), "h").unwrap();
        assert_eq!(envs.len(), 1);
        if let EventPayload::AgenticToolCall(c) = &envs[0].payload {
            assert_eq!(c.status, AgenticStatus::Orphaned);
        } else { panic!(); }
    }

    #[test]
    fn string_output_error_prefix_maps_to_error() {
        let rollout = r#"{"type":"session_meta","payload":{"id":"s","cwd":"/"}}
{"timestamp":"2026-04-02T15:04:40.000Z","type":"response_item","payload":{"type":"function_call","name":"x","arguments":"{}","call_id":"c"}}
{"timestamp":"2026-04-02T15:04:41.000Z","type":"response_item","payload":{"type":"function_call_output","call_id":"c","output":"Error: nope"}}
"#;
        let envs = parse_lines(Cursor::new(rollout), "h").unwrap();
        if let EventPayload::AgenticToolCall(c) = &envs[0].payload {
            assert_eq!(c.status, AgenticStatus::Error);
        } else { panic!(); }
    }

    #[test]
    fn custom_tool_call_pair_emitted() {
        let rollout = r#"{"type":"session_meta","payload":{"id":"s","cwd":"/"}}
{"timestamp":"2026-04-02T15:04:40.000Z","type":"response_item","payload":{"type":"custom_tool_call","name":"apply_patch","arguments":"{}","call_id":"c"}}
{"timestamp":"2026-04-02T15:04:41.000Z","type":"response_item","payload":{"type":"custom_tool_call_output","call_id":"c","output":"ok"}}
"#;
        let envs = parse_lines(Cursor::new(rollout), "h").unwrap();
        assert_eq!(envs.len(), 1);
    }

    #[test]
    fn invalid_json_lines_are_skipped() {
        let rollout = "not json\n{\"type\":\"session_meta\",\"payload\":{\"id\":\"s\",\"cwd\":\"/\"}}\n";
        let envs = parse_lines(Cursor::new(rollout), "h").unwrap();
        assert_eq!(envs.len(), 0);
    }
}
```

- [ ] **Step 2: Wire module**

In `crates/hippo-daemon/src/lib.rs`, add `pub mod codex_session;`.

- [ ] **Step 3: Run tests**

Run: `cargo test -p hippo-daemon --lib codex_session`
Expected: 5 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add crates/hippo-daemon/src/codex_session.rs crates/hippo-daemon/src/lib.rs
git commit -m "feat(hippo-daemon): codex rollout JSONL parser → AgenticToolCall"
```

### Task 5.2: `hippo ingest codex-sessions` CLI

**Files:**
- Modify: `crates/hippo-daemon/src/cli.rs`
- Modify: `crates/hippo-daemon/src/commands.rs`

- [ ] **Step 1: Find how `claude-session` is wired**

Run: `grep -n "claude-session\|ClaudeSession\|ingest" crates/hippo-daemon/src/cli.rs crates/hippo-daemon/src/commands.rs`
Study the pattern; mirror it for Codex.

- [ ] **Step 2: Add the CLI subcommand**

In `crates/hippo-daemon/src/cli.rs` (using whatever clap pattern the file uses), add an `IngestCommand::CodexSessions { path: Option<PathBuf>, since_ms: Option<i64> }` variant.

- [ ] **Step 3: Add the command handler**

In `crates/hippo-daemon/src/commands.rs` (or wherever ingest handlers live), add `async fn ingest_codex_sessions(path: PathBuf, since_ms: Option<i64>, socket: &Path, timeout_ms: u64) -> Result<(usize, usize)>`. It should:

1. Walk `{path}/archived_sessions/*.jsonl` and `{path}/sessions/**/*.jsonl` via `walkdir` or `glob` (whichever the crate already uses).
2. For each file, parse via `codex_session::parse_file`.
3. Optional filter by file mtime / session_meta timestamp against `since_ms`.
4. Send each envelope via `send_event_fire_and_forget`.
5. Return `(sent, errors)`.

- [ ] **Step 4: Add an integration-style test with a real JSONL fixture**

Create `crates/hippo-daemon/tests/codex_fixture.jsonl`:

```
{"timestamp":"2026-04-02T15:04:38.787Z","type":"session_meta","payload":{"id":"019d4eb9","cwd":"/proj","cli_version":"0.118.0","originator":"Codex CLI","model_provider":"openai"}}
{"timestamp":"2026-04-02T15:04:39.000Z","type":"turn_context","payload":{"model":"gpt-5"}}
{"timestamp":"2026-04-02T15:04:40.000Z","type":"response_item","payload":{"type":"function_call","name":"exec_command","arguments":"{\"cmd\":\"ls\"}","call_id":"c1"}}
{"timestamp":"2026-04-02T15:04:41.000Z","type":"response_item","payload":{"type":"function_call_output","call_id":"c1","output":{"exit_code":0,"output":"file"}}}
```

Create `crates/hippo-daemon/tests/codex_ingest.rs`:

```rust
use std::path::PathBuf;

use hippo_core::events::EventPayload;
use hippo_daemon::codex_session::parse_file;

#[test]
fn parse_fixture_file() {
    let mut fixture = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    fixture.push("tests/codex_fixture.jsonl");

    let envs = parse_file(&fixture, "host").unwrap();
    assert_eq!(envs.len(), 1);
    match &envs[0].payload {
        EventPayload::AgenticToolCall(c) => {
            assert_eq!(c.command, "ls");
            assert_eq!(c.model, "gpt-5");
        }
        _ => panic!(),
    }
}
```

- [ ] **Step 5: Run tests**

Run: `cargo test -p hippo-daemon`
Expected: PASS.

- [ ] **Step 6: Smoke-test against real data**

Run: `cargo run -p hippo-daemon --release -- ingest codex-sessions`
Expected: exits 0, logs the file count and event totals. (Verify manually that no obvious panics/warnings appear.)

- [ ] **Step 7: Commit**

```bash
git add crates/
git commit -m "feat(hippo-daemon): hippo ingest codex-sessions CLI"
```

### Task 5.3: Brain-side codex reader

**Files:**
- Create: `brain/src/hippo_brain/codex_reader.py`
- Modify: `brain/src/hippo_brain/agentic_sessions.py`
- Create: `brain/tests/test_codex_reader.py`

- [ ] **Step 1: Write the failing test**

`brain/tests/test_codex_reader.py`:

```python
import tempfile
from pathlib import Path
from textwrap import dedent

from hippo_brain.codex_reader import iter_codex_segments

FIXTURE = dedent("""
{"timestamp":"2026-04-02T15:04:38.787Z","type":"session_meta","payload":{"id":"s1","cwd":"/proj","cli_version":"0.118","originator":"Codex CLI","model_provider":"openai"}}
{"timestamp":"2026-04-02T15:04:39.000Z","type":"turn_context","payload":{"model":"gpt-5","effort":"high"}}
{"timestamp":"2026-04-02T15:04:40.000Z","type":"response_item","payload":{"type":"function_call","name":"exec_command","arguments":"{\\"cmd\\":\\"ls\\"}","call_id":"c1"}}
{"timestamp":"2026-04-02T15:04:41.000Z","type":"response_item","payload":{"type":"function_call_output","call_id":"c1","output":{"exit_code":0,"output":"file"}}}
""").strip()


def test_extract_segment_fields():
    with tempfile.TemporaryDirectory() as d:
        archived = Path(d) / "archived_sessions"
        archived.mkdir()
        (archived / "rollout-x.jsonl").write_text(FIXTURE)

        segments = list(iter_codex_segments(Path(d)))
        assert len(segments) == 1
        seg = segments[0]
        assert seg.harness == "codex"
        assert seg.harness_version == "0.118"
        assert seg.provider == "openai"
        assert seg.agent == "Codex CLI"
        assert seg.effort == "high"
        assert seg.model == "gpt-5"
        assert seg.cwd == "/proj"
        assert seg.tool_calls and seg.tool_calls[0]["name"] == "exec_command"
```

- [ ] **Step 2: Run to fail**

Run: `uv run --project brain --extra dev pytest brain/tests/test_codex_reader.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `brain/src/hippo_brain/codex_reader.py`**

```python
"""Read Codex rollout JSONL files → SessionSegment objects."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from hippo_brain.agentic_sessions import SessionSegment


def iter_codex_segments(codex_root: Path) -> Iterator[SessionSegment]:
    """Yield one SessionSegment per discovered rollout file.

    Scans `<root>/archived_sessions/*.jsonl` and
    `<root>/sessions/**/*.jsonl`. One segment per file — we do not apply
    5-min-gap sub-segmentation for codex because the rollout files are
    already per-turn-scoped.
    """
    for jsonl in _discover(codex_root):
        seg = _segment_for_file(jsonl)
        if seg and (seg.tool_calls or seg.assistant_texts):
            yield seg


def _discover(root: Path) -> Iterator[Path]:
    archived = root / "archived_sessions"
    if archived.is_dir():
        yield from sorted(archived.glob("*.jsonl"))
    live = root / "sessions"
    if live.is_dir():
        yield from sorted(live.rglob("*.jsonl"))


def _iso_to_ms(ts: str) -> int:
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def _segment_for_file(path: Path) -> SessionSegment | None:
    meta_id = ""
    cwd = ""
    cli_version = None
    originator = None
    provider = None
    model = None
    effort = None
    tool_calls: list[dict] = []
    user_prompts: list[str] = []
    assistant_texts: list[str] = []
    start = 0
    end = 0
    message_count = 0

    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    v = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ty = v.get("type", "")
                ts = _iso_to_ms(v.get("timestamp", ""))
                if ts:
                    start = start or ts
                    end = max(end, ts)
                message_count += 1

                if ty == "session_meta":
                    p = v.get("payload", {})
                    meta_id = p.get("id", "")
                    cwd = p.get("cwd", "")
                    cli_version = p.get("cli_version")
                    originator = p.get("originator")
                    provider = p.get("model_provider")
                elif ty == "turn_context":
                    p = v.get("payload", {})
                    if p.get("model"):
                        model = p["model"]
                    if p.get("effort"):
                        effort = p["effort"]
                elif ty == "response_item":
                    p = v.get("payload", {})
                    pt = p.get("type", "")
                    if pt in ("function_call", "custom_tool_call"):
                        try:
                            args = json.loads(p.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            args = {}
                        tool_calls.append({
                            "name": p.get("name", ""),
                            "summary": _summarize(p.get("name", ""), args),
                        })
                    elif pt == "message":
                        role = p.get("role", "")
                        content = p.get("content", [])
                        text = _join_text(content)
                        if text and not text.startswith("<"):
                            if role == "user":
                                user_prompts.append(text[:500])
                            elif role == "assistant":
                                if len(text) > 20:
                                    assistant_texts.append(text[:300])
    except OSError:
        return None

    if not meta_id:
        return None

    return SessionSegment(
        session_id=meta_id,
        project_dir=cwd,
        cwd=cwd,
        git_branch=None,
        segment_index=0,
        start_time=start,
        end_time=end or start,
        user_prompts=user_prompts,
        assistant_texts=assistant_texts,
        tool_calls=tool_calls,
        message_count=message_count,
        source_file=str(path),
        is_subagent=False,
        parent_session_id=None,
        harness="codex",
        harness_version=cli_version,
        model=model,
        provider=provider,
        agent=originator,
        effort=effort,
    )


def _join_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                t = b.get("text") or b.get("input_text") or b.get("output_text")
                if t:
                    parts.append(t)
        return "\n".join(parts).strip()
    return ""


def _summarize(name: str, inp: dict) -> str:
    if name == "exec_command":
        return (inp.get("cmd") or "")[:200]
    if name in ("read", "write", "edit"):
        return inp.get("file_path") or inp.get("path") or ""
    for k, v in inp.items():
        return f"{k}={str(v)[:80]}"
    return ""
```

- [ ] **Step 4: Wire into dispatcher**

In `agentic_sessions.py`, update `iter_all_agentic_segments` to accept a `codex_root: Path | None`:

```python
def iter_all_agentic_segments(
    *,
    claude_projects_dir: Path | None,
    opencode_db_path: Path | None,
    codex_root: Path | None,
) -> Iterator[SessionSegment]:
    ...existing...

    if codex_root is not None and codex_root.exists():
        from hippo_brain.codex_reader import iter_codex_segments
        yield from iter_codex_segments(codex_root)
```

- [ ] **Step 5: Run tests**

Run: `uv run --project brain --extra dev pytest brain/tests -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add brain/
git commit -m "feat(brain): codex rollout JSONL reader"
```

---

## Phase 6: Enrichment prompt, MCP filters, observability, cleanup

### Task 6.1: Harness context line in enrichment prompt

**Files:**
- Modify: `brain/src/hippo_brain/agentic_sessions.py` (`build_agentic_enrichment_prompt`)
- Modify: `brain/tests/test_agentic_sessions.py`

- [ ] **Step 1: Update the prompt builder**

In `build_agentic_enrichment_prompt`, prepend a harness line to the per-segment header:

```python
def _format_harness_line(seg: SessionSegment) -> str:
    parts = [seg.harness]
    if seg.harness_version:
        parts.append(seg.harness_version)
    subparts = []
    if seg.agent:
        subparts.append(f"{seg.agent} agent")
    if seg.model:
        model_bit = seg.model
        if seg.provider:
            model_bit = f"{seg.model} via {seg.provider}"
        subparts.append(model_bit)
    if seg.effort:
        subparts.append(f"effort={seg.effort}")
    if subparts:
        parts.append(f"({', '.join(subparts)})")
    return "Harness: " + " ".join(parts)


# In the main builder loop, add _format_harness_line(seg) as the second line
# (immediately after the "Claude Code session segment..." or equivalent header).
```

Rename the fixed `"Claude Code session segment"` header to `f"{seg.harness} session segment"` (lowercase `harness` is fine as a distinguishing tag).

- [ ] **Step 2: Add test cases in `test_agentic_sessions.py`**

```python
def test_harness_line_claude():
    seg = SessionSegment(
        session_id="s", project_dir="p", cwd="/p", git_branch=None,
        segment_index=0, start_time=0, end_time=0, message_count=1,
        source_file="", harness="claude-code", model="claude-opus-4-7",
        provider="anthropic",
        user_prompts=["hi"], tool_calls=[{"name":"Bash","summary":"ls"}],
    )
    text = build_agentic_enrichment_prompt([seg])
    assert "Harness: claude-code (claude-opus-4-7 via anthropic)" in text


def test_harness_line_opencode_with_agent():
    seg = SessionSegment(
        session_id="s", project_dir="p", cwd="/p", git_branch=None,
        segment_index=0, start_time=0, end_time=0, message_count=1,
        source_file="", harness="opencode", harness_version="1.4.6",
        model="nvidia/nemotron-3-super", provider="lmstudio", agent="build",
        tool_calls=[{"name":"Bash","summary":"ls"}],
    )
    text = build_agentic_enrichment_prompt([seg])
    assert "Harness: opencode 1.4.6 (build agent, nvidia/nemotron-3-super via lmstudio)" in text


def test_harness_line_codex_with_effort():
    seg = SessionSegment(
        session_id="s", project_dir="p", cwd="/p", git_branch=None,
        segment_index=0, start_time=0, end_time=0, message_count=1,
        source_file="", harness="codex", harness_version="0.118",
        model="gpt-5", provider="openai", agent="Codex CLI", effort="high",
        tool_calls=[{"name":"exec_command","summary":"ls"}],
    )
    text = build_agentic_enrichment_prompt([seg])
    assert "Harness: codex 0.118 (Codex CLI agent, gpt-5 via openai, effort=high)" in text
```

- [ ] **Step 3: Run tests**

Run: `uv run --project brain --extra dev pytest brain/tests/test_agentic_sessions.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add brain/
git commit -m "feat(brain): harness-context line in enrichment prompt"
```

### Task 6.2: MCP filter params

**Files:**
- Modify: `brain/src/hippo_brain/mcp_server.py` (or the actual MCP tool definitions — grep)
- Modify: `brain/tests/test_mcp.py` (or equivalent)

- [ ] **Step 1: Locate current MCP tool definitions**

Run: `grep -rn "search_knowledge\|search_events\|tool_definition" brain/src`
Identify the schema/handler for `search_knowledge` and `search_events`.

- [ ] **Step 2: Add optional filter params**

For each tool, add parameters:

```python
{
    "harness": {
        "type": "string",
        "description": "Filter by producing harness: claude-code, opencode, codex",
        "enum": ["claude-code", "opencode", "codex"],
    },
    "model": {
        "type": "string",
        "description": "Filter by model identifier, exact match",
    },
}
```

Update the underlying SQL to add `AND agentic_sessions.harness = ?` and `AND agentic_sessions.model = ?` when provided (use a join if not already joined).

- [ ] **Step 3: Add tests**

In the relevant test file, seed two `agentic_sessions` rows with different harness values and assert filtering works.

- [ ] **Step 4: Run tests**

Run: `uv run --project brain --extra dev pytest brain/tests -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/
git commit -m "feat(brain-mcp): harness/model filters on search_knowledge and search_events"
```

### Task 6.3: Metrics and `hippo doctor`

**Files:**
- Modify: `crates/hippo-daemon/src/metrics.rs`
- Modify: `crates/hippo-daemon/src/opencode_session.rs`
- Modify: wherever `hippo doctor` lives (grep: `grep -n "fn doctor\|\"doctor\"" crates/hippo-daemon/src`)

- [ ] **Step 1: Add counters in `metrics.rs`**

Mirror the existing counter pattern (check how `hippo_shell_events_total` etc. are defined). Add:

```rust
pub static AGENTIC_EVENTS_EMITTED: Lazy<Counter> = Lazy::new(|| { /* ... { harness = labels... } */ });
pub static AGENTIC_POLLER_TICKS: Lazy<Counter> = Lazy::new(|| { /* { harness, outcome } */ });
pub static AGENTIC_BACKFILL_FILES: Lazy<Counter> = Lazy::new(|| { /* { harness } */ });
```

Use whatever `metrics` crate interface the file already imports. Don't add new deps.

- [ ] **Step 2: Increment counters from `opencode_session.rs` and `codex_session.rs`**

- In the poller tick loop: increment `AGENTIC_POLLER_TICKS` with outcome `no_change`/`rows_read`/`error`.
- After each successful `send_event_fire_and_forget`: increment `AGENTIC_EVENTS_EMITTED { harness = "opencode" }`.
- In the Codex importer: increment `AGENTIC_BACKFILL_FILES { harness = "codex" }` per file and `AGENTIC_EVENTS_EMITTED { harness = "codex" }` per event.

Do the same for the Claude emit path (`crates/hippo-daemon/src/claude_session.rs`) with `harness = "claude-code"`.

- [ ] **Step 3: Extend `hippo doctor`**

Locate the doctor implementation and add checks for each agentic harness:

```rust
// opencode
let db = config.opencode.resolved_db_path();
check(
    config.opencode.enabled,
    "opencode ingestion enabled in config",
);
check(db.exists(), &format!("opencode db exists at {}", db.display()));

// codex
let codex_root = std::path::PathBuf::from(std::env::var("HOME").unwrap_or_default())
    .join(".codex");
check(codex_root.exists(), "codex sessions dir exists (historical)");
```

Use the existing `check!` macro / helper in the doctor module.

- [ ] **Step 4: Build + test**

Run: `cargo build --workspace && cargo test --workspace`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add crates/
git commit -m "feat(hippo-daemon): agentic metrics + doctor checks"
```

### Task 6.4: Wire redaction over `AgenticToolCall` envelopes

**Files:**
- Modify: wherever the redactor is invoked for outgoing `EventEnvelope`s (locate via `grep -rn "redact\|Redactor" crates/hippo-daemon/src`)
- Modify: `crates/hippo-core/src/redact.rs` (or whichever module owns the redactor) — add a helper for recursive JSON-value redaction if not already present.

- [ ] **Step 1: Check existing redactor surface**

Run: `grep -rn "fn redact\|pub fn.*redact\|impl.*Redactor" crates/hippo-core/src crates/hippo-daemon/src`
Note whether the redactor currently accepts only `&str` or also structured values.

- [ ] **Step 2: Add recursive JSON redaction if missing**

If the redactor is string-only, add to the redactor module:

```rust
/// Recursively apply redaction over every string leaf in `value`. Returns
/// the total number of replacements made across the subtree.
pub fn redact_value(redactor: &Redactor, value: &mut serde_json::Value) -> u32 {
    use serde_json::Value;
    match value {
        Value::String(s) => {
            let (redacted, count) = redactor.redact(s);
            *s = redacted;
            count
        }
        Value::Array(arr) => arr.iter_mut().map(|v| redact_value(redactor, v)).sum(),
        Value::Object(map) => map.values_mut().map(|v| redact_value(redactor, v)).sum(),
        _ => 0,
    }
}
```

Use the exact type / return signature used by the existing `Redactor::redact` (string → (String, u32) or whatever the actual shape is).

- [ ] **Step 3: Test recursive redaction**

Add to the redactor tests (`crates/hippo-core/src/redact.rs` `#[cfg(test)] mod tests` or the existing test file):

```rust
#[test]
fn redact_value_walks_nested_structure() {
    let redactor = Redactor::default(); // or whatever the default constructor is
    let mut v = serde_json::json!({
        "command": "export API_KEY=sk-abc123",
        "nested": {"token": "sk-xyz"},
        "args": ["sk-other"],
        "count": 42,
    });
    let n = redact_value(&redactor, &mut v);
    assert!(n >= 3);
    assert!(!serde_json::to_string(&v).unwrap().contains("sk-abc123"));
}
```

If `Redactor::default()` does not exist, adapt to the actual constructor (the existing redactor tests will show the pattern).

Run: `cargo test -p hippo-core redact`
Expected: PASS.

- [ ] **Step 4: Apply redaction to outbound `AgenticToolCall` envelopes**

In the daemon's envelope-send path (where shell envelopes already get redacted — grep to find it), add an arm that redacts `command`, `tool_input`, and `tool_output.content` on `EventPayload::AgenticToolCall`:

```rust
EventPayload::AgenticToolCall(call) => {
    let mut total = 0u32;
    let (redacted_cmd, n) = redactor.redact(&call.command);
    call.command = redacted_cmd;
    total += n;
    total += redact_value(&redactor, &mut call.tool_input);
    if let Some(out) = call.tool_output.as_mut() {
        let (red, n) = redactor.redact(&out.content);
        out.content = red;
        total += n;
    }
    call.redaction_count = total;
}
```

- [ ] **Step 5: Add an integration-style test for the daemon redaction path**

If the daemon has existing tests for shell-event redaction, mirror them for `AgenticToolCall` in the same test module.

- [ ] **Step 6: Build + test + clippy**

Run: `cargo build --workspace && cargo test --workspace && cargo clippy --workspace --all-targets -- -D warnings`
Expected: all clean.

- [ ] **Step 7: Commit**

```bash
git add crates/
git commit -m "feat: run redactor over AgenticToolCall command, tool_input, and tool_output"
```

### Task 6.5: Remove legacy `ShellKind::Unknown(\"claude-code\")` paths

**Files:**
- Modify: anywhere that explicitly checks for `claude-code` on `ShellKind`.

- [ ] **Step 1: Find every reference**

Run: `grep -rn 'claude-code' --include="*.rs" --include="*.py" crates/ brain/`
Triage:
  - References in **test fixtures**: some assertions may still check the shell string; update or delete now that the payload is `AgenticToolCall`.
  - References in **SQL queries** that selected `events WHERE shell = 'claude-code'`: replace with queries against `agentic_sessions`, or remove if now redundant.
  - References in **docs / comments**: leave alone unless they document behavior that no longer exists.

- [ ] **Step 2: Run full test suite after removals**

Run: `cargo test --workspace && uv run --project brain --extra dev pytest brain/tests -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "refactor: drop legacy ShellKind::Unknown(\"claude-code\") references"
```

### Task 6.6: Docs update

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md` (if it describes Claude ingestion)
- Create: `docs/agentic-ingestion.md`

- [ ] **Step 1: Update CLAUDE.md**

Replace the `Claude Session Ingestion` section with a more general `Agentic Session Ingestion` section covering the three harnesses. Cite the new v6 schema and `[opencode]` config block.

- [ ] **Step 2: Write `docs/agentic-ingestion.md`**

A short operator guide: how to enable opencode, how to run the codex backfill, how to query by harness via MCP.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md README.md docs/agentic-ingestion.md
git commit -m "docs: agentic ingestion operator guide"
```

---

## Final verification

- [ ] **Workspace lint + test**

Run:
```bash
cargo fmt --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
uv run --project brain --extra dev ruff check brain/
uv run --project brain --extra dev ruff format --check brain/
uv run --project brain --extra dev pytest brain/tests --cov=hippo_brain
```
Expected: all clean.

- [ ] **Manual smoke test**

1. On a dev machine with opencode installed and a non-empty `opencode.db`:
   - Edit `~/.config/hippo/config.toml`, set `[opencode] enabled = true`.
   - `mise run restart` the daemon.
   - `tail -f ~/.local/share/hippo/hippo-daemon.log` and confirm opencode poller starts.
   - Trigger an opencode tool call; verify it shows up within a few seconds via `hippo ask "what did opencode do recently"`.

2. Codex backfill:
   - `hippo ingest codex-sessions` — confirm exit 0 and a reasonable event count.
   - Verify enrichment proceeds and a knowledge node appears with `harness='codex'`.

3. Claude:
   - Start a new Claude Code session; verify the existing SessionStart hook still works and the resulting events now carry `harness='claude-code'` in `agentic_sessions` (not the old `claude_sessions` name).

- [ ] **PR**

Use `commit-commands:commit-push-pr` to open the PR. Title: "Agentic session ingestion: opencode live + Codex historical + harness labeling". Link the spec in the description.
