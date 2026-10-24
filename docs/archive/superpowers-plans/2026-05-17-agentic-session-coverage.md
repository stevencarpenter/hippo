# Agentic Session Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Codex and opencode capture/enrichment/observability meet the same useful-context standard as Claude Code.

**Architecture:** Codex rollout JSONL remains the canonical transcript source, with `~/.codex/state_5.sqlite` used as the coverage oracle and `logs_2.sqlite` surfaced only as diagnostics. Opencode stays in `agentic_sessions`, but the poller reads `message` and `part` rows to build transcript-rich `summary_text`. Brain metrics and Grafana dashboards expose `codex`, `opencode`, and `workflow` as first-class enrichment sources.

**Tech Stack:** Rust 2024, rusqlite, serde_json, Hippo SQLite schema v15, Python brain telemetry, Grafana dashboard JSON, cargo/pytest.

---

## File Structure

- Modify `crates/hippo-daemon/src/codex_session.rs`: add read-only Codex state/log coverage helpers.
- Modify `crates/hippo-daemon/src/commands.rs`: include Codex state/log coverage in doctor source freshness details.
- Modify `crates/hippo-daemon/tests/codex_session.rs`: add coverage-helper tests using fabricated Codex state DB and rollout files.
- Modify `crates/hippo-daemon/src/opencode_session.rs`: read `message`/`part`, build redacted transcript-rich summaries, count messages/tokens.
- Modify `crates/hippo-daemon/tests/opencode_session.rs`: add opencode DB message/part fixtures and summary/redaction assertions.
- Modify `brain/src/hippo_brain/server.py`: queue-depth observable includes shell, claude, codex, browser, workflow, opencode.
- Add or modify `brain/tests/test_server_extended.py`: verify queue-depth SQL source split.
- Modify `otel/grafana/dashboards/hippo-enrichment.json`: add codex/opencode/workflow source queries to queue depth and claimed-rate panels.
- Add `tests/otel/test_dashboards.py` if no existing dashboard test covers this.
- Modify `docs/capture/sources.md` and `docs/schema.md`: update source-standard and schema-version notes if behavior changes enough to document.

## Task 1: Codex State Coverage Helper

**Files:**
- Modify: `crates/hippo-daemon/src/codex_session.rs`
- Test: `crates/hippo-daemon/tests/codex_session.rs`

- [ ] **Step 1: Write failing tests**

Add tests that create:

```rust
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    source TEXT NOT NULL,
    model_provider TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT NOT NULL,
    sandbox_policy TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    has_user_event INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0
);
```

Assert:

- a thread whose rollout file has a Hippo `claude_sessions.source_file` row is covered;
- a thread with an idle rollout file but no Hippo row is reported as missing;
- a thread with a rollout file modified within `min_idle_secs` is reported as in-flight;
- a missing rollout path is reported separately.

Run:

```bash
cargo test -p hippo-daemon codex_state_coverage
```

Expected: FAIL because the coverage helper does not exist.

- [ ] **Step 2: Implement helper**

Add a public testable helper:

```rust
pub struct CodexCoverageReport {
    pub total_state_threads: usize,
    pub covered_threads: usize,
    pub in_flight_threads: Vec<String>,
    pub missing_rollout_threads: Vec<String>,
    pub missing_hippo_threads: Vec<String>,
    pub log_only_thread_count: usize,
}

pub fn check_codex_coverage(
    hippo_conn: &rusqlite::Connection,
    state_db_path: &Path,
    logs_db_path: Option<&Path>,
    min_idle_secs: u64,
) -> Result<CodexCoverageReport>
```

Open Codex DBs read-only with `busy_timeout=5000`. Compare `threads.id` and `threads.rollout_path` against Hippo Codex rows in `claude_sessions`. Treat fresh mtimes as in-flight. Count `logs.thread_id` values absent from state/Hippo as diagnostics.

- [ ] **Step 3: Run tests**

Run:

```bash
cargo test -p hippo-daemon codex_state_coverage
```

Expected: PASS.

## Task 2: Wire Codex Coverage Into Doctor

**Files:**
- Modify: `crates/hippo-daemon/src/commands.rs`
- Test: existing command unit tests near Codex staleness classification

- [ ] **Step 1: Write failing classification test**

Add a test proving an old state thread missing from Hippo is not suppressed as merely idle Codex.

Run:

```bash
cargo test -p hippo-daemon commands::tests::test_codex
```

Expected: FAIL until doctor consumes the new coverage report.

- [ ] **Step 2: Implement doctor integration**

In the Codex freshness block, call the coverage helper when `~/.codex/state_5.sqlite` exists. Preserve current idle suppression for machines with no Codex use. Surface warning details with counts:

- `state threads`
- `missing Hippo rows`
- `missing rollout files`
- `in-flight threads`
- `log-only diagnostic threads`

- [ ] **Step 3: Run tests**

Run:

```bash
cargo test -p hippo-daemon commands::tests::test_codex
cargo test -p hippo-daemon codex_session
```

Expected: PASS.

## Task 3: Opencode Transcript-Rich Summary

**Files:**
- Modify: `crates/hippo-daemon/src/opencode_session.rs`
- Test: `crates/hippo-daemon/tests/opencode_session.rs`

