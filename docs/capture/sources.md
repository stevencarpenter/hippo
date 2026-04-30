# Capture Sources

Per-source coverage map: what each source captures, the entry point that writes it, the tables it lands in, and what fires when it stops landing. Companion to [`architecture.md`](architecture.md) (the system reference) and [`test-matrix.md`](test-matrix.md) (failure-mode-to-test mapping).

For the rules every contributor must follow when adding a new source, see [`anti-patterns.md`](anti-patterns.md). For first-aid when one of these sources stops working, see [`operator-runbook.md`](operator-runbook.md).

## Source matrix

| # | Source | Entry point | Tables | Invariant | Probe | Status |
|---|---|---|---|---|---|---|
| 1 | **Shell commands** (zsh hook) | `hippo.zsh::preexec/precmd` → unix socket → `commands.rs::handle_send_event_shell` → `daemon.rs::flush_events` → `storage.rs::insert_event_at` | `events` (`source_kind='shell'`) | I-1 | Yes (synthetic command via `hippo probe --source shell`) | healthy |
| 2 | **Claude tool events** | Derived during Claude-session ingest. `claude_session.rs::build_envelope` → same `flush_events` path; `storage.rs` derives `source_kind='claude-tool'` when `tool_name.is_some()` | `events` (`source_kind='claude-tool'`, `tool_name` set) | I-3 | Indirect (rides on claude-session probe) | healthy |
| 3 | **Claude session segments** (FS watcher) | `com.hippo.claude-session-watcher` LaunchAgent → FSEvents on `~/.claude/projects/**/*.jsonl` → `watch_claude_sessions.rs::process_file` → `claude_session.rs::ingest_session_file` → `insert_segments` | `claude_sessions`, `claude_enrichment_queue` | I-2 | Yes (synthetic JSONL append) | healthy |
| 4 | **Claude subagent sessions** (`agent-*.jsonl`) | Same FS-watcher path; `SessionFile::from_path` detects `<project>/<parent-uuid>/subagents/<id>.jsonl` and sets `is_subagent=true` | `claude_sessions` with `is_subagent=1`, `parent_session_id` | I-2 | (rides on parent) | healthy |
| 5 | **Browser visits** (Firefox extension) | `extension/firefox` content script → background → native messaging stdio → `native_messaging.rs::run` → `send_event_fire_and_forget` → `flush_events` → `storage.rs::insert_browser_event` | `browser_events`, `browser_enrichment_queue` | I-4 | Yes (synthetic NM frame) | healthy |
| 6 | **GitHub workflow runs** | `gh_poll.rs::run_once` (poller) → `storage.rs::workflow_store::*` | `workflow_runs`, `workflow_jobs`, `workflow_annotations`, `workflow_log_excerpts`, `workflow_enrichment_queue` | (no real-time invariant; doctor checks freshness) | No | healthy; opt-in via `[github] enabled = true` |
| 7 | **Xcode ClaudeAgentConfig sessions** | `com.hippo.xcode-claude-ingest` LaunchAgent → `scripts/hippo-ingest-claude.py` → Python `insert_segment` (same `claude_sessions` schema) | `claude_sessions`, `claude_enrichment_queue` | I-2 (shared) | Indirect | healthy |
| 8 | **Xcode Codex rollouts** | `com.hippo.xcode-codex-ingest` LaunchAgent → `scripts/hippo-ingest-codex.py` → Python `codex_sessions.py::extract_codex_segments` → `insert_segment` (shared `claude_sessions`; `source='codex'` on segment) | `claude_sessions` (shared), `claude_enrichment_queue` | I-2 (shared) | No | intermittent — Python-only path; not covered by Rust source-audit suite |
| 9 | **Probe events** | `com.hippo.probe` LaunchAgent → `crates/hippo-daemon/src/probe.rs` → per-source synthetic-event path | `events` / `browser_events` / `claude_sessions` with `probe_tag IS NOT NULL` | I-8 | (drives the others' probes) | healthy |
| 10 | **Watchdog heartbeat** | `com.hippo.watchdog` → `crates/hippo-daemon/src/watchdog.rs` → `source_health WHERE source='watchdog'` UPDATE every cycle | `source_health` only | I-7 | n/a | healthy |

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

Two sibling LaunchAgents poll the Xcode CodingAssistant directories every 5 minutes and feed the Python ingest script. Both write into the shared `claude_sessions` table. Codex rollouts have a distinct JSONL envelope (`session_meta`, `response_item/function_call`) handled by `brain/src/hippo_brain/codex_sessions.py`; the Rust daemon does not parse Codex's shape.

### Probes

Synthetic events sent through each path every 5 minutes. Probe rows are tagged with a per-run UUID in `probe_tag IS NOT NULL` and are filtered out of every user-facing query. The filter is enforced both upstream (the daemon never enqueues probe events for enrichment) and downstream (every query in `commands.rs`, `mcp.py`, `retrieval.py` adds `AND probe_tag IS NULL`). A Semgrep rule blocks new query call-sites that omit the filter. (See [`anti-patterns.md`](anti-patterns.md) AP-6.)

## Adding a new source

The contract for a new capture source is documented end-to-end in the [`docs/adding-a-source.md`](../adding-a-source.md) guide (filed in [#114](https://github.com/stevencarpenter/hippo/issues/114) — pending). At minimum:

1. Add `source_kind` enum value (where defined).
2. Capture path that writes to `events` (or a source-specific table) AND to `source_health` in the same transaction.
3. Enrichment-eligibility predicate in `is_enrichment_eligible`.
4. Entry in `enrichment_queue` (or a source-specific queue).
5. Probe implementation, OR an explicit "probe-exempt with rationale" entry.
6. New invariant in this directory's [`architecture.md`](architecture.md).
7. New row in [`test-matrix.md`](test-matrix.md).
8. New row in this document.

Until the dedicated guide ships, the [`test-matrix.md`](test-matrix.md) "How to extend" section is the closest existing reference.
