# GitHub Actions Source and Hippo Brain Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add GitHub Actions workflow outcomes as hippo's fourth data source and ship a default Claude Code skill (`using-hippo-brain`) so CI outcomes and repeated mistakes become cross-session working memory.

**Architecture:** A new `hippo gh-poll` Rust subcommand polls the GitHub REST API on a launchd timer, parsing workflow runs, jobs, annotations, and failure-log excerpts into SQLite schema v5. A `sha_watchlist` mechanism lets recently-pushed SHAs get tight polling. The Python brain adds a change-outcome enrichment pass that joins workflow outcomes to co-temporal shell/Claude/browser events, plus a lesson-synthesis pass that clusters repeated failures. Two new MCP tools (`get_ci_status`, `get_lessons`) expose structured data for agent consumption; a default `using-hippo-brain` skill teaches Claude when to use them. Two small hooks (`PostToolUse` on `git push`, `SessionStart` for pending failures) close the loop.

**Tech Stack:** Rust (edition 2024, tokio, clap, rusqlite, reqwest, wiremock for tests), Python 3.14+ (FastMCP, pydantic, pytest), SQLite WAL, launchd, Claude Code skills/hooks.

**Working branch:** `design/github-actions-source-and-skill` (spec already committed there).

---

## File Structure

**Rust — new files:**
- `crates/hippo-core/src/gh_annotations.rs` — annotation-body parser (pure, unit-testable)
- `crates/hippo-daemon/src/gh_api.rs` — GitHub REST client wrapper
- `crates/hippo-daemon/src/gh_poll.rs` — poll orchestration loop
- `crates/hippo-daemon/src/watchlist.rs` — sha_watchlist storage helpers

**Rust — modified:**
- `crates/hippo-core/src/schema.sql` — v5 DDL (additive)
- `crates/hippo-core/src/storage.rs` — migration block + workflow_*/watchlist/lesson insert helpers
- `crates/hippo-core/src/config.rs` — `[github]` config section
- `crates/hippo-core/src/protocol.rs` — new DaemonRequest variants for watchlist writes
- `crates/hippo-daemon/src/cli.rs` — `GhPoll` subcommand variant
- `crates/hippo-daemon/src/commands.rs` — route `GhPoll` to the new orchestrator
- `crates/hippo-daemon/src/daemon.rs` — handle new watchlist DaemonRequest variants
- `crates/hippo-daemon/src/install.rs` — install `com.hippo.gh-poll.plist` + skill + hooks
- `crates/hippo-daemon/src/lib.rs` — module declarations

**Python — new files:**
- `brain/src/hippo_brain/workflow_enrichment.py` — change-outcome enrichment
- `brain/src/hippo_brain/lessons.py` — lesson clustering/synthesis

**Python — modified:**
- `brain/src/hippo_brain/models.py` — `CIStatus`, `Lesson`, `Annotation` dataclasses
- `brain/src/hippo_brain/mcp_queries.py` — `get_ci_status_impl`, `get_lessons_impl`
- `brain/src/hippo_brain/mcp.py` — register new tools
- `brain/src/hippo_brain/enrichment.py` — wire workflow_enrichment into scheduler loop

**Non-code assets:**
- `extension/claude-skill/using-hippo-brain/SKILL.md` — new skill
- `launchd/com.hippo.gh-poll.plist` — new launchd job
- `shell/claude-hooks/post-git-push.sh` — PostToolUse hook
- `shell/claude-hooks/pending-ci-session-start.sh` — SessionStart hook
- `config/config.toml.template` — add `[github]` section (if template exists; else document in README)

**Tests — new:**
- `crates/hippo-core/tests/` — migration v4→v5 test, annotation parser tests
- `crates/hippo-daemon/tests/` — gh_api mock tests, poll loop integration test, watchlist tests
- `brain/tests/test_workflow_enrichment.py`
- `brain/tests/test_lessons.py`
- `brain/tests/test_mcp_queries_gh.py`

---

## Phase 0 — Workspace Setup

### Task 1: Confirm branch and worktree

**Files:** none

- [ ] **Step 1: Verify you're on the design branch**

Run: `git status && git log --oneline -2`
Expected: on `design/github-actions-source-and-skill`, latest commit is the design spec.

- [ ] **Step 2: Confirm spec is present**

Run: `ls docs/superpowers/specs/2026-04-15-github-actions-source-and-hippo-skill-design.md`
Expected: file exists. If missing, abort — plan depends on spec being in tree.

- [ ] **Step 3: Build baseline to confirm green starting state**

Run: `cargo build && cargo test --no-run && uv run --project brain pytest brain/tests --collect-only -q`
Expected: all compile/collect cleanly. Any baseline failure is out of scope — resolve before starting.

---

## Phase 1 — Schema v5 Migration

### Task 2: Write failing test for v4→v5 migration

**Files:**
- Create: `crates/hippo-core/tests/schema_v5_migration.rs`

- [ ] **Step 1: Write the failing test**

```rust
use hippo_core::storage::open_db;
use rusqlite::Connection;
use tempfile::TempDir;

fn seed_v4(path: &std::path::Path) {
    let conn = Connection::open(path).unwrap();
    // Minimally seed v4 schema by running the pre-v5 schema.sql snapshot.
    // For this test we bootstrap via open_db at an older version by
    // setting PRAGMA user_version = 4 after creating the events table.
    conn.execute_batch(include_str!("fixtures/schema_v4.sql")).unwrap();
    conn.pragma_update(None, "user_version", 4).unwrap();
}

#[test]
fn v4_db_migrates_to_v5_and_has_workflow_tables() {
    let tmp = TempDir::new().unwrap();
    let db = tmp.path().join("hippo.db");
    seed_v4(&db);

    // First open triggers migration.
    let conn = open_db(&db).unwrap();

    let version: i64 = conn
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap();
    assert_eq!(version, 5);

    // Each new table exists.
    for table in [
        "workflow_runs",
        "workflow_jobs",
        "workflow_annotations",
        "workflow_log_excerpts",
        "sha_watchlist",
        "workflow_enrichment_queue",
        "lessons",
        "knowledge_node_workflow_runs",
        "knowledge_node_lessons",
    ] {
        let exists: i64 = conn
            .query_row(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?1",
                [table],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(exists, 1, "table {table} missing after v5 migration");
    }
}
```

- [ ] **Step 2: Create the v4 fixture**

`crates/hippo-core/tests/fixtures/schema_v4.sql` — snapshot `crates/hippo-core/src/schema.sql` as-of the v4 state so the test is reproducible. (Copy current `schema.sql` contents verbatim; this locks v4 shape for the test.)

- [ ] **Step 3: Run test to verify it fails**

Run: `cargo test -p hippo-core --test schema_v5_migration`
Expected: FAIL — assertion that `user_version == 5` fails (currently 4) OR `workflow_runs` does not exist.

### Task 3: Implement v5 migration

**Files:**
- Modify: `crates/hippo-core/src/schema.sql` (append v5 DDL)
- Modify: `crates/hippo-core/src/storage.rs` (bump `EXPECTED_VERSION`, add migration block)

- [ ] **Step 1: Append v5 tables to `schema.sql`**

```sql
-- ─── v5: GitHub Actions ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS workflow_runs (
    id              INTEGER PRIMARY KEY,
    repo            TEXT NOT NULL,
    head_sha        TEXT NOT NULL,
    head_branch     TEXT,
    event           TEXT NOT NULL,
    status          TEXT NOT NULL,
    conclusion      TEXT,
    started_at      INTEGER,
    completed_at    INTEGER,
    html_url        TEXT NOT NULL,
    actor           TEXT,
    raw_json        TEXT NOT NULL,
    first_seen_at   INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    enriched        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_sha
    ON workflow_runs(head_sha);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_repo_started
    ON workflow_runs(repo, started_at);

CREATE TABLE IF NOT EXISTS workflow_jobs (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL,
    conclusion      TEXT,
    started_at      INTEGER,
    completed_at    INTEGER,
    runner_name     TEXT,
    raw_json        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_jobs_run ON workflow_jobs(run_id);

CREATE TABLE IF NOT EXISTS workflow_annotations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES workflow_jobs(id) ON DELETE CASCADE,
    level           TEXT NOT NULL,
    tool            TEXT,
    rule_id         TEXT,
    path            TEXT,
    start_line      INTEGER,
    message         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_annotations_job
    ON workflow_annotations(job_id);
CREATE INDEX IF NOT EXISTS idx_workflow_annotations_tool_rule
    ON workflow_annotations(tool, rule_id);

CREATE TABLE IF NOT EXISTS workflow_log_excerpts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES workflow_jobs(id) ON DELETE CASCADE,
    step_name       TEXT,
    excerpt         TEXT NOT NULL,
    truncated       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sha_watchlist (
    sha             TEXT NOT NULL,
    repo            TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    terminal_status TEXT,
    notified        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (sha, repo)
);
CREATE INDEX IF NOT EXISTS idx_sha_watchlist_expires
    ON sha_watchlist(expires_at);

CREATE TABLE IF NOT EXISTS workflow_enrichment_queue (
    run_id          INTEGER PRIMARY KEY
                        REFERENCES workflow_runs(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','processing','done','failed','skipped')),
    priority        INTEGER NOT NULL DEFAULT 5,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    max_retries     INTEGER NOT NULL DEFAULT 5,
    error_message   TEXT,
    enqueued_at     INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_queue_pending
    ON workflow_enrichment_queue(status, priority);

CREATE TABLE IF NOT EXISTS lessons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo            TEXT NOT NULL,
    tool            TEXT,
    rule_id         TEXT,
    path_prefix     TEXT,
    summary         TEXT NOT NULL,
    fix_hint        TEXT,
    occurrences     INTEGER NOT NULL DEFAULT 1,
    first_seen_at   INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    UNIQUE(repo, tool, rule_id, path_prefix)
);
CREATE INDEX IF NOT EXISTS idx_lessons_repo ON lessons(repo);

CREATE TABLE IF NOT EXISTS knowledge_node_workflow_runs (
    knowledge_node_id INTEGER NOT NULL REFERENCES knowledge_nodes(id),
    run_id            INTEGER NOT NULL
                          REFERENCES workflow_runs(id) ON DELETE CASCADE,
    PRIMARY KEY (knowledge_node_id, run_id)
);

CREATE TABLE IF NOT EXISTS knowledge_node_lessons (
    knowledge_node_id INTEGER NOT NULL REFERENCES knowledge_nodes(id),
    lesson_id         INTEGER NOT NULL
                          REFERENCES lessons(id) ON DELETE CASCADE,
    PRIMARY KEY (knowledge_node_id, lesson_id)
);
```

- [ ] **Step 2: Add v4→v5 migration block to `storage.rs`**

Find the current highest migration block (v3→v4 for browser). After it, add:

```rust
// Migrate v4 → v5: GitHub Actions tables
if version <= 4 {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS workflow_runs ( ... );
         -- (full DDL, identical to the v5 block in schema.sql)
         PRAGMA user_version = 5;",
    )?;
}
```

Then bump `const EXPECTED_VERSION: i64 = 4;` → `5`.

The DDL inside the migration block must be verbatim copy of the v5 block from `schema.sql`. This duplication is deliberate: `schema.sql` is the from-scratch bootstrap; the inline migration is for existing DBs.

- [ ] **Step 3: Run the failing test — it should now pass**

Run: `cargo test -p hippo-core --test schema_v5_migration`
Expected: PASS.

