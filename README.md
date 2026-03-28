# Hippo

Local knowledge capture daemon for macOS. Hippo watches your shell activity, redacts secrets, enriches events with local LLMs, and builds a searchable second brain — all without sending data off your machine.

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

- **hippo-daemon** (Rust) — captures shell events via Unix socket, applies secret redaction, stores to SQLite, serves CLI queries
- **hippo-brain** (Python) — polls enrichment queue from SQLite, calls LM Studio for summarization, writes knowledge nodes + embeddings to LanceDB, serves HTTP query API on port 9175

## Prerequisites

- macOS (launchd for service management)
- [Rust](https://rustup.rs/) (edition 2024)
- [Python](https://www.python.org/) 3.13+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [LM Studio](https://lmstudio.ai/) (local LLM inference)
- [mise](https://mise.jdx.dev/) (optional, for task running)

## Quick Start

```bash
# Build everything
mise run build:all

# Install the LaunchAgent and start the daemon
mise run install
mise run start

# Source the shell hook in your .zshrc
source /path/to/hippo/shell/hippo.zsh

# Start the brain server (or install its LaunchAgent)
mise run run:brain

# Check health
mise run doctor
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

| Task | Description |
|---|---|
| `mise run build` | Build Rust crates (debug) |
| `mise run build:release` | Build Rust crates (release) |
| `mise run build:brain` | Sync Python dependencies |
| `mise run test` | Run all tests (Rust + Python) |
| `mise run lint` | Run all linters (clippy + ruff) |
| `mise run fmt` | Format all code |
| `mise run fmt:check` | Check formatting without changes |
| `mise run check` | Full CI: lint + format + test |
| `mise run run:daemon` | Run daemon in foreground |
| `mise run run:brain` | Run brain server |
| `mise run doctor` | Run diagnostic checks |
| `mise run start` / `stop` / `restart` | Manage launchd service |

Run `mise tasks` for the full list.

## Configuration

Config lives at `~/.config/hippo/config.toml`. See [`config/`](config/) for defaults.

Key settings:

```toml
[lmstudio]
base_url = "http://localhost:1234/v1"

[models]
enrichment = ""   # Set to your preferred LM Studio model
query = ""
embedding = ""

[daemon]
flush_interval_ms = 100
flush_batch_size = 50

[brain]
port = 9175
poll_interval_secs = 5
```

Secret redaction patterns are configured in `~/.config/hippo/redact.toml`. See [`config/redact.default.toml`](config/redact.default.toml).

## Project Structure

```
├── crates/
│   ├── hippo-core/       # Shared library (types, config, storage, redaction)
│   └── hippo-daemon/     # Binary (daemon + CLI)
├── brain/                # Python enrichment + query server
├── shell/                # zsh hooks (preexec/precmd integration)
├── config/               # Default config templates
├── launchd/              # macOS LaunchAgent plist templates
└── docs/                 # Research and design docs
```

## Data Storage

| Store | Path | Purpose |
|---|---|---|
| SQLite | `~/.local/share/hippo/hippo.db` | Events, sessions, enrichment queue |
| LanceDB | `~/.local/share/hippo/lancedb/` | Vector embeddings for semantic search |
| Config | `~/.config/hippo/config.toml` | User configuration |
| Logs | `~/.local/share/hippo/*.log` | Daemon and brain logs |

## License

MIT
