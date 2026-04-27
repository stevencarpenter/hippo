# Watcher Data-Loss Fix: Tracking Plan

> **Status (2026-04-27, shipped):** Phase 1 (Bug A fix) SHIPPED (review feedback addressed in same PR; see commits f-z for fix-up). T-A.1–T-A.7 merged on `main`. T-A.8 verification report forthcoming (in flight). Bug B and follow-ups deferred to dedicated phases.

**TL;DR:** The Claude-session FS-watcher is silently lossy in two distinct ways, both discovered on 2026-04-26 while ostensibly closing out the I-3 invariant:

- **Bug A — Segment truncation (data loss).** Watcher reparses the full JSONL on every FSEvents notification, but `insert_segments` uses `INSERT OR IGNORE` on `(session_id, segment_index)`. The first partial extraction wins forever; subsequent reparses with more content are silently rejected. Result: every Claude session segment since T-7 (2026-04-25 evening) is a tiny prefix of the actual content. Empty `tool_calls_json` in 59/63 recent sessions. Root cause of the user's "fans are quiet — enrichment seems idle" symptom. **Fixed in Phase 1.**
- **Bug B — Missing `events.source_kind='claude-tool'` rows.** No path under the current FS-watcher architecture writes per-tool events into the `events` table. The events that exist are entirely synthetic probes from `hippo probe` every 5 min. Real Claude tool calls have not appeared in the events table since 2026-04-25 23:45. **Deferred to Phase 2.**

Both bugs were reproduced live by resuming a 5-day-old session (`296e9905`) and watching segment 20 land in the DB with 4 messages / 0 tools while the file gained 117 lines / 13 tool_uses.

This document tracks the multi-phase fix. **Update the status block at the top and check off DoD items in the same PR that ships each phase.**

---

## Discovery context

- I-3 implementation kicked off on 2026-04-26 to clear the test-matrix TBD.
- While tracing the existing claude-tool flow, discovered that `events.last_event_ts` for `claude-tool` had not advanced (other than probes) in 24+ hours.
- Root cause: under T-7/T-8 (FS-watcher sole ingester), the watcher only writes `claude_sessions` rows — never `events`. And the `claude_sessions` writes are silently truncated by `INSERT OR IGNORE` on every reparse.
- The user observed the symptom independently ("fans are quiet, enrichment is normally working hard") in a parallel Claude session.
- **I-3 implementation paused** because the spec assumes a working `events.claude-tool` write path; with Bug B in play, implementing I-3 literally would just produce a perma-firing alarm. I-3 will be revisited after Bug B is fixed and the architecture is settled.
- Schema migration v11→v12 (added `last_tool_use_seen_ts` for I-3) was reverted to keep the tree clean.

## Live reproduction (2026-04-27)

Resumed session `296e9905-6ce8-415a-a016-0188693f888a` from the Claude Code resume picker; sent two prompts including hippo MCP tool calls.

| | Baseline (before resume) | After (file growth done) | Δ |
|---|---|---|---|
| inode | 17137774 | 17137774 | same (append-on-resume confirmed) |
| size | 8 855 614 B | 9 150 760 B | +295 KB |
| lines | 2 872 | 2 989 | +117 |
| tool_use blocks in JSONL | 456 | 469 | **+13** |
| segments in DB | 20 | 21 | +1 (new segment_index=20) |
| **segment 20 message_count** | — | **4** | should be ≥117 |
| **segment 20 n_tools** | — | **0** | should be 13 |
| `events.claude-tool` rows (non-probe) since 2026-04-25 23:45 | 0 | **0** | unchanged |

The new segment 20 was written once (at the very first FSEvents notification after Claude Code re-opened the file) and frozen at that 4-message snapshot for the remaining 35 minutes of activity. **Bug A and Bug B both reproduced.**

---

## Confirmed design facts (informs all phases)