- [ ] **Step 4: Run the rest of the core test suite to confirm no regression**

Run: `cargo test -p hippo-core`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-core/src/schema.sql crates/hippo-core/src/storage.rs \
        crates/hippo-core/tests/schema_v5_migration.rs \
        crates/hippo-core/tests/fixtures/schema_v4.sql
git commit -m "feat(core): add schema v5 for GitHub Actions source"
```

---

## Phase 2 — Annotation Parser

### Task 4: Write failing tests for annotation parser

**Files:**
- Create: `crates/hippo-core/src/gh_annotations.rs`

- [ ] **Step 1: Stub the module and add unit tests**

```rust
//! Parser for GitHub Actions annotations → (tool, rule_id) tuples.

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedAnnotation {
    pub tool: Option<String>,
    pub rule_id: Option<String>,
}

pub fn parse(job_name: &str, message: &str) -> ParsedAnnotation {
    let _ = (job_name, message);
    ParsedAnnotation { tool: None, rule_id: None }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(j: &str, m: &str) -> ParsedAnnotation { parse(j, m) }

    #[test]
    fn ruff_rule_from_message() {
        let got = p("ruff", "F401 [*] 'os' imported but unused");
        assert_eq!(got.tool.as_deref(), Some("ruff"));
        assert_eq!(got.rule_id.as_deref(), Some("F401"));
    }

    #[test]
    fn ruff_rule_e_class() {
        let got = p("lint", "E501 line too long (120 > 100 characters)");
        assert_eq!(got.tool.as_deref(), Some("ruff"));
        assert_eq!(got.rule_id.as_deref(), Some("E501"));
    }

    #[test]
    fn cargo_rustc_error() {
        let got = p("build", "error[E0308]: mismatched types");
        assert_eq!(got.tool.as_deref(), Some("cargo"));
        assert_eq!(got.rule_id.as_deref(), Some("E0308"));
    }

    #[test]
    fn mypy_error_with_code() {
        let got = p("typecheck", "error: Argument 1 has incompatible type [arg-type]");
        assert_eq!(got.tool.as_deref(), Some("mypy"));
        assert_eq!(got.rule_id.as_deref(), Some("arg-type"));
    }

    #[test]
    fn pytest_assertion_no_rule() {
        let got = p("test", "FAILED tests/test_x.py::test_y - AssertionError");
        assert_eq!(got.tool.as_deref(), Some("pytest"));
        assert_eq!(got.rule_id, None);
    }

    #[test]
    fn unknown_falls_through() {
        let got = p("misc", "some random message");
        assert_eq!(got.tool, None);
        assert_eq!(got.rule_id, None);
    }
}
```

- [ ] **Step 2: Register the module**

Edit `crates/hippo-core/src/lib.rs` — add `pub mod gh_annotations;`.

- [ ] **Step 3: Run tests to confirm they fail**

Run: `cargo test -p hippo-core gh_annotations::`
Expected: 5/6 tests fail (only `unknown_falls_through` passes with the stub).

### Task 5: Implement parser

**Files:**
- Modify: `crates/hippo-core/src/gh_annotations.rs`

- [ ] **Step 1: Replace the stub with a working parser**

```rust
use regex::Regex;
use std::sync::LazyLock;

static RUFF_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\b([EWF]\d{3,4})\b").unwrap());
static CARGO_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"error\[(E\d{4})\]").unwrap());
static MYPY_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\[([a-z][a-z0-9-]+)\]\s*$").unwrap());

pub fn parse(job_name: &str, message: &str) -> ParsedAnnotation {
    // 1. Cargo is unambiguous from the message alone.
    if let Some(c) = CARGO_RE.captures(message) {
        return ParsedAnnotation {
            tool: Some("cargo".into()),
            rule_id: Some(c[1].to_string()),
        };
    }

    // 2. Ruff rule-codes: job name is a strong prior.
    let job_lower = job_name.to_ascii_lowercase();
    if job_lower.contains("ruff") || job_lower.contains("lint") {
        if let Some(c) = RUFF_RE.captures(message) {
            return ParsedAnnotation {
                tool: Some("ruff".into()),
                rule_id: Some(c[1].to_string()),
            };
        }
    }

    // 3. mypy / pyright: bracket-suffixed error code.
    if job_lower.contains("type") || job_lower.contains("mypy") || job_lower.contains("pyright") {
        if let Some(c) = MYPY_RE.captures(message) {
            let tool = if job_lower.contains("pyright") { "pyright" } else { "mypy" };
            return ParsedAnnotation {
                tool: Some(tool.into()),
                rule_id: Some(c[1].to_string()),
            };
        }
    }

    // 4. pytest: FAILED marker or AssertionError heuristic.
    if job_lower.contains("pytest") || job_lower.contains("test") {
        if message.contains("FAILED ") || message.contains("AssertionError") {
            return ParsedAnnotation {
                tool: Some("pytest".into()),
                rule_id: None,
            };
        }
    }

    ParsedAnnotation { tool: None, rule_id: None }
}
```

- [ ] **Step 2: Add `regex` to `hippo-core` Cargo.toml if not present**

```toml
# Under [dependencies], ensure:
regex = "1"
```

- [ ] **Step 3: Run tests**

Run: `cargo test -p hippo-core gh_annotations::`
Expected: all 6 pass.

- [ ] **Step 4: Commit**

```bash
git add crates/hippo-core/src/gh_annotations.rs \
        crates/hippo-core/src/lib.rs \
        crates/hippo-core/Cargo.toml
git commit -m "feat(core): annotation parser for CI tool/rule extraction"
```

---

## Phase 3 — Watchlist and Daemon Protocol

### Task 6: Watchlist storage helpers

**Files:**
- Create: `crates/hippo-core/tests/watchlist.rs`
- Modify: `crates/hippo-core/src/storage.rs`

- [ ] **Step 1: Write failing test**

```rust
use hippo_core::storage::{open_db, watchlist};
use tempfile::TempDir;

#[test]
fn upsert_then_resolve() {
    let tmp = TempDir::new().unwrap();
    let conn = open_db(&tmp.path().join("hippo.db")).unwrap();

    watchlist::upsert(
        &conn, "abc123", "me/repo",
        /*created_at=*/ 1_700_000_000_000,
        /*expires_at=*/ 1_700_000_600_000,
    ).unwrap();

    let active = watchlist::list_active(&conn, 1_700_000_000_000).unwrap();
    assert_eq!(active.len(), 1);
    assert_eq!(active[0].sha, "abc123");

    watchlist::mark_terminal(&conn, "abc123", "me/repo", "failure").unwrap();
    let pending = watchlist::pending_notifications(&conn).unwrap();
    assert_eq!(pending.len(), 1);
    assert_eq!(pending[0].terminal_status.as_deref(), Some("failure"));
}

#[test]
fn expired_entry_not_active() {
    let tmp = TempDir::new().unwrap();
    let conn = open_db(&tmp.path().join("hippo.db")).unwrap();

    watchlist::upsert(&conn, "abc", "me/repo", 0, 1_000).unwrap();
    let active = watchlist::list_active(&conn, 2_000).unwrap();
    assert!(active.is_empty());
}
```

- [ ] **Step 2: Run test (compile should fail — module doesn't exist)**

Run: `cargo test -p hippo-core --test watchlist`
Expected: compile error on `storage::watchlist`.

- [ ] **Step 3: Implement the watchlist sub-module in `storage.rs`**

Append to the bottom of `storage.rs`:

```rust
pub mod watchlist {
    use anyhow::Result;
    use rusqlite::{Connection, params};

    #[derive(Debug, Clone)]
    pub struct WatchEntry {
        pub sha: String,
        pub repo: String,
        pub created_at: i64,
        pub expires_at: i64,
        pub terminal_status: Option<String>,
        pub notified: bool,
    }

    pub fn upsert(
        conn: &Connection, sha: &str, repo: &str, created_at: i64, expires_at: i64,
    ) -> Result<()> {
        conn.execute(
            "INSERT INTO sha_watchlist (sha, repo, created_at, expires_at)
             VALUES (?1, ?2, ?3, ?4)
             ON CONFLICT(sha, repo) DO UPDATE SET expires_at = excluded.expires_at",
            params![sha, repo, created_at, expires_at],
        )?;
        Ok(())
    }

    pub fn list_active(conn: &Connection, now_ms: i64) -> Result<Vec<WatchEntry>> {
        let mut stmt = conn.prepare(
            "SELECT sha, repo, created_at, expires_at, terminal_status, notified
             FROM sha_watchlist
             WHERE expires_at > ?1 AND terminal_status IS NULL
             ORDER BY created_at DESC",
        )?;
        let rows = stmt.query_map([now_ms], |r| {
            Ok(WatchEntry {
                sha: r.get(0)?, repo: r.get(1)?,
                created_at: r.get(2)?, expires_at: r.get(3)?,
                terminal_status: r.get(4)?, notified: r.get::<_, i64>(5)? != 0,
            })
        })?.collect::<Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    pub fn mark_terminal(
        conn: &Connection, sha: &str, repo: &str, status: &str,
    ) -> Result<()> {
        conn.execute(
            "UPDATE sha_watchlist SET terminal_status = ?3
             WHERE sha = ?1 AND repo = ?2",
            params![sha, repo, status],
        )?;
        Ok(())
    }

    pub fn pending_notifications(conn: &Connection) -> Result<Vec<WatchEntry>> {
        let mut stmt = conn.prepare(
            "SELECT sha, repo, created_at, expires_at, terminal_status, notified
             FROM sha_watchlist
             WHERE terminal_status IN ('failure', 'cancelled') AND notified = 0",
        )?;
        let rows = stmt.query_map([], |r| {
            Ok(WatchEntry {
                sha: r.get(0)?, repo: r.get(1)?,
                created_at: r.get(2)?, expires_at: r.get(3)?,
                terminal_status: r.get(4)?, notified: r.get::<_, i64>(5)? != 0,
            })
        })?.collect::<Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    pub fn mark_notified(conn: &Connection, sha: &str, repo: &str) -> Result<()> {
        conn.execute(
            "UPDATE sha_watchlist SET notified = 1 WHERE sha = ?1 AND repo = ?2",
            params![sha, repo],
        )?;
        Ok(())
    }
}
```

- [ ] **Step 4: Run test — should pass**

Run: `cargo test -p hippo-core --test watchlist`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-core/src/storage.rs crates/hippo-core/tests/watchlist.rs
git commit -m "feat(core): sha_watchlist storage helpers"
```

### Task 7: Daemon protocol — RegisterWatchSha request

**Files:**
- Modify: `crates/hippo-core/src/protocol.rs`
- Modify: `crates/hippo-daemon/src/daemon.rs`
- Create test: `crates/hippo-daemon/tests/watchlist_rpc.rs`

- [ ] **Step 1: Write failing integration test**

```rust
// This test spawns the daemon in-process, sends a RegisterWatchSha request,
// and asserts the watchlist row is created.

use hippo_core::protocol::{DaemonRequest, DaemonResponse};
// ... boilerplate from existing daemon integration tests (copy pattern
// from whatever tests/ file already exercises DaemonRequest round-trip —
// reuse its setup helpers rather than duplicating).

#[tokio::test]
async fn register_watch_sha_creates_row() {
    let harness = DaemonHarness::start_ephemeral().await;

    let resp = harness.send(&DaemonRequest::RegisterWatchSha {
        sha: "abc123".into(),
        repo: "me/repo".into(),
        ttl_secs: 1200,
    }).await.unwrap();

    assert!(matches!(resp, DaemonResponse::Ok));

    let conn = rusqlite::Connection::open(harness.db_path()).unwrap();
    let count: i64 = conn
        .query_row("SELECT count(*) FROM sha_watchlist WHERE sha = 'abc123'",
                   [], |r| r.get(0))
        .unwrap();
    assert_eq!(count, 1);
}
```

> **NOTE for the implementer:** if `DaemonHarness` does not exist, look for the pattern used by existing daemon integration tests (e.g., tests that exercise shell event ingestion). Extract the common setup into a shared helper module rather than inlining — future source additions will need the same harness.

- [ ] **Step 2: Add the request variant to `protocol.rs`**

```rust
// In the DaemonRequest enum:
RegisterWatchSha {
    sha: String,
    repo: String,
    ttl_secs: u64,
},
```

- [ ] **Step 3: Handle the new request in `daemon.rs`**

Locate the `DaemonRequest` match arm handler (search for existing `DaemonRequest::` cases) and add:

```rust
DaemonRequest::RegisterWatchSha { sha, repo, ttl_secs } => {
    let now = chrono::Utc::now().timestamp_millis();
    let expires = now + (ttl_secs as i64) * 1000;
    hippo_core::storage::watchlist::upsert(
        &conn_guard, sha, repo, now, expires,
    )?;
    DaemonResponse::Ok
}
```

- [ ] **Step 4: Run tests**

Run: `cargo test -p hippo-daemon --test watchlist_rpc`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-core/src/protocol.rs \
        crates/hippo-daemon/src/daemon.rs \
        crates/hippo-daemon/tests/watchlist_rpc.rs
git commit -m "feat(daemon): RegisterWatchSha protocol variant"
```

---

## Phase 4 — GitHub API Client

### Task 8: Write failing gh_api tests with wiremock

**Files:**
- Create: `crates/hippo-daemon/tests/gh_api.rs`

- [ ] **Step 1: Add dev-dependency on `wiremock`**

Edit `crates/hippo-daemon/Cargo.toml`:

```toml
[dev-dependencies]
wiremock = "0.6"
tokio = { version = "1", features = ["macros", "rt-multi-thread"] }
```

- [ ] **Step 2: Write the failing test**

```rust
use hippo_daemon::gh_api::{GhApi, ListRunsQuery};
use wiremock::matchers::{method, path, query_param, header};
use wiremock::{Mock, MockServer, ResponseTemplate};

#[tokio::test]
async fn list_runs_paginates_and_dedups() {
    let server = MockServer::start().await;

    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs"))
        .and(header("authorization", "Bearer test-token"))
        .and(query_param("per_page", "20"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "total_count": 1,
            "workflow_runs": [
                {
                    "id": 999,
                    "head_sha": "abc",
                    "head_branch": "main",
                    "status": "completed",
                    "conclusion": "success",
                    "event": "push",
                    "html_url": "https://github.com/me/repo/actions/runs/999",
                    "run_started_at": "2026-04-15T12:00:00Z",
                    "updated_at":    "2026-04-15T12:05:00Z",
                    "actor": {"login": "me"}
                }
            ]
        })))
        .mount(&server)
        .await;

