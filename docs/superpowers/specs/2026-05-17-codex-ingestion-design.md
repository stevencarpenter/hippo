# Codex session ingestion — design

**Date:** 2026-05-17
**Status:** Approved, ready for implementation planning
**Scope:** Add OpenAI Codex CLI sessions as a hippo capture source, alongside
Claude Code and opencode.

## 1. Summary

hippo captures Claude Code and opencode coding sessions. This adds **Codex** —
the OpenAI Codex CLI — as a third agentic-session source. A new Rust daemon
poller (`codex_session.rs`) reads Codex rollout JSONL files from disk on a
launchd-scheduled interval, parses them into segments, and writes them into the
existing `claude_sessions` table. The legacy Python ingestion path is retired.

This is deliberately an **interim** design. The end state — a single unified
`agentic_sessions` table for all harnesses — is captured as a separate GitHub
issue series (see §10). This spec covers only the in-scope work: getting Codex
captured, cleanly, without a schema migration of the session tables.

## 2. Background: two things named "codex"

Codex runs in two places on this machine, both writing the **identical** rollout
format:

- **Standalone Codex CLI** — `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`,
  plus archived sessions under `~/.codex/archived_sessions/`. Not currently
  ingested.
- **Xcode-embedded Codex** — Xcode 26's coding assistant bundles a Codex copy at
  `~/Library/Developer/Xcode/CodingAssistant/codex/sessions/...`. This *is*
  ingested today, by `scripts/hippo-ingest-codex.py` + the
  `com.hippo.xcode-codex-ingest` launchd job.

Rollout files are newline-delimited JSON with typed lines: `session_meta`,
`event_msg`, `response_item`, `turn_context`. The two roots differ only by the
`originator` field and path. A single parser handles both.

The unified ingester watches **both roots**, including archived sessions, so all
Codex activity is captured regardless of where it was launched.

## 3. Decision: write to `claude_sessions`, segmented

There are two session tables in the repo, mid-migration:

- `claude_sessions` — **segmented** (`UNIQUE(session_id, segment_index)`,
  `content_hash` dedup). Written by the Claude ingesters and, today, by the
  legacy Codex script. The brain enriches per segment.
- `agentic_sessions` — **one row per session** (`UNIQUE(session_id, harness)`,
  explicit `harness` column). Written only by the opencode poller.

`agentic_sessions` was introduced for opencode, whose source rows are naturally
1:1 with sessions. Its `UNIQUE(session_id, harness)` constraint makes
"one row per session" a property of that table — not a designed invariant for
agentic sessions in general.

Codex rollout files are **long, multi-task sessions**, the same shape as Claude
sessions. Segmenting them at task boundaries (one knowledge node per task) is
real enrichment fidelity. Collapsing a multi-hour Codex session into a single
enrichment unit, only to fit opencode's table shape, is a regression.

Therefore Codex writes to **`claude_sessions`**, segmented — the same data model
the Claude ingesters and the legacy Codex script already use. This change is
purely mechanical: it moves Codex ingestion from a Python launchd script into a
Rust daemon poller and unifies the two roots. **No session-table schema change.**

Rejected alternatives:

- *Codex → `agentic_sessions`, one row per session.* Worst of both: drops task
  segmentation **and** new code lands on a table that can't hold Claude either.
- *Extend `agentic_sessions` with `segment_index` and migrate Codex there.* The
  correct end state, but a schema migration touching opencode's upsert and the
  enrichment FK. That is the unification project (§10), not "add a source."

## 4. Architecture

### 4.1 Data model

Codex sessions land in `claude_sessions` as segments, alongside Claude rows.
Codex session UUIDs and Claude session UUIDs occupy distinct spaces, so they
coexist without collision under `UNIQUE(session_id, segment_index)`.

The poller populates the same columns the Claude watcher does, because the brain
enriches `claude_sessions` from the structured columns, not `summary_text`
alone (it `SELECT`s `summary_text`, `tool_calls_json`, `user_prompts_json`):

- `session_id` — rollout `session_meta.payload.id` (UUID)
- `segment_index` — 0-based, per the segmentation rule below
- `project_dir`, `cwd` — from `session_meta.payload.cwd`, updated by
  `turn_context.cwd`
- `start_time` / `end_time` — first / last line timestamps in the segment
  (epoch ms)
- `summary_text` — human-readable digest built by the poller (Codex-framed)
- `tool_calls_json` — JSON array of `{name, summary}` tool calls
- `user_prompts_json` — JSON array of user prompt strings
- `message_count`, `token_count` — counts (`token_count` best-effort, 0 if the
  rollout has no usage line)
- `source_file` — absolute path of the rollout file (**the harness signal** —
  see §7)
