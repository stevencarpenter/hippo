# Agentic Session Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended for parallel tasks) or `superpowers:executing-plans` (sequential) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Plan rewritten 2026-04-25** following the spec audit. The original 2026-04-17 plan is preserved in git history (commit `d5ea991`). This rewrite reflects: schema target shift to v9 → v10, Phase 1 partial completion on `agentic-ingestion` branch, Codex's actual current state (already ingesting on main), source_health / probe_tag inheritance, OTel `source` attribute alignment.

**Goal:** Promote agentic coding-assistant tool calls to a first-class event type. Migrate Claude Code's session ingestion onto `AgenticToolCall`, add opencode as a live SQLite-polled producer, and rewire the existing Codex ingestion onto the same abstraction.

**Architecture summary:**
- New `AgenticToolCall` variant in `hippo-core::events::EventPayload` with a `Harness` enum and per-harness readers.
- Schema v9 → v10: rename `claude_sessions` → `agentic_sessions`, add harness/model/provider/agent/effort/tokens/cost columns, add `agentic_cursor`, update `source_health` rows.
- Daemon side: opencode runs as a background task using `PRAGMA data_version` to gate polling; Claude's existing JSONL ingester switches to emit `AgenticToolCall`.
- Brain side: `claude_sessions.py` renames to `agentic_sessions.py`; `codex_sessions.py` slots in as a reader; new `opencode_sessions.py` reader for SQLite-side enrichment context.

**Spec:** `docs/superpowers/specs/2026-04-17-opencode-ingestion-and-agentic-labeling-design.md` (read this first; the plan does not duplicate field mappings, SQL, or design rationale).

**Tech stack:** Rust 2024 (hippo-core, hippo-daemon), Python 3.14 (brain, uv), rusqlite, serde/serde_json, chrono, uuid, tokio, tracing, pytest, ruff.

---

## Pre-flight

- [ ] **Pre-flight 1: Confirm current schema is v9.** Run `sqlite3 ~/.local/share/hippo/hippo.db 'PRAGMA user_version'` — must return `9`. If lower, the user needs to run `mise run install` against current main first.
- [ ] **Pre-flight 2: Confirm `agentic-ingestion` branch is unchanged.** `git log agentic-ingestion --oneline | head -1` should show `c7b8534`. If it has moved, re-audit before cherry-picking.
- [ ] **Pre-flight 3: Branch from current `main`.** `git checkout main && git pull && git checkout -b agentic-ingestion-v2`. **Do not** branch from `agentic-ingestion`; that branch carries a regressed `events.rs`.
- [ ] **Pre-flight 4: Worktree (recommended).** `git worktree add ~/projects/hippo-agentic-v2 agentic-ingestion-v2` so the existing checkout stays clean.
- [ ] **Pre-flight 5: Run baseline.** `cargo test -p hippo-core -p hippo-daemon` and `uv run --project brain pytest brain/tests -q` — must pass on current main before any work starts. Failures here are pre-existing and need to be acknowledged or fixed first.

---

## Phase 1: Cherry-pick Foundation from `agentic-ingestion`

Phase 1 work (types, renderer, EventPayload variant, round-trip tests) was authored on the `agentic-ingestion` branch and is mostly intact. One commit (`cf6d20f`) is regressed and must be replayed by hand.

**Reference commits on `agentic-ingestion`:**
- `21c98f9 feat(hippo-core): scaffold agentic module with AgenticToolCall type`
- `d332279 feat(hippo-core): shared agentic command renderer`
- `18dde05 test(hippo-core): round-trip and db-str assertions for AgenticToolCall`
- `cf6d20f feat(hippo-core): add AgenticToolCall variant to EventPayload` ← **DO NOT cherry-pick; replay manually**

### Task 1.1: Cherry-pick the three clean commits