    let api = GhApi::new(server.uri(), "test-token".into());
    let runs = api.list_runs("me/repo", &ListRunsQuery::default()).await.unwrap();

    assert_eq!(runs.len(), 1);
    assert_eq!(runs[0].id, 999);
    assert_eq!(runs[0].head_sha, "abc");
    assert_eq!(runs[0].conclusion.as_deref(), Some("success"));
}

#[tokio::test]
async fn rate_limit_respects_reset_header() {
    let server = MockServer::start().await;

    // First response: 429 with retry-after.
    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs"))
        .respond_with(ResponseTemplate::new(429).insert_header("retry-after", "1"))
        .up_to_n_times(1)
        .mount(&server).await;

    // Second response: 200 with empty body.
    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "total_count": 0, "workflow_runs": []
        })))
        .mount(&server).await;

    let api = GhApi::new(server.uri(), "test-token".into());
    let start = std::time::Instant::now();
    let runs = api.list_runs("me/repo", &ListRunsQuery::default()).await.unwrap();
    assert!(runs.is_empty());
    assert!(start.elapsed().as_secs() >= 1, "should have waited at least 1s");
}
```

- [ ] **Step 3: Run — should fail to compile (module missing)**

Run: `cargo test -p hippo-daemon --test gh_api`
Expected: compile error.

### Task 9: Implement GhApi client

**Files:**
- Create: `crates/hippo-daemon/src/gh_api.rs`
- Modify: `crates/hippo-daemon/src/lib.rs` (declare module)

- [ ] **Step 1: Implement the module**

```rust
//! GitHub REST client for the gh-poll subcommand.

use anyhow::{Context, Result, bail};
use reqwest::{Client, StatusCode, header};
use serde::Deserialize;
use std::time::Duration;

#[derive(Debug, Clone, Deserialize)]
pub struct WorkflowRun {
    pub id: i64,
    pub head_sha: String,
    pub head_branch: Option<String>,
    pub status: String,
    pub conclusion: Option<String>,
    pub event: String,
    pub html_url: String,
    pub run_started_at: Option<String>,
    pub updated_at: Option<String>,
    pub actor: Option<Actor>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Actor {
    pub login: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Job {
    pub id: i64,
    pub name: String,
    pub status: String,
    pub conclusion: Option<String>,
    pub started_at: Option<String>,
    pub completed_at: Option<String>,
    pub runner_name: Option<String>,
    pub check_run_url: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Annotation {
    pub annotation_level: String,
    pub message: String,
    pub path: Option<String>,
    pub start_line: Option<i64>,
}

#[derive(Debug, Default, Clone)]
pub struct ListRunsQuery {
    pub per_page: Option<u32>,
    pub created_since: Option<String>, // ISO8601
}

pub struct GhApi {
    base_url: String,
    token: String,
    http: Client,
}

impl GhApi {
    pub fn new(base_url: String, token: String) -> Self {
        let http = Client::builder()
            .user_agent(concat!("hippo-gh-poll/", env!("CARGO_PKG_VERSION")))
            .timeout(Duration::from_secs(30))
            .build()
            .expect("reqwest client");
        Self { base_url, token, http }
    }

    async fn get_json<T: for<'de> Deserialize<'de>>(&self, url: &str) -> Result<T> {
        loop {
            let resp = self.http.get(url)
                .header(header::AUTHORIZATION, format!("Bearer {}", self.token))
                .header(header::ACCEPT, "application/vnd.github+json")
                .header("X-GitHub-Api-Version", "2022-11-28")
                .send().await?;

            let status = resp.status();
            if status == StatusCode::TOO_MANY_REQUESTS ||
               (status == StatusCode::FORBIDDEN && resp.headers().get("x-ratelimit-remaining")
                   .and_then(|v| v.to_str().ok()) == Some("0"))
            {
                let wait = resp.headers().get("retry-after")
                    .and_then(|v| v.to_str().ok())
                    .and_then(|v| v.parse::<u64>().ok())
                    .unwrap_or(60);
                tokio::time::sleep(Duration::from_secs(wait)).await;
                continue;
            }
            if !status.is_success() {
                let body = resp.text().await.unwrap_or_default();
                bail!("GitHub API {status}: {body}");
            }
            return resp.json::<T>().await.context("parse GitHub response");
        }
    }

    pub async fn list_runs(&self, repo: &str, q: &ListRunsQuery) -> Result<Vec<WorkflowRun>> {
        #[derive(Deserialize)]
        struct Envelope { workflow_runs: Vec<WorkflowRun> }

        let per_page = q.per_page.unwrap_or(20);
        let mut url = format!("{}/repos/{repo}/actions/runs?per_page={per_page}",
                              self.base_url);
        if let Some(ref created) = q.created_since {
            url.push_str(&format!("&created=%3E={created}"));
        }
        let env: Envelope = self.get_json(&url).await?;
        Ok(env.workflow_runs)
    }

    pub async fn list_jobs(&self, repo: &str, run_id: i64) -> Result<Vec<Job>> {
        #[derive(Deserialize)]
        struct Envelope { jobs: Vec<Job> }
        let url = format!("{}/repos/{repo}/actions/runs/{run_id}/jobs", self.base_url);
        let env: Envelope = self.get_json(&url).await?;
        Ok(env.jobs)
    }

    pub async fn get_annotations(&self, check_run_url: &str) -> Result<Vec<Annotation>> {
        // check_run_url looks like:
        //   https://api.github.com/repos/{owner}/{repo}/check-runs/{id}
        let url = format!("{check_run_url}/annotations");
        self.get_json(&url).await
    }

    pub async fn get_log_tail(
        &self, repo: &str, job_id: i64, max_bytes: usize,
    ) -> Result<(String, bool)> {
        let url = format!("{}/repos/{repo}/actions/jobs/{job_id}/logs", self.base_url);
        let resp = self.http.get(&url)
            .header(header::AUTHORIZATION, format!("Bearer {}", self.token))
            .send().await?;
        let bytes = resp.bytes().await?;
        if bytes.len() <= max_bytes {
            return Ok((String::from_utf8_lossy(&bytes).to_string(), false));
        }
        let tail = &bytes[bytes.len() - max_bytes..];
        Ok((String::from_utf8_lossy(tail).to_string(), true))
    }
}
```

- [ ] **Step 2: Register module in `lib.rs`**

```rust
pub mod gh_api;
```

- [ ] **Step 3: Run tests — should pass**

Run: `cargo test -p hippo-daemon --test gh_api`
Expected: both tests pass. The rate-limit test takes ≥1s — expected.

- [ ] **Step 4: Commit**

```bash
git add crates/hippo-daemon/src/gh_api.rs \
        crates/hippo-daemon/src/lib.rs \
        crates/hippo-daemon/Cargo.toml \
        crates/hippo-daemon/tests/gh_api.rs
git commit -m "feat(daemon): GitHub REST client with rate-limit handling"
```

---

## Phase 5 — Poll Loop

### Task 10: Poll loop skeleton with integration test

**Files:**
- Create: `crates/hippo-daemon/src/gh_poll.rs`
- Create: `crates/hippo-daemon/tests/gh_poll_integration.rs`

- [ ] **Step 1: Write the failing integration test**

```rust
// Full round-trip: mock GH API, run one poll pass, assert DB state.

use hippo_daemon::gh_poll::{run_once, PollConfig};
use hippo_daemon::gh_api::GhApi;
use hippo_core::storage::open_db;
use wiremock::{Mock, MockServer, ResponseTemplate};
use wiremock::matchers::{method, path};
use tempfile::TempDir;

#[tokio::test]
async fn single_pass_inserts_runs_jobs_annotations() {
    let gh = MockServer::start().await;
    // Mock /runs, /jobs, /check-runs/{id}/annotations — one each.
    // (Use JSON fixtures checked into tests/fixtures/gh_*.json so the
    // test body stays readable.)
    mount_fixtures(&gh).await;

    let tmp = TempDir::new().unwrap();
    let db_path = tmp.path().join("hippo.db");
    let _ = open_db(&db_path).unwrap(); // initialize schema

    let api = GhApi::new(gh.uri(), "t".into());
    let cfg = PollConfig {
        watched_repos: vec!["me/repo".into()],
        log_excerpt_max_bytes: 1024,
        ..Default::default()
    };

    run_once(&api, &db_path, &cfg).await.unwrap();

    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let runs: i64 = conn.query_row("SELECT count(*) FROM workflow_runs", [], |r| r.get(0)).unwrap();
    assert_eq!(runs, 1);
    let jobs: i64 = conn.query_row("SELECT count(*) FROM workflow_jobs", [], |r| r.get(0)).unwrap();
    assert!(jobs >= 1);
    let queued: i64 = conn.query_row(
        "SELECT count(*) FROM workflow_enrichment_queue WHERE status='pending'",
        [], |r| r.get(0)).unwrap();
    assert_eq!(queued, 1);
}
```

> **NOTE:** put the fixture JSON under `crates/hippo-daemon/tests/fixtures/` with names like `gh_runs.json`, `gh_jobs.json`, `gh_annotations.json`. Capture real API responses (with secrets stripped) for realism.

- [ ] **Step 2: Stub the module so the test compiles**

```rust
//! Orchestrates a single poll pass over watched repos.

use anyhow::Result;
use crate::gh_api::GhApi;
use std::path::Path;

#[derive(Debug, Clone, Default)]
pub struct PollConfig {
    pub watched_repos: Vec<String>,
    pub log_excerpt_max_bytes: usize,
    pub tight_poll_repo_whitelist: Vec<String>,
}

pub async fn run_once(_api: &GhApi, _db_path: &Path, _cfg: &PollConfig) -> Result<()> {
    anyhow::bail!("unimplemented")
}
```

- [ ] **Step 3: Register module in `lib.rs`**

```rust
pub mod gh_poll;
```

- [ ] **Step 4: Run test — should fail**

Run: `cargo test -p hippo-daemon --test gh_poll_integration`
Expected: FAIL with "unimplemented".

### Task 11: Implement poll loop

**Files:**
- Modify: `crates/hippo-daemon/src/gh_poll.rs`
- Modify: `crates/hippo-core/src/storage.rs` — add `workflow_runs` insert helpers

- [ ] **Step 1: Add insert helpers to `storage.rs`**

Append to `storage.rs` (follow the existing browser_events insertion pattern):

```rust
pub mod workflow_store {
    use anyhow::Result;
    use rusqlite::{Connection, params};
    use crate::gh_annotations::parse as parse_annotation;

    pub struct RunRow<'a> {
        pub id: i64,
        pub repo: &'a str,
        pub head_sha: &'a str,
        pub head_branch: Option<&'a str>,
        pub event: &'a str,
        pub status: &'a str,
        pub conclusion: Option<&'a str>,
        pub started_at: Option<i64>,
        pub completed_at: Option<i64>,
        pub html_url: &'a str,
        pub actor: Option<&'a str>,
        pub raw_json: &'a str,
    }

    pub fn upsert_run(conn: &Connection, run: &RunRow, now_ms: i64) -> Result<()> {
        conn.execute(
            "INSERT INTO workflow_runs
                (id, repo, head_sha, head_branch, event, status, conclusion,
                 started_at, completed_at, html_url, actor, raw_json,
                 first_seen_at, last_seen_at)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?13)
             ON CONFLICT(id) DO UPDATE SET
                status=excluded.status, conclusion=excluded.conclusion,
                completed_at=excluded.completed_at, last_seen_at=excluded.last_seen_at,
                raw_json=excluded.raw_json",
            params![
                run.id, run.repo, run.head_sha, run.head_branch, run.event,
                run.status, run.conclusion, run.started_at, run.completed_at,
                run.html_url, run.actor, run.raw_json, now_ms,
            ],
        )?;
        Ok(())
    }

    pub struct JobRow<'a> {
        pub id: i64,
        pub run_id: i64,
        pub name: &'a str,
        pub status: &'a str,
        pub conclusion: Option<&'a str>,
        pub started_at: Option<i64>,
        pub completed_at: Option<i64>,
        pub runner_name: Option<&'a str>,
        pub raw_json: &'a str,
    }

