# Agentic Session Ingestion: opencode, Codex, and First-Class Harness Labeling

**Date:** 2026-04-17
**Status:** Design approved; implementation plan pending
**Scope:** Expand Claude Code session ingestion into a general "agentic tool-call" abstraction that also covers opencode (live) and Codex (historical backfill). Add first-class fields for harness, model, provider, agent/mode, and effort.

## Motivation

Claude Code session capture today emits `ShellEvent`s tagged with `ShellKind::Unknown("claude-code")`. Agentic-session data is being shoved into a shape designed for shell commands, and the harness/model/agent are second-class — invisible to the daemon, to redaction, and to any query that isn't also walking enrichment JSON.

As hippo grows to cover multiple agentic coding tools, this will ossify into a pile of `ShellKind::Unknown("...")` strings. We need a first-class `AgenticToolCall` event payload before more consumers pile on. This spec carves that abstraction, migrates Claude onto it, and ingests two new sources (opencode live; Codex historical).

## Non-Goals

- **Copilot CLI ingestion.** Its local `session-store.db` does not capture tool-level events (only coarse `user_message` / `assistant_response` turns), and the user has not actively used it. Covered as future work; out of scope here.
- **Reasoning/patch/file event types in opencode.** v1 ingests only `tool` and (for enrichment-side transcript reads) `text` parts. Reasoning and patch events can become their own payload variants later if value emerges.
- **Per-call cost attribution.** `cost_usd` is a best-effort passthrough; we do not derive cost from model+tokens in this spec.
- **Copilot-style "turn" abstraction** (`AgenticTurn` for coarser harnesses). Revisit when a real consumer needs it.

## The Abstraction: `AgenticToolCall`

A new `EventPayload` variant in `crates/hippo-core/src/events.rs`:

```rust
pub enum EventPayload {
    Shell(Box<ShellEvent>),
    FsChange(FsChangeEvent),
    IdeAction(IdeActionEvent),
    Browser(Box<BrowserEvent>),
    AgenticToolCall(Box<AgenticToolCall>),  // NEW
    Raw(serde_json::Value),
}

pub struct AgenticToolCall {
    pub session_id: Uuid,
    pub parent_session_id: Option<Uuid>,   // subagent / child-session chain
    pub harness: Harness,
    pub harness_version: Option<String>,   // e.g., opencode "1.4.6", codex cli_version
    pub model: String,                     // "claude-opus-4-7", "gpt-5", "nvidia/nemotron-3-super"
    pub provider: Option<String>,          // "anthropic", "openai", "lmstudio"
    pub agent: Option<String>,             // opencode mode/agent; Claude subagent_type; codex originator
    pub effort: Option<String>,            // "low"|"medium"|"high", null when harness does not expose
    pub tool_name: String,                 // "Bash", "Edit", "skill", "exec_command", ...
    pub tool_input: serde_json::Value,     // full structured input (for analysis)
    pub command: String,                   // rendered form for display/grep (e.g., "cargo test")
    pub tool_output: Option<CapturedOutput>,
    pub status: AgenticStatus,             // Ok | Error | Orphaned
    pub duration_ms: u64,
    pub cwd: PathBuf,
    pub hostname: String,
    pub git_state: Option<GitState>,
    pub tokens: Option<TokenUsage>,
    pub cost_usd: Option<f64>,
    pub redaction_count: u32,
}

pub enum Harness {
    ClaudeCode,
    Opencode,
    Codex,
    Unknown(String),
}

pub enum AgenticStatus { Ok, Error, Orphaned }

pub struct TokenUsage {
    pub input: u64,
    pub output: u64,
    pub reasoning: u64,
    pub cache_read: u64,
    pub cache_write: u64,
}
```

### Design rationale

- **Both structured `tool_input` and rendered `command`.** Losing either hurts downstream analysis. Keep both; renderer is per-harness.
- **`Harness` is an enum with `Unknown(String)` escape hatch.** Future harnesses (aider, sst/opencode forks, etc.) don't need a schema migration to be captured — they just show up as `Unknown("<name>")` until promoted.
- **`effort` is `Option<String>` not an enum.** Neither Claude nor opencode nor Codex reliably exposes it today. Over-constraining the type forces a migration when a fourth harness shows up with "minimal"/"ultrahigh"/whatever. Document conventional values in rustdoc; validate at source-specific ingesters if needed.
- **No `preceding_assistant_text` field.** Originally considered; cut because it pollutes the event for text-heavy harnesses. Daemon emits tool events; brain re-reads the raw transcript for enrichment context (same pattern as Claude today).

