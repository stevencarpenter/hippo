# AGENTS.md

## What This Is

Hippo - a local knowledge capture daemon for macOS. Rust daemon captures shell activity, Python brain enriches it via
local LLMs.

## Project Structure

- `crates/hippo-core/` - shared Rust library (types, config, storage, redaction)
- `crates/hippo-daemon/` - Rust binary (daemon + CLI)
- `brain/` - Python project (enrichment, embeddings, query server, sqlite-vec retrieval)
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

Two long-lived processes share a single SQLite database at `~/.local/share/hippo/hippo.db`:

1. **hippo-daemon** (Rust) — captures events via Unix socket and Native Messaging, redacts secrets, writes to SQLite, serves CLI queries
2. **hippo-brain** (Python) — polls enrichment queues from SQLite, calls LM Studio API, writes knowledge nodes + vector embeddings to SQLite via sqlite-vec (vec0 virtual tables + FTS5)

Three additional LaunchAgents support capture reliability and Claude session ingestion:

3. **com.hippo.claude-session-watcher** — `notify`/FSEvents watcher on `~/.claude/projects/**/*.jsonl`; ingests Claude Code sessions into `claude_sessions` (`crates/hippo-daemon/src/watch_claude_sessions.rs`)
4. **com.hippo.watchdog** — runs every 60 s, asserts I-1..I-10 invariants against `source_health`, writes `capture_alarms` rows on violations
5. **com.hippo.probe** — runs every 5 min, round-trips synthetic events through each capture path, records latency in `source_health.probe_lag_ms`

Communication:

- Shell hook to daemon: fire-and-forget via Unix socket (length-prefixed JSON)
- CLI to daemon: request/response via same Unix socket
- Watcher to SQLite: direct write via `claude_session::ingest_session_file` (own connection, `INSERT OR IGNORE` makes re-processing idempotent)
- Watchdog to SQLite: direct read of `source_health`, write to `capture_alarms`; never touches the daemon socket so a wedged daemon can't silence its own alarm
- `hippo query` (non-raw) to brain: HTTP request to brain local server
- Brain to SQLite: direct read/write (WAL mode, busy_timeout=5000); vectors live in the same DB via sqlite-vec

The capture-reliability stack (P0–P3, shipped 2026-04-24 → 2026-04-26) is documented in `docs/capture-reliability/`. The retired tmux-based session tailer and its sev1 history are archived under `docs/archive/`.

## Data Storage

| Store  | Path                            | Purpose                                                              |
|--------|---------------------------------|----------------------------------------------------------------------|
| SQLite | `~/.local/share/hippo/hippo.db` | Events, sessions, enrichment queue, knowledge nodes, vector embeddings, source health, capture alarms (sqlite-vec vec0 + FTS5) |
| Config | `~/.config/hippo/config.toml`   | User configuration                                                   |
| Logs   | `~/.local/share/hippo/*.log`    | Daemon, brain, watcher, watchdog, and probe logs (7-day rotation via tracing-appender) |

## Style

- Rust: edition 2024, clippy clean, anyhow for errors
- Python: 3.14+ required, ruff for lint+format, uv for package management
- All timestamps: Unix epoch milliseconds (i64/INTEGER)
- SQLite: WAL mode, PRAGMA foreign_keys=ON, PRAGMA busy_timeout=5000 on every connection
- Vectors: 768d embeddings in `knowledge_vectors` vec0 virtual table (sqlite-vec); see brain/src/hippo_brain/vector_store.py
- Semantic search and RAG pipeline are live — see rag.py, retrieval.py, mcp.py, and the /ask endpoint