- [ ] **Step 1:** From `agentic-ingestion-v2`, run `git cherry-pick 21c98f9 d332279 18dde05`.
- [ ] **Step 2:** Verify no merge conflicts. Files added/modified: `crates/hippo-core/src/agentic/{mod,types,render}.rs`, `crates/hippo-core/src/lib.rs` (one-line `pub mod agentic;` insertion), `crates/hippo-core/tests/{agentic_envelope,agentic_render,agentic_types}.rs`.
- [ ] **Step 3:** Verification: `cargo build -p hippo-core` succeeds. (`cargo test` will fail at this stage — `agentic_envelope.rs` references `EventPayload::AgenticToolCall` which doesn't exist yet. Expected; Task 1.2 fixes it.)

**Acceptance:** Three commits applied cleanly. `crates/hippo-core/src/agentic/types.rs` is 85 LOC and exports `AgenticToolCall`, `Harness`, `AgenticStatus`, `TokenUsage`. `crates/hippo-core/tests/agentic_render.rs` has 13 tests.

### Task 1.2: Manually add the `AgenticToolCall` variant to `EventPayload`

The original commit `cf6d20f` removed `EventEnvelope.probe_tag` and `ShellEvent.tool_name` (both landed on main after that commit was authored). We add ONLY the variant; we keep the existing fields.

**Files:** modify `crates/hippo-core/src/events.rs`.

- [ ] **Step 1:** Add the variant to `EventPayload`:

```rust
pub enum EventPayload {
    Shell(Box<ShellEvent>),
    FsChange(FsChangeEvent),
    IdeAction(IdeActionEvent),
    Browser(Box<BrowserEvent>),
    AgenticToolCall(Box<crate::agentic::AgenticToolCall>),  // NEW
    Raw(serde_json::Value),
}
```

- [ ] **Step 2:** Verify `EventEnvelope.probe_tag: Option<String>` and `ShellEvent.tool_name: Option<String>` are still present (do not delete). The cherry-picked test fixtures from Task 1.1 will have stale literals; fix them in Step 3.
- [ ] **Step 3:** Edit `crates/hippo-core/tests/agentic_envelope.rs` — the `EventEnvelope` literal in `envelope_roundtrip_adjacently_tagged` needs `probe_tag: None` added. (One-line addition next to `payload:`.)
- [ ] **Step 4:** Verification: `cargo test -p hippo-core` — all 100+ existing tests pass plus the 3 new agentic test files (envelope round-trip, render, types).
- [ ] **Step 5:** Verification: `cargo clippy --all-targets -- -D warnings` clean. New `AgenticToolCall` variant might trigger `match` exhaustiveness warnings in places where `EventPayload` is matched without a wildcard — fix by adding the variant to those matches (likely none today, since main uses `_` arms).
- [ ] **Step 6:** Commit: `feat(hippo-core): add AgenticToolCall variant to EventPayload (replay)`. The commit message references the original `cf6d20f` and explains the manual replay.

**Acceptance:** `cargo test -p hippo-core` green; `cargo clippy --all-targets -- -D warnings` green; `git log --oneline | head -4` shows the four Phase 1 commits in order.

### Task 1.3: Mark Phase 1 complete

- [ ] **Step 1:** Push `agentic-ingestion-v2` to remote: `git push -u origin agentic-ingestion-v2`.
- [ ] **Step 2:** Open a draft PR titled `Agentic ingestion: foundation (Phase 1)` referencing this plan and the spec. Mark draft so it's not accidentally merged before later phases land.

**Out of scope for Phase 1:** any change to the `EventPayload::Shell { ShellKind::Unknown("claude-code") }` path used by Claude's current ingester. Phase 4 swaps that.

---

## Phase 2: Schema migration v9 → v10

The migration is described in detail in the spec (`## Schema migration v9 → v10` section). This phase implements it.

### Task 2.1: Add the v9 → v10 migration block to `storage.rs`

**Files:** modify `crates/hippo-core/src/storage.rs`.

- [ ] **Step 1:** Locate the existing migration cascade (`if version < 9 { ... }` block at line ~418) and add the next block. Copy the full migration SQL verbatim from the spec's "Schema migration v9 → v10" section (lines ~263-345 in `docs/superpowers/specs/2026-04-17-opencode-ingestion-and-agentic-labeling-design.md`):

```rust
if version < 10 {
    conn.execute_batch(
        // SQL block from spec section "Schema migration v9 → v10"
        "ALTER TABLE claude_sessions RENAME TO agentic_sessions; ..."
    )?;
    tracing::info!("Migrated to schema v10 (agentic session ingestion)");
}
```

The SQL must remain byte-identical to the spec; if you find yourself wanting to deviate, update the spec first and reference the change in the commit message.

- [ ] **Step 2:** Update the `EXPECTED_VERSION` constant in this file to `10`.
- [ ] **Step 3:** Verify the migration is idempotent: running it twice on a v10 DB must be a no-op. SQLite's `ALTER TABLE ... RENAME TO` errors if the source doesn't exist; if the migration is partly applied, re-running fails. Wrap each statement in checks where necessary, OR rely on the version-gate (`if version < 10`) so the block only runs once.

**Acceptance:** `cargo test -p hippo-core` still green at this point (no test exercises v10 yet — that's Task 2.3).

### Task 2.2: Update `schema.sql` to reflect v10 final state

**Files:** modify `crates/hippo-core/src/schema.sql`.

- [ ] **Step 1:** Rename table definitions: `claude_sessions` → `agentic_sessions`, `claude_enrichment_queue` → `agentic_enrichment_queue`, `knowledge_node_claude_sessions` → `knowledge_node_agentic_sessions`. Update FK references and the column name `claude_session_id` → `agentic_session_id`.
- [ ] **Step 2:** Add the new columns on `agentic_sessions`: `harness TEXT NOT NULL DEFAULT 'claude-code'`, `harness_version TEXT`, `model TEXT`, `provider TEXT`, `agent TEXT`, `effort TEXT`, `tokens_input INTEGER`, `tokens_output INTEGER`, `tokens_reasoning INTEGER`, `tokens_cache_read INTEGER`, `tokens_cache_write INTEGER`, `cost_usd REAL`. `probe_tag TEXT` is already present from v8.
- [ ] **Step 3:** Add new indexes: `idx_agentic_sessions_harness`, `idx_agentic_sessions_model`. Rename existing: `idx_claude_sessions_cwd` → `idx_agentic_sessions_cwd`, `idx_claude_sessions_session` → `idx_agentic_sessions_session`, `idx_claude_queue_pending` → `idx_agentic_queue_pending`.
- [ ] **Step 4:** Add `CREATE TABLE agentic_cursor (...)` near the source_health / capture_alarms section.
- [ ] **Step 5:** Update the `source_health` seed `INSERT OR IGNORE` to use the new naming: `agentic-session-claude` (rename from `claude-session`), `agentic-session-codex`, `agentic-session-opencode`. Keep `shell` and `claude-tool` and `browser` rows. (Note: `claude-tool` will become source of `AgenticToolCall` events in Phase 4; it stays as-is for now.)
- [ ] **Step 6:** Bump `PRAGMA user_version = 10;` at end of file.
- [ ] **Step 7:** Verification: `cargo test -p hippo-core test_fresh_install_schema_matches_target` (or equivalent) — fresh-install path must match the migrated path byte-for-byte (this is the existing fresh-vs-migrated parity check).

**Acceptance:** Fresh install and migrated DB produce identical schema (verified by existing parity test).

### Task 2.3: Add the v10 migration golden test

**Files:** create `crates/hippo-core/tests/schema_v10_migration.rs`.

- [ ] **Step 1:** Build a v9-shaped DB programmatically: create the v8 table set (`claude_sessions`, `claude_enrichment_queue`, `knowledge_node_claude_sessions`, `source_health`, `capture_alarms`, etc.). Set `PRAGMA user_version = 9`.
- [ ] **Step 2:** Seed test rows:
  - One Claude session: `source_file = '/Users/x/.claude/projects/foo/session-abc.jsonl'`
  - One Codex session: `source_file = '/Users/x/Library/Developer/Xcode/CodingAssistant/codex/sessions/2026/04/24/rollout-xyz.jsonl'`
  - One row with `probe_tag = 'probe-uuid-1'` to verify probe_tag survives rename
  - One `claude_enrichment_queue` row referencing one of the sessions
  - One `knowledge_node_claude_sessions` link
  - `source_health` rows for `shell`, `claude-tool`, `claude-session`, `browser`
- [ ] **Step 3:** Run `open_db()` (which triggers the migration cascade).
- [ ] **Step 4:** Assertions:
  - `PRAGMA user_version` returns `10`.
  - `agentic_sessions` exists; `claude_sessions` does not.
  - Claude row has `harness = 'claude-code'`; Codex row has `harness = 'codex'` (backfill).
  - `probe_tag = 'probe-uuid-1'` row survives.
  - `knowledge_node_agentic_sessions` link still resolves to the right row.
  - `source_health` has rows: `shell`, `claude-tool`, `agentic-session-claude` (renamed), `agentic-session-codex` (new), `agentic-session-opencode` (new), `browser`. No `claude-session` row.
  - `agentic_cursor` table exists and is empty.
  - All renamed indexes (`idx_agentic_*`) exist; old indexes (`idx_claude_*`) do not.
- [ ] **Step 5:** Verification: `cargo test -p hippo-core schema_v10_migration` green.

**Acceptance:** Test passes; running the migration on real `~/.local/share/hippo/hippo.db` (after a backup) should also succeed and pass `hippo doctor`.

### Task 2.4: Update Python schema_version expectations

**Files:** modify `brain/src/hippo_brain/schema_version.py`.

- [ ] **Step 1:** Bump `EXPECTED_SCHEMA_VERSION = 10`.
- [ ] **Step 2:** Update `ACCEPTED_READ_VERSIONS = frozenset({EXPECTED_SCHEMA_VERSION, 9, 8, 7, 6, 5})` (additive — keep all current accepted reads, add 10).
- [ ] **Step 3:** Verification: `uv run --project brain pytest brain/tests/test_schema_version.py -v` green (the file may need a small update to mention 10).

**Acceptance:** Python brain accepts both v9 (rollback) and v10 DBs.

### Task 2.5: Update existing migration tests to expect v10 as final

**Files:** modify `crates/hippo-core/tests/schema_v{6,7,8,9}_migration.rs` (whichever exist; check before editing).

- [ ] **Step 1:** Find the assertion in each that expects `PRAGMA user_version` equal to a specific number; update to `10` (or to the parameterized "current" if the test uses one).
- [ ] **Step 2:** Verification: `cargo test -p hippo-core schema_v` green for all migration tests.

**Acceptance:** Every migration test passes through to v10.

---

## Phase 3: Brain module rename + harness plumbing

### Task 3.1: Rename `claude_sessions.py` → `agentic_sessions.py`

**Files:**
- Rename: `brain/src/hippo_brain/claude_sessions.py` → `brain/src/hippo_brain/agentic_sessions.py`
- Modify: every importer of `claude_sessions` (run `grep -rn "from hippo_brain.claude_sessions\|hippo_brain\.claude_sessions" brain/`).

- [ ] **Step 1:** `git mv brain/src/hippo_brain/claude_sessions.py brain/src/hippo_brain/agentic_sessions.py`.
- [ ] **Step 2:** Update all imports (~5-10 files including `enrichment.py`, `server.py`, `mcp.py`, tests). No compat shim — per repo convention against backwards-compat hacks.
- [ ] **Step 3:** Update SQL strings inside the module to reference `agentic_sessions` / `agentic_enrichment_queue` / `knowledge_node_agentic_sessions` / `agentic_session_id`.
- [ ] **Step 4:** Verification: `uv run --project brain pytest brain/tests -q` — green except for tests we'll fix in 3.5.

### Task 3.2: Add harness fields to `SessionSegment`

**Files:** modify `brain/src/hippo_brain/agentic_sessions.py`.

- [ ] **Step 1:** Extend the `SessionSegment` dataclass:

```python
@dataclass
class SessionSegment:
    # ... existing fields ...
    source: str = "claude"  # already exists; keep for back-compat in the dataclass
    harness: str = "claude-code"  # NEW — persisted to DB
    harness_version: str | None = None
    model: str | None = None
    provider: str | None = None
    agent: str | None = None
    effort: str | None = None
    tokens_input: int | None = None
    tokens_output: int | None = None
    tokens_reasoning: int | None = None
    tokens_cache_read: int | None = None
    tokens_cache_write: int | None = None
    cost_usd: float | None = None
```

- [ ] **Step 2:** Decide on `source` deprecation. The in-memory `source` field today is used to pick the enrichment summary builder (`if segment.source == "codex": ...`). Replace with `if segment.harness == "codex"` everywhere; keep `source` as a transitional alias for one PR cycle, then remove. (Tracked: file a follow-up issue if not removed in this work.)

### Task 3.3: Update `insert_segment` to persist new columns

- [ ] **Step 1:** Edit the SQL `INSERT INTO agentic_sessions (...)` statement to include the new columns.
- [ ] **Step 2:** Pass `segment.harness, segment.model, segment.provider, ...` etc. through.
- [ ] **Step 3:** Verification: write a brain test that inserts a segment with a non-default `harness` and confirms the row's column reads back correctly.

### Task 3.4: Reader dispatcher

**Files:** modify `brain/src/hippo_brain/agentic_sessions.py`; reuse `brain/src/hippo_brain/codex_sessions.py`.

- [ ] **Step 1:** Introduce `iter_agentic_segments(harness: str, ...)` that dispatches to the right reader. Existing `iter_session_files` is renamed and becomes the `'claude'` branch.
- [ ] **Step 2:** Add `'codex'` branch that calls into `codex_sessions.iter_codex_session_files` (existing).
- [ ] **Step 3:** opencode branch deferred to Phase 5.

### Task 3.5: Update brain tests

- [ ] **Step 1:** Rename `brain/tests/test_claude_sessions.py` → `test_agentic_sessions.py`.
- [ ] **Step 2:** Add cases that assert `harness='claude-code'` is set on existing Claude path.
- [ ] **Step 3:** Verification: `uv run --project brain pytest brain/tests/test_agentic_sessions.py -v` green.

**Acceptance for Phase 3:** All existing brain tests pass; new tests cover harness-field persistence on Claude segments.

---

## Phase 4: Daemon Claude path emits `AgenticToolCall`

This is the most invasive phase: it changes what the daemon emits for every Claude tool call. Source-audit tests catch regressions.

### Task 4.1: Migrate `claude_session.rs` to emit `AgenticToolCall`

**Files:** modify `crates/hippo-daemon/src/claude_session.rs`.

- [ ] **Step 1:** Map per the spec's "Claude Migration" field table (harness/model/provider/etc. extracted from JSONL message data).
- [ ] **Step 2:** Replace `EventPayload::Shell { ShellKind::Unknown("claude-code"), ... }` construction with `EventPayload::AgenticToolCall(Box::new(...))`. The renderer `crate::agentic::render::render_command` produces the `command` field.
- [ ] **Step 3:** Subagent detection (filename path under `*/subagents/*.jsonl`) populates the `agent` field.
- [ ] **Step 4:** Update `process_line` tests (existing in this file) to assert the new payload shape. Do NOT delete the original assertions; rewrite them to inspect `EventPayload::AgenticToolCall` data.

### Task 4.2: Daemon storage layer persists `AgenticToolCall`

**Files:** modify `crates/hippo-daemon/src/daemon.rs` (or wherever `EventPayload::Shell` is matched on the write path; grep for `EventPayload::Shell`).

- [ ] **Step 1:** Add `EventPayload::AgenticToolCall(call) => { ... }` arm. The arm:
  - Inserts a row into `events` for tool-call grain. `source_kind` follows the per-harness pattern (parallel to existing `claude-tool`): `'claude-tool'` for `Harness::ClaudeCode`, `'opencode-tool'` for `Harness::Opencode`. Codex per-call events are deferred (see Phase 6 / Out of Scope).
  - Updates the corresponding `source_health` row's `last_event_ts`. The migration in Phase 2 pre-seeds rows for `'claude-tool'` (already exists) and `'opencode-tool'` (new); the daemon writes by exact match.
  - Honors `probe_tag` from the envelope, writing it to the row if non-NULL.
- [ ] **Step 2:** Existing `source_kind = 'claude-tool'` write-path continues for Claude rows (no rename — see spec note about dashboard continuity); new `'opencode-tool'` rows appear once Phase 5 lands.

### Task 4.3: Source-audit integration test

**Files:** create `crates/hippo-daemon/tests/source_audit/agentic_tool_calls.rs`. Pattern: copy `claude_tool_events.rs` and adapt.

- [ ] **Step 1:** Spin up a temp DB at v10. Send an `AgenticToolCall` event through the daemon's intake path. Assert the row appears in `events` and `agentic_sessions` (via downstream segmentation).
- [ ] **Step 2:** Cover redaction: include a fake AWS key in `tool_input.command`, verify it's redacted and `redaction_count > 0`.
- [ ] **Step 3:** Cover `probe_tag`: emit one event with `probe_tag = Some(...)`, assert it lands in the `events.probe_tag` column.
- [ ] **Step 4:** Verification: `cargo test --test source_audit` green.

### Task 4.4: `source_health` write-path

- [ ] **Step 1:** Confirm the daemon's existing `source_health` updater handles the `agentic-session-claude` row name. Currently it likely writes `claude-session` — search and replace.
- [ ] **Step 2:** Verification: tail `source_health` after a real Claude session, confirm the right row's `last_event_ts` advances.

### Task 4.5: Smoke test against real Claude session

- [ ] **Step 1:** With the new code installed (`mise run install`), start a fresh Claude Code session in a test repo, do a few tool calls.
- [ ] **Step 2:** Verify: `agentic_sessions` rows have `harness='claude-code'`; `events` rows for tool calls reference the new payload; `source_health.last_event_ts` for `agentic-session-claude` is recent.

**Acceptance for Phase 4:** Live Claude usage produces `agentic_sessions` rows tagged with harness; source-audit test passes; doctor sees a fresh source_health row.

---

## Phase 5: opencode live poller

### Task 5.1: Pin opencode schema and write fixtures

**Files:** create `crates/hippo-daemon/src/opencode_session/{mod.rs, fixtures.rs}` and `crates/hippo-daemon/tests/opencode_session_test.rs`.

- [ ] **Step 1:** Capture the opencode schema: `sqlite3 ~/.local/share/opencode/opencode.db .schema > /tmp/opencode-schema.sql`. Pin it in `fixtures.rs` as a string constant.
- [ ] **Step 2:** Pin the latest known-good `__drizzle_migrations` hash (last row): `sqlite3 ~/.local/share/opencode/opencode.db "SELECT hash FROM __drizzle_migrations ORDER BY id DESC LIMIT 1"`. Constant `KNOWN_DRIZZLE_HASHES: &[&str]`.
- [ ] **Step 3:** Helper: `build_test_opencode_db(conn)` runs the pinned schema + seeds N rows of `(session, message, part)`.

**Acceptance:** Helper produces a valid in-memory opencode-shaped DB usable by all subsequent tests.

### Task 5.2: Poller skeleton + cursor

**Files:** modify `crates/hippo-daemon/src/opencode_session/mod.rs`.

- [ ] **Step 1:** Implement `OpencodeCursor::load_or_init(&db, harness, source_key)` reading from `agentic_cursor`.
- [ ] **Step 2:** Implement `OpencodeCursor::advance(&mut self, last_time, last_id)` writing back.
- [ ] **Step 3:** Tests: cursor round-trip, ties on `time_created`, multi-source-key isolation (different `opencode.db` paths get different cursors).

### Task 5.3: `PRAGMA data_version` gate

- [ ] **Step 1:** Track `last_seen_data_version: u64`. Each tick, `PRAGMA data_version`; skip the heavy query if unchanged.
- [ ] **Step 2:** Test: build DB, snapshot `data_version`, write a row, confirm `data_version` bumps; emit a fake "no write" event, confirm gate skips.

### Task 5.4: Part extraction

- [ ] **Step 1:** Implement the SQL query from the spec (joining `part`, `message`, `session`).
- [ ] **Step 2:** Implement `process_tool_part(row) -> AgenticToolCall` per the spec's field mapping table. JSON extraction with `json_extract` or Rust-side `serde_json`.
- [ ] **Step 3:** Tests: success path, error status mapping, truncation on large output, deterministic UUIDv5 from opencode session_id, malformed JSON handled gracefully.

### Task 5.5: `source_health` write-path for opencode

- [ ] **Step 1:** On each successful tick that emits at least one event, `UPDATE source_health SET last_event_ts = ?, last_heartbeat_ts = ? WHERE source = 'agentic-session-opencode'`.
- [ ] **Step 2:** On idle tick (no rows), update `last_heartbeat_ts` only.

### Task 5.6: Synthetic probe support

**Files:** modify `crates/hippo-daemon/src/probe.rs`.

- [ ] **Step 1:** Add an opencode probe variant: opens `opencode.db` read-only, runs `PRAGMA schema_version` and verifies the latest `__drizzle_migrations` hash is in `KNOWN_DRIZZLE_HASHES`. Success → `probe_ok = 1`. Failure (file not found, schema hash unknown, can't open) → `probe_ok = 0`. The `source_health` schema has no details column, so the *reason* for failure is logged at WARN with the harness label and shows up in OTel logs, not in the row.
- [ ] **Step 2:** Wire into the probe scheduler so `hippo doctor` sees the result. Doctor can't distinguish "opencode not installed" from "opencode installed but unhealthy" from the row alone — both are `probe_ok = 0`. v1 accepts this; the log message is the disambiguator.

### Task 5.7: Daemon wiring

**Files:** modify `crates/hippo-daemon/src/daemon.rs` and `src/main.rs`.

- [ ] **Step 1:** Spawn the opencode poller as a tokio task alongside shell socket / NM / gh-poll.
- [ ] **Step 2:** Add `[opencode]` config section to `crates/hippo-core/src/config.rs` and `config/config.default.toml` per the spec.
- [ ] **Step 3:** Default `enabled = true`. If `db_path` doesn't exist at startup, log "opencode DB not found; ingestion will idle" and set the source_health probe to a clear "not installed" status (NOT a failure).

### Task 5.8: Brain-side opencode reader

**Files:** create `brain/src/hippo_brain/opencode_sessions.py`.

- [ ] **Step 1:** Reader that walks `opencode.db` for full session segments (the daemon emits per-call events; the brain segment-builder emits SessionSegment for retrieval).
- [ ] **Step 2:** Hook into `agentic_sessions.iter_agentic_segments(harness='opencode', ...)`.
- [ ] **Step 3:** Tests: segment boundaries (5-min gap), redaction applied, schema-drift abort path.

**Acceptance for Phase 5:** Live opencode usage flows into `agentic_sessions` with `harness='opencode'`; per-call events appear in `events`; `hippo doctor` shows a healthy `agentic-session-opencode` source.

---

## Phase 6: Codex rewire (segment-level only in v1)

Codex is already on main via `codex_sessions.py`. v1 of this phase does NOT introduce per-call `AgenticToolCall` events for Codex (that's future work — JSONL-based Codex stays on the segment-only path). What changes: the persisted `harness = 'codex'` discriminator goes durable.

### Task 6.1: `codex_sessions.py` writes `harness='codex'`

**Files:** modify `brain/src/hippo_brain/codex_sessions.py`.

- [ ] **Step 1:** When building `SessionSegment` instances, set `segment.harness = 'codex'` (in addition to the existing in-memory `source = 'codex'`).
- [ ] **Step 2:** Migration of existing rows handled by Phase 2's backfill SQL (`UPDATE agentic_sessions SET harness = 'codex' WHERE source_file LIKE ...`).

### Task 6.2: `source_health` for Codex

- [ ] **Step 1:** On each successful Codex ingest run, the Python script updates `source_health.last_event_ts WHERE source = 'agentic-session-codex'`.
- [ ] **Step 2:** Update `scripts/hippo-ingest-codex.py` to do the write.

### Task 6.3: Verify Codex enrichment summary still selects properly

- [ ] **Step 1:** `claude_sessions.py:435` (now `agentic_sessions.py:~435`) used to switch on `segment.source == 'codex'`; ensure the switch is on `segment.harness == 'codex'` after Phase 3.

**Acceptance for Phase 6:** Existing Codex ingestion still works end-to-end; rows now have durable `harness='codex'`; `hippo doctor` shows fresh `agentic-session-codex` source_health.

---

## Phase 7: Final wiring

### Task 7.1: Enrichment-prompt harness context

**Files:** modify `brain/src/hippo_brain/enrichment.py` (or wherever the prompt template is built).

- [ ] **Step 1:** Add a `Harness:` line to the prompt: `Harness: {harness} {harness_version} ({agent or 'main'}, {model} via {provider})`. Falls back gracefully when fields are NULL.
- [ ] **Step 2:** Tests: render for each harness; assert no formatting errors on missing fields.

### Task 7.2: MCP filters

**Files:** modify `brain/src/hippo_brain/mcp.py`.

- [ ] **Step 1:** Add optional `harness` and `model` parameters to `search_knowledge` and `search_events`.
- [ ] **Step 2:** Propagate to SQL `WHERE` clauses (with NULL-handling per spec: `WHERE model = ? AND model IS NOT NULL` when filtering on backfilled rows).
- [ ] **Step 3:** Tests: filter by `harness='opencode'`, by `model='claude-opus-4-7'`, combined.

### Task 7.3: OTel metrics

**Files:** modify `crates/hippo-daemon/src/metrics.rs`.

- [ ] **Step 1:** Add counters: `hippo_agentic_events_emitted_total{harness}`, `hippo_agentic_poller_ticks_total{harness,outcome}`.
- [ ] **Step 2:** Wire into the opencode poller (Phase 5) and Claude path (Phase 4).
- [ ] **Step 3:** Source-audit tests assert metric labels include `harness`.

### Task 7.4: Redaction over `AgenticToolCall`

**Files:** modify `crates/hippo-core/src/redaction.rs`.

- [ ] **Step 1:** Add `redact_json_value(&mut Value)` recursive helper (the existing redactor is string-only).
- [ ] **Step 2:** Wire into the daemon's intake redaction step for `AgenticToolCall`: redact `command`, `tool_input` (recursive), `tool_output.content`. Sum the per-target counts into `redaction_count`.
- [ ] **Step 3:** Tests: planted secret in `tool_input.command` → redacted; planted secret in nested `tool_input.options.api_key` → redacted; per-rule REDACTIONS metric increments correctly.

### Task 7.5: Remove legacy `ShellKind::Unknown("claude-code")` paths

- [ ] **Step 1:** `grep -rn 'ShellKind::Unknown("claude-code")\|"claude-code"' crates/` to find all references; remove dead code.
- [ ] **Step 2:** If any test depends on the legacy path, port to the new payload.

### Task 7.6: Docs update

- [ ] **Step 1:** Update root `CLAUDE.md` and `README.md` to describe opencode as a first-class source alongside shell / Claude / Codex / browser.
- [ ] **Step 2:** Add `docs/sources/opencode.md` (or section in an existing doc) covering: install, config, troubleshooting (`hippo doctor` checks), how `agentic_cursor` works, what to do if opencode upgrades its schema (re-pin `KNOWN_DRIZZLE_HASHES`).
- [ ] **Step 3:** Update `docs/superpowers/specs/2026-04-17-opencode-ingestion-and-agentic-labeling-design.md` Status line to reflect implementation completion (mark phases complete).

---

## Final Verification

- [ ] **`cargo test --all-targets`** green across the workspace.
- [ ] **`cargo clippy --all-targets -- -D warnings`** clean.
- [ ] **`cargo fmt --check`** clean.
- [ ] **`uv run --project brain pytest brain/tests -v --cov=hippo_brain`** green; coverage on new files (`opencode_sessions.py`, dispatcher additions in `agentic_sessions.py`) ≥ existing project baseline.
- [ ] **`uv run --project brain ruff check brain/`** clean.
- [ ] **`uv run --project brain ruff format --check brain/`** clean.
- [ ] **End-to-end smoke:** `mise run install` on a personal-Mac with opencode installed; run a real opencode session; verify `agentic_sessions.harness = 'opencode'` row appears within ~30s; `hippo doctor` all green for `agentic-session-{claude,opencode,codex}`.
- [ ] **Schema parity:** fresh-install vs migrated DB schemas match (existing test).
- [ ] **MCP smoke:** `mcp__hippo__search_knowledge` with `harness='opencode'` filter returns only opencode rows.

---

## Out of Scope (future work; tracked in spec)

- Per-call `AgenticToolCall` events for Codex (today only segments). Add when Codex JSONL processing moves into the daemon.
- Copilot CLI ingestion. Revisit when there's user demand.
- Reasoning/patch event variants distinct from tool calls.
- Cost derivation from model + tokens (today: best-effort passthrough only).
- `assistant_session_files` cross-source file-touch index.
- Watchdog `capture_alarms` invariants for opencode (capture_alarms is feature-flagged off as of v9; opencode hooks land when watchdog enables broadly).
- Renaming `claude-tool` source label (a separate refactor; today's `claude-tool` events are Claude-specific tool events emitted from the JSONL ingester before the AgenticToolCall migration; after Phase 4 they'll all be AgenticToolCall payloads with `harness='claude-code'`, so a rename is plausible but not in scope here).