## Source 1: opencode (live ingestion via DB polling)

### Why polling

SQLite offers no CDC / binlog / cross-process update hook. Real options considered and rejected:

- WAL file tailing — undocumented format, checkpoint-recycled, brittle.
- `sqlite3_update_hook` — same-process only.
- Session extension changesets — same-process only.
- Triggers + change table — would require modifying opencode's schema. Off-limits.

Polling with `PRAGMA data_version` as a gating check is the industry-standard approach for cross-process SQLite observation. `data_version` is a single integer that bumps on any write: we read it each tick, and only run the real query when it has changed. Idle cost is effectively one pragma read per second.

### Poller architecture

New module: `crates/hippo-daemon/src/opencode_session.rs`. Started as a background task alongside the existing daemon tasks (shell socket, browser native messaging, gh polling).

- Opens `${XDG_DATA_HOME:-~/.local/share}/opencode/opencode.db` read-only, WAL mode, `busy_timeout=5000`. Path + enable flag configurable.
- Persists a high-water cursor in hippo's own SQLite DB (new `agentic_cursor` table, keyed by `(harness, source_path_inode)` so reinstalls of opencode do not cause replay). Stores `last_time_created` and `last_id` for tie-breaking on ties in `time_created`.
- Every `poll_interval_ms` (default 1000):
  1. `PRAGMA data_version` — compare to last; if unchanged, sleep.
  2. Query:
     ```sql
     SELECT p.id, p.time_created, p.time_updated, p.data AS part_data,
            m.data AS msg_data, s.id AS session_id, s.directory,
            s.version AS harness_version, s.parent_id
     FROM part p
     JOIN message m ON p.message_id = m.id
     JOIN session s ON p.session_id = s.id
     WHERE (p.time_created > :last_time)
        OR (p.time_created = :last_time AND p.id > :last_id)
     ORDER BY p.time_created, p.id
     LIMIT 500
     ```
  3. For each row where `json_extract(part_data, '$.type') = 'tool'`: build one `AgenticToolCall` event. Other part types are skipped by the daemon — the brain will re-read the DB for enrichment context.
  4. Advance cursor. Commit.

### opencode → `AgenticToolCall` field mapping

| Target field            | Source                                                                 |
|-------------------------|------------------------------------------------------------------------|
| `session_id`            | `session.id` → deterministic UUIDv5 (namespace: URL, name: `session.id`) |
| `parent_session_id`     | `session.parent_id` → same UUIDv5 transform                            |
| `harness`               | `Harness::Opencode`                                                    |
| `harness_version`       | `session.version` (e.g., `"1.4.6"`)                                    |
| `model`                 | `message.data.modelID`                                                 |
| `provider`              | `message.data.providerID`                                              |
| `agent`                 | `message.data.agent` (falls back to `message.data.mode`)               |
| `effort`                | `None` (opencode does not expose)                                      |
| `tool_name`             | `part.data.tool`                                                       |
| `tool_input`            | `part.data.state.input`                                                |
| `command`               | rendered via shared renderer module (see below)                        |
| `tool_output`           | `part.data.state.output` (truncated to `MAX_OUTPUT_BYTES`)             |
| `status`                | `part.data.state.status == "completed"` → `Ok`; `"error"` → `Error`    |
| `duration_ms`           | `part.time_updated - part.time_created`                                |
| `cwd`                   | `message.data.path.cwd`, else `session.directory`                      |
| `git_state`             | best-effort from cwd + `git` command at enrichment time; daemon leaves `None` |
| `tokens`                | from `message.data.tokens` (opencode provides full breakdown)          |
| `cost_usd`              | `message.data.cost`                                                    |
| `redaction_count`       | populated by redactor pipeline                                         |

### Backfill

First run after enabling opencode scans all historical sessions (cursor starts at 0). No window limit. Log count at start. Rationale: matches shell-event behavior; the user has accepted the queue-saturation trade-off.

### Config

Add to `~/.config/hippo/config.toml`:

```toml
[opencode]
enabled = true
db_path = "~/.local/share/opencode/opencode.db"  # optional override
poll_interval_ms = 1000
```

Defaults: `enabled = false` (opt-in for now), standard XDG path.

