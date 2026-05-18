# Agentic session coverage hardening

**Date:** 2026-05-17
**Status:** Approved for implementation
**Scope:** Bring Codex and opencode capture/enrichment/observability up to the useful-context standard already expected from Claude Code.

## Goal

Hippo should capture the context that is useful for future coding sessions from every supported agentic harness. For Claude this means segmented prompts, assistant excerpts, tool calls, source health, queue rows, enrichment, and searchable knowledge nodes. Codex and opencode should meet the same standard, even though their storage formats differ.

## Current State

Codex writes several local stores:

- `~/.codex/sessions/**/rollout-*.jsonl`, `~/.codex/archived_sessions/rollout-*.jsonl`, and Xcode Codex rollout roots contain the canonical transcript.
- `~/.codex/state_5.sqlite` has a `threads` table with `rollout_path`; it is the best coverage oracle for Codex threads.
- `~/.codex/logs_2.sqlite` and `~/.codex/log/codex-tui.log` are operational logs. They can mention thread IDs not present in rollout files, but they are noisy and not the canonical transcript.
- `~/.codex/sqlite/codex-dev.db` is app/automation state on this machine, not session transcript state.

Hippo currently ingests Codex rollout files into `claude_sessions` and queues them in `claude_enrichment_queue`. That keeps segmentation fidelity, but coverage validation only compares rollout files to Hippo rows. It does not reconcile against `state_5.sqlite`, and dashboards do not expose Codex as its own enrichment source.

Opencode writes session, message, and part data to `~/.local/share/opencode/opencode.db`. Hippo currently reads only `session` metadata and diff summaries into `agentic_sessions`. That is weaker than Claude-style useful context because the brain prompt misses user text, assistant text, and tool summaries from `message`/`part`.

## Design

### Codex

Keep rollout JSONL as the canonical transcript source. Do not ingest `logs_2.sqlite` as memory content by default; treat log-only thread IDs as a diagnostic signal unless investigation shows user/assistant/tool context that is unavailable in rollouts.

Use `state_5.sqlite` as the Codex coverage oracle:

- Add a read-only Codex state DB check that compares non-archived and archived `threads.rollout_path` values against Hippo Codex session rows.
- A thread whose rollout file is currently younger than `[codex].min_idle_secs` is considered in-flight, not missing.
- A `state_5.sqlite` thread with no rollout path or missing rollout file is a warning with concrete thread IDs and paths.
- A rollout-backed state thread older than the idle window with no Hippo row is a capture gap and should make `hippo doctor` warn or fail according to existing staleness severity.
- `logs_2.sqlite` thread IDs not backed by `state_5.sqlite` or rollout files are counted and surfaced as diagnostic-only. They do not block doctor unless promoted later by a separate design.

Codex remains in `claude_sessions` for now because it is naturally segmented. A full migration to `agentic_sessions` requires adding segment identity/content hashes to that table and migrating opencode/brain code together; that is larger than this hardening pass.

### Opencode

Keep opencode in `agentic_sessions`, but enrich the stored session context:

- Extend the opencode poller to read `message` and `part` rows for each updated session.
- Build `summary_text` from title, model, agent, user text excerpts, assistant text excerpts, tool summaries, patch/file parts, and snapshot diff stats.
- Populate `message_count` and `token_count` from real opencode data.
- Keep queue semantics unchanged: one `agentic_sessions` row maps to one `agentic_enrichment_queue` row.
- Keep redaction on all prompt/tool text before storage.
- Keep opencode DB access read-only; do not add triggers or modify opencode tables.

This gives the brain enough content to produce identifier-dense knowledge nodes without changing the schema in this pass.

### Observability

Update the OTel/Grafana view so agentic sources are visible:

- Brain queue depth gauge must include `workflow` and `opencode` in addition to shell/Claude/browser.
- Codex should appear separately from Claude in brain metrics even while it physically uses `claude_enrichment_queue`; classify Codex by `claude_sessions.source_file` path.
- Enrichment dashboard panels should include `codex`, `opencode`, and `workflow` in queue depth and claimed-rate views.
- Daemon/source dashboards should include source-health and watchdog views that naturally show `agentic-session-codex` and `agentic-session-opencode`.
- Keep existing `service_namespace!~".+"` production filters.

## Testing

Add tests before implementation:

- Codex state coverage helper reports only the active in-flight thread as skipped and reports older missing rollout/Hippo rows as gaps.
- Codex rollout ingestion still queues every captured segment.
- Opencode poller stores real transcript-derived prompt content from `message`/`part` rows and redacts secrets.
- Brain queue-depth observable includes opencode/workflow and splits Codex from Claude.
- Grafana dashboard JSON includes Codex/opencode/workflow source queries where expected.

## Non-Goals

- Do not ingest Codex `logs_2.sqlite` as first-class memory content in this pass.
- Do not migrate Codex from `claude_sessions` to `agentic_sessions` yet.
- Do not create a new schema migration unless a test proves existing columns cannot carry the needed useful context.
