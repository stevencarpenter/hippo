# Agentic Session Ingestion: opencode, Codex, and First-Class Harness Labeling

**Date:** 2026-04-17 (audited and updated 2026-04-25)
**Status:** Design approved; Phase 1 partial on `agentic-ingestion` branch; plan rewrite in progress on main
**Scope:** Expand Claude Code session ingestion into a general "agentic tool-call" abstraction that also covers opencode (live) and Codex (already on main via legacy path; rewires onto the new abstraction). Add first-class fields for harness, model, provider, agent/mode, and effort.

## Audit & Update Log (2026-04-25)

This spec was originally written 2026-04-17. Eight days of merges to main moved several load-bearing assumptions:

- **Schema target:** spec said v5 → v6; reality is **v9 → v10** (v6 added FTS5 + sqlite-vec on knowledge_nodes; v7 added enrichment-queue cleanup; v8 added `source_health` table + `probe_tag` columns on `events`/`claude_sessions`/`browser_events`; v9 added `capture_alarms` for the watchdog). The `agentic-ingestion` branch retargeted to v6 → v7 in commit `c7b8534`; that retarget is also stale.
- **`probe_tag` column inheritance:** synthetic-probe machinery (PR #82) put a `probe_tag TEXT` column on every event-bearing table. The renamed `agentic_sessions` table inherits it; `AgenticToolCall` events flowing through the daemon will carry probe attribution like every other source.
- **`source_health` integration:** PR #67 added a `source_health` table pre-seeded with rows for `shell` / `claude-tool` / `claude-session` / `browser`. Doctor checks are no longer bespoke per source; they read this table for staleness. `AgenticToolCall` ingestion adds rows for opencode and (eventually) renames or retires `claude-session`/`claude-tool` once the rename lands.
- **OTel `source` attribute (PR #66):** daemon metrics renamed `type → source` for source_health alignment. New per-harness metrics in this spec use the `source` (or `harness`) attribute name to stay consistent.
- **Per-rule redaction attribution (PR #74):** the `REDACTIONS` counter is now per-rule. The `redaction_count` field on `AgenticToolCall` continues to hold the per-event total; per-rule attribution emerges from the redactor's metrics, not the event payload.
- **Codex's actual state on main:** Codex is **not** "future work / batch importer" — it is **already shipping** as `brain/src/hippo_brain/codex_sessions.py` + `scripts/hippo-ingest-codex.py` + `launchd/com.hippo.xcode-codex-ingest.plist`, routed through the legacy `claude_sessions` table with provenance implicit in `source_file` paths under `~/Library/Developer/Xcode/CodingAssistant/codex/sessions/`. The migration story below has to **rewire existing Codex** onto `AgenticToolCall`, not introduce it from scratch.
- **Phase 1 partially executed:** the `agentic-ingestion` branch contains working `agentic/{types,render,mod}.rs`, the `EventPayload::AgenticToolCall` variant, and three test files. **One commit on that branch is regressed** (`cf6d20f` was authored before `probe_tag` and `tool_name` landed on main, and removes both fields). The plan rewires the Phase 1 landing to cherry-pick the four good commits onto a fresh branch off current main and replay the regressed commit by hand.
- **Source-audit test pattern:** `crates/hippo-daemon/tests/source_audit/{shell_events,claude_tool_events,browser_events}.rs` is now the canonical integration-test shape per source. New ingesters add a matching `agentic_tool_calls.rs` (or per-harness file) here.
- **Watchdog `capture_alarms`:** v9 added the table; invariants are feature-flagged off today. Out of scope for v1; future work line item.
- **Version baseline:** current is v0.16.0; rollout lands on top.

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
    pub started_at: DateTime<Utc>,         // wall time the tool ran (distinct from
                                           // EventEnvelope.timestamp, which is the
                                           // producer's ingest time — these differ
                                           // for batch backfills like Codex JSONLs)
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

// Two derived strings per harness:
//   `as_db_str()`        → "claude-code", "opencode", "codex" (agentic_sessions.harness column)
//   `source_basename()`  → "claude",      "opencode", "codex" (source_health row composition)
// They differ for ClaudeCode because v8 seeded source_health with `claude-tool`
// and `claude-session`, not `claude-code-tool` / `agentic-session-claude-code`.
// Future ingesters compose source_health row names as `agentic-session-{basename}`
// or `{basename}-tool` via `source_basename()`.

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

## Source 2: Codex (already on main; rewire onto AgenticToolCall)

**Current state on main:** Codex is already ingesting via `brain/src/hippo_brain/codex_sessions.py` + `scripts/hippo-ingest-codex.py` + `launchd/com.hippo.xcode-codex-ingest.plist` (5-min `StartInterval` + `WatchPaths` polling). It writes into the `claude_sessions` table with provenance implicit in `source_file` paths under `~/Library/Developer/Xcode/CodingAssistant/codex/sessions/`. The brain-side enrichment summary builder (`build_codex_enrichment_summary`) is selected via the in-memory `SessionSegment.source` discriminator (see `brain/src/hippo_brain/claude_sessions.py:435`), but the discriminator is not persisted today.

**The work here is rewire, not introduce.** Three concrete changes:

1. **Daemon emit path:** today the Python ingester writes directly to SQLite via `claude_sessions.insert_segment(...)`. After v9→v10, the same path emits one `EventPayload::AgenticToolCall` per `function_call`/`custom_tool_call` (matching the spec's "Codex importer" description) AND maintains an `agentic_sessions` segment row (so existing `hippo ask` retrieval keeps working). Phased: per-call events first; segment-row writes continue unchanged.
2. **Schema migration backfills `harness = 'codex'`** for existing rows with `source_file LIKE '%/CodingAssistant/codex/sessions/%'` (see migration SQL above). No data loss; the discriminator becomes durable.
3. **Source naming:** the existing `xcode-codex-ingest` launchd plist label stays as-is (don't break `launchctl` semantics during a schema change). Doctor checks read `source_health.source = 'agentic-session-codex'` for staleness.

**Storage layout (unchanged):**

- `~/Library/Developer/Xcode/CodingAssistant/codex/sessions/YYYY/MM/DD/rollout-*.jsonl` — Xcode Codex (currently capturing).
- `~/.codex/archived_sessions/rollout-<ISO>-<uuid>.jsonl` — Codex CLI archived (not currently captured; included for completeness if reactivated).
- `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` — Codex CLI live (not currently captured).

Both `~/.codex` paths are referenced in the importer code path but not actively used; they're retained because the JSONL format is identical and a future enable is a config change, not a code change.

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

### Schema migration v9 → v10

Current schema is at v9 (capture_alarms / watchdog, feature-flagged). The agentic work is **v9 → v10**. The migration helper lives in `crates/hippo-core/src/storage.rs` next to the existing per-version blocks (v4→v5 through v8→v9).

`claude_sessions` already carries a `probe_tag TEXT` column from v8; the rename preserves it on `agentic_sessions`. `source_health` already has rows for `shell`/`claude-tool`/`claude-session`/`browser`; the migration adds `opencode` and renames `claude-session` → `agentic-session-claude` (or keeps both during a deprecation window — see "Source naming during migration" below).

```sql
-- Rename Claude-specific tables to harness-agnostic.
-- claude_sessions already has 19 columns (verified against schema.sql v9):
-- id, session_id, project_dir, cwd, git_branch, segment_index, start_time,
-- end_time, summary_text, tool_calls_json, user_prompts_json, message_count,
-- token_count, source_file, is_subagent, parent_session_id, enriched,
-- probe_tag, created_at. All carried through unchanged by the rename.
ALTER TABLE claude_sessions RENAME TO agentic_sessions;
ALTER TABLE claude_enrichment_queue RENAME TO agentic_enrichment_queue;
ALTER TABLE knowledge_node_claude_sessions RENAME TO knowledge_node_agentic_sessions;
ALTER TABLE knowledge_node_agentic_sessions
    RENAME COLUMN claude_session_id TO agentic_session_id;
ALTER TABLE agentic_enrichment_queue
    RENAME COLUMN claude_session_id TO agentic_session_id;

-- Recreate the indexes from v8 under their new names. SQLite does not
-- automatically rename indexes during ALTER TABLE RENAME (it preserves the
-- index DEFINITION but the index name remains the old one), so we drop and
-- recreate explicitly to keep schema.sql / sqlite_master tidy.
DROP INDEX IF EXISTS idx_claude_sessions_cwd;
DROP INDEX IF EXISTS idx_claude_sessions_session;
DROP INDEX IF EXISTS idx_claude_queue_pending;
CREATE INDEX idx_agentic_sessions_cwd     ON agentic_sessions (cwd);
CREATE INDEX idx_agentic_sessions_session ON agentic_sessions (session_id);
CREATE INDEX idx_agentic_queue_pending    ON agentic_enrichment_queue (status, priority)
    WHERE status = 'pending';

-- Add harness-labeling columns (all nullable except harness, which defaults).
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

-- Backfill harness for existing Codex rows (these are real on main today —
-- routed through claude_sessions today with provenance implicit in source_file).
UPDATE agentic_sessions
SET harness = 'codex'
WHERE harness = 'claude-code'
  AND source_file LIKE '%/CodingAssistant/codex/sessions/%';

-- Cursor table for live pollers (opencode today; future live sources).
CREATE TABLE agentic_cursor (
    harness           TEXT NOT NULL,
    source_key        TEXT NOT NULL,  -- inode or canonical path for rename-safety
    last_time_created INTEGER NOT NULL,
    last_id           TEXT NOT NULL,
    updated_at        INTEGER NOT NULL,
    PRIMARY KEY (harness, source_key)
);

-- Update source_health rows. Two grains per agentic source: per-call (events
-- table; source_kind = '<harness>-tool') and per-segment (agentic_sessions).
-- Existing rows: 'shell', 'claude-tool', 'claude-session', 'browser'.
-- - Rename 'claude-session' → 'agentic-session-claude' (harness-aware naming).
-- - Keep 'claude-tool' (the per-call events row stays under that name; the
--   row's events come from EventPayload::AgenticToolCall after Phase 4 but
--   the source_kind stays 'claude-tool' for dashboard continuity).
-- - Add new rows for opencode (both grains) and codex (segment grain only;
--   per-call events for codex are future work).
INSERT OR IGNORE INTO source_health (source, last_event_ts, updated_at) VALUES
    ('opencode-tool',            NULL, unixepoch('now') * 1000),
    ('agentic-session-opencode', NULL, unixepoch('now') * 1000),
    ('agentic-session-codex',
     (SELECT MAX(start_time) FROM agentic_sessions WHERE harness = 'codex'),
     unixepoch('now') * 1000);

UPDATE source_health
SET source = 'agentic-session-claude'
WHERE source = 'claude-session';

PRAGMA user_version = 10;
```

**Backfill semantics:** existing rows that do NOT match the Codex-path filter retain `harness = 'claude-code'` (the column default). `model`/`provider`/etc. remain NULL on backfilled rows — populating them requires re-parsing the original JSONLs, which is a separate one-shot job and not required for this spec. Backfilled NULL `model` columns are excluded from `model`-filtered MCP queries (filter applies `WHERE model = ? AND model IS NOT NULL`).

**Source naming during migration:** the v8-seeded `source_health` row `claude-session` is renamed in-place to `agentic-session-claude`. Daemon code that previously wrote `source = 'claude-session'` updates in lock-step (search/replace + tests). The grace-period alternative (insert new row, leave old in place, write to both for one release) is not used — the rename is atomic per migration step and there's no external consumer reading these rows.

**`probe_tag` carries through:** `claude_sessions.probe_tag` is preserved by the table rename (it's a column on the renamed table). New ingesters write `NULL` for real captures and the synthetic-probe envelope's UUID for probe rows, matching the existing convention on `events` / `browser_events`.

### Brain-side module reorg

- `brain/src/hippo_brain/claude_sessions.py` → `agentic_sessions.py`. Existing in-tree imports update; `claude_sessions` shim is **not** kept (per repo convention against compat shims).
- `brain/src/hippo_brain/codex_sessions.py` stays as `codex_sessions.py` (it's already a per-harness reader; no rename needed). `build_codex_enrichment_summary` is reused by the dispatcher.
- `iter_session_files(...)` becomes `iter_agentic_segments(harness=...)` — a dispatcher that picks a reader by harness:
  - **Claude:** existing JSONL walker (unchanged logic, segments tagged `harness='claude-code'`).
  - **opencode:** new SQLite reader (`opencode_sessions.py`) that walks `session`/`message`/`part` from `~/.local/share/opencode/opencode.db`. Segmentation by 5-min user-prompt gap rule applies uniformly. Cursor lives in `agentic_cursor` (NOT `ingestion_state` — `ingestion_state` is for the daemon's coarse per-source watermarks; `agentic_cursor` is per-source-key for tie-breaking on `(time_created, id)`).
  - **Codex:** existing JSONL walker (`codex_sessions.py`); reads `~/Library/Developer/Xcode/CodingAssistant/codex/sessions/**/rollout-*.jsonl` plus the unused-but-allowed `~/.codex/archived_sessions/`.
- `SessionSegment` gains `harness`, `harness_version`, `model`, `provider`, `agent`, `effort`, `tokens_*`, `cost_usd` fields (all optional except `harness`, which defaults to `'claude-code'` for the unspecified case).
- `brain/src/hippo_brain/schema_version.py`: bump `EXPECTED_SCHEMA_VERSION` to `10`; expand `ACCEPTED_READ_VERSIONS` (currently `{9, 8, 7, 6, 5}`) to include `10`. Whether to drop `5` (which requires `migrate-v5-to-v6.py` to read) is a separate cleanup question outside this spec.

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

`redaction_count` is summed across all three and reported in the event payload (one number, total hits across the three targets).

**Per-rule attribution (PR #74) is orthogonal:** the redactor already emits a per-rule counter via the `REDACTIONS{rule}` metric on the daemon. `AgenticToolCall` does not duplicate per-rule attribution into the event payload (the metric pipeline is the source of truth for which rules fired). The event-level `redaction_count` is a totals-only field for in-row debuggability and for the source-audit tests' assertion that no PII slips through unredacted.

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
- **`crates/hippo-core/src/agentic/render.rs` unit tests:** one per tool_name renderer (Bash, Read, Edit, Write, Grep, Glob, Agent, TaskCreate, TaskUpdate, exec_command, skill, unknown fallback). Already on `agentic-ingestion` branch; cherry-picks into the fresh branch.
- **Source-audit integration test** (`crates/hippo-daemon/tests/source_audit/agentic_tool_calls.rs`): matches the existing `shell_events.rs` / `claude_tool_events.rs` / `browser_events.rs` pattern. End-to-end: emit a fixture `AgenticToolCall`, assert (a) row inserted into `agentic_sessions`, (b) `harness` populated, (c) `probe_tag IS NULL` on real captures, (d) `redaction_count` non-zero for events containing seeded secrets, (e) `source_health` row updated.
- **Migration golden test** (`crates/hippo-core/tests/schema_v10_migration.rs`): programmatically build a v9-shaped DB with a known `claude_sessions` row (one Claude path, one Codex `~/Library/Developer/Xcode/CodingAssistant/codex/sessions/...` path), `claude_enrichment_queue` row, `knowledge_node_claude_sessions` link, `source_health` rows for `claude-session`/`browser`, and a `probe_tag` value on one row. Run migration, assert: (a) all data preserved, (b) `harness='claude-code'` on the Claude row, `harness='codex'` on the Codex row (backfill), (c) `probe_tag` survives the rename, (d) `source_health` row renamed to `agentic-session-claude` and new `agentic-session-opencode`/`agentic-session-codex` rows present, (e) all renamed indexes match the new schema, (f) `PRAGMA user_version = 10`.

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

- **New OTel metrics** (gated behind the existing OTel env flag; attribute name is `harness` to align with the schema column, not `source` — see naming note below):
  - `hippo_agentic_events_emitted_total{harness}` counter
  - `hippo_agentic_poller_ticks_total{harness,outcome}` (outcome: `no_change`, `rows_read`, `error`)
  - `hippo_agentic_backfill_files_total{harness}` counter (Codex importer)
- **Attribute naming:** PR #66 renamed daemon-level `type → source` for source_health alignment. The `harness` attribute on the new metrics is a *finer-grained* label than `source` (one `source` row in `source_health` corresponds to one harness via the `agentic-session-<harness>` naming). Where a metric needs both, emit `source = "agentic-session-opencode"` plus `harness = "opencode"`.
- **`hippo doctor` integration** lands via `source_health` rather than bespoke per-source code:
  - Each harness has a `source_health` row (`agentic-session-claude` / `agentic-session-opencode` / `agentic-session-codex`).
  - The existing P0.3 staleness / fail-count check (PR #70) covers all three uniformly. No new doctor check code per harness — only the seed rows in the migration.
  - One-off harness-specific health (e.g., "is opencode actually installed") lives in the `probe_ok` column populated by the synthetic-probe machinery (PR #82). For opencode that means a probe that opens `opencode.db` read-only and verifies the schema hash; failed probe → `probe_ok = 0` → doctor flags it.

## Future Work

- **Copilot CLI ingestion.** Revisit when (a) real session data exists to analyze and (b) decide whether to introduce a coarser `AgenticTurn` variant or skip.
- **Codex live tailing.** Straightforward reuse of the importer if subscription is reactivated — watch `~/.codex/sessions/YYYY/...` for new files and tail them.
- **Reasoning/patch events as separate payload variants.** Revisit if downstream queries want them as first-class.
- **Cost derivation from model + tokens.** Central table or plugin; out of scope here.
- **Per-harness rate limiting on backfill.** If enrichment queue saturation becomes a problem in practice.

## Rollout Plan (high level, for the implementation plan step)

1. **Phase 1 — agentic types + renderer + EventPayload variant.** Cherry-pick `21c98f9`, `d332279`, `18dde05` from `agentic-ingestion` onto a fresh branch off current main. Manually replay `cf6d20f` (the events.rs change), preserving `EventEnvelope.probe_tag` (v8) and `ShellEvent.tool_name` (source-audit) which the original commit removed. Add `probe_tag: None` to the cherry-picked `agentic_envelope.rs` test fixture. Verify with `cargo test -p hippo-core` + `cargo clippy --all-targets -- -D warnings`. No behavioral change yet — types only.
2. **Phase 2 — schema v9 → v10 migration.** Rename tables / indexes; add harness-labeling columns; backfill `harness = 'codex'` from `source_file` paths; create `agentic_cursor`; rename `source_health` row + add new ones; bump `EXPECTED_SCHEMA_VERSION` and `ACCEPTED_READ_VERSIONS` in Rust + Python. Land migration golden test (`schema_v10_migration.rs`).
3. **Phase 3 — Brain module rename + reader dispatcher.** Move `claude_sessions.py` → `agentic_sessions.py`; introduce `iter_agentic_segments(harness=...)` dispatcher; `SessionSegment` gains harness-labeling fields; `codex_sessions.py` reader is wrapped by the dispatcher; existing Claude + Codex enrichment paths continue to work end-to-end at this stage.
4. **Phase 4 — Daemon Claude path migrates to `AgenticToolCall`.** `process_line` in `claude_session.rs` emits `EventPayload::AgenticToolCall` instead of `EventPayload::Shell { ShellKind::Unknown("claude-code") }`. Source-audit test added (`tests/source_audit/agentic_tool_calls.rs`). Existing `claude-tool` ShellEvents stop being emitted; the daemon's source attribution moves to `agentic-session-claude`.
5. **Phase 5 — opencode live poller** (`crates/hippo-daemon/src/opencode_session.rs`). Daemon background task; `PRAGMA data_version` gating; cursor in `agentic_cursor`; redaction; `source_health` write-paths; config `[opencode] enabled` (defaults true on personal-mac install, but no-op if `~/.local/share/opencode/opencode.db` doesn't exist). Synthetic-probe support in `crates/hippo-daemon/src/probe.rs` so `hippo doctor` can flag a missing or unreadable opencode DB.
6. **Phase 6 — Codex rewire.** Existing `codex_sessions.py` + ingestion script + launchd plist stay (don't break running deployments). The brain reader is moved under the dispatcher and now emits `harness='codex'` on segments; the Rust daemon does NOT yet emit per-call `AgenticToolCall` events for Codex (that's a future-work line item — JSONL-based Codex still goes through the Python segment writer).
7. **Phase 7 — final wiring.** Enrichment-prompt harness context line; MCP `harness` / `model` filter parameters; metrics emission; docs (CLAUDE.md, `docs/`); `hippo doctor` smoke against the new `source_health` rows.

**Cleanup of `agentic-ingestion` branch:** kept until the new branch merges. After merge, `git branch -d agentic-ingestion` removes the local; the GitHub branch (if any) gets deleted via the PR-merge UI. Open PRs from the old branch are closed without merging in favor of the fresh branch.