## Source 2: Codex (historical batch ingestion only)

Codex ingestion is **batch-only** — the user has cancelled their Codex subscription, so there are no live sessions to tail. Future live support can reuse the same importer with a tailing wrapper if reactivated.

### Storage layout

- `~/.codex/archived_sessions/rollout-<ISO>-<uuid>.jsonl` — completed sessions.
- `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` — live sessions (not tailed here).

Both paths are scanned; all discovered JSONL files are treated as historical.

### JSONL entry shape (`response_item` types)

- `session_meta` — one per file: `{id, timestamp, cwd, originator ("Codex Desktop"|"Codex CLI"), cli_version, model_provider, base_instructions, ...}`
- `turn_context` — per turn context
- `response_item` with `payload.type` of:
  - `message` (role=developer|user|assistant, content[].type=input_text|output_text)
  - `reasoning` (skipped for tool events)
  - `function_call` `{name, arguments (JSON string), call_id}`
  - `function_call_output` `{call_id, output}`
  - `custom_tool_call` / `custom_tool_call_output` (paired the same way)
- `event_msg` — internal event log (skipped)

### Importer

New CLI: `hippo ingest codex-sessions [--path <dir>] [--since <date>] [--batch]`. Default path is `~/.codex`. Walks `archived_sessions/` and `sessions/YYYY/...` globs.

For each JSONL file:
1. Read `session_meta` first line; extract session UUID, `cwd`, `cli_version`, `originator`, `model_provider`.
2. Stream remaining lines. Maintain a `HashMap<call_id, PendingToolCall>` identical in pattern to Claude's `PendingToolUse`.
3. On `function_call` / `custom_tool_call`: insert pending.
4. On `function_call_output` / `custom_tool_call_output`: pair by `call_id`, emit `AgenticToolCall`, remove from pending.
5. On EOF: flush orphans with `status = Orphaned`.

### Codex → `AgenticToolCall` field mapping