- [ ] **Step 1: Write failing tests**

Extend the fabricated opencode DB with `message` and `part` tables. Insert:

- one user message with `data.role = "user"`;
- one assistant `part` with `type = "text"`;
- one tool `part` with `type = "tool"`, `tool = "bash"`, `state.input.command`, and `state.output`;
- one patch `part` with file paths;
- token data in assistant message or `step-finish`.

Assert `agentic_sessions.summary_text` contains the user text, assistant text, tool command, patch file, and redacted secret text, and that `message_count > 0`.

Run:

```bash
cargo test -p hippo-daemon opencode_session::poll_tick_writes_transcript_summary
```

Expected: FAIL because the poller reads only `session`.

- [ ] **Step 2: Implement transcript extraction**

Add internal structs/helpers:

```rust
struct OpencodeContext {
    user_prompts: Vec<String>,
    assistant_texts: Vec<String>,
    tool_summaries: Vec<String>,
    patch_files: Vec<String>,
    message_count: i64,
    token_count: i64,
}
```

Read `message` and `part` for each session, parse JSON with `serde_json`, cap stored excerpts, and run `RedactionEngine::builtin()` over all user/assistant/tool text. Keep DB read-only.

- [ ] **Step 3: Update write path**

Pass `OpencodeContext` into `build_summary_text` and `upsert_session`; write `message_count` and `token_count` to `agentic_sessions`.

- [ ] **Step 4: Run tests**

Run:

```bash
cargo test -p hippo-daemon opencode_session
```

Expected: PASS.

## Task 4: Brain Queue Metrics Source Coverage

**Files:**
- Modify: `brain/src/hippo_brain/server.py`
- Test: `brain/tests/test_server_extended.py`

- [ ] **Step 1: Write failing test**

Add a small unit around a helper such as `_queue_depth_queries()` or `_observe_queue_depths_for_conn(conn)` that verifies sources include:

```python
{"shell", "claude", "codex", "browser", "workflow", "opencode"}
```

Codex SQL must count rows in `claude_enrichment_queue` joined to `claude_sessions` where `source_file LIKE '%/.codex/%' OR source_file LIKE '%/CodingAssistant/codex/%'`. Claude SQL must exclude those rows.

Run:

```bash
uv run --project brain pytest brain/tests/test_server_extended.py -k queue_depth -v
```

Expected: FAIL because only shell/claude/browser are observed.

- [ ] **Step 2: Implement helper and observable**

Refactor the queue-depth callback to use a hardcoded source-to-SQL mapping for shell, claude, codex, browser, workflow, and opencode. Keep status values `pending`, `processing`, `failed`.

- [ ] **Step 3: Run tests**

Run:

```bash
uv run --project brain pytest brain/tests/test_server_extended.py -k queue_depth -v
```

Expected: PASS.

## Task 5: Grafana Dashboard Coverage

**Files:**
- Modify: `otel/grafana/dashboards/hippo-enrichment.json`
- Test: `tests/otel/test_dashboards.py`

- [ ] **Step 1: Write failing test**

Create a pytest test that loads `hippo-enrichment.json` and asserts the PromQL expressions mention `source="codex"`, `source="opencode"`, and `source="workflow"` for queue depth or claimed-rate panels.

Run:

```bash
uv run --project brain pytest tests/otel/test_dashboards.py -v
```

Expected: FAIL before dashboard JSON is updated.

- [ ] **Step 2: Update dashboard JSON**

Add sources to:

- `Queue Depth by Source & Status`
- `Events Claimed / min`

Prefer a single aggregate query grouped by source/status where practical, otherwise add explicit targets matching existing style. Keep `service_namespace!~".+"`.

- [ ] **Step 3: Run dashboard test**

Run:

```bash
uv run --project brain pytest tests/otel/test_dashboards.py -v
```

Expected: PASS.

## Task 6: End-to-End Verification

**Files:**
- No code changes expected

- [ ] **Step 1: Run targeted tests**

```bash
cargo test -p hippo-daemon codex_session
cargo test -p hippo-daemon opencode_session
uv run --project brain pytest brain/tests/test_server_extended.py -k queue_depth -v
uv run --project brain pytest tests/otel/test_dashboards.py -v
```

- [ ] **Step 2: Run live checks**

```bash
hippo codex-poll
hippo opencode-poll
hippo doctor
```

Expected:

- Codex only reports active in-flight files as skipped.
- No captured Codex or opencode row is missing an enrichment queue row.
- Doctor shows `agentic-session-codex` and `agentic-session-opencode` with concrete coverage diagnostics instead of silent gaps.

- [ ] **Step 3: Commit implementation**

```bash
git add crates/hippo-daemon/src/codex_session.rs crates/hippo-daemon/src/commands.rs crates/hippo-daemon/src/opencode_session.rs crates/hippo-daemon/tests/codex_session.rs crates/hippo-daemon/tests/opencode_session.rs brain/src/hippo_brain/server.py brain/tests/test_server_extended.py otel/grafana/dashboards/hippo-enrichment.json tests/otel/test_dashboards.py docs/capture/sources.md docs/schema.md
git commit -m "feat(agentic): harden Codex and opencode coverage"
```
