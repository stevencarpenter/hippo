# Cursor Agent Session Ingestion — Design

**Status:** approved (brainstorm) — pending implementation plan
**Date:** 2026-05-25
**Companion runbook:** [`docs/capture/adding-a-source.md`](../../capture/adding-a-source.md)
**Closest precedent:** [`2026-05-17-codex-ingestion-design.md`](2026-05-17-codex-ingestion-design.md)

## 1. Goal & context

Add **Cursor** as hippo's fourth agentic-coding capture source, alongside
Claude Code, Codex, and opencode. The runbook already anticipates this:

> Cursor IDE / Aider sessions — Codex has its own `com.hippo.codex-session`
> Rust poller; Cursor would be analogous. New source if the JSONL shape
> diverges from Anthropic's.

Cursor's transcript shape *is* Anthropic-style but diverges in two load-bearing
ways (no per-line timestamps, no in-file session metadata), so it is a new
source rather than a variant of an existing one. The implementation is ~90% a
faithful mirror of the Codex poller; this spec is mostly about the ~10% that is
genuinely Cursor-specific.

## 2. What Cursor writes on disk

The **Cursor Agent** (the CLI/headless agent, `cursor-agent`; state in
`~/.cursor/agent-cli-state.json`) writes transcripts here:

```
~/.cursor/projects/<project-slug>/agent-transcripts/<session-uuid>/<session-uuid>.jsonl   ← main
~/.cursor/projects/<project-slug>/agent-transcripts/<session-uuid>/subagents/<subagent-uuid>.jsonl  ← subagents
```

- `<project-slug>` mirrors the `~/.claude/projects/` convention:
  `Users-carpenter-projects-whistlepost` ⇒ `/Users/carpenter/projects/whistlepost`.
  Some slugs are **not** decodable paths: `empty-window`, numeric IDs
  (`1779680566655`), `var-folders-…-T-…` temp dirs.
- Each line is one message, **Anthropic Messages-style**:

```json
{"role":"user","message":{"content":[{"type":"text","text":"<user_query>\n…\n</user_query>"}]}}
{"role":"assistant","message":{"content":[
    {"type":"text","text":"I'll verify the stack…\n\n[REDACTED]"},
    {"type":"tool_use","name":"Shell","input":{"command":"docker compose ps","description":"…"}}
]}}
```

Empirically verified on a real 205-line transcript: the **only** top-level keys
per line are `role` and `message`. There is **no** `timestamp`, **no** `cwd`,
**no** `sessionId`, **no** `session_meta` line. `repo.json` (when present)
contains only `{"id": "<uuid>"}` — not a cwd. The only time signal available is
the **file mtime**.

> **Not in scope:** the Cursor *IDE* (GUI) chat/composer history, which lives in
> `~/Library/Application Support/Cursor/User/**/state.vscdb` (a VS Code SQLite
> blob store). It has no stable session boundaries and a brittle, undocumented
> schema. See §9.

## 3. Scope & decisions

Three decisions were settled during brainstorming:

1. **Source = Cursor Agent CLI transcripts only** (`agent-transcripts/**/*.jsonl`).
   Not the IDE `state.vscdb`.
2. **Mechanism = scheduled poller**, mirroring Codex (`com.hippo.codex-session`),
   plus a manual `hippo ingest cursor-session <path>` recovery command.
3. **Subagents are ingested as first-class sessions.** Each `.jsonl` (main and
   every `subagents/*.jsonl`) becomes its own row keyed by its own UUID, with
   `is_subagent = 1` and `parent_session_id = <enclosing session uuid>`. The
   `claude_sessions` table already has both columns (Codex hardcodes them
   `0`/`NULL`), so this needs **no schema change**.

Cursor rows reuse the `claude_sessions` table, the `claude_enrichment_queue`,
and the `agentic_cursor` resume table — distinguished, like Codex, purely by
their `source_file` path (`/.cursor/`). Capture-health key:
**`agentic-session-cursor`**.

## 4. Parsing & segmentation

New module `crates/hippo-daemon/src/cursor_session.rs`, a sibling of
`codex_session.rs`. It reuses Codex's `ToolCall`, `tool_summary`,
`build_summary_text`, `compute_content_hash`, `decide_enqueue`, and
`upsert_segment_tx` verbatim where possible; the Cursor-specific logic is
discovery, identity, the block parser, and segmentation.

### 4.1 Discovery

