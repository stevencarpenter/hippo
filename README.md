# Hippo

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![macOS](https://img.shields.io/badge/platform-macOS-lightgrey.svg)
[![Rust](https://img.shields.io/badge/rust-edition_2024-orange.svg)](https://www.rust-lang.org/)
[![Python](https://img.shields.io/badge/python-3.14%2B-blue.svg)](https://www.python.org/)

Local knowledge capture daemon for macOS. Hippo watches your shell activity, Claude Code sessions,
and Firefox browsing, redacts secrets, enriches events with local LLMs, and builds a searchable
second brain -- all without sending data off your machine.

## Quick Install

```bash
curl -fsSL https://github.com/stevencarpenter/hippo/releases/latest/download/install.sh | bash
```

This one-liner downloads and installs the daemon, brain, and GUI app with checksum verification. See [Manual Installation](#manual-installation) below for a step-by-step approach.

## Architecture

```
+-----------+                  +--------------+                +--------------+
|  zsh      |  Unix socket     |              |  SQLite (WAL)  |              |
|  shell    | ---------------> |              | <------------> | hippo-brain  |
+-----------+                  |              |                | (Python)     |
+-----------+  JSONL ingest    | hippo-daemon |                +------+-------+
|  Claude   | ---------------> | (Rust)       |                       |
|  Code     |                  |              |                +------+-------+
+-----------+                  |              |                |  hippo-mcp   |
+-----------+  Native Msg      |              |                | (MCP server) |
|  Firefox  | ---------------> |              |                +--------------+
|  ext.     |                  |              |                  ^         |
+-----------+                  +--------------+           stdio  |  SQLite |
                                                        (JSONL) | LanceDB |
                                                                |  LM API |
                                                          Claude Code / Desktop
```

| Component | Language | Role |
|-----------|----------|------|
| **hippo-daemon** | Rust | Captures events via Unix socket and Native Messaging. Applies secret redaction, stores to SQLite, serves CLI queries. |
| **hippo-brain** | Python | Polls enrichment queues, calls LM Studio for summarization, correlates browser research with shell activity, writes knowledge nodes + vector embeddings to LanceDB. |
| **hippo-mcp** | Python | MCP server exposing the knowledge base over stdio. Claude Code queries your personal knowledge base mid-conversation. |

## Prerequisites

| Dependency | Required | Notes |
|------------|----------|-------|
| macOS | Yes | Uses launchd for service management |
| [Rust](https://rustup.rs/) | Yes | Edition 2024 (1.85+) |
| [Python](https://www.python.org/) | Yes | 3.14+ |
| [uv](https://docs.astral.sh/uv/) | Yes | Python package manager |
| [LM Studio](https://lmstudio.ai/) | Yes | Local LLM inference -- load any model that supports chat + embedding |
| [mise](https://mise.jdx.dev/) | Recommended | Task runner; all workflows are defined in `mise.toml` |
| [glow](https://github.com/charmbracelet/glow) | Recommended | Renders `hippo ask` markdown output in the terminal |
| [Firefox Dev Edition](https://www.mozilla.org/en-US/firefox/developer/) | Optional | Browser activity capture |

## Manual Installation

```bash
# Clone or fork hippo and enter the repo
git clone https://github.com/stevencarpenter/hippo.git # or clone your fork of course
cd hippo

# Build and install everything (release binary, LaunchAgents, config, symlink)
mise run install

# Add shell hooks to your zsh config
echo 'source /path/to/hippo/shell/hippo-env.zsh' >> ~/.zshenv
echo 'source /path/to/hippo/shell/hippo.zsh'     >> ~/.zshrc
exec zsh  # reload

# Configure your LM Studio model
hippo config edit
```

In the config editor, set the `[models]` section to match a model loaded in LM Studio:

```toml
[models]
enrichment = "your-model-name"       # check: curl -s localhost:1234/v1/models
enrichment_bulk = "your-model-name"
query = "your-model-name"
embedding = "text-embedding-nomic-embed-text-v2-moe"
```

Verify everything is wired up:

```bash
hippo doctor
```

## Usage

```bash
hippo status                            # Daemon status
hippo sessions --today                  # List today's sessions
hippo events --since 2h                 # Recent shell events
hippo ask "how did I fix that build error"  # RAG query (synthesized answer)
hippo query --raw "cargo build"         # Raw lexical search (no brain needed)
hippo entities                          # Known projects, tools, files, concepts
hippo export-training --since 30d --out ./export  # Export training data
hippo redact test "password=hunter2"    # Test redaction patterns
```

## MCP Server

The MCP server lets Claude Code (or any MCP client) query your knowledge base mid-conversation.

| Tool | Description |
|------|-------------|
| `ask` | RAG query -- synthesizes an answer from relevant knowledge nodes |
| `search_knowledge` | Semantic or lexical search over enriched knowledge nodes |
| `search_events` | Search raw shell commands, Claude sessions, and browser visits |
| `get_entities` | List known projects, tools, files, domains, and concepts |

Add to your Claude Code MCP config (e.g., `~/.claude/settings.json` or your MCP config file):

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

Replace `/path/to/hippo` with the absolute path to your clone.

The MCP server reads SQLite and LanceDB directly (no dependency on the brain HTTP server).

## Firefox Extension (Optional)

Captures browsing activity from allowlisted developer domains and sends it to the daemon via
Native Messaging.

```bash
# Install the native messaging host (included in `mise run install`)
hippo daemon install --force

# Build the extension
cd extension/firefox && npx web-ext build --overwrite-dest && cd ../..
```

1. In Firefox Dev Edition: `about:config` -> set `xpinstall.signatures.required` to `false`
2. `about:addons` -> gear icon -> **Install Add-on From File** -> select the `.zip` from `web-ext-artifacts/`

See [`extension/firefox/README.md`](extension/firefox/README.md) for full details.

## Configuration

| File | Purpose |
|------|---------|
| `~/.config/hippo/config.toml` | Runtime config (models, ports, browser allowlist, telemetry) |
| `~/.config/hippo/redact.toml` | Secret redaction patterns (regex-based) |

Created automatically by `mise run install`. Edit with `hippo config edit`.
See [`config/config.default.toml`](config/config.default.toml) and
[`config/redact.default.toml`](config/redact.default.toml) for the templates.

The `[models]` section must be configured for enrichment to work. Set the model name to
whatever LM Studio is serving:

```bash
curl -s http://localhost:1234/v1/models | python3 -m json.tool
```

## Development

### With mise (recommended)

```bash
mise run build              # Build Rust crates (debug)
mise run build:release      # Build Rust crates (release)
mise run build:brain        # Sync Python dependencies
mise run test               # Full test suite (Rust + Python + lint + format check)
mise run lint               # clippy + ruff check
mise run fmt                # Format all code
mise run doctor             # Diagnostic checks
```

Run `mise tasks` for the complete list.

### Without mise

```bash
# Rust
cargo build
cargo test
cargo clippy --all-targets -- -D warnings
cargo fmt --check

# Python
uv sync --project brain
uv run --project brain pytest brain/tests -v
uv run --project brain ruff check brain/
uv run --project brain ruff format --check brain/

# MCP server
uv run --project brain hippo-mcp
```

### HippoGUI

`hippo-gui/` is a native macOS app project layered over a local Swift package library (`HippoGUIKit`) for browsing Hippo knowledge, events, sessions, and system status.

```bash
cd hippo-gui
swift test
xed HippoGUI.xcodeproj
./scripts/build-native-app.sh
```

See [`hippo-gui/README.md`](hippo-gui/README.md) for app-specific notes on Xcode, previews, package tests, and bundle builds.

### Service management

```bash
mise run start              # Start daemon + brain via launchd
mise run stop               # Stop both services
mise run restart            # Stop + start
mise run nuke               # SIGKILL everything (preserves data)
```

## Project Structure

```
crates/
  hippo-core/              Shared library (types, config, storage, redaction)
  hippo-daemon/            Binary (daemon + CLI + native messaging host)
brain/                     Python enrichment, query server, and MCP server
hippo-gui/                 SwiftUI macOS app (Swift Package)
extension/
  firefox/                 Firefox WebExtension for browser activity capture
shell/                     zsh hooks (preexec/precmd integration)
config/                    Default config templates
launchd/                   macOS LaunchAgent plist templates
scripts/                   Utility scripts (bulk enrich, re-embed, monitor)
otel/                      OpenTelemetry observability stack (docker-compose)
docs/                      Design specs, plans, and architecture diagrams
tools/                     Developer utilities (SQL formatting)
```

## Data Storage

All paths follow XDG defaults. Override with `XDG_DATA_HOME` / `XDG_CONFIG_HOME`.

| Store | Path | Purpose |
|-------|------|---------|
| SQLite | `~/.local/share/hippo/hippo.db` | Events, sessions, browser visits, enrichment queue, knowledge nodes, entities |
| LanceDB | `~/.local/share/hippo/vectors/` | Vector embeddings for semantic search |
| Config | `~/.config/hippo/config.toml` | User configuration |
| Logs | `~/.local/share/hippo/*.log` | Daemon and brain logs |

## License

[MIT](LICENSE)
