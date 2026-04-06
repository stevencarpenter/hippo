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
Configure in `~/.config/mcp/mcp-master.json` (or via mcp-sync). The `autoApprove` array allows tool calls without manual confirmation:

```json
{
  "hippo": {
    "type": "stdio",
    "command": "uv",
    "args": ["run", "--project", "/path/to/hippo/brain", "hippo-mcp"],
    "autoApprove": ["ask", "search_knowledge", "search_events", "get_entities"]
  }
}
```

After editing `mcp-master.json`, run `chezmoi apply` or `sync-mcp-configs` to propagate the config to all MCP-aware tools (Claude Code, Copilot, etc.).

The `ask` tool performs RAG: embeds the question, retrieves relevant knowledge nodes from LanceDB,
synthesizes an answer via a local LLM (`models.query` in config.toml), and returns the answer with
scored source references. Requires `glow` for rendered CLI output (`brew install glow`).

Logs go to stderr. Metrics available via `MetricsCollector.snapshot()` for future OTel export.

## Path Conventions

All paths use XDG defaults (not macOS-native ~/Library paths):

- Data: `~/.local/share/hippo/` (DB, logs, socket, fallback, lancedb)
- Config: `~/.config/hippo/` (config.toml, redact.toml)
- Binary: `~/.local/bin/hippo` (symlink to target/release/hippo)

Override with `XDG_DATA_HOME` / `XDG_CONFIG_HOME` env vars.

## Architecture

Two processes share a SQLite database at ~/.local/share/hippo/hippo.db:

1. hippo-daemon (Rust) - captures shell events via Unix socket, redacts secrets, writes to SQLite, serves CLI queries.
   `hippo doctor` checks version alignment between CLI, running daemon, and brain.
2. hippo-brain (Python) - polls enrichment queues from SQLite, calls LM Studio API, writes knowledge nodes + embeddings
   to LanceDB. Shell, Claude, and browser sources are enriched concurrently via `asyncio.gather()`;
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

`shell/claude-session-hook.sh` is a Claude Code SessionStart hook that tails session JSONL files into hippo via a detached tmux window (`hippo:` prefix).

**Key gotcha:** Claude Code wraps hook commands in an intermediate bash process (`claude → bash → hook.sh`). The hook must use the grandparent PID (the actual Claude process), not `$PPID` (the ephemeral bash). The Rust tailer's `kill(pid, 0)` check must distinguish ESRCH (process gone) from EPERM (process exists, no permission).

**Batch import:** `hippo ingest claude-session --batch <path>` for one-shot import of completed sessions.

**Hook config:** Add to `~/.claude/settings.json` under `hooks.SessionStart` (see `shell/claude-session-hook.sh` header for exact JSON). `hippo doctor` verifies the hook path matches the repo.

## Style

- Rust: edition 2024, clippy clean, anyhow for errors, favor immutability and functional combinators
- Python: 3.14+, ruff for lint+format, uv for package management
- All timestamps: Unix epoch milliseconds (i64/INTEGER)
- SQLite: WAL mode, PRAGMA foreign_keys=ON, PRAGMA busy_timeout=5000 on every connection