    pub fn upsert_job(conn: &Connection, job: &JobRow) -> Result<()> {
        conn.execute(
            "INSERT INTO workflow_jobs
                (id, run_id, name, status, conclusion, started_at, completed_at,
                 runner_name, raw_json)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)
             ON CONFLICT(id) DO UPDATE SET
                status=excluded.status, conclusion=excluded.conclusion,
                completed_at=excluded.completed_at, raw_json=excluded.raw_json",
            params![
                job.id, job.run_id, job.name, job.status, job.conclusion,
                job.started_at, job.completed_at, job.runner_name, job.raw_json,
            ],
        )?;
        Ok(())
    }

    pub fn insert_annotation(
        conn: &Connection, job_id: i64, job_name: &str,
        level: &str, message: &str, path: Option<&str>, start_line: Option<i64>,
    ) -> Result<()> {
        let parsed = parse_annotation(job_name, message);
        conn.execute(
            "INSERT INTO workflow_annotations
                (job_id, level, tool, rule_id, path, start_line, message)
             VALUES (?1,?2,?3,?4,?5,?6,?7)",
            params![
                job_id, level, parsed.tool, parsed.rule_id, path, start_line, message,
            ],
        )?;
        Ok(())
    }

    pub fn insert_log_excerpt(
        conn: &Connection, job_id: i64, step_name: Option<&str>,
        excerpt: &str, truncated: bool,
    ) -> Result<()> {
        conn.execute(
            "INSERT INTO workflow_log_excerpts (job_id, step_name, excerpt, truncated)
             VALUES (?1,?2,?3,?4)",
            params![job_id, step_name, excerpt, truncated as i64],
        )?;
        Ok(())
    }

    pub fn enqueue_enrichment(conn: &Connection, run_id: i64, now_ms: i64) -> Result<()> {
        conn.execute(
            "INSERT INTO workflow_enrichment_queue (run_id, enqueued_at, updated_at)
             VALUES (?1, ?2, ?2)
             ON CONFLICT(run_id) DO NOTHING",
            params![run_id, now_ms],
        )?;
        Ok(())
    }
}
```

- [ ] **Step 2: Implement `run_once` in `gh_poll.rs`**

```rust
use anyhow::Result;
use chrono::Utc;
use hippo_core::storage::{open_db, workflow_store, watchlist};
use std::path::Path;

use crate::gh_api::{GhApi, ListRunsQuery};

fn parse_ts(s: Option<&str>) -> Option<i64> {
    s.and_then(|v| chrono::DateTime::parse_from_rfc3339(v).ok())
        .map(|dt| dt.timestamp_millis())
}

pub async fn run_once(api: &GhApi, db_path: &Path, cfg: &PollConfig) -> Result<()> {
    let conn = open_db(db_path)?;
    let now = Utc::now().timestamp_millis();

    for repo in &cfg.watched_repos {
        let runs = api.list_runs(repo, &ListRunsQuery {
            per_page: Some(20),
            ..Default::default()
        }).await?;

        for run in runs {
            let actor = run.actor.as_ref().map(|a| a.login.as_str());
            let raw = serde_json::to_string(&run)?;
            workflow_store::upsert_run(&conn, &workflow_store::RunRow {
                id: run.id,
                repo,
                head_sha: &run.head_sha,
                head_branch: run.head_branch.as_deref(),
                event: &run.event,
                status: &run.status,
                conclusion: run.conclusion.as_deref(),
                started_at: parse_ts(run.run_started_at.as_deref()),
                completed_at: parse_ts(run.updated_at.as_deref()),
                html_url: &run.html_url,
                actor,
                raw_json: &raw,
            }, now)?;

            // Watchlist linkage: if this SHA is on the watchlist and run is terminal,
            // mark it so the SessionStart hook can notify.
            if run.status == "completed" {
                if let Some(concl) = run.conclusion.as_deref() {
                    watchlist::mark_terminal(&conn, &run.head_sha, repo, concl)?;
                }

                // Drill-down: jobs + annotations + maybe logs.
                let jobs = api.list_jobs(repo, run.id).await?;
                for job in &jobs {
                    let job_raw = serde_json::to_string(job)?;
                    workflow_store::upsert_job(&conn, &workflow_store::JobRow {
                        id: job.id, run_id: run.id, name: &job.name,
                        status: &job.status, conclusion: job.conclusion.as_deref(),
                        started_at: parse_ts(job.started_at.as_deref()),
                        completed_at: parse_ts(job.completed_at.as_deref()),
                        runner_name: job.runner_name.as_deref(),
                        raw_json: &job_raw,
                    })?;

                    if let Some(cru) = &job.check_run_url {
                        let annotations = api.get_annotations(cru).await.unwrap_or_default();
                        for a in annotations {
                            workflow_store::insert_annotation(
                                &conn, job.id, &job.name,
                                &a.annotation_level, &a.message,
                                a.path.as_deref(), a.start_line,
                            )?;
                        }
                    }

                    if matches!(job.conclusion.as_deref(), Some("failure") | Some("cancelled"))
                    {
                        if let Ok((excerpt, truncated)) = api
                            .get_log_tail(repo, job.id, cfg.log_excerpt_max_bytes).await
                        {
                            workflow_store::insert_log_excerpt(
                                &conn, job.id, None, &excerpt, truncated)?;
                        }
                    }
                }

                workflow_store::enqueue_enrichment(&conn, run.id, now)?;
            }
        }
    }

    Ok(())
}
```

- [ ] **Step 3: Run integration test — should pass**

Run: `cargo test -p hippo-daemon --test gh_poll_integration`
Expected: PASS. If the fixtures are wrong, iterate on them until one clean pass.

- [ ] **Step 4: Commit**

```bash
git add crates/hippo-daemon/src/gh_poll.rs \
        crates/hippo-daemon/src/lib.rs \
        crates/hippo-core/src/storage.rs \
        crates/hippo-daemon/tests/gh_poll_integration.rs \
        crates/hippo-daemon/tests/fixtures/gh_*.json
git commit -m "feat(daemon): gh-poll loop with annotations and log tails"
```

---

## Phase 6 — CLI Subcommand, Config, launchd

### Task 12: Add `gh-poll` subcommand to CLI

**Files:**
- Modify: `crates/hippo-daemon/src/cli.rs`
- Modify: `crates/hippo-daemon/src/commands.rs`
- Modify: `crates/hippo-daemon/src/main.rs` (route subcommand)

- [ ] **Step 1: Add the variant to `Commands` in `cli.rs`**

```rust
/// Run one pass of the GitHub Actions poller.
GhPoll {
    /// Override config; mainly for testing.
    #[arg(long)]
    repo: Option<String>,
},
```

- [ ] **Step 2: Route it in `commands.rs` / `main.rs`**

```rust
Commands::GhPoll { repo } => {
    let cfg = hippo_core::config::HippoConfig::load()?;
    let token = std::env::var(&cfg.github.token_env)
        .map_err(|_| anyhow!("env var {} not set", cfg.github.token_env))?;
    let api = hippo_daemon::gh_api::GhApi::new(
        "https://api.github.com".into(), token,
    );
    let poll_cfg = hippo_daemon::gh_poll::PollConfig {
        watched_repos: repo.map(|r| vec![r]).unwrap_or(cfg.github.watched_repos),
        log_excerpt_max_bytes: cfg.github.log_excerpt_max_bytes,
        ..Default::default()
    };
    hippo_daemon::gh_poll::run_once(&api, &cfg.storage.db_path, &poll_cfg).await?;
}
```

- [ ] **Step 3: Smoke test**

Run: `cargo run -p hippo-daemon -- gh-poll --help`
Expected: help text displays.

- [ ] **Step 4: Commit**

```bash
git add crates/hippo-daemon/src/cli.rs \
        crates/hippo-daemon/src/commands.rs \
        crates/hippo-daemon/src/main.rs