Walk each `[cursor].session_roots` dir (default `~/.cursor/projects`). A file is
an ingest candidate iff its path matches `**/agent-transcripts/**` and ends in
`.jsonl`. This naturally captures both the main transcript and the nested
`subagents/*.jsonl`. Skip files whose mtime is within `min_idle_secs` of now
(avoid partial reads), and skip files whose mtime `<=` the stored resume cursor
(unchanged since last parse).

### 4.2 Identity derivation (path-based)

Nothing inside the file identifies it; derive everything from the path:

- `session_id` = file stem (the UUID). Works for main (`<uuid>.jsonl`) and
  subagents (`subagents/<subuuid>.jsonl`).
- `is_subagent` = the file's parent dir is `subagents/`.
- `parent_session_id` = for a subagent, the UUID of the enclosing
  `agent-transcripts/<uuid>/` dir; else `NULL`.
- `cwd` / `project_dir` = decode the `<project-slug>` segment of the path
  (`-` → `/`, leading `/`). Non-path slugs (`empty-window`, numeric,
  `var-folders-*`) decode to an empty `cwd`; the row is still ingested.
  `project_dir` is the last path component of `cwd`, falling back to the slug.

We ingest **all** agent-transcripts regardless of slug. Filtering ephemeral
slugs would silently drop real delegated work.

### 4.3 Per-line block parser

For each JSONL line, read `role` and iterate `message.content[]`:

