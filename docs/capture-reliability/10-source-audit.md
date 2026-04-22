# 10 — Source Audit: every raw data source

Companion to `09-test-matrix.md`. The test matrix enumerates failure-mode
invariants; this document enumerates **sources** — every place hippo collects
raw data from — and pins each one to a specific entry-point and table, plus
an integration test that proves rows land where they should.

The motivating incident: on 2026-04-22 we discovered that BOTH the batch and
tailer Claude-session ingesters had been silently **not** writing the
`claude_sessions` rows they were supposed to (only the tool-call events
landed). Every source below now has an explicit end-to-end test so a
regression surfaces on CI instead of in a 272-session backfill audit.

## Status key

- **healthy** — production write path exercised by a test and rows land in
  the expected table(s).
- **broken — fix needed** — test reveals a missing row; source-code fix
  required. Flagged in **Gaps** below.
- **intermittent** — test passes but the source is known to skip writes in
  certain states (e.g. brain offline, socket timeout). See note.

## Source matrix

| # | Source | Entry point | Expected tables | Test | Status | Notes |
|---|---|---|---|---|---|---|
| 1 | Shell commands (zsh hook) | `crates/hippo-daemon/src/commands.rs:259` (`handle_send_event_shell`) → `send_event_fire_and_forget` → `daemon.rs:210` (`flush_events`) → `storage.rs:462` (`insert_event_at`) | `events` (`source_kind='shell'`) | `tests/source_audit.rs::shell_events` | healthy | Also exercised end-to-end via `shell_hook.rs` (zsh integration). |
| 2 | Claude-tool events (MCP tool calls from ingested JSONLs) | `claude_session.rs:build_envelope` (L181) → same flush path; `storage.rs:494` derives `source_kind='claude-tool'` when `tool_name.is_some()` | `events` (`source_kind='claude-tool'`, `tool_name` set) | `tests/source_audit.rs::claude_tool_events` | healthy | Driven by `ingest_batch` today — no standalone producer. |
| 3 | Claude session segments (batch import) | `claude_session.rs:952` (`ingest_batch`) → `write_session_segments` (L917) → `insert_segments` (L845) direct SQLite | `claude_sessions`, `claude_enrichment_queue` | `tests/source_audit.rs::claude_session_batch` | healthy | Fixed in #59 — previously only events flowed. |
| 4 | Claude session segments (tailer) | `claude_session.rs:1062` (`ingest_tail`) → same `write_session_segments` on every non-empty tick and on final drain | `claude_sessions`, `claude_enrichment_queue` | `tests/source_audit.rs::claude_session_tailer` | healthy | Spawned by the Claude Code `SessionStart` hook. The test drives `ingest_tail` with a growing JSONL and a `HIPPO_WATCH_PID` pointing at a short-lived child so the tailer exits cleanly. |
| 5 | Browser visits (Firefox extension) | `extension/firefox` → NM stdio → `native_messaging.rs:142` (`run`) → `send_event_fire_and_forget` → `flush_events` → `storage.rs:548` (`insert_browser_event`) | `browser_events`, `browser_enrichment_queue` | `tests/source_audit.rs::browser_events` | healthy | The test drives `send_event_fire_and_forget` with a `BrowserEvent` envelope directly — the NM stdio path is covered by `native_messaging.rs` unit tests. |
| 6 | GitHub workflow runs (Actions poller) | `gh_poll.rs:24` (`run_once`) → `storage.rs:2300` (`workflow_store::upsert_run`/`upsert_job`/`insert_annotation`/`insert_log_excerpt`/`enqueue_enrichment`) | `workflow_runs`, `workflow_jobs`, `workflow_annotations`, `workflow_log_excerpts`, `workflow_enrichment_queue` | `tests/source_audit.rs::workflow_runs` | healthy | Uses `wiremock` to fake the GitHub REST API. |
| 7 | Claude subagent sessions (`agent-*.jsonl`) | same `ingest_batch`/`ingest_tail`; `SessionFile::from_path` (L396) detects `<project>/<parent-uuid>/subagents/<id>.jsonl` and sets `is_subagent=true` | `claude_sessions` with `is_subagent=1`, `parent_session_id=<parent uuid>` | `tests/source_audit.rs::claude_subagent` | healthy | Subagent segments are enqueued for enrichment like main segments. |
| 8 | Xcode ClaudeAgentConfig sessions (`~/Library/Developer/Xcode/CodingAssistant/ClaudeAgentConfig/projects/<p>/<uuid>.jsonl`) | LaunchAgent `com.hippo.xcode-claude-ingest.plist` → `scripts/hippo-ingest-claude.py --claude-dir <xcode path>` → Python `insert_segment` (same `claude_sessions` schema) | `claude_sessions`, `claude_enrichment_queue` | `tests/source_audit.rs::xcode_codingassistant` | healthy | Rust `ingest_batch` also handles this format — the JSONL schema matches `~/.claude/projects/`. Extra `queue-operation` rows are silently skipped by `process_line`. The test fixture mimics the exact Xcode JSONL shape (including a `queue-operation` line) and asserts both `claude_sessions` writes and tool-event writes land. |
| 9 | Codex (Xcode) rollouts (`~/Library/Developer/Xcode/CodingAssistant/codex/sessions/YYYY/MM/DD/rollout-*.jsonl`) | LaunchAgent `com.hippo.xcode-codex-ingest.plist` → `scripts/hippo-ingest-codex.py` → Python `brain/src/hippo_brain/codex_sessions.py::extract_codex_segments` → `insert_segment` | `claude_sessions` (shared table; `source` field on `SessionSegment` is `"codex"`), `claude_enrichment_queue` | not tested here — Python-only path | intermittent — see notes | The Rust daemon does NOT know about Codex — this source flows through Python only, gated by a LaunchAgent that polls every 5 min. No regression test in the Rust suite because the Rust ingest path does not parse Codex's distinct JSONL shape (`session_meta`, `response_item/function_call`). Python-side tests live in `brain/tests/test_codex_sessions.py` (if present — see **Gaps**). |

