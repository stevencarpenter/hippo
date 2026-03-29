# AGENTS.md

## What This Is

Hippo - a local knowledge capture daemon for macOS. Rust daemon captures shell activity, Python brain enriches it via
local LLMs.

## Project Structure

- `crates/hippo-core/` - shared Rust library (types, config, storage, redaction)
- `crates/hippo-daemon/` - Rust binary (daemon + CLI)
- `brain/` - Python project (enrichment, embeddings, query server, LanceDB integration)
- `shell/` - zsh hook scripts
- `config/` - default config templates
- `launchd/` - LaunchAgent plists

## Commands

### Canonical Workflow: mise

All build, test, lint, and service management workflows are defined in `mise.toml` at the project root. Use `mise run <task>` for all common operations. Examples:

mise run build:all         # Build Rust and sync Python deps
mise run test              # Run all tests (Rust + Python, lint, format)
mise run run:brain         # Start the brain server
mise run run:daemon        # Start the daemon in foreground
mise run doctor            # Run diagnostic checks
mise run nuke              # Force stop all Hippo processes and clean up

See `mise.toml` for the full list of tasks.

### Rust (daemon + CLI)

cargo build
cargo test
cargo test -p hippo-core
cargo test -p hippo-daemon
cargo clippy --all-targets -- -D warnings
cargo fmt --check
cargo run --bin hippo -- daemon run
cargo run --bin hippo -- status

### Python (brain)

uv sync --project brain
uv run --project brain pytest brain/tests -v
uv run --project brain ruff check brain/
uv run --project brain ruff format --check brain/
uv run --project brain hippo-brain serve

## Architecture

Two processes share a SQLite database at ~/.local/share/hippo/hippo.db and a LanceDB vector store at ~/.local/share/hippo/lancedb/:

1. hippo-daemon (Rust) - captures shell events via Unix socket, redacts secrets, writes to SQLite, serves CLI queries
2. hippo-brain (Python) - polls enrichment queue from SQLite, calls LM Studio API, writes knowledge nodes + vector embeddings to LanceDB

Communication:

- Shell hook to daemon: fire-and-forget via Unix socket (length-prefixed JSON)
- CLI to daemon: request/response via same Unix socket
- hippo query (non-raw) to brain: HTTP request to brain local server
- Brain to SQLite: direct read/write (WAL mode, busy_timeout=5000)
- Brain to LanceDB: direct read/write for vector embeddings (semantic search pipeline implemented, not yet wired to /query)

## Data Storage

| Store   | Path                            | Purpose                               |
|---------|---------------------------------|---------------------------------------|
| SQLite  | `~/.local/share/hippo/hippo.db` | Events, sessions, enrichment queue    |
| LanceDB | `~/.local/share/hippo/lancedb/` | Vector embeddings for semantic search |
| Config  | `~/.config/hippo/config.toml`   | User configuration                    |
| Logs    | `~/.local/share/hippo/*.log`    | Daemon and brain logs                 |

## Style

- Rust: edition 2024, clippy clean, thiserror for lib errors, anyhow for bin errors
- Python: 3.13+ required, ruff for lint+format, uv for package management
- All timestamps: Unix epoch milliseconds (i64/INTEGER)
- SQLite: WAL mode, PRAGMA foreign_keys=ON, PRAGMA busy_timeout=5000 on every connection
- LanceDB: vector embeddings (2560d/384d) stored at ~/.local/share/hippo/lancedb/; see brain/src/hippo_brain/embeddings.py
- Semantic search pipeline is implemented (see embeddings.py, test_embeddings.py), but /query endpoint currently performs lexical search only
