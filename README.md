# Hippo

Local knowledge capture daemon for macOS. Hippo watches your shell activity, Claude Code sessions, and Firefox browsing,
redacts secrets, enriches events with local LLMs, and builds a searchable second brain — all without sending data off
your machine. An MCP server lets Claude query your knowledge base mid-conversation.

## Architecture

Three sources feed into a Rust daemon that stores events in SQLite. A Python brain enriches
them via local LLMs, writes vector embeddings, and an MCP server exposes it all to Claude.

```
┌─────────┐                  ┌──────────────┐                ┌──────────────┐
│  zsh     │  Unix socket    │              │  SQLite (WAL)  │              │
│  shell   │ ──────────────► │              │ ◄────────────► │ hippo-brain  │
└─────────┘                  │              │                │ (Python)     │
┌─────────┐  JSONL ingest    │ hippo-daemon │                └──────┬───────┘
│  Claude  │ ──────────────► │ (Rust)       │                       │
│  Code    │                 │              │                ┌──────┴───────┐
└─────────┘                  │              │                │  hippo-mcp   │
┌─────────┐  Native Msg      │              │                │ (MCP server) │
│  Firefox │ ──────────────► │              │                └──────────────┘
│  ext.    │                 │              │                  ▲         │
└─────────┘                  └──────────────┘           stdio │    SQLite│
                                                       (JSONL)│  LanceDB│
                                                              │   LM API│
                                                        Claude Code / Desktop
```

- **hippo-daemon** (Rust) — captures events from shell hooks, Claude Code sessions, and Firefox
  browsing via Unix socket and Native Messaging. Applies secret redaction, stores to SQLite, serves CLI queries.
- **hippo-brain** (Python) — polls enrichment queues from SQLite, calls LM Studio for summarization,
  correlates browser research with shell activity, writes knowledge nodes + embeddings to LanceDB,
  serves HTTP query API on port 9175.
- **hippo-mcp** (Python, MCP server) — exposes the knowledge base as MCP tools (`search_knowledge`,
  `search_events`, `get_entities`) over stdio. Claude Code queries your personal knowledge base
  mid-conversation. Reads SQLite + LanceDB directly, calls LM Studio for semantic search.

## Prerequisites

