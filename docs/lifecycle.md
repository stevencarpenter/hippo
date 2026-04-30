# Lifecycle of an Event

End-to-end traces of how events become knowledge in hippo, citing the real symbols that handle each step. Companion to [`capture/architecture.md`](capture/architecture.md) (which describes the system in cross-section) and [`capture/operator-runbook.md`](capture/operator-runbook.md) (first-aid recipes).

For a power user diagnosing a missing event, this doc + [the SQL recipes at the bottom](#diagnosing-a-missing-event) should be enough. Three lifecycles are covered: **shell command**, **Claude session segment**, and **browser visit**. Workflow runs and Codex/Xcode-side ingest follow analogous patterns and are noted where they diverge.

## Shell command

```
zsh preexec
       |
       v
hippo.zsh::preexec captures cmd_start_ms, cwd, git_*
       |
       v  (foreground, microseconds)
zsh runs the command
       |
       v
hippo.zsh::precmd captures exit_code, duration_ms, stdout (truncated), stderr
       |
       v  (DISOWNED, fire-and-forget)
hippo send-event-shell  -- a child process
       |
       v  (length-prefixed JSON over Unix socket)
crates/hippo-daemon/src/commands.rs::send_event_fire_and_forget
       |  -- writes into in-memory buffer, returns immediately
       |  -- contract: success = "the frame hit the socket", NOT "SQLite was touched"
       v
crates/hippo-daemon/src/daemon.rs::flush_events
       |  -- background tokio timer, every flush_interval_ms (default 100 ms)
       |  -- batches buffered frames, opens single SQLite transaction
       v
crates/hippo-core/src/storage.rs::insert_event_at
       |  -- writes events row + source_health row in same transaction
       |  -- redaction runs here: hippo-core/src/redaction.rs
       |  -- on failure: write_fallback_jsonl and bump drop counter
       v
events table (source_kind='shell') + source_health row updated
       |
       v
brain/src/hippo_brain/enrichment.py::is_enrichment_eligible
       |  -- runs at claim time, NOT at insert time
       |  -- filters trivial commands (clear, exec zsh, true, :) under 100 ms
       |     with no stdout/stderr; sets queue.status='skipped' inline
       v
brain/src/hippo_brain/enrichment.py::claim_pending_events_by_session
       |  -- session-grouped, 60s gap-split, max_claim_batch cap
       v
brain/src/hippo_brain/server.py::_enrich_shell_batches
       |  -- builds prompt via build_enrichment_prompt
       |  -- 3 retries via _call_llm_with_retries (LM Studio /v1/chat/completions)
       v
brain/src/hippo_brain/enrichment.py::write_knowledge_node
       |  -- single transaction: knowledge_nodes + tags + entities + link tables
       |  -- bumps queue.status='done' atomically
       v
knowledge_nodes row + knowledge_node_entities + knowledge_node_events
       |
       v  (background asyncio.create_task)
brain/src/hippo_brain/embeddings.py::embed_knowledge_node
       v
knowledge_vectors row (sqlite-vec INSERT OR REPLACE)
       |
       v
MCP-visible: search_events / search_knowledge / ask
```

**The key invariant for shell capture:** the hook never touches SQLite. Latency in the user's interactive prompt is bounded by the socket write — typically 20–50 ms. SQLite writes happen in `flush_events` on the daemon's tokio runtime. (See [`capture/anti-patterns.md`](capture/anti-patterns.md) AP-1.)

**Truncation.** Stdout and stderr are truncated to `[capture] output_head_lines` lines from the head and `output_tail_lines` from the tail (default: 50 each). Long outputs in between are replaced with an ellipsis marker. Configure in `~/.config/hippo/config.toml`.

**Redaction.** `crates/hippo-core/src/redaction.rs` runs on the event's command, stdout, and stderr before storage. Patterns come from `~/.config/hippo/redact.toml`. (Limits are documented in [`config/README.md`](../config/README.md); a deeper redaction reference is tracked in [#114](https://github.com/stevencarpenter/hippo/issues/114).)

## Claude session segment

```
Claude Code writes to ~/.claude/projects/<project>/<session>.jsonl
       |
       v  (file growth)
macOS FSEvents notifies com.hippo.claude-session-watcher (LaunchAgent)
       |
       v
crates/hippo-daemon/src/watch_claude_sessions.rs::process_file
       |  -- reads from claude_session_offsets per file (resume state)
       |  -- re-runs extract_segments on every growth event (idempotent)
       v
brain/src/hippo_brain/claude_sessions.py::extract_segments
       |  -- splits the JSONL into time-bounded SessionSegments
       |  -- segment_index is monotonic, derived from message ranges
       v
crates/hippo-daemon/src/claude_session.rs::insert_segments
       |  -- INSERT ... ON CONFLICT(session_id, segment_index) DO UPDATE SET
       |       (mutable cols) -- AP-12: NOT "OR IGNORE"; the segment grows
       |  -- content_hash compared with last_enriched_content_hash
       |     to gate re-enrichment of unchanged segments
       v
claude_sessions table  -- one row per (session_id, segment_index)
       +
events table (source_kind='claude-tool')  -- per tool_use line
       +
claude_enrichment_queue  -- for segments where content_hash changed
       |
       v
brain/src/hippo_brain/claude_sessions.py::claim_pending_claude_segments
       |  -- one segment at a time (no session grouping like shell)
       v
brain/src/hippo_brain/server.py::_enrich_claude_batches
       |  -- prompt = "\n---\n".join(segment.summary_text for segment in batch)
       |  -- the live brain joins pre-summarized segment text rather than
       |     calling build_claude_enrichment_prompt -- which is reserved for
       |     contexts (re-enrichment, eval) that need the full segment shape
       v
brain/src/hippo_brain/claude_sessions.py::write_claude_knowledge_node
       |  -- writes knowledge_nodes + knowledge_node_claude_sessions +
       |     entities + last_enriched_content_hash on the segment
       v
knowledge_nodes (one node per claim batch) + entities + embedding
```

**Key idempotency contract:** the watcher re-runs `extract_segments` on every FSEvents notification. The same `(session_id, segment_index)` will appear with growing `message_count` over time. The historical bug class — `INSERT OR IGNORE` on a bucket key whose content mutates — is documented in [`capture/anti-patterns.md`](capture/anti-patterns.md) AP-12. The current code uses `ON CONFLICT DO UPDATE` plus a content hash to detect "did anything actually change?" before re-enqueueing.

**Manual recovery.** If the watcher is wedged, `hippo ingest claude-session <path>` does a one-shot batch import via `claude_session.rs::ingest_session_file`.

**Subagent sessions** (`<project>/<parent>/subagents/<id>.jsonl`) follow the same path; `is_subagent=1` and `parent_session_id` are set during segment extraction.

**Codex/Xcode-side rollouts** are Python-only — `brain/src/hippo_brain/codex_sessions.py::extract_codex_segments` parses the distinct envelope shape, then writes through the same `claude_sessions` table with `source='codex'` on the segment. The Rust daemon does not parse Codex's JSONL.

## Browser visit

```
Firefox content script in extension/firefox/src/content.ts
       |  -- captures URL/title/dwell on page departure (visibilitychange)
       |  -- runs Mozilla Readability to extract main article text
       |  -- only fires on allowlisted domains
       v
extension/firefox/src/background.ts
       |  -- batches recent visits, applies engagement filter
       |     (scroll >= 15% OR has search query OR dwell > long_dwell_bypass_ms)
       v  (Native Messaging stdio)
crates/hippo-daemon/src/native_messaging.rs::run
       |  -- length-prefixed JSON frames over stdin/stdout
       |  -- strip_sensitive_params runs against the URL using
       |     [browser.url_redaction] strip_params
       |  -- make_envelope_id deduplicates same-URL repeats within
       |     [browser] dedup_window_minutes (default 10)
       v
crates/hippo-daemon/src/commands.rs::send_event_fire_and_forget
       v
crates/hippo-daemon/src/daemon.rs::flush_events
       v
crates/hippo-core/src/storage.rs::insert_browser_event
       |
       v
browser_events table + browser_enrichment_queue
       |
       v
brain/src/hippo_brain/browser_enrichment.py::claim_pending_browser_events
       |  -- 5-minute gap chunking; engagement filter applied at claim time
       v
brain/src/hippo_brain/server.py::_enrich_browser_batches
       |  -- build_browser_enrichment_prompt(events)
       v
write_knowledge_node + entities + embedding (same write path as shell)
```

**Allowlist.** Configured in `[browser.allowlist]` in `config.toml`. Visits to non-allowlisted domains are dropped in the content script — they never reach the daemon.

**URL redaction.** `[browser.url_redaction] strip_params` lists query-parameter names to strip (default includes `session_id`, `auth_token`, `access_token`, etc.). Path components are preserved; only matching query params are removed.

**Dedup.** Same URL within `dedup_window_minutes` collapses to a single envelope via `make_envelope_id` (UUID derived from URL + window-bucket).

## Probe events

Synthetic round-trips that bypass none of the above. `com.hippo.probe` LaunchAgent invokes `hippo probe --source <name>`, which emits an event tagged with `probe_tag` (a per-run UUID) through the same capture path the source uses. The probe code waits for the event to land and records `source_health.probe_lag_ms`.

Probe events are filtered out of every user-facing query at the daemon-side query path (and the brain side enforces the same filter as belt-and-braces). A Semgrep rule blocks new query call-sites that omit `AND probe_tag IS NULL`. See [`capture/anti-patterns.md`](capture/anti-patterns.md) AP-6.

## Where capture can fail silently

The historical reasons hippo built [`capture/architecture.md`](capture/architecture.md)'s I-1..I-10 invariants:

- **Hook not sourced.** The user's `~/.zshrc` was edited but never re-loaded. No errors anywhere — events just never appear.
- **NM manifest missing.** The Firefox extension was reloaded but `hippo daemon install --force` wasn't re-run. The extension can't reach the daemon. Captured by I-4.
- **`INSERT OR IGNORE` on growing JSONL** (AP-12). Segments captured at first FSEvents notification — usually 2–4 messages. Subsequent reparses with full content silently rejected. Symptom: `pct_with_tools` drops from ~50% to ~6%. Captured by I-2 once the migration to `ON CONFLICT DO UPDATE` shipped.
- **Daemon crash mid-flush.** Buffer empties to fallback JSONL via `write_fallback_jsonl`. Drained on next start. Captured by I-9 (fallback file age) if the daemon comes back but the drain is broken.
- **Brain unreachable but daemon up.** Capture continues to land events; only enrichment is delayed. The watchdog must NOT couple capture health to enrichment health. Captured by I-10 (architectural invariant).

## Diagnosing a missing event

If a shell command ran at 14:30 and isn't in `hippo events`, walk the lifecycle backward:

### Recipe 1 — Did the event reach SQLite at all?

```sql
-- shell (replace timestamp window as appropriate)
SELECT id, command, timestamp, exit_code, source_kind
FROM events
WHERE source_kind = 'shell'
  AND timestamp BETWEEN strftime('%s','now') * 1000 - 1800000   -- 30 min ago
                    AND strftime('%s','now') * 1000
ORDER BY id DESC LIMIT 20;
```

If the row is there but you don't see it via `hippo events`, check whether your filters exclude it (e.g., session, branch, source) and whether `probe_tag` is non-null (probes are filtered out of user-facing queries — that's correct behavior).