git commit -m "feat(cli): hippo gh-poll subcommand"
```

### Task 13: Config additions

**Files:**
- Modify: `crates/hippo-core/src/config.rs`

- [ ] **Step 1: Add `GithubConfig` struct and field on `HippoConfig`**

```rust
#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct GithubConfig {
    pub enabled: bool,
    pub poll_interval_secs: u64,
    pub tight_poll_interval_secs: u64,
    pub watchlist_ttl_secs: u64,
    pub log_excerpt_max_bytes: usize,
    pub watched_repos: Vec<String>,
    pub token_env: String,
    pub lessons: LessonsConfig,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct LessonsConfig {
    pub cluster_window_days: u32,
    pub min_occurrences: u32,
    pub path_prefix_segments: u32,
}

impl Default for GithubConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            poll_interval_secs: 300,
            tight_poll_interval_secs: 45,
            watchlist_ttl_secs: 1200,
            log_excerpt_max_bytes: 51_200,
            watched_repos: vec![],
            token_env: "HIPPO_GITHUB_TOKEN".into(),
            lessons: LessonsConfig::default(),
        }
    }
}

impl Default for LessonsConfig {
    fn default() -> Self {
        Self { cluster_window_days: 30, min_occurrences: 2, path_prefix_segments: 2 }
    }
}
```

- [ ] **Step 2: Add `pub github: GithubConfig` to `HippoConfig` with default**

- [ ] **Step 3: Write a config-parsing test**

```rust
#[test]
fn parses_github_section() {
    let toml = r#"
        [github]
        enabled = true
        watched_repos = ["sjcarpenter/hippo"]
        [github.lessons]
        min_occurrences = 3
    "#;
    let cfg: HippoConfig = toml::from_str(toml).unwrap();
    assert!(cfg.github.enabled);
    assert_eq!(cfg.github.watched_repos, vec!["sjcarpenter/hippo"]);
    assert_eq!(cfg.github.lessons.min_occurrences, 3);
}
```

- [ ] **Step 4: Run tests**

Run: `cargo test -p hippo-core`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-core/src/config.rs
git commit -m "feat(core): [github] config section"
```

### Task 14: launchd plist for gh-poll

**Files:**
- Create: `launchd/com.hippo.gh-poll.plist`

- [ ] **Step 1: Create the plist**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.hippo.gh-poll</string>
    <key>ProgramArguments</key>
    <array>
        <string>HIPPO_BIN_PLACEHOLDER</string>
        <string>gh-poll</string>
    </array>
    <key>StartInterval</key><integer>300</integer>
    <key>RunAtLoad</key><false/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HIPPO_GITHUB_TOKEN</key><string>TOKEN_PLACEHOLDER</string>
        <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>LOG_PLACEHOLDER/gh-poll.out.log</string>
    <key>StandardErrorPath</key>
    <string>LOG_PLACEHOLDER/gh-poll.err.log</string>
</dict>
</plist>
```

Placeholders are substituted at install time by `install.rs` — follow the existing `com.hippo.daemon.plist` install flow for the string replacement logic.

- [ ] **Step 2: Commit**

```bash
git add launchd/com.hippo.gh-poll.plist
git commit -m "feat: gh-poll launchd job"
```

### Task 15: Update install.rs to handle new plist

**Files:**
- Modify: `crates/hippo-daemon/src/install.rs`

- [ ] **Step 1: Add a new plist entry to the install list**

Locate the install registry (existing code that handles `com.hippo.daemon.plist` and `com.hippo.brain.plist`). Add a third entry for `com.hippo.gh-poll.plist`. Gate it on `cfg.github.enabled` — if disabled, `hippo daemon install` should skip loading this plist (but still write it for easy manual enable).

- [ ] **Step 2: Token substitution from env**

At install time, read `$HIPPO_GITHUB_TOKEN` from the installer's env and substitute into `TOKEN_PLACEHOLDER`. If the env var is missing AND `github.enabled = true`, fail the install with a clear error message: "HIPPO_GITHUB_TOKEN must be set to enable the github source."

- [ ] **Step 3: Run install flow manually**

Run: `cargo run -p hippo-daemon -- daemon install --force` (with `[github]` enabled in config + token env set)
Expected: plist appears in `~/Library/LaunchAgents/` and `launchctl list | grep gh-poll` shows it loaded.

- [ ] **Step 4: Commit**

```bash
git add crates/hippo-daemon/src/install.rs
git commit -m "feat(install): wire gh-poll plist into install flow"
```

---

## Phase 7 — Claude Code Hooks

### Task 16: PostToolUse hook on `git push`

**Files:**
- Create: `shell/claude-hooks/post-git-push.sh`

- [ ] **Step 1: Write the hook script**

```bash
#!/usr/bin/env bash
# Claude Code PostToolUse hook — registers a pushed SHA in the hippo watchlist.
# Invoked by Claude Code with JSON on stdin: { tool_name, tool_input, ... }
# Matcher (in settings.json): tool_name == "Bash" && command matches 'git push'
set -euo pipefail

# Read tool input; only act when the command is a git push.
input=$(cat)
cmd=$(echo "$input" | jq -r '.tool_input.command // ""')
if [[ "$cmd" != *"git push"* ]]; then
    exit 0
fi

# Determine SHA and repo (resolve from the project cwd).
cwd=$(echo "$input" | jq -r '.cwd // ""')
[[ -n "$cwd" ]] || exit 0
cd "$cwd" || exit 0

sha=$(git rev-parse HEAD 2>/dev/null) || exit 0
remote=$(git config --get remote.origin.url 2>/dev/null) || exit 0
# Extract owner/repo from either https or ssh remote.
repo=$(echo "$remote" | sed -E 's#(git@github\.com:|https://github\.com/)(.*)\.git#\2#' | head -1)
[[ -n "$repo" ]] || exit 0

# Fire-and-forget to the daemon.
hippo send-event watchlist --sha "$sha" --repo "$repo" --ttl 1200 >/dev/null 2>&1 || true
```

> **NOTE:** this hook depends on a `hippo send-event watchlist` CLI path. If that doesn't exist yet, add it as a thin subcommand that calls `commands::send_request` with `DaemonRequest::RegisterWatchSha`. That is part of this task.

- [ ] **Step 2: Make it executable**

Run: `chmod +x shell/claude-hooks/post-git-push.sh`

- [ ] **Step 3: Add the CLI plumbing for `hippo send-event watchlist`**

In `cli.rs` under `SendEventSource`, add a `Watchlist` variant with `--sha`, `--repo`, `--ttl` args. Route it in `commands.rs` to issue `DaemonRequest::RegisterWatchSha`.

- [ ] **Step 4: Manual smoke test**

Run:
```bash
hippo send-event watchlist --sha testsha123 --repo me/repo --ttl 60
sqlite3 ~/.local/share/hippo/hippo.db "SELECT * FROM sha_watchlist"
```
Expected: one row for `testsha123`.

- [ ] **Step 5: Commit**

```bash
git add shell/claude-hooks/post-git-push.sh \
        crates/hippo-daemon/src/cli.rs \
        crates/hippo-daemon/src/commands.rs
git commit -m "feat(hooks): PostToolUse hook for git push → watchlist"
```

### Task 17: SessionStart hook for pending failures

**Files:**
- Create: `shell/claude-hooks/pending-ci-session-start.sh`

- [ ] **Step 1: Write the hook**

```bash
#!/usr/bin/env bash
# Claude Code SessionStart hook — injects a notice if CI has failed on
# a SHA pushed from this repo since the last session.
set -euo pipefail

input=$(cat)
cwd=$(echo "$input" | jq -r '.cwd // ""')
[[ -n "$cwd" ]] || exit 0
cd "$cwd" || exit 0

remote=$(git config --get remote.origin.url 2>/dev/null) || exit 0
repo=$(echo "$remote" | sed -E 's#(git@github\.com:|https://github\.com/)(.*)\.git#\2#' | head -1)
[[ -n "$repo" ]] || exit 0

# Ask hippo for pending notifications for this repo.
# The hippo CLI acks the notifications (marks them notified) so we don't
# re-inject on the next session.
pending=$(hippo gh-pending-notifications --repo "$repo" --ack 2>/dev/null || echo "")
if [[ -n "$pending" ]]; then
    # Output JSON per Claude SessionStart hook spec for additionalContext.
    jq -n --arg msg "$pending" '{
        hookSpecificOutput: {
            hookEventName: "SessionStart",
            additionalContext: $msg
        }
    }'
fi
```

> **NOTE:** needs a new `hippo gh-pending-notifications --repo <r> --ack` CLI path that queries `watchlist::pending_notifications`, returns a human-readable multi-line string, and marks acked entries with `watchlist::mark_notified`. Add that as part of this task.

- [ ] **Step 2: Add the CLI subcommand**

In `cli.rs`:

```rust
/// List (and ack) pending CI failure notifications for a repo.
GhPendingNotifications {
    #[arg(long)]
    repo: String,
    /// Mark retrieved notifications as acknowledged.
    #[arg(long)]
    ack: bool,
},
```

Route in `commands.rs`: query `watchlist::pending_notifications`, filter by repo, print "CI failed on SHA X pushed at Y; use get_ci_status to investigate" lines, mark_notified if --ack.

- [ ] **Step 3: Make executable and smoke-test**

Run: `chmod +x shell/claude-hooks/pending-ci-session-start.sh`

Manual: seed a pending-failure row, invoke the script with a synthesized SessionStart JSON on stdin, confirm JSON output.

- [ ] **Step 4: Commit**

```bash
git add shell/claude-hooks/pending-ci-session-start.sh \
        crates/hippo-daemon/src/cli.rs \
        crates/hippo-daemon/src/commands.rs
git commit -m "feat(hooks): SessionStart hook surfaces pending CI failures"
```

---

## Phase 8 — Python: Models and Query Helpers

### Task 18: Dataclasses for CIStatus, Lesson, Annotation

**Files:**
- Modify: `brain/src/hippo_brain/models.py`

- [ ] **Step 1: Add the models**

```python
from dataclasses import dataclass, field

@dataclass
class CIAnnotation:
    level: str
    tool: str | None
    rule_id: str | None
    path: str | None
    start_line: int | None
    message: str

@dataclass
class CIJob:
    id: int
    name: str
    conclusion: str | None
    started_at: int | None
    completed_at: int | None
    annotations: list[CIAnnotation] = field(default_factory=list)

@dataclass
class CIStatus:
    run_id: int
    repo: str
    head_sha: str
    head_branch: str | None
    status: str
    conclusion: str | None
    started_at: int | None
    completed_at: int | None
    html_url: str
    jobs: list[CIJob] = field(default_factory=list)

@dataclass
class Lesson:
    id: int
    repo: str
    tool: str | None
    rule_id: str | None
    path_prefix: str | None
    summary: str
    fix_hint: str | None
    occurrences: int
    first_seen_at: int
    last_seen_at: int
```

- [ ] **Step 2: Commit**

```bash
git add brain/src/hippo_brain/models.py
git commit -m "feat(brain): CIStatus, CIJob, CIAnnotation, Lesson dataclasses"
```

### Task 19: `get_ci_status_impl` query

**Files:**
- Modify: `brain/src/hippo_brain/mcp_queries.py`
- Create: `brain/tests/test_mcp_queries_gh.py`

- [ ] **Step 1: Write failing test**

```python
import sqlite3
from pathlib import Path
import pytest
from hippo_brain.mcp_queries import get_ci_status_impl
from hippo_brain.models import CIStatus