1. **Resume model:** Claude Code appends to the existing JSONL on session resume. Same inode, same `sessionId`. New activity has new (much later) timestamps. `extract_segments` naturally splits on user-message gap > 5 min, so resumed-day content lands in a new `segment_index`.
2. **Segment immutability:** A `(session_id, segment_index)` row's content can grow over time as the file grows (until the segment splits), so the row is **not** immutable once written. The current `INSERT OR IGNORE` semantics are wrong for this access pattern.
3. **Enrichment value model:** Per the user, enrichment serves *future* sessions, not the current one. Sub-5-minute freshness is not a goal; **completeness** by the time the next session starts is the goal. This justifies wider debounce windows.
4. **Probe rows:** `hippo probe` writes one `events.claude-tool` row per 5 min as a synthetic canary. These rows are tagged with `probe_tag` and filtered from user-facing queries. Watchdog signal comes from `source_health.probe_*` columns, **not** the events rows themselves — so probe rows could be TTL-pruned without breaking the watchdog. Tracked as a Phase-3 follow-up.

---

## Phase 1 — Bug A fix (segment truncation)

**Goal:** Watcher reparses become content-correct and idempotent. Segments enrich when settled, not when first written. Backfill recovers lost data from 2026-04-25 onward.

**Status: SHIPPED (2026-04-27). T-A.1–T-A.7 merged on `main`.**

### Design

- **Schema v11→v12** (additive, idempotent ALTERs):
  - `claude_sessions.content_hash TEXT` — SHA256 of `(tool_calls_json + "|" + user_prompts_json + "|" + assistant_texts.join("\n"))` — full text, not counts; catches edits even when structure is unchanged.
  - `claude_sessions.last_enriched_content_hash TEXT` — written by the brain enrichment worker when it completes a segment; NULL until first enrichment.
  - Brain `EXPECTED_SCHEMA_VERSION` bumped to 12; `ACCEPTED_READ_VERSIONS` keeps 11 for rollback.
- **`insert_segments` rewrite:** `INSERT … ON CONFLICT(session_id, segment_index) DO UPDATE SET (content fields)` — keeps the DB in sync with the latest reparse. The `RETURNING` clause yields whether the row pre-existed.
- **Enqueue policy:**
  - **INSERT path** (no conflict): always enqueue. Catches orphan segments (`message_count=1, never grows`).
  - **UPDATE path:** enqueue iff `current_hash != last_enriched_hash` AND `(now - claude_enrichment_queue.updated_at) > 300s` (5-min debounce) AND existing queue row is not `processing`.
  - Skip enqueue entirely for empty segments (no `tool_calls`, no `assistant_texts`) — brain's `_skip_ineligible_claude_segments` would skip them anyway, no point queuing.
- **Brain worker:** when an enrichment cycle completes successfully, `UPDATE claude_sessions SET last_enriched_content_hash = ? WHERE id = ?` with the hash that was just enriched. This is the safety mechanism that closes the race window where a reparse arrives mid-enrichment.
- **Watcher heartbeat sweep (every 30s):** `SELECT segments WHERE content_hash != COALESCE(last_enriched_content_hash, '') AND mtime(file) < now - 30min AND not in queue AND has content`. Enqueue any matches. Backstop for the "user walked away" case where the debounce window elapsed but no further file growth occurred. Note: the sweep does NOT recover historical data — the watcher's per-file offset short-circuits before re-extracting segments from before the resume point. Backfill CLI is the only recovery mechanism for pre-existing truncated segments.
- **Watcher startup-warn (option B for I-4):** on `run_watcher` startup, a one-shot SQL check counts segments with `content_hash IS NULL AND end_time > 2026-04-25 cutoff`. If > 0, a `warn!()` log line surfaces the count and the exact `hippo ingest claude-session-backfill` command needed to recover. This catches users who upgrade the daemon but forget the manual backfill step. Pre-migration safe (graceful no-op if columns absent).
- **Backfill CLI:** `hippo ingest claude-session-backfill <glob> [--since YYYY-MM-DD] [--dry-run]`. For matching files: reset `claude_session_offsets.size_at_last_read = 0` and trigger a single reparse. Idempotent under the new content_hash dedup so already-correct segments don't churn.

### Tasks

- [x] **T-A.1 — Schema v11→v12.** `crates/hippo-core/src/schema.sql` adds the two columns; `crates/hippo-core/src/storage.rs` adds the v11→v12 migration block (matching v10→v11 pattern); bump `EXPECTED_VERSION` to 12. Bump `brain/src/hippo_brain/schema_version.py` to 12, keep 11 in `ACCEPTED_READ_VERSIONS`.
  - DoD: Migration test added (mirror `test_migrate_v10_to_v11_adds_auto_resolve_columns`); partial-success recovery test added; `cargo test -p hippo-core` green.