- macOS (launchd for service management)
- [Rust](https://rustup.rs/) (edition 2024)
- [Python](https://www.python.org/) 3.14+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [LM Studio](https://lmstudio.ai/) (local LLM inference)
- [Firefox Developer Edition](https://www.mozilla.org/en-US/firefox/developer/) (optional, for browser capture)
- [mise](https://mise.jdx.dev/) (optional, for task running)

## Quick Start

```bash
# Build, install, and start everything (release binary, LaunchAgents, config, symlink)
mise run install

# Source the shell hooks (add to your shell config)
source /path/to/hippo/shell/hippo-env.zsh   # in .zshenv
source /path/to/hippo/shell/hippo.zsh       # in .zshrc

# Set your LM Studio model
hippo config edit
# Fill in [models] enrichment = "your-model-name"

# Verify
hippo doctor

# (Optional) Enable MCP server for Claude Code
# Add to ~/.config/mcp/mcp-master.json:
# {
#   "hippo": {
#     "type": "stdio",
#     "command": "uv",
#     "args": ["run", "--project", "/path/to/hippo/brain", "hippo-mcp"],
#     "autoApprove": ["search_knowledge", "search_events", "get_entities"]
#   }
# }

# (Optional) Load Firefox extension for browser capture
# about:debugging → Load Temporary Add-on → extension/firefox/manifest.json
```

## Usage

```bash
# Check daemon status
hippo status

# List today's sessions
hippo sessions --today

# List recent events
hippo events --since 2h

# Query the knowledge base (via brain server)
hippo query "how did I fix that cargo build error"

# Raw keyword search (no brain server needed)
hippo query --raw "cargo build"

# List known entities
hippo entities

# Export training data
hippo export-training --since 30d --out ./export

# Test redaction patterns
hippo redact test "password=hunter2"
```

## MCP Server

The MCP server lets Claude Code (or any MCP client) query your knowledge base mid-conversation.
Three tools are exposed over stdio transport:

| Tool               | What it does                                                          |
|--------------------|-----------------------------------------------------------------------|
| `search_knowledge` | Semantic or lexical search over enriched knowledge nodes              |
| `search_events`    | Search raw shell commands, Claude sessions, and browser visits        |
| `get_entities`     | List known projects, tools, files, domains, and concepts              |

```bash
# Run standalone (for testing)
uv run --project brain hippo-mcp

# Configure for Claude Code — add to ~/.config/mcp/mcp-master.json:
{
  "hippo": {
    "type": "stdio",
    "command": "uv",
    "args": ["run", "--project", "/path/to/hippo/brain", "hippo-mcp"],
    "autoApprove": ["search_knowledge", "search_events", "get_entities"]
  }
}

# Then propagate to all tools
chezmoi apply   # or: sync-mcp-configs
```

The MCP server reads SQLite and LanceDB directly (no dependency on hippo-brain HTTP server).
Logs go to stderr. Metrics are tracked via `MetricsCollector` for future OTel export.

## Task Runner (mise)

All common workflows are defined in `mise.toml`:

| Task                                  | Description                      |
|---------------------------------------|----------------------------------|
| `mise run build`                      | Build Rust crates (debug)        |
| `mise run build:release`              | Build Rust crates (release)      |
| `mise run build:brain`                | Sync Python dependencies         |
| `mise run test`                       | Run all tests (Rust + Python)    |
| `mise run lint`                       | Run all linters (clippy + ruff)  |
| `mise run fmt`                        | Format all code                  |
| `mise run fmt:check`                  | Check formatting without changes |
| `mise run check`                      | Alias for `test` (full CI suite) |
| `mise run run:daemon`                 | Run daemon in foreground         |
| `mise run run:brain`                  | Run brain server                 |
| `mise run install`                    | Full clean-install from local repo state |
| `mise run doctor`                     | Run diagnostic checks            |
| `mise run start` / `stop` / `restart` | Manage launchd services          |
| `mise run nuke`                       | Kill everything (preserves data) |

Run `mise tasks` for the full list.

## Configuration

Runtime config: `~/.config/hippo/config.toml` (created by `mise run install`).
Edit with `hippo config edit`. See [`config/config.default.toml`](./config/config.default.toml) for the template.

The `[models]` section must be configured for brain enrichment to work — set the model name
to whatever LM Studio is serving (`curl -s http://localhost:1234/v1/models` to check).

Secret redaction patterns: `~/.config/hippo/redact.toml`. See [
`config/redact.default.toml`](config/redact.default.toml).

## Project Structure

```
├── crates/
│   ├── hippo-core/       # Shared library (types, config, storage, redaction)
│   └── hippo-daemon/     # Binary (daemon + CLI + native messaging host)
├── brain/                # Python enrichment, query server, and MCP server
├── extension/
│   └── firefox/          # Firefox WebExtension for browser activity capture
├── shell/                # zsh hooks (preexec/precmd integration)
├── config/               # Default config templates
├── launchd/              # macOS LaunchAgent plist templates
├── tools/                # Developer utility scripts (SQL formatting, etc.)
└── docs/                 # Design specs, plans, and architecture diagrams
```

## Data Storage

| Store   | Path                              | Purpose                                          |
|---------|-----------------------------------|--------------------------------------------------|
| SQLite  | `~/.local/share/hippo/hippo.db`   | Events, sessions, browser visits, enrichment queue, knowledge nodes, entities |
| LanceDB | `~/.local/share/hippo/vectors/`   | Vector embeddings for semantic search            |
| Config  | `~/.config/hippo/config.toml`     | User configuration                               |
| Logs    | `~/.local/share/hippo/*.log`      | Daemon and brain logs                            |

## License

MIT