@pytest.fixture
def db_with_run(tmp_path: Path) -> Path:
    db = tmp_path / "hippo.db"
    conn = sqlite3.connect(db)
    # Minimal v5 schema for the query under test.
    conn.executescript(Path(__file__).parent.parent
        .joinpath("src/hippo_brain/_fixtures/schema_v5_min.sql").read_text())
    conn.execute("""
        INSERT INTO workflow_runs
          (id, repo, head_sha, event, status, conclusion, html_url,
           raw_json, first_seen_at, last_seen_at)
        VALUES (1, 'me/r', 'abc', 'push', 'completed', 'failure',
                'https://x', '{}', 1000, 2000)
    """)
    conn.execute("""
        INSERT INTO workflow_jobs
          (id, run_id, name, status, conclusion, raw_json)
        VALUES (10, 1, 'lint', 'completed', 'failure', '{}')
    """)
    conn.execute("""
        INSERT INTO workflow_annotations
          (job_id, level, tool, rule_id, path, start_line, message)
        VALUES (10, 'failure', 'ruff', 'F401', 'brain/x.py', 3,
                'F401 unused import')
    """)
    conn.commit()
    return db

def test_get_ci_status_by_sha(db_with_run: Path):
    status = get_ci_status_impl(str(db_with_run), repo="me/r", sha="abc")
    assert isinstance(status, CIStatus)
    assert status.conclusion == "failure"
    assert len(status.jobs) == 1
    assert status.jobs[0].annotations[0].rule_id == "F401"

def test_get_ci_status_missing_returns_none(db_with_run: Path):
    status = get_ci_status_impl(str(db_with_run), repo="me/r", sha="zzz")
    assert status is None
```

> **NOTE:** the fixture SQL file `_fixtures/schema_v5_min.sql` contains the subset of v5 tables needed for MCP tests (workflow_runs, workflow_jobs, workflow_annotations, lessons, knowledge_nodes stub). Create it verbatim by excerpting from the Rust `schema.sql`.

- [ ] **Step 2: Run tests — should fail**

Run: `uv run --project brain pytest brain/tests/test_mcp_queries_gh.py -v`
Expected: FAIL (import error / function missing).

- [ ] **Step 3: Implement `get_ci_status_impl` in `mcp_queries.py`**

```python
def get_ci_status_impl(
    db_path: str,
    repo: str,
    sha: str | None = None,
    branch: str | None = None,
) -> CIStatus | None:
    """Return the most recent completed workflow run for (repo, sha|branch).

    If `sha` is given, prefer the latest run on that SHA.
    If `branch` is given (no SHA), return the latest run on that branch.
    Returns None if no matching run exists.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if sha:
            cur = conn.execute("""
                SELECT id, repo, head_sha, head_branch, status, conclusion,
                       started_at, completed_at, html_url
                FROM workflow_runs
                WHERE repo = ? AND head_sha = ?
                ORDER BY started_at DESC LIMIT 1
            """, (repo, sha))
        elif branch:
            cur = conn.execute("""
                SELECT id, repo, head_sha, head_branch, status, conclusion,
                       started_at, completed_at, html_url
                FROM workflow_runs
                WHERE repo = ? AND head_branch = ?
                ORDER BY started_at DESC LIMIT 1
            """, (repo, branch))
        else:
            raise ValueError("must supply sha or branch")

        row = cur.fetchone()
        if row is None:
            return None

        status = CIStatus(
            run_id=row["id"], repo=row["repo"], head_sha=row["head_sha"],
            head_branch=row["head_branch"], status=row["status"],
            conclusion=row["conclusion"], started_at=row["started_at"],
            completed_at=row["completed_at"], html_url=row["html_url"],
        )

        jobs_cur = conn.execute("""
            SELECT id, name, conclusion, started_at, completed_at
            FROM workflow_jobs WHERE run_id = ? ORDER BY started_at
        """, (row["id"],))
        for j in jobs_cur.fetchall():
            job = CIJob(
                id=j["id"], name=j["name"], conclusion=j["conclusion"],
                started_at=j["started_at"], completed_at=j["completed_at"],
            )
            ann_cur = conn.execute("""
                SELECT level, tool, rule_id, path, start_line, message
                FROM workflow_annotations WHERE job_id = ? LIMIT 10
            """, (j["id"],))
            for a in ann_cur.fetchall():
                job.annotations.append(CIAnnotation(
                    level=a["level"], tool=a["tool"], rule_id=a["rule_id"],
                    path=a["path"], start_line=a["start_line"], message=a["message"],
                ))
            status.jobs.append(job)

        return status
    finally:
        conn.close()
```

- [ ] **Step 4: Run — should pass**

Run: `uv run --project brain pytest brain/tests/test_mcp_queries_gh.py -v`
Expected: both tests pass.

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/mcp_queries.py \
        brain/src/hippo_brain/_fixtures/schema_v5_min.sql \
        brain/tests/test_mcp_queries_gh.py
git commit -m "feat(brain): get_ci_status query"
```

### Task 20: `get_lessons_impl` query

**Files:**
- Modify: `brain/src/hippo_brain/mcp_queries.py`
- Modify: `brain/tests/test_mcp_queries_gh.py` (append)

- [ ] **Step 1: Write failing test**

```python
def test_get_lessons_filters(db_with_run: Path):
    conn = sqlite3.connect(db_with_run)
    conn.execute("""
        INSERT INTO lessons
          (repo, tool, rule_id, path_prefix, summary, fix_hint,
           occurrences, first_seen_at, last_seen_at)
        VALUES
          ('me/r', 'ruff', 'F401', 'brain/',
           'unused imports in brain/', 'remove import', 4, 1000, 5000),
          ('me/r', 'pytest', NULL, 'brain/tests/',
           'flaky ordering', NULL, 2, 3000, 7000)
    """)
    conn.commit()
    conn.close()

    from hippo_brain.mcp_queries import get_lessons_impl
    all_ = get_lessons_impl(str(db_with_run), repo="me/r")
    assert len(all_) == 2
    assert all_[0].occurrences == 4  # ordered by occurrences DESC

    only_ruff = get_lessons_impl(str(db_with_run), tool="ruff")
    assert len(only_ruff) == 1
    assert only_ruff[0].rule_id == "F401"

    by_path = get_lessons_impl(str(db_with_run), path="brain/tests/")
    assert len(by_path) == 1
    assert by_path[0].tool == "pytest"
```

- [ ] **Step 2: Implement**

```python
def get_lessons_impl(
    db_path: str,
    repo: str | None = None,
    path: str | None = None,
    tool: str | None = None,
    limit: int = 10,
) -> list[Lesson]:
    clauses = []
    params: list = []
    if repo:
        clauses.append("repo = ?"); params.append(repo)
    if tool:
        clauses.append("tool = ?"); params.append(tool)
    if path:
        clauses.append("path_prefix = ?"); params.append(path)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(min(limit, MAX_LIMIT))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""SELECT id, repo, tool, rule_id, path_prefix, summary, fix_hint,
                       occurrences, first_seen_at, last_seen_at
                FROM lessons {where}
                ORDER BY occurrences DESC, last_seen_at DESC
                LIMIT ?""",
            params,
        ).fetchall()
    finally:
        conn.close()

    return [
        Lesson(
            id=r["id"], repo=r["repo"], tool=r["tool"], rule_id=r["rule_id"],
            path_prefix=r["path_prefix"], summary=r["summary"],
            fix_hint=r["fix_hint"], occurrences=r["occurrences"],
            first_seen_at=r["first_seen_at"], last_seen_at=r["last_seen_at"],
        )
        for r in rows
    ]
```

- [ ] **Step 3: Run — should pass**

Run: `uv run --project brain pytest brain/tests/test_mcp_queries_gh.py -v`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add brain/src/hippo_brain/mcp_queries.py brain/tests/test_mcp_queries_gh.py
git commit -m "feat(brain): get_lessons query with filters"
```

---

## Phase 9 — Lesson Clustering

### Task 21: Clustering logic

**Files:**
- Create: `brain/src/hippo_brain/lessons.py`
- Create: `brain/tests/test_lessons.py`

- [ ] **Step 1: Write failing test**

```python
import sqlite3
from pathlib import Path
import pytest
from hippo_brain.lessons import upsert_cluster, ClusterKey

def test_first_occurrence_does_not_create_lesson(db_path):
    # A single failure does not graduate to a lesson (min_occurrences=2).
    key = ClusterKey(repo="me/r", tool="ruff", rule_id="F401", path_prefix="brain/")
    promoted = upsert_cluster(db_path, key, min_occurrences=2,
                              summary_fn=lambda k: "unused imports",
                              now_ms=1000)
    assert promoted is False
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT count(*) FROM lessons").fetchone()[0] == 0

def test_second_occurrence_promotes_and_synthesizes(db_path):
    key = ClusterKey(repo="me/r", tool="ruff", rule_id="F401", path_prefix="brain/")
    upsert_cluster(db_path, key, min_occurrences=2,
                   summary_fn=lambda k: "unused imports", now_ms=1000)
    promoted = upsert_cluster(db_path, key, min_occurrences=2,
                              summary_fn=lambda k: "unused imports", now_ms=2000)
    assert promoted is True
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT occurrences, summary, last_seen_at FROM lessons"
    ).fetchone()
    assert row == (2, "unused imports", 2000)
```

> **NOTE:** `db_path` fixture should create a temp SQLite file and execute the same `_fixtures/schema_v5_min.sql` used by Task 19.

- [ ] **Step 2: Implement**