## Doctor extension

`tests/source_audit.rs::doctor_source_freshness_check` calls
`commands::print_source_freshness` (new helper in `commands.rs`) and asserts
it emits one status line per source. The helper is also wired into
`hippo doctor` output between the existing freshness-adjacent checks and the
Firefox-extension check.

Design (fits the pattern laid out in `03-doctor-upgrades.md` but without
requiring the full `source_health` table, which is still a P0.1 item in
`07-roadmap.md`):

```sql
-- Queries per source (run against hippo.db)
SELECT MAX(timestamp) FROM events WHERE source_kind='shell';
SELECT MAX(timestamp) FROM events WHERE source_kind='claude-tool';
SELECT MAX(start_time) FROM claude_sessions WHERE is_subagent=0;
SELECT MAX(start_time) FROM claude_sessions WHERE is_subagent=1;
SELECT MAX(timestamp) FROM browser_events;
SELECT MAX(started_at) FROM workflow_runs;
```

Each source gets a **staleness threshold** picked from the context note
(shell: 24h during a working day, forever-ok overnight; browser: 24h;
claude-session: 2h during active Claude use, 24h else; workflow: 48h).
Thresholds are deliberately lenient because the doctor runs on
user-invoked command, not a continuous watchdog — the point is to surface
"zero rows ever" or "rows haven't moved in a week" faults, not jitter.

The helper emits one of:

- `[OK] <source>: N rows, freshest <human-duration> ago`
- `[WW] <source>: freshest <human-duration> ago (> soft threshold)`
- `[!!] <source>: freshest <human-duration> ago (> hard threshold)`
- `[--] <source>: zero rows ever`

Where "zero rows ever" on shell/claude-tool is a **hard** signal that the
capture chain is broken — the motivating incident for this doc.

## Gaps

As of 2026-04-22 the following production writers are NOT covered by the
tests in this PR, so a regression WILL ship silently:

1. **Codex (Xcode) rollouts → `claude_sessions`** — Python-only path,
   invoked by a LaunchAgent. No Rust test exists because the Rust
   `process_line` does not understand Codex's envelope shape (distinct from
   the Anthropic Claude JSONL). The Python module `codex_sessions.py` has
   unit tests for parsing but there is no end-to-end test that covers the
   "LaunchAgent fires → segments land in `claude_sessions`" pipeline.
   **Action:** main agent decides whether to add `brain/tests/test_codex_sessions_insert.py` that drives `insert_segment` on a fixture Codex rollout and asserts the row lands with `source='codex'` metadata.

2. **Tailer watcher (FS-native)** — `06-claude-session-watcher.md` P2.1 is
   not yet implemented. Today the tailer is spawned per-session by the
   `SessionStart` Claude hook; if the hook fails to fire (e.g. Claude
   launched outside the hook chain, or `hippo-ingest-claude` LaunchAgent is
   the sole ingester), live sessions won't tail at all — the Python
   LaunchAgent picks them up 5 min later, which is acceptable but not
   real-time. Not a "broken writer" — just an observation for the watcher
   roadmap.

3. **`source_health` table** — referenced throughout
   `capture_invariants.rs` as a blocker for 5 P0.1 tests, and the recent
   commit `9caad39` ("feat: implement source health tracking with new
   source_health table") is **docs-only** despite its title. The
   per-source doctor check here is a bridge until the real table lands.
