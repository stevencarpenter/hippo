# Hippo

Local knowledge capture daemon for macOS. Hippo watches your shell activity, redacts secrets, enriches events with local
LLMs, and builds a searchable second brain — all without sending data off your machine.

## Architecture

Two always-on processes share a SQLite database at `~/.local/share/hippo/hippo.db`:

```
┌─────────┐  preexec/precmd  ┌──────────────┐  SQLite (WAL)  ┌──────────────┐
│  zsh     │ ──────────────► │ hippo-daemon  │ ◄────────────► │ hippo-brain  │
│  shell   │  Unix socket    │ (Rust)        │                │ (Python)     │
└─────────┘                  └──────────────┘                └──────────────┘
                                    │                               │
                              captures events,                enriches via
                              redacts secrets,                LM Studio,
                              serves CLI queries              writes embeddings
```

- **hippo-daemon** (Rust) — captures shell events via Unix socket, applies secret redaction, stores to SQLite, serves
  CLI queries
- **hippo-brain** (Python) — polls enrichment queue from SQLite, calls LM Studio for summarization, writes knowledge
  nodes + embeddings to LanceDB, serves HTTP query API on port 9175

## Prerequisites

- macOS (launchd for service management)
- [Rust](https://rustup.rs/) (edition 2024)
- [Python](https://www.python.org/) 3.14+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [LM Studio](https://lmstudio.ai/) (local LLM inference)
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
│   └── hippo-daemon/     # Binary (daemon + CLI)
├── brain/                # Python enrichment + query server
├── shell/                # zsh hooks (preexec/precmd integration)
├── config/               # Default config templates
├── launchd/              # macOS LaunchAgent plist templates
├── tools/                # Developer utility scripts (SQL formatting, etc.)
└── docs/                 # Research and design docs
```

## Data Storage

| Store   | Path                            | Purpose                               |
|---------|---------------------------------|---------------------------------------|
| SQLite  | `~/.local/share/hippo/hippo.db` | Events, sessions, enrichment queue    |
| LanceDB | `~/.local/share/hippo/lancedb/` | Vector embeddings for semantic search |
| Config  | `~/.config/hippo/config.toml`   | User configuration                    |
| Logs    | `~/.local/share/hippo/*.log`    | Daemon and brain logs                 |

## License

MIT