- [x] **T-A.2 — Hash computation helper.** Add `compute_segment_content_hash(seg: &SessionSegment) -> String` in `claude_session.rs`. Tests cover identical-content equality and tool-content sensitivity.
  - DoD: Unit tests cover empty segment, segment with tools only, segment with prompts only, segment whose redaction changed (different hash).
- [x] **T-A.3 — Upsert in `insert_segments`.** Replace `INSERT OR IGNORE` with `INSERT … ON CONFLICT DO UPDATE`. Compute hash, write to `content_hash`. Return whether the row was newly inserted vs updated.
  - DoD: Unit test feeds the same file twice with growing content; asserts final row matches the latest extract; idempotent against same input.
- [x] **T-A.4 — Enqueue gate logic.** New helper `decide_enqueue(was_insert, current_hash, last_enriched_hash, queue_state, now_ms) -> bool` with the rules above. Wired into `insert_segments` post-upsert.
  - DoD: Unit tests cover: orphan-segment-enqueues, hash-unchanged-skips, debounce-window-not-elapsed-skips, processing-state-skips, hash-changed-and-debounced-enqueues.
- [x] **T-A.5 — Brain writes `last_enriched_content_hash`.** In `brain/src/hippo_brain/claude_sessions.py` (or wherever the segment-enrichment write completes), update the row with the hash that was just enriched. Hash is recomputed from the segment content read at claim time so the brain doesn't depend on the watcher.
  - DoD: Python unit test verifies hash propagates; integration test verifies a re-enqueue after change re-runs enrichment.
- [x] **T-A.6 — Watcher heartbeat settling sweep.** In `watch_claude_sessions.rs`, every heartbeat tick, run a SELECT for unsettled segments (hash mismatch + file idle > 30 min + no queue row + has content) and enqueue. Bounded by a per-tick batch cap so a recovery storm doesn't fire 1000 enrichments at once.
  - DoD: Integration test simulates "user walks away" — segment created, file goes idle, sweep enqueues after threshold.
- [x] **T-A.7 — Backfill CLI.** New subcommand `hippo ingest claude-session-backfill <glob> [--since DATE] [--dry-run]`. Resets watcher offsets for matching files, triggers reparse via the existing watcher code path. Logs a summary table (files processed, segments updated, segments unchanged).
  - DoD: CLI help text documented; integration test runs backfill against a fixture dir; `--dry-run` flag for safety.
- [ ] **T-A.8 — Verification.** Full lint/test suite. Run backfill on the user's machine against `~/.claude/projects/**/*.jsonl --since 2026-04-25`. Validate via SQL: distinct session segments with non-empty `tool_calls_json` should jump from current 4/63 back toward historical 50%+ ratio.
  - DoD: `cargo test -p hippo-core -p hippo-daemon` green; `cargo clippy --all-targets -- -D warnings` clean; `cargo fmt --check` clean; `uv run --project brain pytest brain/tests` green; `uv run --project brain ruff check brain/` clean. Backfill recovery sql sanity check passes.
  - **Note: verification report forthcoming (in flight as of 2026-04-27).**
- [x] **T-A.9 — Docs.** Update `docs/capture-reliability/04-watchdog.md` (note the upsert + sweep), `08-anti-patterns.md` (add AP-12: "INSERT OR IGNORE on a derived bucket key whose content is mutable" with this incident as the case study), `09-test-matrix.md` (link the new tests).
  - DoD: Doc changes shipped in same PR as the code; no `TBD`/`FIXME` left in touched sections.

**Phase 1 gate (M5):** All T-A.* checked, backfill recovered ≥80% of historical tool-extraction ratio, no new clippy warnings, no schema regressions, brain handshake clean across daemon and brain.

---

## Phase 2 — Bug B fix (per-tool events emission)

**Goal:** Tool calls observed by the watcher land as `events.source_kind='claude-tool'` rows so downstream queries (RAG, MCP `search_events`, `hippo ask`) see them. No double-enrichment with Phase 1 (segments are still the canonical enrichment unit).

