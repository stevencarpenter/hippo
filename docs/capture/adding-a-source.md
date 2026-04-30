# Adding a New Capture Source

The contract for plugging a new event source into hippo. Companion to [`architecture.md`](architecture.md) (the system reference) and [`sources.md`](sources.md) (per-source coverage). For the rules that constrain *how* you implement these steps, see [`anti-patterns.md`](anti-patterns.md) — every step below points back at one of AP-1..AP-12.

The bar for "it ships": `hippo doctor` is green, the watchdog asserts at least one freshness invariant for the new source, and the source-audit and test-matrix tables both have rows naming a regression test.

## Before you start

Adding a source is a multi-week piece of work, not a weekend project. The steps below are deliberately exhaustive because every step is a place a previous source's bug came from. If you're trying to plumb something through quickly, ask whether the data is already available via an existing source:

| Want to capture | Already covered by | If you still need a new source |
|---|---|---|
| Bash history | Shell hook (`hippo.zsh`) — bash equivalent welcome as a small variant rather than new source kind | A bash version of `hippo.zsh` belongs in `shell/` and reuses the daemon socket; no new `source_kind` needed unless metadata diverges |
| macOS Notification Center events | Not yet | New source — full contract below |
| Cursor IDE / Aider sessions | Not yet (Codex flows through ClaudeAgentConfig path; Cursor would be analogous) | New source if the JSONL shape diverges from Anthropic's |
| iMessage / Slack messages | No, and probably should not be added — privacy footprint exceeds redaction's reach | Don't |

If after that filter you still want to add a source, read on.

## The contract

Every new source must implement all of these. Skipping any one is a known-bug shape.

### 1. Source identity

Pick a `source_kind` value. It joins the existing set: `'shell'`, `'claude-tool'`, `'claude-session'`, `'claude-session-watcher'`, `'browser'`, `'workflow'`, `'watchdog'`, `'probe'`. Use kebab-case.

The new value goes in:

- `crates/hippo-core/src/storage.rs` — wherever `source_kind` is referenced; specifically `source_health` is seeded at v8 with one row per kind, so add a seed row in your migration (next step).
- `crates/hippo-daemon/src/probe.rs` — the probe enum if you want probe coverage (see step 5).

### 2. Schema migration

Bump `EXPECTED_VERSION` in `storage.rs` AND `EXPECTED_SCHEMA_VERSION` in `brain/src/hippo_brain/schema_version.py` (they must agree — see [`docs/schema.md`](../schema.md)).

The migration block in `storage.rs::open_db` does at minimum:

```sql
-- Seed a source_health row so the watchdog has something to assert against
INSERT OR IGNORE INTO source_health (source, last_event_ts, updated_at)
VALUES ('<your-source-kind>', NULL, unixepoch('now') * 1000);
```

If your source needs a dedicated table (browser-style, with extracted text + dwell), add it here too. Match the existing patterns: PK `id INTEGER PRIMARY KEY`, `created_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)`, dedup column `envelope_id TEXT` with a unique index `WHERE envelope_id IS NOT NULL`.

If your source events flow through the existing `events` table (shell/claude-tool style), no new table is needed; just `source_kind = '<your-source-kind>'`.

Add a row to [`docs/schema.md`](../schema.md)'s changelog. Add a doc note about what the new source captures.

### 3. Capture path

Where the actual writes happen. The contract is two writes in **one** SQLite transaction — the event row AND a `source_health` UPDATE — so the watchdog sees source health in lockstep with the event landing.

For sources that flow through the daemon's existing `flush_events` path (recommended for anything that can speak the Unix socket protocol):

- Add a new envelope variant in `crates/hippo-daemon/src/commands.rs` (analogous to `handle_send_event_shell` and `handle_send_event_browser`).
- Route to `send_event_fire_and_forget` for fire-and-forget capture; the durability contract (success = "frame hit the socket") is documented at the function definition.
- Implement the event-row write in `crates/hippo-core/src/storage.rs` analogous to `insert_event_at` / `insert_browser_event`.
- Update `source_health` in the same transaction. Existing helpers in `storage.rs` show the pattern.

For sources that watch a file system path (claude-session-style):

- Add a watcher under `crates/hippo-daemon/src/`. `watch_claude_sessions.rs` is the model — FSEvents subscribe + per-file resume state in a `*_offsets` table + idempotent re-run on every growth event.
- The watcher's resume table is part of step 2's migration.

For sources that poll a remote API (workflow-runs style):

- Add a poller under `crates/hippo-daemon/src/`. `gh_poll.rs::run_once` is the model — config-driven enable/poll-interval, wiremock-backed test fixture.
- Apply rate-limiting and a kill switch (`enabled = false` in `config.toml` by default).

