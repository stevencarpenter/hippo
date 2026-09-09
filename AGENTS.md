# AGENTS.md

## What This Is

Hippo captures local developer activity on macOS. The Rust daemon and source
watchers store events; the Python brain enriches them through a local LLM.

## Project Structure

- `crates/hippo-core/` - shared Rust library (types, config, storage, redaction)
- `crates/hippo-daemon/` - Rust binary (daemon + CLI)
- `brain/` - Python project (enrichment, embeddings, query server, sqlite-vec retrieval)
- `shell/` - zsh hook scripts
- `config/` - default config templates
- `launchd/` - LaunchAgent plists
- `extension/firefox/` - browser activity capture

## Commands

Use `mise run <task>` for common build, test, lint, and service operations.
Run these commands from the repository root:

```bash
mise run build:all         # Build Rust and sync Python deps
mise run test              # Run all tests (Rust + Python, lint, format)
mise run test:core         # hippo-core tests
mise run test:daemon       # hippo-daemon unit tests
mise run test:integration  # hippo-daemon integration tests
mise run test:python       # Brain tests with coverage
mise run lint              # clippy + ruff
mise run fmt:check         # Rust + Python formatting
mise run run:brain         # Start the brain server
mise run run:daemon        # Start the daemon in foreground
mise run doctor            # Run diagnostic checks
mise run install           # Rebuild, install, configure, start, verify
mise run start             # Start services via launchd
mise run stop              # Stop services via launchd
mise run restart           # Stop + start
mise run nuke              # Force stop processes; preserve captured data
```

See [mise.toml](mise.toml) for task definitions and
[CONTRIBUTING.md](CONTRIBUTING.md) for focused tests and CI differences.

## Architecture

Processes share SQLite at `~/.local/share/hippo/hippo.db`:

- **hippo-daemon** captures shell and Native Messaging events, redacts secrets,
  stores events, and serves CLI requests over a length-prefixed JSON Unix socket.
  Shell sends are fire-and-forget; CLI queries use request/response.
- **Source watchers and pollers** write directly to SQLite. Claude Code, Codex,
  Cursor, opencode, and Pi share `agentic_sessions`, keyed by
  `(session_id, harness, segment_index)`, and `agentic_enrichment_queue`.
  The Claude FSEvents watcher is `watch_claude_sessions.rs`; repeated ingest
  upserts segments and re-enqueues changed content.
- **hippo-brain** polls enrichment queues, calls a local OpenAI-compatible
  inference server (default oMLX, also tested with LM Studio), and writes knowledge
  nodes and embeddings. Retrieval uses sqlite-vec and FTS5 in the same database.
  `hippo query` without `--raw` and `hippo ask` call the brain HTTP API.
- **hippo-mcp** serves stdio clients and reads SQLite directly.
- **Probe LaunchAgents** record round-trip results in `source_health`.
  The watchdog reads this state independently of the daemon socket and writes
  violations to `capture_alarms`, so a wedged daemon cannot silence its alarms.

See [capture architecture](docs/capture/architecture.md),
[source contracts](docs/capture/sources.md), and the
[operator runbook](docs/capture/operator-runbook.md). Capture changes must follow
the [anti-pattern rules](docs/capture/anti-patterns.md). Historical designs and
incidents remain in [docs/archive/](docs/archive/).

## Data Storage

| Store  | Path                            | Purpose                                                              |
|--------|---------------------------------|----------------------------------------------------------------------|
| SQLite | `~/.local/share/hippo/hippo.db` | Events, sessions, enrichment queue, knowledge nodes, vector embeddings, source health, capture alarms (sqlite-vec vec0 + FTS5) |
| Config | `~/.config/hippo/config.toml`   | User configuration                                                   |
| Logs   | `~/.local/share/hippo/*.log`    | Daemon, brain, watcher, watchdog, and probe logs (7-day rotation via tracing-appender) |

Rust defaults respect `XDG_DATA_HOME` and `XDG_CONFIG_HOME`. Brain and MCP read
`~/.config/hippo/config.toml`; set `[storage].data_dir` there for a custom database
location.

## Code Exploration

When codegraph MCP tools are available (`mcp__codegraph__*`), use `codegraph_explore` first for "how does X work / trace X" questions over Rust source — typically 2–5 calls vs. 30+ for grep+read. Fall back to `rg`/`Read` for non-indexed artifacts: `.plist`, YAML, Python, shell scripts.

## Style

- Rust: edition 2024, clippy clean, anyhow for errors; favor immutability and functional combinators
- Python: 3.14+ required, ruff for lint+format, uv for package management
- All timestamps: Unix epoch milliseconds (i64/INTEGER)
- SQLite: WAL mode, PRAGMA foreign_keys=ON, PRAGMA busy_timeout=5000 on every connection
- Vectors: 768d embeddings in `knowledge_vectors` vec0 virtual table (sqlite-vec); see brain/src/hippo_brain/vector_store.py
- Semantic search and RAG pipeline are live — see rag.py, retrieval.py, mcp.py, and the /ask endpoint