```python
"""Lesson clustering: promote repeat-failure patterns into queryable lessons."""

import sqlite3
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ClusterKey:
    repo: str
    tool: str | None
    rule_id: str | None
    path_prefix: str | None


def upsert_cluster(
    db_path: str,
    key: ClusterKey,
    min_occurrences: int,
    summary_fn: Callable[[ClusterKey], str],
    now_ms: int,
    fix_hint_fn: Callable[[ClusterKey], str | None] | None = None,
) -> bool:
    """Register a cluster occurrence.

    Returns True if a lesson row now exists (either newly promoted or already
    present); False if this is the first occurrence and min_occurrences not met.
    """
    conn = sqlite3.connect(db_path)
    try:
        # Probe existing row.
        row = conn.execute(
            """SELECT id, occurrences FROM lessons
               WHERE repo=? AND tool IS ? AND rule_id IS ? AND path_prefix IS ?""",
            (key.repo, key.tool, key.rule_id, key.path_prefix),
        ).fetchone()

        if row is not None:
            conn.execute(
                "UPDATE lessons SET occurrences = occurrences + 1, last_seen_at = ? "
                "WHERE id = ?",
                (now_ms, row[0]),
            )
            conn.commit()
            return True

        # New cluster — track the occurrence count in a side-channel key-value.
        # For simplicity store a pending row with occurrences=1 once min_occurrences>=2
        # is reached on the *next* call.
        _increment_pending(conn, key, now_ms)
        count = _pending_count(conn, key)
        if count < min_occurrences:
            conn.commit()
            return False

        summary = summary_fn(key)
        fix_hint = fix_hint_fn(key) if fix_hint_fn else None
        conn.execute(
            """INSERT INTO lessons
               (repo, tool, rule_id, path_prefix, summary, fix_hint,
                occurrences, first_seen_at, last_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (key.repo, key.tool, key.rule_id, key.path_prefix,
             summary, fix_hint, count, now_ms, now_ms),
        )
        _clear_pending(conn, key)
        conn.commit()
        return True
    finally:
        conn.close()


# Helpers _increment_pending / _pending_count / _clear_pending use a small
# auxiliary table `lesson_pending(repo, tool, rule_id, path_prefix, count,
# first_seen_at)` which must be added to schema v5. See Task 21a.
```

- [ ] **Step 3: Add the `lesson_pending` table to schema v5**

Go back to `crates/hippo-core/src/schema.sql` and the v4→v5 migration in `storage.rs`. Append:

```sql
CREATE TABLE IF NOT EXISTS lesson_pending (
    repo          TEXT NOT NULL,
    tool          TEXT,
    rule_id       TEXT,
    path_prefix   TEXT,
    count         INTEGER NOT NULL DEFAULT 1,
    first_seen_at INTEGER NOT NULL,
    UNIQUE(repo, tool, rule_id, path_prefix)
);
```

Bump this into the same v5 migration block (not a new version) since the spec implementation is still open.

Re-run `cargo test -p hippo-core --test schema_v5_migration` and amend the test to assert `lesson_pending` exists.

- [ ] **Step 4: Implement the pending helpers**

```python
def _increment_pending(conn, key, now_ms):
    conn.execute(
        """INSERT INTO lesson_pending (repo, tool, rule_id, path_prefix, count, first_seen_at)
           VALUES (?,?,?,?,1,?)
           ON CONFLICT(repo,tool,rule_id,path_prefix) DO UPDATE SET count = count + 1""",
        (key.repo, key.tool, key.rule_id, key.path_prefix, now_ms),
    )

def _pending_count(conn, key) -> int:
    row = conn.execute(
        """SELECT count FROM lesson_pending
           WHERE repo=? AND tool IS ? AND rule_id IS ? AND path_prefix IS ?""",
        (key.repo, key.tool, key.rule_id, key.path_prefix),
    ).fetchone()
    return row[0] if row else 0

def _clear_pending(conn, key):
    conn.execute(
        """DELETE FROM lesson_pending
           WHERE repo=? AND tool IS ? AND rule_id IS ? AND path_prefix IS ?""",
        (key.repo, key.tool, key.rule_id, key.path_prefix),
    )
```

- [ ] **Step 5: Run tests — should pass**

Run: `uv run --project brain pytest brain/tests/test_lessons.py -v && cargo test -p hippo-core`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add brain/src/hippo_brain/lessons.py \
        brain/tests/test_lessons.py \
        crates/hippo-core/src/schema.sql \
        crates/hippo-core/src/storage.rs \
        crates/hippo-core/tests/schema_v5_migration.rs
git commit -m "feat(brain): lesson clustering with promotion threshold"
```

---

## Phase 10 — Change-Outcome Enrichment

### Task 22: Workflow enrichment pass

**Files:**
- Create: `brain/src/hippo_brain/workflow_enrichment.py`
- Create: `brain/tests/test_workflow_enrichment.py`

- [ ] **Step 1: Write failing test**

```python
# A workflow_run with co-temporal shell 'git push' event should yield a
# knowledge node linked to both events.
def test_enrichment_links_workflow_to_push(db_path, fake_lm_client):
    # Seed an events row with the same SHA within ±15 min of the run's start.
    # Seed a workflow_runs row with enrichment queued.
    # Run the enrichment worker once.
    # Assert a knowledge_nodes row exists with edges to both.
    ...
```

*(Test body follows the existing `test_browser_enrichment.py` pattern — reuse fake LM client, fixture events, and assertion helpers.)*

- [ ] **Step 2: Implement the module**

```python
"""Change-outcome enrichment: join workflow_runs to co-temporal shell/claude events.

Run by the brain's scheduler alongside claude/shell/browser enrichment.
"""

import json
import sqlite3
import time
from pathlib import Path

from hippo_brain.client import LMStudioClient
from hippo_brain.embeddings import embed_and_store

CORRELATION_WINDOW_MS = 15 * 60 * 1000


def enrich_one(db_path: str, run_id: int, lm: LMStudioClient, query_model: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute(
            "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if run is None:
            return

        # Find co-temporal shell events by SHA (preferred) or timestamp window.
        shell_rows = conn.execute(
            """SELECT id, payload FROM events
               WHERE payload LIKE ? OR (timestamp_ms BETWEEN ? AND ?)""",
            (f'%{run["head_sha"]}%',
             (run["started_at"] or 0) - CORRELATION_WINDOW_MS,
             (run["started_at"] or 0) + CORRELATION_WINDOW_MS),
        ).fetchall()

        # Find co-temporal claude sessions.
        claude_rows = conn.execute(
            """SELECT id, session_id, summary_text FROM claude_sessions
               WHERE start_time <= ? AND end_time >= ?""",
            ((run["started_at"] or 0) + CORRELATION_WINDOW_MS,
             (run["started_at"] or 0) - CORRELATION_WINDOW_MS),
        ).fetchall()

        # Top failing annotations (up to 10).
        ann_rows = conn.execute(
            """SELECT a.tool, a.rule_id, a.path, a.start_line, a.message
               FROM workflow_annotations a
               JOIN workflow_jobs j ON j.id = a.job_id
               WHERE j.run_id = ? AND a.level = 'failure'
               ORDER BY a.id LIMIT 10""",
            (run_id,),
        ).fetchall()

        prompt = _build_prompt(run, shell_rows, claude_rows, ann_rows)
        summary = lm.complete(model=query_model, prompt=prompt, max_tokens=300)

        now = int(time.time() * 1000)
        cur = conn.execute(
            """INSERT INTO knowledge_nodes (kind, title, body, created_at)
               VALUES ('change_outcome', ?, ?, ?)""",
            (f'{run["repo"]}@{run["head_sha"][:7]} — {run["conclusion"]}',
             summary, now),
        )
        node_id = cur.lastrowid
        conn.execute(
            "INSERT INTO knowledge_node_workflow_runs (knowledge_node_id, run_id) VALUES (?,?)",
            (node_id, run_id),
        )
        for s in shell_rows:
            conn.execute(
                "INSERT OR IGNORE INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (?,?)",
                (node_id, s["id"]),
            )
        for c in claude_rows:
            conn.execute(
                "INSERT OR IGNORE INTO knowledge_node_claude_sessions (knowledge_node_id, claude_session_id) VALUES (?,?)",
                (node_id, c["id"]),
            )

        conn.execute("UPDATE workflow_runs SET enriched = 1 WHERE id = ?", (run_id,))
        conn.execute(
            "UPDATE workflow_enrichment_queue SET status='done', updated_at=? WHERE run_id=?",
            (now, run_id),
        )
        conn.commit()
        embed_and_store(db_path, node_id, summary)
    finally:
        conn.close()


def _build_prompt(run, shell_rows, claude_rows, ann_rows) -> str:
    # Deterministic string build — keep it concise and structured so the
    # model emits a summary, not a novel.
    parts = [
        f"Workflow run: {run['repo']} @ {run['head_sha'][:7]}",
        f"Status: {run['status']}  Conclusion: {run['conclusion']}",
        "",
        f"Annotations ({len(ann_rows)}):",
    ]
    for a in ann_rows:
        parts.append(f"  - [{a['tool'] or '?'}:{a['rule_id'] or '?'}] "
                     f"{a['path']}:{a['start_line']}: {a['message']}")
    parts.append(f"\nCo-temporal shell events: {len(shell_rows)}")
    parts.append(f"Co-temporal claude sessions: {len(claude_rows)}")
    parts.append("\nSummarize what changed, whether it succeeded, and if it "
                 "failed, the root cause and one-line fix suggestion.")
    return "\n".join(parts)
```

- [ ] **Step 3: Wire into `enrichment.py`**

Locate the existing enrichment scheduler loop (follows the pattern for `browser_enrichment.enrich_one` and `claude_sessions.enrich_one`). Add a similar sibling poll:

```python
# In the asyncio.gather block that dispatches enrichment per source:
workflow_task = asyncio.create_task(
    poll_workflow_queue(db_path, lm, query_model)
)
```

where `poll_workflow_queue` reads pending rows from `workflow_enrichment_queue`, calls `workflow_enrichment.enrich_one` for each, and handles retry/failed status transitions consistent with the other queues.

- [ ] **Step 4: Run tests**

Run: `uv run --project brain pytest brain/tests/test_workflow_enrichment.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/workflow_enrichment.py \
        brain/src/hippo_brain/enrichment.py \
        brain/tests/test_workflow_enrichment.py
git commit -m "feat(brain): change-outcome enrichment joins CI to shell+claude"
```

### Task 23: Lesson synthesis invocation

**Files:**
- Modify: `brain/src/hippo_brain/workflow_enrichment.py`

- [ ] **Step 1: Add test case**

Append to `test_workflow_enrichment.py`:

```python
def test_repeat_ruff_failure_promotes_lesson(db_path, fake_lm_client):
    # Seed two completed workflow runs, both with an annotation
    # (ruff, F401, brain/x.py). Enrich both. After the second, assert
    # a `lessons` row exists with occurrences == 2.
    ...
```

- [ ] **Step 2: In `workflow_enrichment.enrich_one`, call `lessons.upsert_cluster` for each failing annotation**

```python
from hippo_brain.lessons import upsert_cluster, ClusterKey

for a in ann_rows:
    path_prefix = _path_prefix(a["path"], cfg.lessons.path_prefix_segments)
    upsert_cluster(
        db_path,
        ClusterKey(repo=run["repo"], tool=a["tool"], rule_id=a["rule_id"],
                   path_prefix=path_prefix),
        min_occurrences=cfg.lessons.min_occurrences,
        summary_fn=lambda k: _synthesize_lesson_summary(lm, query_model, k, a),
        now_ms=now,
    )
```

with a small helper:

```python
def _path_prefix(path: str | None, segments: int) -> str | None:
    if not path:
        return None
    parts = Path(path).parts
    return str(Path(*parts[:segments])) if parts else None
```

- [ ] **Step 3: Run tests**

Run: `uv run --project brain pytest brain/tests/test_workflow_enrichment.py -v`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add brain/src/hippo_brain/workflow_enrichment.py \
        brain/tests/test_workflow_enrichment.py
git commit -m "feat(brain): promote repeat failures into lessons during enrichment"
```

---

## Phase 11 — MCP Tool Registration

### Task 24: Register `get_ci_status` and `get_lessons` MCP tools

**Files:**
- Modify: `brain/src/hippo_brain/mcp.py`
- Create: `brain/tests/test_mcp_server_gh.py`

- [ ] **Step 1: Write failing test**

Follow the pattern in `test_mcp_server.py` — instantiate the FastMCP server and assert the new tool names are registered with correct input schemas.

```python
def test_get_ci_status_registered():
    from hippo_brain.mcp import mcp
    tools = {t.name: t for t in mcp.list_tools()}  # or whatever accessor FastMCP exposes
    assert "get_ci_status" in tools
    assert "get_lessons" in tools
```

*(Match the exact accessor used by the existing `test_mcp_server.py`.)*

- [ ] **Step 2: Register the tools**

In `mcp.py`, alongside existing `@mcp.tool()` decorators:

```python
@mcp.tool()
def get_ci_status(repo: str, sha: str | None = None, branch: str | None = None) -> dict:
    """Return the most recent CI workflow run for a repo, filtered by SHA or branch.

    Use this after a 'git push' to check whether CI passed. Returns structured
    job and annotation data — prefer over `ask` for known-shape queries.
    """
    status = get_ci_status_impl(_state.db_path, repo=repo, sha=sha, branch=branch)
    return dataclasses.asdict(status) if status else {}


@mcp.tool()
def get_lessons(
    repo: str | None = None,
    path: str | None = None,
    tool: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Return distilled past-mistake lessons for the given filters.

    Use pre-flight before editing code in a known failure-prone area. Lessons
    only appear for patterns seen 2+ times (single failures do not graduate).
    """
    lessons = get_lessons_impl(_state.db_path, repo=repo, path=path, tool=tool, limit=limit)
    return [dataclasses.asdict(l) for l in lessons]
```

- [ ] **Step 3: Run tests**

Run: `uv run --project brain pytest brain/tests/test_mcp_server_gh.py brain/tests/test_mcp_server.py -v`
Expected: all pass (no regression on existing MCP tests).

- [ ] **Step 4: Commit**

```bash
git add brain/src/hippo_brain/mcp.py brain/tests/test_mcp_server_gh.py
git commit -m "feat(brain): register get_ci_status and get_lessons MCP tools"
```

---

## Phase 12 — Claude Skill and Install

### Task 25: Create the `using-hippo-brain` skill

**Files:**
- Create: `extension/claude-skill/using-hippo-brain/SKILL.md`

- [ ] **Step 1: Write the skill file (verbatim from spec Appendix A)**

```markdown
---
name: using-hippo-brain
description: Use when working in a repo with hippo coverage — query past
  CI outcomes for in-flight pushes, retrieve lessons before editing in
  failure-prone areas, and answer retrospective questions about what
  was done. Do not invoke for routine acknowledgments or tiny exchanges.
---

# Using the Hippo Brain

You have access to a persistent local knowledge base via the `hippo` MCP
server. It captures shell activity, prior Claude sessions, browser
history, and CI outcomes from GitHub Actions. Use it as memory across
sessions.

## When to query (and when not to)

| Situation | Action |
|---|---|
| Starting substantive work in a repo for the first time this session | Optional: `get_lessons(repo=<repo>)` for high-frequency patterns |
| Just edited or about to edit a file with a known failure history | `get_lessons(path=<path>)` |
| `git push` happened earlier in this session | Track the SHA mentally; when the user next re-engages or pauses, call `get_ci_status(repo, sha)` once |
| User asks "did it pass" / "what failed" / "what did I do" | `get_ci_status` or `ask` as appropriate |
| User says "yes", "ok", "proceed", "go ahead" | Do nothing. These are flow control, not work boundaries. |
| Routine multi-turn implementation | Do nothing. Don't poll between every edit. |

## In-flight SHA mental model

After `git push origin <branch>`, that SHA is "in flight" until CI
reaches a terminal state (typically 3–10 min). You don't need to poll.
Check once when the user re-engages after a quiet pause, or when
starting a new task. If CI failed, surface the annotations and propose
a fix — don't bury it. If CI passed, no need to mention unless asked.

## Tool selection

- `get_ci_status(repo, sha)` — structured CI outcome. Use for "did it pass."
- `get_lessons(repo?, path?, tool?)` — distilled past mistakes. Use pre-flight.
- `search_knowledge(query)` — semantic retrieval over knowledge nodes.
- `search_events(query)` — raw event timeline.
- `ask(question)` — synthesized prose answer. Use for human-shaped questions.
- `get_entities(...)` — graph exploration.

Prefer the structured tools over `ask` when you know what shape you
want — they are cheaper and machine-friendly. `ask` runs a full RAG
pipeline and returns prose.
```

- [ ] **Step 2: Commit**

```bash
git add extension/claude-skill/using-hippo-brain/SKILL.md
git commit -m "feat: using-hippo-brain Claude Code skill"
```

### Task 26: Install skill via mise task

**Files:**
- Modify: `mise.toml`
- Modify: `crates/hippo-daemon/src/install.rs` (or a dedicated script)

- [ ] **Step 1: Add a mise task that symlinks the skill**

```toml
[tasks."install:skill"]
description = "Install the using-hippo-brain Claude Code skill"
run = """
#!/usr/bin/env bash
set -euo pipefail
SKILL_SRC="$(pwd)/extension/claude-skill/using-hippo-brain"
SKILL_DST="$HOME/.claude/skills/using-hippo-brain"
mkdir -p "$(dirname "$SKILL_DST")"
if [ -L "$SKILL_DST" ] || [ -e "$SKILL_DST" ]; then
    rm -rf "$SKILL_DST"
fi
ln -s "$SKILL_SRC" "$SKILL_DST"
echo "Installed skill: $SKILL_DST -> $SKILL_SRC"
"""
```

- [ ] **Step 2: Chain into the top-level install task**

Find the existing `install` mise task and add `install:skill` to its `depends`.

- [ ] **Step 3: Smoke test**

Run: `mise run install:skill && readlink ~/.claude/skills/using-hippo-brain`
Expected: prints the absolute path to the repo's skill directory.

- [ ] **Step 4: Commit**

```bash
git add mise.toml
git commit -m "feat(install): symlink using-hippo-brain skill into ~/.claude/skills"
```

---

## Phase 13 — hippo doctor

### Task 27: doctor checks for github source and skill

**Files:**
- Modify: the file that implements `hippo doctor` (locate via `grep` for `Doctor` command)

- [ ] **Step 1: Add checks**

Append to the doctor check list:

- GitHub source:
  - If `[github].enabled`: check `HIPPO_GITHUB_TOKEN` is set, `https://api.github.com/rate_limit` is reachable (with that token), and `workflow_runs.last_seen_at` max is within `2 * poll_interval_secs` of now.
  - If disabled: skip (noted, not failed).
- Skill:
  - `~/.claude/skills/using-hippo-brain/SKILL.md` exists.
  - If a symlink: `readlink` resolves to the current hippo install's `extension/claude-skill/...`.
- Watchlist health:
  - `SELECT count(*) FROM sha_watchlist WHERE expires_at < now_ms - 2 * watchlist_ttl_secs * 1000`
  - should be 0; >0 → warn "stale watchlist entries; poller may be failing."
- Hook registration:
  - Parse `~/.claude/settings.json`, assert the `post-git-push.sh` and `pending-ci-session-start.sh` paths match the current repo's hook scripts.

- [ ] **Step 2: Manual smoke test**

Run: `hippo doctor`
Expected: new checks appear, all green in a healthy install.

- [ ] **Step 3: Commit**

```bash
git add <file>
git commit -m "feat(doctor): github source + skill + watchlist checks"
```

---

## Phase 14 — End-to-End Validation

### Task 28: Manual smoke checklist

**Files:** none — manual validation

- [ ] **Step 1: Simulate a push workflow**

1. Push a trivial change to `sjcarpenter/hippo` (in a disposable branch, or mock push).
2. Confirm `sqlite3 ~/.local/share/hippo/hippo.db "SELECT * FROM sha_watchlist"` shows the SHA.
3. Wait for GH Actions to complete, or manually invoke `hippo gh-poll`.
4. Confirm `workflow_runs`, `workflow_jobs`, `workflow_annotations` populate.
5. If CI fails, confirm `workflow_log_excerpts` has a row for the failed job.

- [ ] **Step 2: Force a lesson**

1. Break `ruff` twice on the same file (push twice with the same unused import).
2. Run `hippo gh-poll` after each.
3. Wait for brain enrichment to run (or kick manually).
4. Confirm `SELECT * FROM lessons` shows one row with `occurrences = 2`.

- [ ] **Step 3: MCP roundtrip**

From within Claude Code with the skill loaded:
1. Ask "did my last push to hippo pass?"
2. Expect Claude to call `get_ci_status` and report structured outcome.
3. Ask "what past ruff mistakes have I made?"
4. Expect Claude to call `get_lessons(tool='ruff')`.

- [ ] **Step 4: Skill noise check**

Open a fresh session, respond "yes" to a trivial prompt. Expect no hippo MCP call — Claude should recognize flow-control acknowledgment.

- [ ] **Step 5: Failure notification safety net**

1. Push a failing change.
2. Exit Claude Code before CI finishes.
3. Wait for `hippo gh-poll` to observe the terminal failure.
4. Re-open Claude Code in the same repo.
5. Expect the SessionStart hook to inject "CI failed on SHA X" as additional context.

If all five pass, the feature is ready for PR.

### Task 29: Open the pull request

**Files:** none

- [ ] **Step 1: Push the branch and open a PR**

```bash
git push
gh pr create --title "feat: GitHub Actions source and using-hippo-brain skill" \
  --body "$(cat <<'EOF'
## Summary
- Adds GitHub Actions as hippo's fourth data source: workflow runs, jobs, annotations, failure-log excerpts
- Adds two-tier polling (background + tight-watch on recently pushed SHAs)
- Adds change-outcome enrichment that joins CI results to shell and Claude session events
- Adds lesson synthesis that clusters repeated failures into queryable past-mistake records
- Adds two MCP tools: `get_ci_status`, `get_lessons`
- Ships a default `using-hippo-brain` Claude Code skill that teaches Claude when/how to use the brain
- Minimal hooks: `PostToolUse` on git push, `SessionStart` for pending CI failures

Spec: `docs/superpowers/specs/2026-04-15-github-actions-source-and-hippo-skill-design.md`
Plan: `docs/superpowers/plans/2026-04-15-github-actions-source-and-hippo-skill.md`

## Test plan
- [ ] `cargo test` green
- [ ] `uv run --project brain pytest` green
- [ ] End-to-end smoke checklist (manual) — see plan Task 28
EOF
)"
```

---

## Self-Review Checklist (run before starting implementation)

- [x] **Spec coverage.** Every spec section maps to a task: schema → T2–3; annotation parser → T4–5; watchlist → T6–7; gh_api → T8–9; poll loop → T10–11; CLI/config/launchd/install → T12–15; hooks → T16–17; Python models/queries → T18–20; lessons → T21–23; MCP registration → T24; skill + install → T25–26; doctor → T27; e2e → T28.
- [x] **No placeholders.** No "TBD", "implement later", or "fill in details" in task steps. Where existing patterns are referenced (DaemonHarness, browser_enrichment, MCP tool registration style), the plan instructs the implementer to locate the pattern rather than prescribing generic boilerplate — this is intentional, not hand-waving.
- [x] **Type consistency.** `CIStatus`, `CIJob`, `CIAnnotation`, `Lesson`, `ClusterKey` names match across Rust SQL, Python models, and MCP signatures. `sha_watchlist` has `terminal_status` + `notified` columns consistent across DDL, watchlist helpers, and hook scripts. `workflow_enrichment_queue` status values (`pending`, `processing`, `done`, `failed`, `skipped`) match the Claude queue's CHECK constraint.
- [x] **Test harness reuse.** Daemon integration tests lean on an assumed `DaemonHarness` helper. If it doesn't exist, Task 7 Step 1 instructs the implementer to extract it from existing patterns rather than re-invent.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-15-github-actions-source-and-hippo-skill.md`.**

**Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Well-suited to this plan because phases are largely independent (annotation parser, gh_api, and lesson clustering can run in parallel once schema is landed).

**2. Inline Execution** — Execute tasks in this session using the executing-plans skill, batch execution with checkpoints for review.

**Which approach?**