[**AP-1 forbids**](anti-patterns.md#ap-1-blocking-the-shell-hook-on-health-writes) doing the `source_health` write from inside the user-facing capture call site (e.g. inside the shell hook). Health writes must happen on the daemon side, in the same SQLite transaction as the event row, after the event is buffered.

[**AP-2 forbids**](anti-patterns.md#ap-2-coupling-capture-health-to-enrichment-health) coupling capture-side health to enrichment health. Your `source_health` row must NOT track LM Studio reachability, brain-process state, or queue depth.

[**AP-11 forbids**](anti-patterns.md#ap-11-silently-swallowing-capture-errors) `.filter_map(Result::ok)` or `.ok().unwrap_or_default()` in any write path. Every error gets a `warn!` log and a counter bump.

### 4. Redaction (if applicable)

If your source captures user-typed content (not metadata), it must run through `crates/hippo-core/src/redaction.rs::RedactionEngine` before storage. See [`docs/redaction.md`](../redaction.md) for what the engine does. Browser content goes through redaction; URLs go through `strip_sensitive_params` separately.

Sources that capture only metadata (workflow-runs is structural, not user-typed) can skip this. Document the reasoning if you skip.

### 5. Probe coverage

The synthetic-probe job runs every 5 minutes and round-trips a tagged event through every probed source. To add probe support:

- Implement a probe in `crates/hippo-daemon/src/probe.rs`. Existing entries (shell, browser, claude-session) are the templates. The probe inserts an event with `probe_tag` set to a per-run UUID, then waits for the event to land in the source's table.
- Probes must work on a daemon-only basis — they cannot depend on the brain or LM Studio.
- The probe's `probe_ok` definition is source-specific (e.g. shell: `pgrep -x zsh` non-empty; browser: Firefox running). Document yours.

If your source genuinely cannot be probed (e.g., a one-shot import path, or a source that depends on user activity that synthetic probes can't fake), add an explicit "probe-exempt" entry in [`sources.md`](sources.md) with a one-sentence rationale.

[**AP-6 forbids**](anti-patterns.md#ap-6-letting-probes-appear-in-user-facing-queries) letting probe rows appear in user-facing queries. Every query path against the source's table must include `AND probe_tag IS NULL`. The Semgrep rule blocks new query call-sites that omit it.

### 6. Enrichment eligibility

Edit `brain/src/hippo_brain/enrichment.py::is_enrichment_eligible` to add a branch for your source. Decide:

- Should every event go to the LLM? (Probably no.)
- What's the trivial-event threshold? (Shell: command in a fixed-set with < 100 ms duration and no output. Browser: dwell < 1 s. Claude session: < 3 messages and no tool calls.)
- Where do skipped events go? (`enrichment_queue.status = 'skipped'`, no LLM call, no knowledge node.)

If your source warrants its own queue table (browser-style), add it to the migration in step 2. Otherwise reuse `enrichment_queue` (events) or one of the existing per-source tables.

### 7. Brain-side enrichment path

Add a `_enrich_<your_source>_batches` method to `brain/src/hippo_brain/server.py`, modeled on `_enrich_shell_batches` / `_enrich_browser_batches`. The shape:

1. `claim_pending_<source>` returns batches of events to enrich.
2. Build a prompt via a source-specific `build_<source>_enrichment_prompt(events)` function in your own module under `brain/src/hippo_brain/`.
3. Call `_call_llm_with_retries(SYSTEM_PROMPT, prompt, "<source-label>")`.
4. Parse with `parse_enrichment_response(raw)` (returns the canonical `EnrichmentResult` shape).
5. Write via `write_knowledge_node` (or a source-specific writer if you need extra link-table columns, like `write_claude_knowledge_node`).
6. Background-embed via `embed_knowledge_node` (asyncio task, gathered at end of batch).

Copy a System Prompt from one of the existing modules; honor the verbatim-preservation rule (PR #100) and the identifier-density rule for `embed_text`.

### 8. Watchdog invariant

Add an entry to the I-1..I-N invariant list in `crates/hippo-daemon/src/watchdog.rs` and the matching documentation in [`architecture.md`](architecture.md).

A typical freshness invariant looks like:

> **I-N**: If `<context-condition>` is true (your source is "active"), `source_health.<your-source>.last_event_ts` must be within `<threshold>` of `now`.

The context-condition is essential: shell silence overnight is normal, browser silence is normal when Firefox is closed. Don't fire alarms on absolute silence — gate on a positive activity signal. ([**AP-3 forbids**](anti-patterns.md#ap-3-alerting-on-absolute-silence-without-upstream-context) unconditional silence alarms.)

Threshold guidance: pick at least 3× the expected event-rate interval. Shell is 60 s (1000× the 50 ms typical hook latency). Claude-session is 5 min. Browser is 2 min.

### 9. Doctor check

`hippo doctor` already prints per-source freshness via `commands.rs::print_source_freshness`. Add a row for your source to that helper's source list. Severity:

- `[OK]` if last event < soft threshold
- `[WW]` if last event between soft and hard threshold
- `[!!]` if last event > hard threshold AND your `probe_ok` says the source should be active
- `[--]` if zero rows ever (this is informational on startup; once events have flowed, "zero rows ever" reverts to `[!!]`)

Soft + hard thresholds go in your watchdog config block; let the doctor read the same constants.

### 10. Test matrix + source audit

Every source row in [`sources.md`](sources.md) names an integration test that proves rows land. Add yours:

```rust
// crates/hippo-daemon/tests/source_audit.rs::your_source_events
fn your_source_events() {
    // Drive a real event through the capture path (no mocks of the
    // write layer; mocks of upstream are okay if needed).
    // Assert the event_table row exists and source_health updated.
}
```

Add a row to [`test-matrix.md`](test-matrix.md) for each known failure mode you've thought through. For a new source you'll typically add at least:

- F-N: source-event landed in expected table
- F-N+1: source_health updated in same transaction (decoupling test)
- F-N+2: probe round-trip lands within probe-freshness threshold
- F-N+3: probe events do NOT appear in user-facing queries (AP-6 regression)

### 11. Documentation

Add rows to:

- [`sources.md`](sources.md) — entry point, tables, invariant, probe coverage, status.
- [`docs/schema.md`](../schema.md) — changelog entry for the migration in step 2.
- [`docs/lifecycle.md`](../lifecycle.md) — a new lifecycle diagram if your source's data flow diverges from the three documented templates.
- README "Why hippo" if your source materially expands what hippo captures.

Don't add to [`anti-patterns.md`](anti-patterns.md) — that file is for review-blocker rules learned from real bugs, not for net-new constraints from speculation. If your source uncovers a genuine new failure mode in review or production, then add it.

## Worked example: bash history

Suppose you're adding `bash` history capture as a new source. (For zsh, just edit `hippo.zsh`; this example is illustrative.)

| Step | What you'd do |
|---|---|
| 1. Source identity | `source_kind = 'bash'` |
| 2. Migration | Bump `EXPECTED_VERSION` to N+1; seed `INSERT OR IGNORE INTO source_health (source, last_event_ts, updated_at) VALUES ('bash', NULL, ...)`. No new table — bash events flow through `events`. |
| 3. Capture path | Write `shell/hippo.bash` analogous to `hippo.zsh` (preexec/precmd → fire-and-forget on the daemon socket). The daemon side reuses `handle_send_event_shell` with `source_kind` parameterized — minor refactor in `commands.rs`. |
| 4. Redaction | Same `RedactionEngine` runs on commands + stdout + stderr; nothing source-specific. |
| 5. Probe | Add `Source::Bash` variant to `probe.rs` enum; `probe_ok` is `pgrep -x bash` non-empty. The probe injects a synthetic command via the same socket path. |
| 6. Eligibility | `is_enrichment_eligible(event_dict, "bash")` mirrors the shell branch — same trivial-command set, same duration threshold. |
| 7. Brain | If shell/bash share enrichment shape, no new method needed: parameterize `_enrich_shell_batches` to pull from both `source_kind in ('shell','bash')`. |
| 8. Watchdog invariant | I-N+1 mirrors I-1: bash liveness when bash is the user's active shell, 60 s threshold. |
| 9. Doctor | New row in `print_source_freshness`. |
| 10. Tests | `source_audit::bash_events` (event lands), `nm_bash_restart` (capture survives daemon restart), bash-specific probe round-trip. |
| 11. Docs | Row in `sources.md`; changelog in `schema.md`; bash hook documented in [`shell/README.md`](../../shell/README.md). |

Estimated effort for a competent contributor: **3-5 days of focused work** if all the daemon/brain abstractions accommodate the new source cleanly. The wider half is the test matrix.

## When NOT to add a new source

- **The data is already in another source.** Browser-extension-extracted Stack Overflow article text is already captured; don't add a separate "Stack Overflow source."
- **The source's privacy footprint exceeds redaction's reach.** Personal messaging apps (iMessage, Signal, Slack DMs) capture content that isn't well-formed-secret-shaped and that the user didn't consent to feeding an LLM. Hippo's design assumes the user can audit captured text; that breaks down for chat content.
- **You only want enrichment, not capture.** If you have a JSONL of past activity from another tool, use `hippo ingest claude-session <path>` (or write an ingest script) — that's a one-shot import, not a new source. New sources are for *continuous* capture paths.

## See also

- [`architecture.md`](architecture.md) — system reference; what every new source plugs into.
- [`sources.md`](sources.md) — per-source coverage matrix; your new source ends up here.
- [`anti-patterns.md`](anti-patterns.md) — AP-1..AP-12 review blockers; every step above points at one.
- [`test-matrix.md`](test-matrix.md) — the failure-mode-to-test contract you extend.
- [`docs/schema.md`](../schema.md) — the schema migration playbook.
- [`docs/redaction.md`](../redaction.md) — what the redaction engine does and doesn't catch.