- `content_hash` — `SHA256(tool_calls_json | user_prompts_json | assistant_texts)`,
  computed identically to the Claude watcher so re-enrichment triggers on
  content change
- `is_subagent` — 0; `parent_session_id` — NULL (Codex rollouts have no
  sub-session concept)
- `git_branch` — best-effort from cwd, or NULL

Enqueue each upserted segment into `claude_enrichment_queue`
(`ON CONFLICT(claude_session_id) DO UPDATE` re-pending unless `processing`),
mirroring the Claude watcher.

### 4.2 Segmentation

Port the proven logic from the legacy `codex_sessions.py`:

- A new segment starts when the gap between consecutive user prompts exceeds
  5 minutes, or accumulated content exceeds ~12k characters.
- `segment_index` is 0-based within a session file.
- Empty segments (no prompts, tool calls, or assistant text) are dropped.

### 4.3 The poller — `crates/hippo-daemon/src/codex_session.rs`

`pub fn poll_tick(config: &HippoConfig) -> Result<usize>`, structured like
`opencode_session::poll_tick` (the *mechanism*), but writing segmented
`claude_sessions` rows (the *data model*):

1. Return early if `config.codex.enabled` is false.
2. Open the hippo DB via `hippo_core::storage::open_db`.
3. Walk every configured root for `rollout-*.jsonl` files.
4. For each file: `stat` it; **skip** files whose mtime is within
   `min_idle_secs` of now (in-flight — avoid partial reads).
5. Consult the per-file cursor (§4.5). Skip files whose mtime has not advanced
   past the cursor.
6. Parse the file into segments; in **one transaction per file**, upsert all
   the file's segments, enqueue each into `claude_enrichment_queue`, and bump
   `source_health` (`agentic-session-codex`): `last_event_ts`,
   `last_success_ts`, `consecutive_failures = 0`. AP-1: the segment inserts and
   the health bump land in lockstep so the watchdog sees a consistent picture.
7. On success, advance the per-file cursor. On a failed file, leave the cursor
   unadvanced (the file is retried next tick), increment `consecutive_failures`
   and record `last_error_*`, mirroring `opencode_session::record_upsert_error`
   so the watchdog's consecutive-failure invariant stays reachable.

The launchd job owns scheduling; `poll_tick` is one scan+upsert cycle.
Idempotency is guaranteed by `INSERT … ON CONFLICT(session_id, segment_index)`
plus the `content_hash` comparison — the cursor is purely a performance
optimization to avoid re-parsing unchanged files.

### 4.4 The parser

A Rust port of the `codex_sessions.py` rollout parser. Line handling:

- `session_meta` — canonical session id and cwd.
- `turn_context` — may update cwd mid-session.
- User prompts — the parser must handle **both** observed shapes:
  `event_msg` with `payload.type == "user_message"`, **and** `response_item`
  with `payload.type == "message"`, `role == "user"`. (The standalone CLI and
  Xcode-embedded Codex have been observed emitting different shapes; the
  implementation plan must validate against real fixtures from both roots.)
- Tool calls — `response_item` with `payload.type` in
  `{function_call, custom_tool_call}`; summarized via the `_tool_summary`
  heuristic (cmd / command / path / query → short string).
- Assistant text — `response_item`, `role == "assistant"`, `output_text`
  content blocks.
- `role == "developer"` messages are skipped (system injections).
- Xcode injects a project-context prefix into user messages; strip it via the
  `_extract_user_text_from_codex_message` heuristic.

The parser builds `summary_text` with Codex-appropriate framing (user requests
+ work performed + assistant excerpts, capped at the existing 500/120/300-char
limits). Because this framing now lives in the Rust poller, the legacy Python
`insert_segment` Codex branch and `build_codex_enrichment_summary` become dead
code and are removed (§6).

### 4.5 Cursor

A per-file mtime cursor in the existing `agentic_cursor` table (generic
`(source_key, last_seen_updated_at, last_id, updated_at)` schema — no change):

- `source_key` = `codex-<inode>`. Inode is stable across the `mv` Codex
  performs when archiving a session, so an archived file is correctly
  recognized as already-ingested rather than re-parsed.
- `last_seen_updated_at` = the file's mtime (epoch ms) at last successful parse.
- `last_id` = the session id.

First run: no cursor rows exist, so every file — including archived sessions —
is parsed once (backfill). A resumed session re-grows its file, bumping mtime,
triggering a re-parse; `content_hash` ensures only changed segments re-enqueue.

## 5. Integration points

- **Config** — new `CodexConfig` struct in `hippo-core` (`enabled: bool`,
  `session_roots: Vec<PathBuf>`, `min_idle_secs: u64`, `poll_interval_secs: u64`),
  added to `HippoConfig`; `[codex]` section in `config.toml`. Default
  `session_roots`: `~/.codex/sessions`, `~/.codex/archived_sessions`,
  `~/Library/Developer/Xcode/CodingAssistant/codex/sessions`.
