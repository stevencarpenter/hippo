# Capture Sources

Per-source coverage map: what each source captures, the entry point that writes it, the tables it lands in, and what fires when it stops landing. Companion to [`architecture.md`](architecture.md) (the system reference) and [`test-matrix.md`](test-matrix.md) (failure-mode-to-test mapping).

For the rules every contributor must follow when adding a new source, see [`anti-patterns.md`](anti-patterns.md). For first-aid when one of these sources stops working, see [`operator-runbook.md`](operator-runbook.md).

## Source matrix

| # | Source | Entry point | Tables | Invariant | Probe | Status |
|---|---|---|---|---|---|---|
| 1 | **Shell commands** (zsh hook) | `hippo.zsh::preexec/precmd` → unix socket → `commands.rs::handle_send_event_shell` → `daemon.rs::flush_events` → `storage.rs::insert_event_at` | `events` (`source_kind='shell'`) | I-1 | Yes (synthetic command via `hippo probe --source shell`) | healthy |
| 2 | **Claude tool events** | Derived during Claude-session ingest. `claude_session.rs::build_envelope` → same `flush_events` path; `storage.rs` derives `source_kind='claude-tool'` when `tool_name.is_some()` | `events` (`source_kind='claude-tool'`, `tool_name` set) | I-3 | Indirect (rides on claude-session probe) | healthy |
| 3 | **Claude session segments** (FS watcher) | `com.hippo.claude-session-watcher` LaunchAgent → FSEvents on `~/.claude/projects/**/*.jsonl` → `watch_claude_sessions.rs::process_file` → `claude_session.rs::ingest_session_file` → `insert_segments` | `claude_sessions`, `claude_enrichment_queue` (capture-health key `agentic-session-claude`) | I-2 | Yes (synthetic JSONL append) | healthy |
| 4 | **Claude subagent sessions** (`agent-*.jsonl`) | Same FS-watcher path; `SessionFile::from_path` detects `<project>/<parent-uuid>/subagents/<id>.jsonl` and sets `is_subagent=true` | `claude_sessions` with `is_subagent=1`, `parent_session_id` | I-2 | (rides on parent) | healthy |
| 5 | **Browser visits** (Firefox extension) | `extension/firefox` content script → background → native messaging stdio → `native_messaging.rs::run` → `send_event_fire_and_forget` → `flush_events` → `storage.rs::insert_browser_event` | `browser_events`, `browser_enrichment_queue` | I-4 | Yes (synthetic NM frame) | healthy |
| 6 | **GitHub workflow runs** | `gh_poll.rs::run_once` (poller) → `storage.rs::workflow_store::*` | `workflow_runs`, `workflow_jobs`, `workflow_annotations`, `workflow_log_excerpts`, `workflow_enrichment_queue` | (no real-time invariant; doctor checks freshness) | No | healthy; opt-in via `[github] enabled = true` |
| 7 | **Xcode ClaudeAgentConfig sessions** | `com.hippo.xcode-claude-ingest` LaunchAgent → `scripts/hippo-ingest-claude.py` → Python `insert_segment` (same `claude_sessions` schema) | `claude_sessions`, `claude_enrichment_queue` | I-2 (shared) | Indirect | healthy |
| 8 | **Xcode Codex rollouts** | `com.hippo.codex-session` LaunchAgent (scheduled poller) → `hippo codex-poll` → `codex_session::poll_tick` (Rust) walks `~/.codex/sessions` (+ archived + Xcode CodingAssistant dir) for `rollout-*.jsonl`, upserts segmented rows into `claude_sessions` via `(session_id, segment_index)` ON CONFLICT; capture-health key `agentic-session-codex` | `claude_sessions` (shared), `claude_enrichment_queue` | I-2 (shared) | No | healthy — Rust poller, same source-audit coverage as other `claude_sessions` sources |
| 9 | **Probe events** | `com.hippo.probe` LaunchAgent → `crates/hippo-daemon/src/probe.rs` → per-source synthetic-event path | `events` / `browser_events` / `claude_sessions` with `probe_tag IS NOT NULL` | I-8 | (drives the others' probes) | healthy |
| 10 | **Watchdog heartbeat** | `com.hippo.watchdog` → `crates/hippo-daemon/src/watchdog.rs` → `source_health WHERE source='watchdog'` UPDATE every cycle | `source_health` only | I-7 | n/a | healthy |
| 11 | **Opencode sessions** | `com.hippo.opencode-poll` LaunchAgent (every `[opencode] poll_interval_secs`) → `hippo opencode-poll` → `opencode_session.rs::poll_tick` reads opencode's own SQLite → upserts `agentic_sessions` + enqueues `agentic_enrichment_queue` | `agentic_sessions` (harness='opencode'), `agentic_enrichment_queue`, `knowledge_node_agentic_sessions` | I-11 | No (deferred; doctor uses opencode DB mtime as a freshness proxy) | new in v14 — no production probe yet |

## Per-source notes

### Shell

The shell hook runs in the user's interactive prompt critical path. The capture path is fire-and-forget at the socket boundary:

- `hippo.zsh::preexec` records command start.
- `hippo.zsh::precmd` records exit code, duration, captures stdout/stderr (head + tail truncation per `[capture]` config), and `disown`s a background `hippo send-event-shell` invocation that writes to the daemon socket.
- The hook never touches SQLite directly. (See [`anti-patterns.md`](anti-patterns.md) AP-1.)
- Daemon's `flush_events` batches socket frames every `flush_interval_ms` and writes events + `source_health` in the same transaction.

Captures: command, exit_code, duration_ms, cwd, hostname, shell, git_branch, git_commit, git_repo, stdout (truncated to head/tail), stderr (truncated). Redaction runs before storage; see [`config/README.md`](../../config/README.md).

### Claude session segments

Two distinct sources write to `claude_sessions`:

1. The **FS watcher** (`com.hippo.claude-session-watcher`, KeepAlive=true) is the canonical real-time path. FSEvents on `~/.claude/projects/**/*.jsonl` triggers `extract_segments`; segments are upserted via `(session_id, segment_index)` ON CONFLICT. The legacy per-session tmux tailer was deleted in T-8 (PR #89); the `SessionStart` hook is now a no-op debug log.
2. **Manual recovery** via `hippo ingest claude-session <path>` does a one-shot batch import. Useful when the watcher is wedged or for backfilling a single file.

The watcher's resume state lives in `claude_session_offsets` per file. Content-hash dedup gates re-enrichment: a segment whose content hasn't changed since last enrichment is not re-enqueued. (See [`anti-patterns.md`](anti-patterns.md) AP-12 for the historical bug class that motivated content-hash dedup.)

### Browser

The Firefox extension is a TypeScript build (`extension/firefox/`); the daemon-side adapter is `native_messaging.rs`. The extension only captures from allow-listed domains (`[browser.allowlist]` in `config.toml`). Page content is extracted via Mozilla Readability on page departure — full readable article text plus URL, title, dwell time, and scroll depth. URL query parameters listed in `[browser.url_redaction]` are stripped before storage.

The native messaging manifest at `~/Library/Application Support/Mozilla/NativeMessagingHosts/hippo-native-messaging.json` is installed by `hippo daemon install --force`.

### Workflow runs (GitHub Actions)

Opt-in via `[github] enabled = true` and a token in `HIPPO_GITHUB_TOKEN` (env var, `~/.config/zsh/.env`, or `gh auth token`; see `config/config.default.toml` for full token-scope guidance). Polls the Actions API every `[github] poll_interval_secs` (default 60), upserts runs/jobs/annotations/log excerpts, and enqueues for enrichment.

There is no real-time invariant — doctor's source-freshness probe (`crates/hippo-daemon/src/commands.rs::source_freshness_probes`) checks `MAX(workflow_runs.started_at)` against soft (3 d) and hard (30 d) thresholds.

### Xcode-side sources (ClaudeAgentConfig + Codex)

Two LaunchAgents write into the shared `claude_sessions` table. The ClaudeAgentConfig path (`com.hippo.xcode-claude-ingest`) still uses the Python script `scripts/hippo-ingest-claude.py`. The Codex rollout path (`com.hippo.codex-session`) is a Rust poller: `hippo codex-poll` runs `codex_session::poll_tick`, which walks `~/.codex/sessions` (+ archived + the Xcode CodingAssistant directory) for `rollout-*.jsonl` files, parses their distinct JSONL envelope (`session_meta`, `response_item/function_call`) natively in Rust, and upserts segments into `claude_sessions`. Both paths share the `claude_enrichment_queue` enrichment path.

### Opencode sessions

Polled (not watched) — opencode owns its SQLite DB and we open it read-only, so we cannot subscribe to writes the way the Claude FS watcher does for JSONL files. `hippo opencode-poll` runs every `[opencode] poll_interval_secs` (default 30 s) under `com.hippo.opencode-poll`.

Schema-wise this source is harness-agnostic by design: `agentic_sessions` carries a `harness` column (`'claude-code'`, `'opencode'`, `'codex'`) and is the destination for any future agentic-harness poller. v14 only wires opencode; codex/claude-code rows in this table are aspirational.

Change detection is a **per-session watermark**, not a global cursor. Each tick full-scans opencode's `session` table and compares every row's `time_updated` against the `end_time` Hippo already stored for that same session in `agentic_sessions` — the row keyed by `(session_id, harness='opencode')` (a session with no such row is new; one whose source `time_updated` exceeds its stored `end_time` has grown). `INSERT … ON CONFLICT DO UPDATE` keeps the destination row idempotent across re-reads. Because each session is its own watermark, this is gap-free and duplicate-free under partial failure: a failed upsert rolls back, leaving `end_time` behind, so that one session is retried next tick while unchanged siblings — including same-millisecond ones — are left alone. (Opencode does **not** use `agentic_cursor`; that table now serves only the codex per-file cursor. The earlier global-cursor design produced an unbounded duplicate-node "loop of sadness" at the watermark boundary and a partial-failure lost-update — both eliminated by going per-session.)

`agentic_sessions.summary_text` is built at write time from the opencode columns we have (`title`, `agent`, `model`, snapshot diff stats). The brain's `_enrich_opencode_batches` reads this column verbatim as the LLM prompt body, so any future enrichment quality work flows through `build_summary_text` in `opencode_session.rs`.

The brain side mirrors `claude_sessions.py`: `claim_pending_opencode_segments` flips queue rows to `processing`, the LLM call produces a `knowledge_nodes` row, `write_opencode_knowledge_node` links via `knowledge_node_agentic_sessions` and closes out the queue entry. Eligibility filter (in `enrichment.py::is_enrichment_eligible`) skips sessions with `<3` messages and no diffs/commits.

No production probe yet — `hippo probe --source opencode` is deferred. The doctor freshness check uses the opencode DB's own mtime as a suppression signal so an idle day in opencode doesn't fail the run.

### Probes

Synthetic events sent through each path every 5 minutes. Probe rows are tagged with a per-run UUID in `probe_tag IS NOT NULL` and are filtered out of every user-facing query. The filter is enforced both upstream (the daemon never enqueues probe events for enrichment) and downstream (every query in `commands.rs`, `mcp.py`, `retrieval.py` adds `AND probe_tag IS NULL`). A Semgrep rule blocks new query call-sites that omit the filter. (See [`anti-patterns.md`](anti-patterns.md) AP-6.)

## Adding a new source

The full contract for a new capture source is documented in [`adding-a-source.md`](adding-a-source.md). It covers the eleven required pieces: source identity, schema migration, capture path, redaction, probes, eligibility predicate, brain enrichment path, watchdog invariant, doctor check, test matrix, and documentation. A worked example (hypothetical `bash` source) walks through every step with concrete file references.
