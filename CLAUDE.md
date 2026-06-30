# CLAUDE.md

## What This Is

Hippo - a local knowledge capture daemon for macOS. Rust daemon captures shell activity, Python brain enriches it via
local LLMs.

## Project Structure

- `crates/hippo-core/` - shared Rust library (types, config, storage, redaction)
- `crates/hippo-daemon/` - Rust binary (daemon + CLI)
- `brain/` - Python project (enrichment, embeddings, query server)
- `shell/` - zsh hook scripts
- `config/` - default config templates
- `launchd/` - LaunchAgent plists
- `extension/firefox/` - Firefox WebExtension for browser activity capture

## Commands

### Install / Service Management

    mise run install          # Full clean-install: build, install, configure, start, verify
    mise run start            # Start services via launchd
    mise run stop             # Stop services via launchd
    mise run restart          # Stop + start
    mise run nuke             # Kill everything (SIGKILL), preserves data
    hippo doctor              # Health check
    hippo config edit         # Edit runtime config
    hippo ask "<question>"    # RAG query: synthesized answer from knowledge base

### Rust (daemon + CLI)

    cargo build
    cargo test
    cargo test -p hippo-core
    cargo test -p hippo-daemon
    cargo clippy --all-targets -- -D warnings
    cargo fmt --check

### Python (brain)

    uv sync --project brain
    uv run --project brain pytest brain/tests -v
    uv run --project brain ruff check brain/
    uv run --project brain ruff format --check brain/

### MCP Server

    uv run --project brain hippo-mcp    # Start MCP server (stdio transport)