- **user** + block `type == "text"`: this is a user prompt. Strip a
  `<user_query>…</user_query>` wrapper if present (take the inner text), else
  use the raw text. Cap at 500 chars (matches Codex's `extract_user_text`).
  Skip blocks of `type == "tool_result"` (these are tool outputs returned to
  the model, not user intent).
- **assistant** + block `type == "text"`: append to `assistant_texts`, capped at
  300 chars (Codex parity). A trailing bare `[REDACTED]` marker is stripped.
- **assistant** + block `type == "tool_use"`: a tool call. `name` = block
  `name`; summary = `tool_summary(input_json)` (reuses the Codex helper, which
  prefers `cmd`/`command`/`filePath`/`path`/`uri`/`query`/`pattern`).

All three text channels pass through `RedactionEngine::builtin()` before
storage. Cursor emits its own `[REDACTED]` markers, but those are not
trustworthy for tool inputs (e.g. a `Shell` command embedding a token), so
hippo redacts independently (defense in depth; honors runbook step 4).

### 4.4 Segmentation — **timestamp-free** (the key adaptation)

Codex splits a session into task-boundary segments on *either* a >5-minute gap
between user prompts *or* a 12k-char cap — but **both checks are nested behind
`ts > 0`**. Cursor lines have no `ts`, so a naive copy of that code would never
split, collapsing every session into one unbounded segment and silently
dropping content past `build_summary_text`'s count caps.

Cursor segmentation therefore:

- **Drops the time-gap rule entirely** (no signal exists for it).
- **Splits only on `current_chars > MAX_SEGMENT_CHARS` (12_000), unconditionally**
  — not gated on any timestamp.
- Stamps `start_time` and `end_time` of every segment with the **file mtime**
  (epoch ms), passed in by the poller. This is coarse but is the only honest
  time signal Cursor provides, and it keeps Cursor rows time-orderable in RAG
  alongside Claude/Codex rows.

Net behavior: a short Cursor session → one segment; a very long one → a few
size-bounded segments. This is the single highest-risk piece of logic in the
feature and gets a dedicated unit test (§8).

### 4.5 Resume cursor

A per-file entry in the existing `agentic_cursor` table (generic
`(source_key, last_seen_updated_at, last_id, updated_at)` — no schema change):

- `source_key` = `cursor-agent-<inode>`. The `cursor-agent-` prefix
  disambiguates from the `agentic_cursor` table's own name; inode keying
  matches Codex and is robust to a project-dir rename (inode survives `mv`).
- `last_seen_updated_at` = file mtime (epoch ms) at last successful parse.
- `last_id` = the session UUID.

**First run:** no cursor rows exist, so every existing transcript is parsed once
(backfill of the user's Cursor Agent history). A resumed session re-grows its
file, bumping mtime, triggering a re-parse; `content_hash` + `decide_enqueue`
ensure only changed segments re-enqueue (guards the known "re-enqueue
multiplies nodes" failure mode).

### 4.6 Upsert & enqueue

`upsert_segment_tx` is reused unchanged from the Codex design: one
`INSERT … ON CONFLICT (session_id, segment_index) DO UPDATE` into
`claude_sessions`, then a content-hash-gated upsert into
`claude_enrichment_queue`. The only difference from Codex's call site is that
Cursor passes real `is_subagent` / `parent_session_id` values instead of
`0` / `NULL`.

## 5. Integration points

- **Config** — new `CursorConfig` in `hippo-core` (`enabled: bool`,
  `session_roots: Vec<PathBuf>`, `min_idle_secs: u64`, `poll_interval_secs: u64`),
  added to `HippoConfig`; `[cursor]` section in `config.default.toml`. Default
  `session_roots`: `~/.cursor/projects`. `enabled = true` by default;
  `enabled = false` makes `poll_tick` a no-op (kill switch).
- **CLI / daemon** — new `Commands::CursorPoll` → `cursor_session::poll_tick`,
  mirroring `CodexPoll`. Plus `hippo ingest cursor-session <path>` for one-shot
  manual recovery/backfill (the poll already scans idempotently, so this is a
  convenience, not the primary path).
- **launchd** — new `launchd/com.hippo.cursor-session.plist`: `ProgramArguments`
  runs `hippo cursor-poll`; `StartInterval = 60`; `WatchPaths` on
  `~/.cursor/projects`; `ThrottleInterval` to coalesce bursts. Wire install in
  `main.rs` / `install.rs` and `mise.toml`.
- **Schema v16** — bump `EXPECTED_VERSION` 15 → 16 (`storage.rs`) **and**
  `EXPECTED_SCHEMA_VERSION` 15 → 16 (`brain/src/hippo_brain/schema_version.py`);
  they must agree. The v16 migration is a single
  `INSERT OR IGNORE INTO source_health … ('agentic-session-cursor', NULL, …)`
  (gated on the table existing), then `PRAGMA user_version = 16`. Add the same
  seed row to `schema.sql`'s fresh-install seed block. Without the row the
  poller's `source_health` UPDATE is a silent no-op.
- **doctor** — add `'agentic-session-cursor'` to `check_source_staleness`'s
  `WHERE source IN (…)` list (`commands.rs`), a `SourceFreshnessProbe` in
  `source_freshness_probes()`, `cursor_sessions_exist` / `cursor_sessions_recent`
  signals, and a `SourceStalenessThresholds` arm (warn 300s, mirroring
  Codex/opencode).
- **watchdog** — new invariant **I-15** (I-13 is Codex, I-14 is the
  embedding-orphan reaper; I-15 is the next free id): proxy predicate —
  `agentic-session-cursor.consecutive_failures > 3` ⇒ the poller is actively
  broken. Inherently activity-gated (only fires after the poller ran and
  failed), satisfying AP-3. Mirrors `check_i13_codex_coverage_proxy`. Document
  in `architecture.md`.
- **probe** — **assertion-only** (mirror claude-session, not inject-and-poll):
  for every agent-transcript JSONL whose age (`now - mtime`) falls in the
  eligibility window `[CURSOR_PROBE_SETTLE_MS, CURSOR_PROBE_WINDOW_MS]`
  (90 s settle floor … 10 min outer edge), assert a matching `claude_sessions`
  row exists. The settle floor is a **fixed 90 s constant decoupled from
  `cursor.min_idle_secs`** on purpose: deriving it from config (the old
  `2 * min_idle` formula) let a large `min_idle_secs` push the floor past the
  window, collapsing the eligibility window to empty so the probe silently
  trivial-passed and stopped covering the source. Transcripts that parse to
  **zero segments are skipped** (an empty transcript legitimately produces no
  row), and if no settled transcript is in-window the probe trivially passes.
  Implemented as the `agentic-session-cursor` arm in `probe.rs::run` +
  `probe_cursor_session`. **AP-6 is satisfied by construction**: assertion-only
  means no synthetic rows are ever written, so none can leak into user-facing
  queries.

## 6. Brain enrichment

**No new enrichment path.** Cursor segments enter `claude_enrichment_queue` and
are enriched by the existing `claude_sessions` loop exactly like Claude and
Codex segments — the loop reads the structured `tool_calls_json` /
`user_prompts_json` / `summary_text` columns, which are source-agnostic.

The **only** brain edit is the source-labeler in `server.py` (~lines 124–130),
which today maps a `source_file` containing `/.codex/` (or
`/CodingAssistant/codex/`) to the Codex harness and everything else to Claude.
Add a branch: `source_file` containing `/.cursor/` ⇒ harness `"cursor"`. Without
it, Cursor knowledge nodes would be mislabeled as Claude.

Enrichment eligibility needs **no** change: `is_enrichment_eligible` governs the
`events` table, not `claude_sessions`; Cursor rows inherit the shared
claude-session trivial-skip (`< 3 messages && no tool calls`), exactly as Codex
does.

## 7. Error handling & crash safety

- **Per-file isolation:** a parse/IO error on one transcript calls `record_error`
  (bumps `consecutive_failures` + `last_error_msg`, `error!` log) and continues
  to the next file. One bad file never halts the poll.
- **No silent swallowing in write paths (AP-11):** every parse/upsert/cursor
  skip or error logs — per-file `metadata`/`mtime` failures `warn!` and name the
  file, parse/IO failures `record_error` + `error!` — and no
  `.ok().unwrap_or_default()` swallows a write result. The one deliberate
  exception is the top-level directory traversal
  (`WalkDir::…filter_map(|e| e.ok())`, mirroring Codex), which skips an
  unreadable directory entry so a single bad dir can't abort the whole poll;
  the next tick re-walks, so any transcript that matters resurfaces.
- **Ordering is the crash-safety design:** within a poll, per file —
  (1) parse + upsert all segments in one transaction and commit,
  (2) bump `source_health` only if ≥1 segment landed,
  (3) advance the resume cursor **last**. A crash before step 3 leaves the
  cursor unadvanced, so the next poll re-parses the file; every write is
  idempotent, so re-parsing is free and self-healing.
- **Empty parse (0 segments):** advance the cursor (don't re-parse forever) but
  do **not** bump health — nothing landed. Mirrors Codex.

## 8. Testing

Rust unit tests (`cursor_session.rs`) with hand-authored fixtures — never commit
a real transcript (privacy + churn):

- **Parser:** committed-fixture parse; user-prompt extraction with and without
  the `<user_query>` wrapper; `tool_result` blocks skipped; `tool_use`
  summarized; assistant `[REDACTED]` handling; redaction applied.
- **Segmentation (the adaptation):** a transcript whose accumulated content
  exceeds `MAX_SEGMENT_CHARS` splits into >1 segment **despite having no
  timestamps** — this is the test that would fail on a naive Codex copy.
- **Identity:** subagent file → `is_subagent = 1` + correct `parent_session_id`;
  slug decode for a real path slug and an empty result for `empty-window`.

Integration tests (`tests/cursor_session.rs`), temp DB + temp roots:

- `poll_tick` first-run backfill → rows in `claude_sessions`, `source_health`
  bumped, cursor advanced.
- Idempotent re-poll of an unchanged file → no new rows, **no re-enqueue**
  (content-hash dedup gate).
- In-flight skip (`min_idle_secs`); `source_health` error bump on a malformed
  file.
- Subagent file → parent linkage in the row.

Cross-cutting:

- **Migration** (`storage.rs` test): v16 seeds `agentic-session-cursor` (mirror
  the existing v15 test).
- **source audit** (`tests/source_audit.rs::cursor_events`): a real drive
  through the poller lands a row and updates health.
- **Brain** (`test_server_extended.py`): a `/.cursor/` `source_file` labels
  harness `"cursor"`.
- **test-matrix.md** rows F-N…F-N+3 (event landed; health updated; probe
  assertion; probe rows absent from user queries).

## 9. Out of scope

- **Cursor IDE (GUI) chat/composer** from `state.vscdb`. Different storage,
  brittle schema, no stable session boundaries. A possible future source, not
  this one.
- A synthetic inject-and-poll probe (Cursor's probe is assertion-only).
- Model-name extraction beyond what is trivially present in the transcript.
- Any `claude_sessions` / `agentic_sessions` schema change beyond the v16
  `source_health` seed row.

## 10. Follow-up: relation to the `agentic_sessions` unification

Like Codex (#154), this Cursor source is an **interim** step that lands rows in
`claude_sessions`, distinguished by `source_file`. The desired end state is a
single `agentic_sessions` table for all harnesses (epic #113, unification
#155–#160). This design is built to be absorbed cleanly by that migration via
the same `source_file` → harness derivation Codex uses: a `/.cursor/` path ⇒
harness `cursor`. No `harness`/`source` column is added to `claude_sessions`
(it would add schema to a table the migration will retire).
