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

### Swift (hippo-gui)

    mise run gui:build
    mise run gui:test
    mise run gui:lint
    mise run gui:format
    mise run gui:open

    # Tool prerequisites
    brew install swiftlint
    brew install swift-format

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
      "args": ["run", "--project", "/path/to/hippo/brain", "hippo-mcp"]
    }
  }
}
```

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
2. hippo-brain (Python) - polls enrichment queues from SQLite, calls LM Studio API, writes knowledge nodes + embeddings
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

Ingestion is handled by `crates/hippo-daemon/src/watch_claude_sessions.rs`, a long-lived `notify`/FSEvents watcher that runs under launchd (`com.hippo.claude-session-watcher`, `KeepAlive=true`). It subscribes to `~/.claude/projects/**/*.jsonl`, re-runs `extract_segments` on every file growth event, and inserts segments via `INSERT OR IGNORE` on `(session_id, segment_index)` so repeated processing is idempotent. Per-file resume state lives in `claude_session_offsets`.

**SessionStart hook:** `shell/claude-session-hook.sh` (14 lines, no-op as of T-8 / 2026-04-25). It writes a "hook invoked" line to `$DATA_DIR/session-hook-debug.log` so doctor's `check_session_hook_log` can verify hook activity, and exits 0. It does **not** spawn anything, **not** touch tmux, **not** parse the input JSON. Existing `~/.claude/settings.json` entries continue to work without modification.

**Manual recovery:** `hippo ingest claude-session <path>` does a one-shot batch import (handy if the watcher is wedged or for backfilling a single file).

**Hook install:** `hippo daemon install` writes the hook entry into `~/.claude/settings.json`. `hippo doctor` verifies the hook path matches the repo.

### Capture Reliability (v0.16+)

Capture-reliability stack (the result of the P0–P3 overhaul shipped through v0.16). Reference docs live in [`docs/capture/`](docs/capture/architecture.md); historical design records are in [`docs/archive/capture-reliability-overhaul/`](docs/archive/capture-reliability-overhaul/). Key pieces:

- **`source_health` table**: single SQL ground truth of "did the event land?" per source — `shell`, `claude-tool`, `claude-session`, `claude-session-watcher`, `browser`, `watchdog`, `probe`. Every capture path writes its row in the same transaction as the event insert. See [`docs/capture/architecture.md`](docs/capture/architecture.md).
- **`hippo watchdog run`** (launchd `com.hippo.watchdog`, every 60 s): asserts the I-1..I-10 invariants against `source_health`, writes `capture_alarms` rows on violations, rate-limited per invariant. See [`docs/capture/architecture.md`](docs/capture/architecture.md).
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