| Target field            | Source                                                                 |
|-------------------------|------------------------------------------------------------------------|
| `session_id`            | `session_meta.id` → UUIDv5 if not already a UUID                       |
| `parent_session_id`     | `None` (codex rollouts don't track a parent in the file header)         |
| `harness`               | `Harness::Codex`                                                       |
| `harness_version`       | `session_meta.cli_version`                                             |
| `model`                 | extracted from `turn_context.model` when present, else `"gpt-5"` fallback (codex defaults) |
| `provider`              | `session_meta.model_provider` (typically `"openai"`)                   |
| `agent`                 | `session_meta.originator` (`"Codex Desktop"` / `"Codex CLI"`)          |
| `effort`                | `turn_context.effort` if present                                       |
| `tool_name`             | `function_call.name` or `custom_tool_call.name`                        |
| `tool_input`            | `JSON.parse(function_call.arguments)` (arguments is stringified JSON)  |
| `command`               | shared renderer module                                                 |
| `tool_output`           | `function_call_output.output`, truncated                               |
| `status`                | for `function_call_output` where `output` is a JSON object with an `exit_code` field: `Ok` iff `exit_code == 0`; for string outputs: `Ok` unless the output starts with `"Error:"` / `"error:"` (Codex's convention for shell errors); orphans (no matching output) → `Orphaned` |
| `duration_ms`           | `output.timestamp - call.timestamp`                                    |
| `cwd`                   | `session_meta.cwd`                                                     |
| `tokens`                | `None` (codex rollouts don't emit per-call token usage)                |
| `cost_usd`              | `None`                                                                 |
| `redaction_count`       | populated by redactor                                                  |

## Claude Migration

The existing `crates/hippo-daemon/src/claude_session.rs` path emits `ShellEvent { shell: ShellKind::Unknown("claude-code"), ... }`. Migration:

1. Rewrite `process_line` to emit `AgenticToolCall` instead of `ShellEvent`. Field mapping:

| Target field            | Source                                                                 |
|-------------------------|------------------------------------------------------------------------|
| `harness`               | `Harness::ClaudeCode`                                                  |
| `harness_version`       | `None` (not in JSONL header)                                           |
| `model`                 | `message.model` on assistant entries                                   |
| `provider`              | `Some("anthropic")`                                                    |
| `agent`                 | from subagent filename path when JSONL is under `*/subagents/*.jsonl`, else `None` |
| `effort`                | `None`                                                                 |
| `tokens`                | `message.usage` (input/output/cache_read/cache_creation)               |
| `cost_usd`              | `None`                                                                 |

Rendering logic (`format_tool_command`) is extracted into a shared module `crates/hippo-core/src/agentic/render.rs` so Claude, opencode, and Codex all feed the same renderer.

2. Keep the `SessionStart` tmux hook and `hippo ingest claude-session` CLI as-is — only the event payload emitted changes.
3. Remove `ShellKind::Unknown("claude-code")` paths once nothing produces them.

## Brain / Enrichment Changes

### Schema migration v5 → v6

(The current schema is already at v5 — v5 added GitHub Actions / lessons tables. The agentic work is v5 → v6. The migration helper lives in `crates/hippo-core/src/storage.rs` next to the existing v4→v5 block.)

```sql
-- Rename Claude-specific tables to harness-agnostic
ALTER TABLE claude_sessions RENAME TO agentic_sessions;
ALTER TABLE claude_enrichment_queue RENAME TO agentic_enrichment_queue;
ALTER TABLE knowledge_node_claude_sessions RENAME TO knowledge_node_agentic_sessions;
ALTER TABLE knowledge_node_agentic_sessions
    RENAME COLUMN claude_session_id TO agentic_session_id;
ALTER TABLE agentic_enrichment_queue
    RENAME COLUMN claude_session_id TO agentic_session_id;

-- Add harness-labeling columns
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

CREATE INDEX idx_agentic_sessions_harness ON agentic_sessions (harness);
CREATE INDEX idx_agentic_sessions_model ON agentic_sessions (model);

-- Cursor table for live pollers (opencode today; future live sources)
CREATE TABLE agentic_cursor (
    harness          TEXT NOT NULL,
    source_key       TEXT NOT NULL,  -- inode or canonical path for rename-safety
    last_time_created INTEGER NOT NULL,
    last_id          TEXT NOT NULL,
    updated_at       INTEGER NOT NULL,
    PRIMARY KEY (harness, source_key)
);

PRAGMA user_version = 6;
```

Existing rows get `harness = 'claude-code'` (the default), `model`/`provider`/etc. null — they can be backfilled by a separate one-shot re-parse of the source JSONLs if desired, but that is not required for this spec.

### Brain-side module reorg

- `brain/src/hippo_brain/claude_sessions.py` → `agentic_sessions.py`.
- `iter_session_files(...)` grows a dispatcher. Per-harness readers:
  - Claude: existing JSONL walker (unchanged logic, returns segments tagged `harness='claude-code'`).
  - opencode: new SQLite reader that groups `part` rows by `message_id` → `SessionSegment` with same shape (segmentation by 5-min gap rule applies uniformly).
  - Codex: new JSONL reader for `~/.codex/archived_sessions` + `~/.codex/sessions/**`.
- `SessionSegment` gains `harness`, `harness_version`, `model`, `provider`, `agent`, `effort` fields (all optional except `harness`).

### Enrichment prompt change

Add a harness context line to the prompt, so the enriching LLM can reason about source:

> `Harness: opencode 1.4.6 (build agent, nvidia/nemotron-3-super via lmstudio)`
> `Harness: claude-code (claude-opus-4-7 via anthropic)`
> `Harness: codex 0.118.0-alpha.2 (Codex Desktop, gpt-5 via openai)`

### MCP surface

`search_knowledge` and `search_events` gain optional `harness` and `model` filter parameters. Propagated through to SQL `WHERE agentic_sessions.harness = ?`.

## Redaction

All new `AgenticToolCall` events flow through the existing redactor pipeline. Redaction targets:

- `command` (string)
- `tool_input` (recursively over string leaves — new helper in redactor, since existing one is string-only)
- `tool_output.content`

`redaction_count` is summed across all three and reported in the event.

## Testing

### Principles

- **No binary `.db` files in the repo.** Tests build ephemeral SQLite DBs in-memory or in `tempfile` using SQL literals pinned in a `testdata` module that mirrors the real opencode schema (copied from `sqlite3 opencode.db .schema`). If opencode schema drifts, we regenerate the pinned SQL and the diff is obvious in review.
- **Ephemeral, not shared.** Each test function gets its own fresh DB. No cross-test state.
- **Golden assertions on structured fields, not serialized JSON.** Makes failures readable.
- **Table-driven for shape-mapping tests.** One row per `(source_fixture → expected_AgenticToolCall)` case.

### Rust tests

- **`crates/hippo-core/tests/events_agentic.rs`** — round-trip JSON serialization for `AgenticToolCall` and all its enums; adjacently tagged shape check (matches existing `ShellEvent` patterns).
- **`crates/hippo-daemon/src/opencode_session.rs` unit tests:**
  - `process_tool_part_ok` — build a minimal `part`+`message`+`session` row set in-memory, assert the resulting `AgenticToolCall` fields.
  - `process_tool_part_error_status` — status mapping.
  - `cursor_advancement_tie_breaking` — two parts with identical `time_created`, verify both are read exactly once across two poll cycles.
  - `data_version_skip_when_unchanged` — set `data_version`, advance without writes, verify no query is issued.
  - `truncation_on_large_output` — 10 KB output → truncated to `MAX_OUTPUT_BYTES`.
  - `deterministic_session_uuid_from_opencode_id` — v5 UUID stable across runs.
- **`crates/hippo-daemon/src/codex_session.rs` unit tests:** port of the existing Claude test cases (`format_tool_command`, pairing, orphans, truncation, deterministic envelope IDs, malformed JSON, missing `session_meta`) against codex JSONL fixtures.
- **`crates/hippo-daemon/src/claude_session.rs` existing tests:** updated to assert `AgenticToolCall` output instead of `ShellEvent`.
- **`crates/hippo-core/src/agentic/render.rs` unit tests:** one per tool_name renderer (Bash, Read, Edit, Write, Grep, Glob, Agent, TaskCreate, TaskUpdate, exec_command, skill, unknown fallback).
- **Migration golden test** (`crates/hippo-core/tests/schema_v6_migration.rs`): programmatically build a v4-shaped DB with a known `claude_sessions` row + related queue row + FK link, run the migration, assert (a) all data preserved, (b) `harness='claude-code'` default applied, (c) schema matches v5 spec, (d) `PRAGMA user_version = 5`.

### Python tests

- **`brain/tests/test_agentic_sessions.py`:**
  - Claude extraction parity — existing test cases, asserting `harness='claude-code'` and model/provider are now populated.
  - opencode SQLite extraction — seed an in-memory opencode DB, run reader, verify `SessionSegment` harness/model/provider fields.
  - Codex JSONL extraction — fixture JSONL string, verify segments.
  - Segmentation — uniform 5-min gap rule across all three harnesses.
- **`brain/tests/test_enrichment_prompt.py`:** verify harness-context line formatting for each harness.

### Integration sanity

- `cargo test --all-targets` must pass.
- `uv run --project brain pytest brain/tests -v --cov=hippo_brain` must pass; coverage on new files ≥ existing project baseline.
- `cargo clippy --all-targets -- -D warnings` clean.
- `ruff check` and `ruff format --check` clean.

## Observability

- New metrics (all gated behind the existing OTel env flag):
  - `hippo_agentic_events_emitted_total{harness}` counter
  - `hippo_agentic_poller_ticks_total{harness,outcome}` (outcome: `no_change`, `rows_read`, `error`)
  - `hippo_agentic_backfill_files_total{harness}` counter (Codex importer)
- `hippo doctor` gains a check per active agentic harness: does the DB/dir exist, is the cursor reasonable, is ingestion enabled.

## Future Work

- **Copilot CLI ingestion.** Revisit when (a) real session data exists to analyze and (b) decide whether to introduce a coarser `AgenticTurn` variant or skip.
- **Codex live tailing.** Straightforward reuse of the importer if subscription is reactivated — watch `~/.codex/sessions/YYYY/...` for new files and tail them.
- **Reasoning/patch events as separate payload variants.** Revisit if downstream queries want them as first-class.
- **Cost derivation from model + tokens.** Central table or plugin; out of scope here.
- **Per-harness rate limiting on backfill.** If enrichment queue saturation becomes a problem in practice.

## Rollout Plan (high level, for the implementation plan step)

1. Land `AgenticToolCall` type + renderer module + tests. No behavioral change yet.
2. Land schema v6 migration + Python module rename + backwards-read-only reads still working.
3. Migrate Claude daemon pipeline to emit `AgenticToolCall`. Verify parity on real sessions.
4. Land opencode poller (gated `enabled = false` by default). Dogfood with opt-in.
5. Land Codex batch importer CLI.
6. Enable opencode by default; update `hippo doctor`; add docs.