The MCP server exposes four tools: `ask`, `search_knowledge`, `search_events`, `get_entities`.
Configure in your MCP config (e.g., `~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "hippo": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--project", "/path/to/hippo/brain", "hippo-mcp"],
      "env": {"HIPPO_OTEL_ENABLED": "1"}
    }
  }
}
```

The MCP server is a separate process from the brain daemon; OTel instrumentation (`hippo_brain_mcp_*` metrics) is only active when `HIPPO_OTEL_ENABLED=1` is set in the spawn environment. Without this env block the MCP process emits no telemetry even if the brain daemon has OTel enabled.

The `ask` tool performs RAG: embeds the question, retrieves relevant knowledge nodes from SQLite via sqlite-vec (vec0) + FTS5 hybrid search,
synthesizes an answer via a local LLM (`models.query` in config.toml), and returns the answer with
scored source references. Requires `glow` for rendered CLI output (`brew install glow`).

Logs go to stderr. Metrics available via `MetricsCollector.snapshot()` for future OTel export.

## Path Conventions

All paths use XDG defaults (not macOS-native ~/Library paths):

- Data: `~/.local/share/hippo/` (DB, logs, socket, fallback)
- Config: `~/.config/hippo/` (config.toml, redact.toml)
- Binary: `~/.local/bin/hippo` (symlink to target/release/hippo)

Override with `XDG_DATA_HOME` / `XDG_CONFIG_HOME` env vars.

## Architecture

Two processes share a SQLite database at ~/.local/share/hippo/hippo.db:

1. hippo-daemon (Rust) - captures shell events via Unix socket, redacts secrets, writes to SQLite, serves CLI queries.
   `hippo doctor` checks version alignment between CLI, running daemon, and brain.
2. hippo-brain (Python) - polls enrichment queues from SQLite, calls a local OpenAI-compatible inference server (default oMLX, also tested with LM Studio), writes knowledge nodes + embeddings
   to SQLite (sqlite-vec vec0 virtual tables + FTS5). Shell, Claude, and browser sources are enriched concurrently via `asyncio.gather()`;
   embeddings run as background tasks to overlap with LLM inference.

Communication:

- Shell hook to daemon: fire-and-forget via Unix socket (length-prefixed JSON)
- CLI to daemon: request/response via same Unix socket
- hippo query (non-raw) to brain: HTTP request to brain local server
- Brain to SQLite: direct read/write (WAL mode, busy_timeout=5000)

### Browser Source (Firefox)

Firefox Developer Edition extension captures browsing activity from allowlisted domains and sends it to hippo-daemon via Native Messaging.

**Setup:**
1. Build: `cargo build --release`
2. Install: `hippo daemon install --force` (installs LaunchAgents + Native Messaging manifest)
3. Load extension: `about:debugging` → Load Temporary Add-on → `extension/firefox/manifest.json`

**Key paths:**
- Extension: `extension/firefox/`
- Native Messaging manifest: `~/Library/Application Support/Mozilla/NativeMessagingHosts/hippo_daemon.json`
- Config: `[browser]` section in `~/.config/hippo/config.toml`

**CLI:** `hippo native-messaging-host` — stdin/stdout bridge invoked by Firefox, not run manually

**Schema:** v4 adds `browser_events`, `browser_enrichment_queue`, `knowledge_node_browser_events`

### Claude Session Ingestion

Ingestion is handled by `crates/hippo-daemon/src/watch_claude_sessions.rs`, a long-lived `notify`/FSEvents watcher that runs under launchd (`com.hippo.claude-session-watcher`, `KeepAlive=true`). It subscribes to `~/.claude/projects/**/*.jsonl`, re-runs `extract_segments` on every file growth event, and upserts each segment into the `agentic_sessions` table (`harness = 'claude-code'`) via `INSERT … ON CONFLICT (session_id, harness, segment_index) DO UPDATE SET …` so repeated processing is idempotent. Genuinely new content is re-enqueued into `agentic_enrichment_queue` (shared across all agentic sources). Per-file resume state lives in `claude_session_offsets`.

**SessionStart hook:** `shell/claude-session-hook.sh` (14 lines, no-op as of T-8 / 2026-04-25). It writes a "hook invoked" line to `$DATA_DIR/session-hook-debug.log` so doctor's `check_session_hook_log` can verify hook activity, and exits 0. It does **not** spawn anything, **not** touch tmux, **not** parse the input JSON. Existing `~/.claude/settings.json` entries continue to work without modification.

**Manual recovery:** `hippo ingest claude-session <path>` does a one-shot batch import (handy if the watcher is wedged or for backfilling a single file).

**Hook install:** `hippo daemon install` writes the hook entry into `~/.claude/settings.json`. `hippo doctor` verifies the hook path matches the repo.

**Schema (agentic unification):** All agentic sources (Claude Code, Codex, Cursor, opencode) now write the `agentic_sessions` / `agentic_enrichment_queue` / `knowledge_node_agentic_sessions` family, keyed by a `harness` column and a `(session_id, harness, segment_index)` conflict target. The v17→v18 migration idempotently backfilled all historical `claude_sessions` / `knowledge_node_claude_sessions` / `claude_enrichment_queue` rows into the agentic family (harness derived from `source_file`). The legacy `claude_*` tables are now frozen — still created by `schema.sql`, no longer written, dropped in a later unification step. v19 adds the read-only Claude auto-memory source tables. `EXPECTED_VERSION` / `EXPECTED_SCHEMA_VERSION` are 19.

### Codex Session Ingestion

Ingestion is handled by `codex_session::poll_tick` in `crates/hippo-daemon/src/codex_session.rs`, invoked by the `hippo codex-poll` CLI command. It runs under launchd (`com.hippo.codex-session`), `StartInterval`-driven on the `[codex] poll_interval_secs` cadence (default 60 s). Each tick walks the `[codex] session_roots` directories (default: `~/.codex/sessions`, `~/.codex/archived_sessions`, `~/Library/Developer/Xcode/CodingAssistant/codex/sessions`) for `rollout-*.jsonl` files. It skips in-flight files (modified within `min_idle_secs`, default 60 s, to avoid partial reads) and skips unchanged files via an inode-keyed cursor in the `agentic_cursor` table (`source_key = codex-{inode}`; inode survives Codex's archival `mv`).

For each changed file, `extract_segments` parses the rollout into task-boundary segments, then `upsert_segment_tx` upserts each segment into the `agentic_sessions` table (`harness = 'codex'`, a single transaction per file) via `INSERT … ON CONFLICT (session_id, harness, segment_index) DO UPDATE SET …` — so re-ingest of a grown rollout updates existing rows rather than duplicating them. The `.codex/` path stored in `source_file` still records the row's origin. Genuinely new content is re-enqueued into `agentic_enrichment_queue`, which Codex shares with all agentic sources, so the brain enriches Codex segments into knowledge nodes through the same path.

**Config:** `[codex]` section in `~/.config/hippo/config.toml`. `session_roots` may be omitted — the Rust default supplies the three paths above. Set `enabled = false` to make `poll_tick` a no-op.

**Verify:**
```bash
hippo codex-poll   # one-shot ingest; exits 0
hippo doctor       # shows the agentic-session-codex line
```

**Schema:** v15 migration seeds the `agentic-session-codex` row in `source_health` (the capture-health key for this source).

**Spec:** `docs/superpowers/specs/2026-05-17-codex-ingestion-design.md`

### Cursor Session Ingestion

Ingestion is handled by `cursor_session::poll_tick` in `crates/hippo-daemon/src/cursor_session.rs`, invoked by the `hippo cursor-poll` CLI command. It runs under launchd (`com.hippo.cursor-session`), `StartInterval`-driven on the `[cursor] poll_interval_secs` cadence (default 60 s). Each tick walks the `[cursor] session_roots` directories (default: `~/.cursor/projects`) for `agent-transcripts/**/*.jsonl` files. It skips in-flight files (modified within `min_idle_secs`, default 60 s, to avoid partial reads) and skips unchanged files via an inode-keyed cursor in the `agentic_cursor` table (`source_key = cursor-agent-{inode}`).

Cursor transcripts carry no per-line timestamps, so segments are bounded by accumulated character count rather than time gaps, and are time-stamped from the file mtime. For each changed file, `extract_segments` parses the transcript into char-capped segments, then `upsert_segment_tx` upserts each segment into the `agentic_sessions` table (`harness = 'cursor'`) via `INSERT … ON CONFLICT (session_id, harness, segment_index) DO UPDATE SET …` — so re-ingest of a grown transcript updates existing rows rather than duplicating them. The `.cursor/` path stored in `source_file` still records the row's origin. Subagents are ingested as their own sessions with `is_subagent=1` and `parent_session_id` set. Genuinely new content is re-enqueued into `agentic_enrichment_queue`, which Cursor shares with all agentic sources, so the brain enriches Cursor segments into knowledge nodes through the same path.

**Config:** `[cursor]` section in `~/.config/hippo/config.toml`. `session_roots` may be omitted — the Rust default supplies `~/.cursor/projects`. Set `enabled = false` to make `poll_tick` a no-op.

**Verify:**
```bash
hippo cursor-poll   # one-shot ingest; exits 0
hippo doctor        # shows the agentic-session-cursor line
```

**Schema:** v16 migration seeds the `agentic-session-cursor` row in `source_health` (the capture-health key for this source).

**Manual recovery:** `hippo ingest cursor-session <path>` does a one-shot batch import (handy if the poller is wedged or for backfilling a single file).

**Spec:** `docs/superpowers/specs/2026-05-25-cursor-ingestion-design.md`

### Capture Reliability (v0.16+)

Capture-reliability stack (the result of the P0–P3 overhaul shipped through v0.16). Reference docs live in [`docs/capture/`](docs/capture/architecture.md); historical design records are in [`docs/archive/capture-reliability-overhaul/`](docs/archive/capture-reliability-overhaul/). Key pieces:

- **`source_health` table**: single SQL ground truth of "did the event land?" per source (non-exhaustive: `shell`, `claude-tool`, `agentic-session-claude`, `agentic-session-opencode`, `agentic-session-codex`, `agentic-session-cursor`, `browser`, `claude-session-watcher`, `watchdog`, `brain-preflight`). Every capture path writes its row in the same transaction as the event insert. (There is no `probe` row — the probe job writes `probe_*` columns onto each real source's row.) See [`docs/capture/architecture.md`](docs/capture/architecture.md).
- **`hippo watchdog run`** (launchd `com.hippo.watchdog`, every 60 s): asserts the I-1..I-15 invariants against `source_health`, writes `capture_alarms` rows on violations, rate-limited per invariant. See [`docs/capture/architecture.md`](docs/capture/architecture.md).
- **`hippo alarms list / ack`**: CLI for unacknowledged alarms (exit 1 if any).
- **`hippo doctor`**: ten isolated checks with `[OK]`/`[WW]`/`[!!]`/`[--]` severity, exit code = fail count, total wall-clock < 2 s. `--explain` prints CAUSE/FIX/DOC per failure. See [`docs/capture/operator-runbook.md`](docs/capture/operator-runbook.md).
- **`hippo probe`** (launchd `com.hippo.probe`, every 5 min): synthetic canary events round-trip through each capture path; latency recorded in `source_health.probe_lag_ms`. All probe rows carry a `probe_tag` and are filtered out of every user-facing query (RAG, MCP tools, `hippo ask`, etc.) by upstream daemon filtering and a Semgrep rule. See [`docs/capture/architecture.md`](docs/capture/architecture.md).
- **Anti-patterns** are codified in [`docs/capture/anti-patterns.md`](docs/capture/anti-patterns.md) — review blockers (e.g., AP-1: don't block the shell hook on health writes; AP-6: don't let probes appear in user-facing queries).
- **Per-source coverage** is mapped in [`docs/capture/sources.md`](docs/capture/sources.md); the test-matrix table that backs it lives in [`docs/capture/test-matrix.md`](docs/capture/test-matrix.md).

## Style

- Rust: edition 2024, clippy clean, anyhow for errors, favor immutability and functional combinators
- Python: 3.14+, ruff for lint+format, uv for package management
- All timestamps: Unix epoch milliseconds (i64/INTEGER)
- SQLite: WAL mode, PRAGMA foreign_keys=ON, PRAGMA busy_timeout=5000 on every connection

## Observability / OTel

### Metric naming (OTel → Prometheus)

The OTel → Prometheus exporter appends a unit suffix to every instrument name:

| OTel unit | Prometheus suffix appended |
|---|---|
| `ms` | `_milliseconds` |
| `By` | `_bytes` |
| counter (any unit) | `_total` |
| `1` | `_ratio` ← avoid this |

**Do not use `unit="1"` for scores or raw counts.** Unit `"1"` produces a misleading `_ratio` suffix on the Prometheus side (e.g., a 0–100 health score or an alarm count would become `hippo_daemon_health_grade_ratio`, not `hippo_daemon_health_grade`). Use an explicit descriptive unit or omit the unit entirely.

Dashboard PromQL queries must use the suffixed Prometheus name. `brain/tests/test_otel_dashboards.py` enforces dashboard ↔ emitter name agreement — add new metrics there when adding new instruments.

### Bench results

Bench results are surfaced via the self-contained HTML dashboard produced by `hippo-bench export-dashboard` (backed by the SQLite results datastore). There are no Grafana bench dashboards — the planned `bench-run-overview`, `bench-model-drilldown`, and `bench-model-comparison` dashboards were never wired and have been removed. Use `hippo-bench export-dashboard` to view leaderboard, per-run, and per-model results.