### Recipe 2 — Did the source health update recently?

```sql
SELECT source, last_event_ts,
       (strftime('%s','now') * 1000 - last_event_ts) / 1000 AS seconds_ago,
       consecutive_failures, probe_ok, probe_lag_ms
FROM source_health
ORDER BY source;
```

If `seconds_ago` is climbing for the source you expected to capture, the capture path stopped writing (not just enrichment). Fall through to the next recipe.

### Recipe 3 — Are enrichment claims piling up?

```sql
SELECT status, COUNT(*) FROM enrichment_queue GROUP BY status;
SELECT status, COUNT(*) FROM claude_enrichment_queue GROUP BY status;
SELECT status, COUNT(*) FROM browser_enrichment_queue GROUP BY status;
```

`pending` climbing means the brain isn't claiming fast enough (LM Studio slow / unloaded / wrong model name in `[models].enrichment`).
`processing` rows older than `lock_timeout_secs` are reaped by [`docs/brain-watchdog.md`](brain-watchdog.md). A persistent `failed` count means rows hit `max_retries` — inspect with:

```sql
SELECT id, retry_count, error_message
FROM enrichment_queue
WHERE status = 'failed'
ORDER BY id DESC LIMIT 10;
```

### Recipe 4 — Are events landing but stuck in the fallback path?

```bash
ls -la ~/.local/share/hippo/*.fallback.jsonl 2>/dev/null
```

A fallback file present means the daemon was unreachable when the event was generated. The next daemon start replays them. If the file persists for > 24 h, I-9 fires.

### Recipe 5 — Has the watchdog noticed anything?

```bash
hippo alarms list           # exits 1 if any unacknowledged
hippo doctor --explain      # CAUSE / FIX / DOC per failure
```

Doctor is the highest-leverage check; it summarizes everything above in 2 seconds.

## See also

- [`capture/architecture.md`](capture/architecture.md) — the system in cross-section: source_health, invariants, watchdog, probes, alarms.
- [`capture/sources.md`](capture/sources.md) — per-source coverage matrix.
- [`capture/anti-patterns.md`](capture/anti-patterns.md) — review-blocker rules (AP-1..AP-12).
- [`capture/operator-runbook.md`](capture/operator-runbook.md) — first-aid recipes.
- [`brain-watchdog.md`](brain-watchdog.md) — enrichment-queue reaper + claim-batch caps.
- [`crates/hippo-core/src/schema.sql`](../crates/hippo-core/src/schema.sql) — the SQL schema referenced throughout.