- **CLI / daemon** — new `Commands::CodexPoll` → `codex_session::poll_tick`,
  mirroring `OpencodePoll`. No separate `hippo ingest codex` subcommand: the
  poll already scans everything idempotently and doubles as manual recovery.
- **launchd** — rename `launchd/com.hippo.xcode-codex-ingest.plist` →
  `com.hippo.codex-session.plist`; `ProgramArguments` runs `hippo codex-poll`;
  `StartInterval = 60`; `WatchPaths` on both session roots; `ThrottleInterval`
  to coalesce bursts. Update the install wiring in `main.rs` / `install.rs`
  (the two `xcode-codex-ingest` references) and `mise.toml`.
- **Schema v15** — bump `PRAGMA user_version` 14 → 15. The migration is a single
  `INSERT OR IGNORE INTO source_health` for `agentic-session-codex` (the
  capture-path key; health-row names are already decoupled from table names —
  Claude writes `claude_sessions` but its health row is `agentic-session-claude`).
  Without the row, the poller's `source_health` `UPDATE` is a silent no-op.
- **doctor** — add `'agentic-session-codex'` to `check_source_staleness`'s
  `WHERE source IN (...)` list.
- **watchdog** — add Codex freshness coverage mirroring `agentic-session-opencode`.

## 6. Retiring the legacy path

Delete:

- `scripts/hippo-ingest-codex.py`
- `brain/src/hippo_brain/codex_sessions.py` and its tests
- `launchd/com.hippo.xcode-codex-ingest.plist` (replaced by the renamed plist)
- the now-dead `segment.source == "codex"` branch in `claude_sessions.py`
  `insert_segment`, and `build_codex_enrichment_summary`

The brain's enrichment path needs **no new code**: Codex segments enter the
existing `claude_enrichment_queue` and are enriched exactly like Claude
segments. The implementation plan must verify whether the brain wants
Codex-specific prompt *framing*; given enrichment reads the structured
`tool_calls_json` / `user_prompts_json` columns, the expected answer is "no
change needed" — but this is an explicit plan-time check, not an assumption.

## 7. Harness derivation (for the future migration)

`claude_sessions` has no `harness`/`source` column, so Codex and Claude rows are
not explicitly tagged. They are distinguished by `source_file`, which is
`NOT NULL` and always an absolute path. The §10 migration backfills
`agentic_sessions.harness` from it:

- `source_file` contains `/.codex/` **or** `/CodingAssistant/codex/` → `codex`
- otherwise → `claude-code`

A second independent signal cross-checks this: Codex files are named
`rollout-<ts>-<uuid>.jsonl`; Claude session files are named `<uuid>.jsonl`. The
migration should classify on both and **log** any row whose path matches
neither a known Claude nor a known Codex root rather than silently defaulting.

This derivation is reliable because both ingesters always write a real
absolute path, and it is uniform across old legacy-script rows and new
Rust-poller rows. A `source` column on `claude_sessions` was considered and
rejected: it adds schema to a table the migration will retire, for marginal
benefit over an already-unambiguous path signal.

## 8. Testing

Rust unit tests, mirroring `opencode_session` / Claude-watcher coverage:

- **Parser** — fixture rollout files (real samples exist under both roots):
  segmentation at 5-minute gaps, both user-message shapes, tool-call
  summarization, developer-message skip, Xcode-context stripping.
- **`poll_tick`** — temp hippo DB: first-run backfill, cursor advance,
  idempotent re-ingest (unchanged file → no-op), in-flight skip
  (`min_idle_secs`), archived-file inode stability (moved file not re-parsed),
  resumed-session re-enqueue on content change, `source_health` success and
  error bumps.

## 9. Out of scope

- A synthetic Codex probe row (the probe canary system). Follow-up if wanted.
- Deep model-name extraction beyond what is trivially present in the rollout.
- Any change to the `claude_sessions` / `agentic_sessions` schema beyond the
  v15 `source_health` seed row.

## 10. Follow-up: the `agentic_sessions` unification

The desired end state is a single segmentation-capable `agentic_sessions` table
for all harnesses, with `claude_sessions` retired. That is a separate, larger
migration — adding `segment_index` to `agentic_sessions`, changing its unique
constraint, porting the opencode poller / Claude ingesters / Codex poller onto
it, unifying the enrichment queues and `knowledge_node_*` link tables,
backfilling existing rows (harness derived per §7), and dropping the legacy
tables.

This work is tracked as a dedicated GitHub issue series, created alongside this
spec. This Codex feature is explicitly the interim step; it is designed so the
migration can absorb it cleanly via the §7 harness derivation.