**Status: blocked on Phase 1**

### Design

- **In-loop emission:** During `extract_segments`, every parsed `tool_use` block produces an `EventEnvelope` shape with `tool_name = Some(...)`, written directly to the `events` table via `storage::insert_event_at` (or a new direct path that doesn't enqueue for `enrichment_queue`).
- **Stable identity:** `envelope_id = tool_use_id` (the `toolu_*` value Claude assigns is stable per call). `INSERT OR IGNORE` on `envelope_id` is then **correct** — re-parses don't dupe; the original (and only correct) row is preserved. This is structurally simpler than Bug A because the unit of identity is per-tool, not per-segment.
- **No enrichment queue:** Tool events are queryable but not individually enriched. Segment-level enrichment (Phase 1) covers the semantic chunk; per-tool enrichment would duplicate effort and produce noisier knowledge nodes.
- **Backfill:** Existing JSONLs from 2026-04-25 onward should be re-parsed and have their tool_use blocks emitted as events. Same backfill CLI as Phase 1 — extend to also emit events.

### Tasks

- [ ] **T-B.1 — Emit events from `extract_segments`.** During the assistant-content loop, for every `tool_use` block, build an `EventEnvelope` and call `storage::insert_event_at` directly (bypassing the daemon socket; the watcher already shares the DB). Set `probe_tag = NULL`, `envelope_id = tool_use_id`, derive timestamp from the JSONL entry timestamp.
- [ ] **T-B.2 — Skip enrichment_queue for these events.** Either pass a flag to `insert_event_at` to suppress the queue insert, or write a new `insert_event_for_observability_only` helper.
- [ ] **T-B.3 — Backfill extends to events.** Update the Phase 1 backfill CLI to also emit events for tool_use blocks in matching files.
- [ ] **T-B.4 — Tests.** `crates/hippo-daemon/tests/source_audit/claude_tool_events.rs` updated to verify the watcher path now produces events (currently only the batch path is covered). Idempotence test: re-running on the same file does not duplicate events rows.
- [ ] **T-B.5 — Verification.** Full lint/test suite. Manual: run a Claude session, observe `events.claude-tool` rows landing per tool call.
- [ ] **T-B.6 — Docs.** Update `06-claude-session-watcher.md` (archived; restore or supplement as needed), `09-test-matrix.md`, `10-source-audit.md`. Note in `00-overview.md` that the watcher now writes to both `claude_sessions` and `events`.

**Phase 2 gate (M6):** All T-B.* checked, manual smoke test confirms tool events land within seconds of the JSONL append, idempotence verified.

---

## Phase 3 — Optimizations & follow-ups (opportunistic)

These are not blocking. File issues; pick up when convenient.

- [ ] **T-C.1 — I-3 watchdog implementation (resumed).** With Bug B fixed, `events.claude-tool` is a real signal again. Implement I-3 per the original spec in `02-invariants.md`: `claude-session-watcher.last_tool_use_seen_ts` vs `claude-tool.last_event_ts`. Schema change (the column we reverted in this investigation) becomes useful again. Closes the test-matrix TBD on row I-3.
- [ ] **T-C.2 — Probe TTL pruning.** Delete `events` rows where `probe_tag IS NOT NULL AND timestamp < now - 24h`. Watchdog freshness signal lives in `source_health.probe_*` independently, so probe events don't need permanent retention. Saves ~1.6M rows/year.
- [ ] **T-C.3 — Duplicate trailing segment_index investigation.** [#99](https://github.com/stevencarpenter/hippo/issues/99). 44 affected sessions. Independent of Bug A.
- [ ] **T-C.4 — Incremental parse via `byte_offset`.** Watcher currently does full-file reparse every event. With segment state cached in memory per file, only new lines need parsing. Significant perf win on large sessions.
- [ ] **T-C.5 — Differential enrichment.** When a previously-enriched segment grows, send only the delta + previous knowledge_node to the LLM. Cheaper than full re-enrichment.
- [ ] **T-C.6 — Zombie segment cleanup.** Heartbeat sweep deletes `claude_sessions` rows that no longer correspond to any segment in their source file (e.g., after Claude Code compaction).

---

## Decision log

| Date | Decision | Rationale |
|---|---|---|
| 2026-04-26 | Pause I-3 implementation | Spec assumes working `events.claude-tool` path; Bug B makes that path dead. Re-enable in Phase 3 after Bug B ships. |
| 2026-04-26 | Revert v11→v12 migration (`last_tool_use_seen_ts`) | Orphan column with no consumer; cleaner to start Phase 1 with no in-tree changes. |
| 2026-04-26 | Use content_hash dedup, not message_count | Counters miss content-replacement edits; SHA256 is robust and microsecond-cheap. |
| 2026-04-27 | 5-min debounce, 30-min settling sweep | Per user: enrichment serves the *next* session, not real-time. Wider windows reduce LM Studio churn without hurting UX. |
| 2026-04-27 | Bug B uses `tool_use_id` as `envelope_id` | Stable per-tool identifier; `INSERT OR IGNORE` on it is correct (unlike on `(session_id, segment_index)` which is mutable). |
| 2026-04-27 | Phase 2 separate from Phase 1 | Phase 1 is data-loss recovery (urgent); Phase 2 adds queryable surface (less urgent). Independent risk profiles. |
| 2026-04-27 | Pre-migration guard in sweep (`is_missing_claude_session_columns_error` + `OnceLock` warn-throttle) | Mirrors the existing `is_missing_source_health_table_error` pattern in the watcher; prevents noisy errors on pre-v12 installs running against a pre-migration DB while the first migration is in flight. |
| 2026-04-27 | Backfill CLI as `hippo ingest claude-session-backfill` (flat subcommand in `IngestSource` enum) | Matches the existing `hippo ingest claude-session <path>` surface rather than introducing a new top-level `Backfill` command. Keeps the CLI surface coherent without a separate entry-point. |
| 2026-04-27 | Adopted I-4 option B (watcher startup-warn for legacy NULL content_hash) over option A (auto-run backfill via post-install hook) | Option A was rejected because expensive backfills should not fire silently on every chezmoi/mise install on every machine. Option B trades a one-time loud warning for opt-in recovery — user sees the count, decides when to run the backfill. |

---

## Operational notes

### Install ordering

**Daemon ↔ brain ordering during deploy.** Brain at v11 cannot serve a daemon at v12 (the strict schema handshake bails). `mise run install` upgrades both atomically in the right order, so the standard install path is fine. Manual rollback paths (e.g., reverting just the daemon binary while leaving brain at v12) are unsupported — brain at v12 cannot serve a v11 daemon either. If you need to roll back, roll both back together; brain's `ACCEPTED_READ_VERSIONS` keeps v11 for read-only fallback during the rollback window.

---

## Recovery sanity-check queries

After Phase 1 backfill, run these to validate recovery:

```sql
-- Tool extraction rate for sessions since the bug landed (was 4/63 = 6%, expect ≥40%)
SELECT
  COUNT(DISTINCT session_id || '|' || segment_index) AS distinct_segs,
  COUNT(DISTINCT CASE WHEN json_array_length(tool_calls_json) > 0 THEN session_id || '|' || segment_index END) AS with_tools,
  ROUND(100.0 * COUNT(DISTINCT CASE WHEN json_array_length(tool_calls_json) > 0 THEN session_id || '|' || segment_index END)
        / COUNT(DISTINCT session_id || '|' || segment_index), 1) AS pct_with_tools
FROM claude_sessions
WHERE end_time > strftime('%s', '2026-04-25')*1000;

-- 296e9905 segment 20 should grow from 4 messages to ~117
SELECT segment_index, message_count, json_array_length(tool_calls_json) AS n_tools
FROM claude_sessions
WHERE session_id = '296e9905-6ce8-415a-a016-0188693f888a' AND segment_index = 20;
```

After Phase 2:

```sql
-- Real claude-tool events should resume landing (was 0 since 2026-04-25 23:45)
SELECT COUNT(*) AS new_real_tool_events,
       MAX(datetime(timestamp/1000,'unixepoch','localtime')) AS latest
FROM events
WHERE source_kind = 'claude-tool' AND probe_tag IS NULL
  AND timestamp > strftime('%s', '2026-04-26')*1000;
```
