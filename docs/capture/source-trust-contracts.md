# Source trust contracts (SNUG-122)

Agent-facing semantics for every source family that retrieval and MCP tools may surface. Read this before citing Hippo evidence — it tells you **where data lives**, **which rows are synthetic**, **what timestamps mean**, **what “missing” means**, and **which fields are safe to quote**.

Companion docs (do not duplicate here):

| Topic | Doc |
|---|---|
| Eligibility filters (what retrieval excludes by default) | [`retrieval-eligibility.md`](retrieval-eligibility.md) |
| Capture entry points and operator runbooks | [`sources.md`](sources.md), [`operator-runbook.md`](operator-runbook.md) |
| SQLite table reference | [`../schema.md`](../schema.md) |
| Redaction limits and config surface | [`../../config/README.md`](../../config/README.md); deeper redaction reference tracked in [#114](https://github.com/stevencarpenter/hippo/issues/114) |
| MCP tool shapes | [`../mcp-reference.md`](../mcp-reference.md) |

Machine-readable index: `brain/src/hippo_brain/_fixtures/source_trust_contracts.json`.

## How to read a contract

Each family below uses the same headings:

| Field | Meaning |
|---|---|
| **Tables** | SQLite tables holding raw or enriched data |
| **Identity** | Stable row keys agents can cite |
| **Timestamps** | Which column is “when this happened” (all epoch **milliseconds** unless noted) |
| **Synthetic / probe** | Rows that are canaries, not user activity |
| **Expected absent** | Normal gaps — not evidence of failure |
| **In-flight** | Rows that exist but are not yet eligible for retrieval |
| **Freshness** | How to tell whether capture is current (`source_health` key) |
| **Safe to cite** | Fields appropriate for evidence packets |
| **Do not cite** | Operational noise or PII-adjacent columns |

Retrieval applies eligibility from [`retrieval_eligibility.py`](../../brain/src/hippo_brain/retrieval_eligibility.py) by default. Operator debug: `include_excluded=true` or `HIPPO_RETRIEVAL_INCLUDE_EXCLUDED=1`.

---

## Shell commands

**Tables:** `events` (`source_kind='shell'`), `sessions`, `knowledge_node_events` → `knowledge_nodes`.

| | |
|---|---|
| **Identity** | `events.id` (integer); in retrieval: `linked_source_ids` entry `shell-<id>` |
| **Timestamps** | `events.timestamp` — command start (preexec). Duration in `duration_ms`; exit in `exit_code`. |
| **Synthetic / probe** | `events.probe_tag IS NOT NULL` — synthetic `hippo probe` commands. **Excluded** from MCP/RAG by default. |
| **Expected absent** | No rows when terminal idle, hook not sourced, or command redacted to empty. |
| **In-flight** | N/A — shell events are point-in-time. |
| **Freshness** | `source_health.source = 'shell'` → `last_event_ts`, `probe_ok`, `probe_lag_ms`. Invariant **I-1**. |
| **Safe to cite** | `command` (redacted), `cwd`, `git_repo`, `git_branch`, `exit_code`, `duration_ms`, `timestamp` |
| **Do not cite** | Raw `stdout`/`stderr` blobs unless operator mode; `probe_tag`; `env_snapshot_id` internals |

**Safe citation example**

> Shell event `shell-18442`: `cargo test -p hippo-core` in `/Users/me/projects/hippo` on `main` exited 0 after 12.4s (2026-07-05T10:15:00Z).

---

## Claude tool events

**Tables:** `events` (`source_kind='claude-tool'`, `tool_name` set), linked via `knowledge_node_events`.

| | |
|---|---|
| **Identity** | `events.id`; `linked_source_ids`: `shell-<id>` (tool events share the events table) |
| **Timestamps** | `events.timestamp` — when the tool call was ingested from the parent session JSONL |
| **Synthetic / probe** | Inherits session probe tag when parent segment is synthetic; filtered via shell eligibility |
| **Expected absent** | No tool rows when the session had no tool calls in the segment window |
| **In-flight** | Appears while parent `agentic_sessions` segment is inside the 90s settle window |
| **Freshness** | Indirect — `source_health.source = 'claude-tool'`; primary signal is parent `agentic-session-claude` |
| **Safe to cite** | `tool_name`, `command` (tool input summary), `cwd`, `git_branch`, parent session `session_id` |
| **Do not cite** | Full tool I/O if truncated; probe-tagged parent sessions |

**Note:** MCP `search_events` with `source="claude"` queries `agentic_sessions`, **not** `claude-tool` rows. Tool calls appear under `source="shell"` or `source="all"`.

**Safe citation example**

> Claude tool event `shell-9912`: `Bash` ran `rg 'retrieval_eligibility' brain/` in hippo repo (linked to session `sess-abc`).

---

## Agentic sessions (Claude Code, Codex, Cursor, opencode)

**Tables:** `agentic_sessions`, `knowledge_node_agentic_sessions` → `knowledge_nodes`. Legacy `claude_sessions` is frozen (v18+); retrieval reads `agentic_sessions` only.

| Harness | `source_health` key | Origin path |
|---|---|---|
| `claude-code` | `agentic-session-claude` | `~/.claude/projects/**/*.jsonl` (FS watcher) |
| `codex` | `agentic-session-codex` | `~/.codex/sessions/**/rollout-*.jsonl` |
| `cursor` | `agentic-session-cursor` | `~/.cursor/projects/**/agent-transcripts/**/*.jsonl` |
| `opencode` | `agentic-session-opencode` | opencode SQLite → poller |

| | |
|---|---|
| **Identity** | `(session_id, harness, segment_index)` UNIQUE; row `id` for joins; retrieval: `claude-<id>`, `codex-<id>`, `cursor-<id>`, `opencode-<id>` |
| **Timestamps** | `start_time` / `end_time` — segment bounds. Cursor segments use file mtime when JSONL has no per-line times. |
| **Synthetic / probe** | `probe_tag IS NOT NULL` on segment row |
| **Expected absent** | No segment until file idle past `min_idle_secs`; opencode sessions with &lt;3 messages and no diffs (enrichment skip) |
| **In-flight** | `end_time` within **90s** of now (`IN_FLIGHT_SETTLE_MS`); workflow `journal.jsonl` under `subagents/.../workflows/`; empty stubs (`message_count=0` and blank `summary_text`) |
| **Freshness** | Per-harness `source_health` row above; invariants **I-2**, **I-11**, **I-13**, **I-15** |
| **Safe to cite** | `session_id`, `harness`, `segment_index`, `summary_text`, `cwd`/`project_dir`, `git_branch`, `start_time`/`end_time`, `source_file` (path only, not full transcript) |
| **Do not cite** | Full `tool_calls_json` / `user_prompts_json` without redaction review; subagent workflow journals; probe segments |

**Safe citation example**

> Codex segment `codex-5021` (`session_id=rollout-7f3a`, segment 2): summarized dependency bump work in `/Users/me/projects/hippo`, ended 2026-07-04T18:22:00Z.

---

## Browser visits

**Tables:** `browser_events`, `knowledge_node_browser_events` → `knowledge_nodes`.

| | |
|---|---|
| **Identity** | `browser_events.id`; retrieval: `browser-<id>` |
| **Timestamps** | `timestamp` — page departure; `dwell_ms`, `scroll_pct` describe engagement |
| **Synthetic / probe** | `probe_tag IS NOT NULL` — probe canary page views |
| **Expected absent** | Domains outside `[browser.allowlist]`; Firefox down; extension not loaded |
| **In-flight** | N/A |
| **Freshness** | `source_health.source = 'browser'`; invariant **I-4** |
| **Safe to cite** | `domain`, `title`, `url` (query params may be redacted per config), `dwell_ms`, `timestamp` |
| **Do not cite** | Full `content` article text in agent summaries unless user asked for page content; `probe_tag` |

**Safe citation example**

> Browser event `browser-3301`: read "SQLite WAL mode" on `sqlite.org` for 4m 12s (2026-07-03T14:00:00Z).

---

## GitHub CI (workflow runs)

**Tables:** `workflow_runs`, `workflow_jobs`, `workflow_annotations`, `workflow_log_excerpts`, `knowledge_node_workflow_runs` → `knowledge_nodes`.

| | |
|---|---|
| **Identity** | `workflow_runs.id`; GitHub `run_id`; retrieval: `workflow-<id>` |
| **Timestamps** | `started_at` / `completed_at` on run; job-level times on `workflow_jobs` |
| **Synthetic / probe** | No probe rows — opt-in source (`[github] enabled = true`) |
| **Expected absent** | No rows when GitHub polling disabled, no token, or repo has no recent Actions activity |
| **In-flight** | `status = 'in_progress'` and `conclusion IS NULL` — **excluded** from retrieval until completed |
| **Freshness** | `source_health.source = 'workflow'`; doctor soft/hard thresholds on `MAX(started_at)` (3d / 30d) |
| **Safe to cite** | `repo`, `head_sha`, `head_branch`, `name` (workflow), `conclusion`, `html_url`, annotation `message` excerpts |
| **Do not cite** | Full log blobs; secrets from log excerpts (should be redacted at ingest) |

**Safe citation example**

> Workflow run `workflow-88` on `stevencarpenter/hippo` @ `abc1234` (`main`): CI workflow **success** (2026-07-05T09:00:00Z).

---

## Claude auto-memory

**Tables:** `memory_documents`, `memory_revisions`, `memory_chunks`, `knowledge_node_memory_chunks` → `knowledge_nodes`.

| | |
|---|---|
| **Identity** | `memory_documents.id`; retrieval: `memory-<chunk_id>` |
| **Timestamps** | `memory_revisions.created_at` for revision; document `updated_at` |
| **Synthetic / probe** | None |
| **Expected absent** | When `[auto_memory]` disabled or no `~/.claude/...` memory files ingested |
| **In-flight** | Queue rows in `memory_enrichment_queue` with `status != 'done'` |
| **Freshness** | `source_health.source = 'claude-auto-memory'` |
| **Safe to cite** | `repository`, `source_path`, chunk `heading`, `content` excerpt, active revision only (`state = 'active'`) |
| **Do not cite** | Superseded revisions (`active_revision_id` mismatch) |

---

## `source_health` (capture freshness metadata)

**Tables:** `source_health` only — not linked to `knowledge_nodes`. Query via `hippo doctor`, SQL, or Grafana (`hippo_daemon_source_health_*` metrics).

| | |
|---|---|
| **Identity** | `source` (TEXT PK) — e.g. `shell`, `agentic-session-cursor`, `watchdog` |
| **Timestamps** | `last_event_ts`, `updated_at`, `probe_last_run_ts` (all epoch ms) |
| **Synthetic / probe** | `probe_ok`, `probe_lag_ms`, `probe_last_run_ts` describe **canary** health, not user events |
| **Expected absent** | Row missing only on pre-v8 schema; new sources seed via migration |
| **In-flight** | N/A |
| **Freshness** | Self-describing — `last_event_ts` staleness vs invariant thresholds in [`architecture.md`](architecture.md) |
| **Safe to cite** | `source`, `last_event_ts`, `consecutive_failures`, `probe_ok`, `probe_lag_ms` when explaining **coverage gaps** |
| **Do not cite** | As user-activity evidence — health rows are operational telemetry |

**Safe citation example**

> `source_health` for `agentic-session-cursor`: `last_event_ts` 45m ago, `consecutive_failures=0`, `probe_ok=1` — capture healthy.

---

## Watchdog and probe metadata

**Tables:** `source_health` (`source='watchdog'`), `capture_alarms`; probe tags on real event tables.

| | |
|---|---|
| **Identity** | `capture_alarms.id`; invariant `I-1`…`I-16` |
| **Timestamps** | `capture_alarms.raised_at`, `resolved_at`; watchdog bumps `source_health.updated_at` every 60s |
| **Synthetic / probe** | Probe UUID in `probe_tag` on `events` / `browser_events` / `agentic_sessions` |
| **Expected absent** | No alarms when capture healthy; `probe_last_run_ts` NULL until first probe cycle |
| **In-flight** | Active unacked alarms (`acked_at IS NULL`, `resolved_at IS NULL`) |
| **Freshness** | Watchdog **I-7**: `watchdog` row stale &gt; 180s → doctor `[!!]` |
| **Safe to cite** | Alarm `invariant_id`, `details_json` summary, `raised_at` when warning about **stale or broken capture** |
| **Do not cite** | Probe-tagged event rows as user evidence; unacked alarms as factual claims about user work |

---

## Knowledge nodes (enriched layer)

All MCP retrieval tools (`ask`, `search_knowledge`, `search_hybrid`, `get_context`) return **knowledge nodes** — LLM-enriched summaries linked to source rows above.

| | |
|---|---|
| **Identity** | `knowledge_nodes.uuid` (prefer for citations); integer `id` internal |
| **Timestamps** | `created_at` on node; `captured_at` on `SearchResult` = best-effort max linked source time |
| **Synthetic / probe** | Nodes linked **only** to probe-tagged sources are excluded by eligibility |
| **Expected absent** | Enrichment backlog when brain down; ineligible sources never enqueue |
| **Safe to cite** | `uuid`, `summary`, `outcome`, `tags`, `cwd`, `git_branch`, `linked_*_ids` / `linked_source_ids` |
| **Do not cite** | `embed_text` as user-facing prose (identifier-dense index text); orphan nodes with no links (rare) |

Cross-link: node schema and link tables in [`../schema.md`](../schema.md#top-level-table-map).

---

## Quick reference: MCP `source` filter vs storage

| MCP `source` arg | Storage queried | Notes |
|---|---|---|
| `shell` | `knowledge_node_events` → `events` | Shell commands only, not `claude-tool` |
| `claude` | `knowledge_node_agentic_sessions` → `agentic_sessions` | All harnesses |
| `browser` | `knowledge_node_browser_events` → `browser_events` | |
| `workflow` | `knowledge_node_workflow_runs` → `workflow_runs` | Completed runs only (retrieval) |
| `claude-auto-memory` | `knowledge_node_memory_chunks` | Active revisions only |

---

## Adding a contract

When a new capture source ships:

1. Add a section here and an entry in `source_trust_contracts.json`.
2. Extend [`retrieval-eligibility.md`](retrieval-eligibility.md) and `retrieval_eligibility.py`.
3. Add a trust-eval case in `trust_eval_cases.json` if the source appears in retrieval.
4. Update [`sources.md`](sources.md) for operator-oriented capture detail.
