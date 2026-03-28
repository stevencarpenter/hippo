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

## Commands

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

Two processes share a SQLite database at ~/.local/share/hippo/hippo.db:

1. hippo-daemon (Rust) - captures shell events via Unix socket, redacts secrets, writes to SQLite, serves CLI queries
2. hippo-brain (Python) - polls enrichment queue from SQLite, calls LM Studio API, writes knowledge nodes + embeddings
   to LanceDB

Communication:

- Shell hook to daemon: fire-and-forget via Unix socket (length-prefixed JSON)
- CLI to daemon: request/response via same Unix socket
- hippo query (non-raw) to brain: HTTP request to brain local server
- Brain to SQLite: direct read/write (WAL mode, busy_timeout=5000)

## Style

- Rust: edition 2024, clippy clean, thiserror for lib errors, anyhow for bin errors
- Python: 3.13+, ruff for lint+format, uv for package management
- All timestamps: Unix epoch milliseconds (i64/INTEGER)
- SQLite: WAL mode, PRAGMA foreign_keys=ON, PRAGMA busy_timeout=5000 on every connection
