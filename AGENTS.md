# AGENTS.md

`CLAUDE.md` is a symlink to this file. Edit `AGENTS.md`.

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
- `extension/firefox/` - Firefox WebExtension for browser activity capture

## Commands

All build, test, lint, and service workflows are `mise` tasks in `mise.toml` (`mise tasks` lists them):

    mise run build:all        # Build Rust and sync Python deps
    mise run test             # All tests (Rust + Python, lint, format)
    mise run run:brain        # Start the brain server in the foreground
    mise run run:daemon       # Start the daemon in the foreground

### Install / Service Management

    mise run install          # Full clean-install: build, install, configure, start, verify
    mise run start            # Start services via launchd
    mise run stop             # Stop services via launchd
    mise run restart          # Stop + start
    mise run nuke             # Kill everything (SIGKILL), preserves data
    hippo doctor              # Health check
    hippo config edit         # Edit runtime config
    hippo ask "<question>"    # RAG query: synthesized answer from knowledge base
    hippo enrichment pause|resume|status   # Operator pause of LLM enrichment (state in <data_dir>/enrichment-state.json)
    hippo eval export-bench-cases --out DIR   # Private blind-review bench cases from accepted findings and escapes (DIR outside any git checkout)
    hippo agents report [--agent A] [--since 30d] [--project P] [--model M]   # Per-agent scorecards via brain GET /agents/scorecard
    hippo backup init [--destination DIR] [--op-vault V] # Generate age key, store it in 1Password, write [backup] (default destination: Google Drive for desktop)
    hippo backup run                                      # Scheduled run (launchd, hourly); skips if a backup succeeded within 20 h
    hippo backup now [--label TEXT]                       # Checkpoint now; exits 3 when [backup] is not configured
    hippo backup status                                   # Last run, pending uploads, newest artifact per kind
    hippo backup verify [--id ID]                         # Decrypt, rebuild, and check an artifact
    hippo backup restore [--id ID | --before TIME] [--yes] [--extract DIR] [--identity REF] [--destination DIR]

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

### MCP Server

    uv run --project brain hippo-mcp    # Start MCP server (stdio transport)

The MCP server exposes 13 read-only tools: `ask`, `search_knowledge`, `search_hybrid`, `search_events`, `get_entities`, `get_context`, `agent_query`, `get_ci_status`, `get_lessons`, `get_agent_scorecard`, `list_projects`, `query_memory`, `query_memory_history`. Reference: `docs/mcp-reference.md`.
Configure in your MCP config (e.g., `~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "hippo": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--no-sync", "--project", "/path/to/home/.local/share/hippo-brain", "hippo-mcp"],
      "env": {"HIPPO_OTEL_ENABLED": "1"}
    }
  }
}
```

Point `--project` at the installed brain (`~/.local/share/hippo-brain`, written by `scripts/install.sh`), not at the git checkout, so a branch switch cannot change the MCP server and it stays the same version as the brain service. `--no-sync` matches the brain LaunchAgent and keeps the MCP launch from modifying the service environment. `hippo doctor` reports the `MCP server version` line: `[OK]` when the installed `hippo-mcp` package version equals the running brain version, `[WW]` when they differ, `[!!]` when `.venv/bin/hippo-mcp` is missing.

The MCP server is a separate process from the brain daemon; OTel instrumentation (`hippo_brain_mcp_*` metrics) is only active when `HIPPO_OTEL_ENABLED=1` is set in the spawn environment. Without this env block the MCP process emits no telemetry even if the brain daemon has OTel enabled.

The `ask` tool performs RAG: embeds the question, retrieves relevant knowledge nodes from SQLite via sqlite-vec (vec0) + FTS5 hybrid search,
synthesizes an answer via a local LLM (`models.query` in config.toml), and returns the answer with
scored source references. Requires `glow` for rendered CLI output (`brew install glow`).

Logs go to stderr. The brain exports OTel metrics and traces (`brain/src/hippo_brain/telemetry.py`) only when `HIPPO_OTEL_ENABLED=1` is set.

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
2. hippo-brain (Python) - polls enrichment queues from SQLite, calls a local OpenAI-compatible inference server (default oMLX, also tested with LM Studio), writes knowledge nodes + embeddings
   to SQLite (sqlite-vec vec0 virtual tables + FTS5). Shell, Claude, and browser sources are enriched concurrently via `asyncio.gather()`;
   embeddings run as background tasks to overlap with LLM inference.

Further LaunchAgents run the capture pollers and watchers (one plist per job in `launchd/`; the source sections below describe each), plus:

- **com.hippo.watchdog**: every 60 s, asserts the I-1..I-21 invariants against `source_health` and writes `capture_alarms` rows on violations
- **com.hippo.probe**: every 5 min, round-trips synthetic events through each capture path and records latency in `source_health.probe_lag_ms`
- **com.hippo.backup**: hourly knowledge-base backup (see below)

Communication:

- Shell hook to daemon: fire-and-forget via Unix socket (length-prefixed JSON)
- CLI to daemon: request/response via same Unix socket
- Session watchers and pollers to SQLite: direct write on their own connection; `ON CONFLICT … DO UPDATE` upserts make re-processing idempotent
- Watchdog to SQLite: direct read of `source_health`, write to `capture_alarms`; never touches the daemon socket so a wedged daemon can't silence its own alarm
- hippo query (non-raw) to brain: HTTP request to brain local server
- Brain to SQLite: direct read/write (WAL mode, busy_timeout=5000); vectors live in the same DB via sqlite-vec

The retired tmux-based session tailer and its sev1 history are archived under `docs/archive/`.

### Data Storage

| Store  | Path                            | Purpose |
|--------|---------------------------------|---------|
| SQLite | `~/.local/share/hippo/hippo.db` | Events, sessions, enrichment queues, knowledge nodes, vector embeddings, source health, capture alarms (sqlite-vec vec0 + FTS5) |
| Config | `~/.config/hippo/config.toml`   | User configuration |
| Logs   | `~/.local/share/hippo/*.log`    | Daemon, brain, watcher, watchdog, and probe logs (7-day rotation via tracing-appender) |

### Knowledge-Base Backup

`hippo backup` writes age-encrypted, verified artifacts of `hippo.db` (plus config, `enrichment-state.json`, and fallback files) to the `[backup] destination` directory. launchd `com.hippo.backup` runs `hippo backup run` hourly; weekly fulls and page-level deltas; checkpoints run before schema migrations, `gc-event-payloads`, and the known re-enrich, re-embed, and dedup operations. The private key lives only in 1Password. Watchdog I-21 and `hippo doctor` report backup health. See [`docs/backup.md`](docs/backup.md).

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

Ingestion is handled by `crates/hippo-daemon/src/watch_claude_sessions.rs`, a long-lived `notify`/FSEvents watcher that runs under launchd (`com.hippo.claude-session-watcher`, `KeepAlive=true`). It subscribes to `~/.claude/projects/**/*.jsonl`, re-runs `extract_segments` on every file growth event, and upserts each segment into the `agentic_sessions` table (`harness = 'claude-code'`) via `INSERT … ON CONFLICT (session_id, harness, segment_index) DO UPDATE SET …` so repeated processing is idempotent. Genuinely new content is re-enqueued into `agentic_enrichment_queue` (shared across all agentic sources). Per-file resume state lives in `agentic_session_offsets`.

**SessionStart hook:** `shell/claude-session-hook.sh` is a no-op. It writes a "hook invoked" line to `$DATA_DIR/session-hook-debug.log` so doctor's `check_session_hook_log` can verify hook activity, and exits 0. It does **not** spawn anything, **not** touch tmux, **not** parse the input JSON.

**Manual recovery:** `hippo ingest claude-session <path>` does a one-shot batch import (handy if the watcher is wedged or for backfilling a single file).

**Hook install:** `hippo daemon install` writes the hook entry into `~/.claude/settings.json`. `hippo doctor` verifies the hook path matches the repo.

**Schema (agentic unification):** All agentic sources (Claude Code, Codex, Cursor, opencode, Pi) write the `agentic_sessions` / `agentic_enrichment_queue` / `knowledge_node_agentic_sessions` family, keyed by a `harness` column and a `(session_id, harness, segment_index)` conflict target. There are no `claude_*` session tables. The schema also holds the read-only Claude auto-memory source tables, knowledge classification, event payload expiry, derived project identity (`docs/canonical-project-identity.md`), the agent-evaluation columns on `agentic_sessions` plus `agentic_session_facts` (the Claude watcher fills `agent`, `spawn_depth`, `parent_tool_use_id`, and `subagent_finished` from each subagent's sibling `meta.json`), and the five `eval_*` agent-evaluation tables written by `hippo ingest agent-eval <file.jsonl>` (never indexed or enriched, AP-14; read only by the brain's hourly `eval_linkage` job, which records automatic `likely-addressed` verdicts, see `docs/brain-watchdog.md`; free text cleared by `hippo gc-event-payloads`). `EXPECTED_VERSION` / `EXPECTED_SCHEMA_VERSION` are 29; `docs/schema.md` has the per-version history.

### Codex Session Ingestion

Ingestion is handled by `codex_session::poll_tick` in `crates/hippo-daemon/src/codex_session.rs`, invoked by the `hippo codex-poll` CLI command. It runs under launchd (`com.hippo.codex-session`), `StartInterval`-driven on the `[codex] poll_interval_secs` cadence (default 60 s). Each tick walks the `[codex] session_roots` directories (default: `~/.codex/sessions`, `~/.codex/archived_sessions`, `~/Library/Developer/Xcode/CodingAssistant/codex/sessions`) for `rollout-*.jsonl` files. It skips in-flight files (modified within `min_idle_secs`, default 60 s, to avoid partial reads) and skips unchanged files via an inode-keyed cursor in the `agentic_cursor` table (`source_key = codex-{inode}`; inode survives Codex's archival `mv`).

For each changed file, `extract_segments` parses the rollout into task-boundary segments, then `upsert_segment_tx` upserts each segment into the `agentic_sessions` table (`harness = 'codex'`, a single transaction per file) via `INSERT … ON CONFLICT (session_id, harness, segment_index) DO UPDATE SET …` — so re-ingest of a grown rollout updates existing rows rather than duplicating them. The `.codex/` path stored in `source_file` still records the row's origin. Genuinely new content is re-enqueued into `agentic_enrichment_queue`, which Codex shares with all agentic sources, so the brain enriches Codex segments into knowledge nodes through the same path.

**Config:** `[codex]` section in `~/.config/hippo/config.toml`. `session_roots` may be omitted — the Rust default supplies the three paths above. Set `enabled = false` to make `poll_tick` a no-op.

**Verify:**
```bash
hippo codex-poll   # one-shot ingest; exits 0
hippo doctor       # shows the agentic-session-codex line
```

**Schema:** v15 migration seeds the `agentic-session-codex` row in `source_health` (the capture-health key for this source).

**Spec:** `docs/superpowers/specs/2026-05-17-codex-ingestion-design.md`

### Cursor Session Ingestion

Ingestion is handled by `cursor_session::poll_tick` in `crates/hippo-daemon/src/cursor_session.rs`, invoked by the `hippo cursor-poll` CLI command. It runs under launchd (`com.hippo.cursor-session`), `StartInterval`-driven on the `[cursor] poll_interval_secs` cadence (default 60 s). Each tick walks the `[cursor] session_roots` directories (default: `~/.cursor/projects`) for `agent-transcripts/**/*.jsonl` files. It skips in-flight files (modified within `min_idle_secs`, default 60 s, to avoid partial reads) and skips unchanged files via an inode-keyed cursor in the `agentic_cursor` table (`source_key = cursor-agent-{inode}`).

Cursor transcripts carry no per-line timestamps, so segments are bounded by accumulated character count rather than time gaps, and are time-stamped from the file mtime. For each changed file, `extract_segments` parses the transcript into char-capped segments, then `upsert_segment_tx` upserts each segment into the `agentic_sessions` table (`harness = 'cursor'`) via `INSERT … ON CONFLICT (session_id, harness, segment_index) DO UPDATE SET …` — so re-ingest of a grown transcript updates existing rows rather than duplicating them. The `.cursor/` path stored in `source_file` still records the row's origin. Subagents are ingested as their own sessions with `is_subagent=1` and `parent_session_id` set. Genuinely new content is re-enqueued into `agentic_enrichment_queue`, which Cursor shares with all agentic sources, so the brain enriches Cursor segments into knowledge nodes through the same path.

**Config:** `[cursor]` section in `~/.config/hippo/config.toml`. `session_roots` may be omitted — the Rust default supplies `~/.cursor/projects`. Set `enabled = false` to make `poll_tick` a no-op.

**Verify:**
```bash
hippo cursor-poll   # one-shot ingest; exits 0
hippo doctor        # shows the agentic-session-cursor line
```

**Schema:** v16 migration seeds the `agentic-session-cursor` row in `source_health` (the capture-health key for this source).

**Manual recovery:** `hippo ingest cursor-session <path>` does a one-shot batch import (handy if the poller is wedged or for backfilling a single file).

**Spec:** `docs/superpowers/specs/2026-05-25-cursor-ingestion-design.md`

### Pi Session Ingestion

Ingestion is handled by `pi_session::poll_tick` in `crates/hippo-daemon/src/pi_session.rs`, invoked by `hippo pi-poll` under launchd (`com.hippo.pi-session`). It walks `~/.pi/agent/sessions/<slug>/` for main sessions, subagent runs, and reviewer transcripts, and upserts segments into `agentic_sessions` with `harness = 'pi'`. The `source_health` key is `agentic-session-pi`.

### Capture Reliability

Reference docs live in [`docs/capture/`](docs/capture/architecture.md); historical design records are in [`docs/archive/capture-reliability-overhaul/`](docs/archive/capture-reliability-overhaul/). Key pieces:

- **`source_health` table**: single SQL ground truth of "did the event land?" per source (non-exhaustive: `shell`, `claude-tool`, `agentic-session-claude`, `agentic-session-opencode`, `agentic-session-codex`, `agentic-session-cursor`, `browser`, `claude-session-watcher`, `watchdog`, `brain-preflight`). Every capture path writes its row in the same transaction as the event insert. (There is no `probe` row — the probe job writes `probe_*` columns onto each real source's row.) See [`docs/capture/architecture.md`](docs/capture/architecture.md).
- **`hippo watchdog run`** (launchd `com.hippo.watchdog`, every 60 s): asserts the I-1..I-21 invariants against `source_health`, writes `capture_alarms` rows on violations, rate-limited per invariant. See [`docs/capture/architecture.md`](docs/capture/architecture.md).
- **`hippo alarms list / ack`**: CLI for unacknowledged alarms (exit 1 if any).
- **`hippo doctor`**: isolated checks with `[OK]`/`[WW]`/`[!!]`/`[--]` severity, exit code = fail count, total wall-clock < 2 s. `--explain` prints CAUSE/FIX/DOC per failure. See [`docs/capture/operator-runbook.md`](docs/capture/operator-runbook.md).
- **`hippo probe`** (launchd `com.hippo.probe`, every 5 min): synthetic canary events round-trip through each capture path; latency recorded in `source_health.probe_lag_ms`. All probe rows carry a `probe_tag` and are filtered out of every user-facing query (RAG, MCP tools, `hippo ask`, etc.) by upstream daemon filtering and a Semgrep rule. See [`docs/capture/architecture.md`](docs/capture/architecture.md).
- **Anti-patterns** are codified in [`docs/capture/anti-patterns.md`](docs/capture/anti-patterns.md) — review blockers (e.g., AP-1: don't block the shell hook on health writes; AP-6: don't let probes appear in user-facing queries).
- **Per-source coverage** is mapped in [`docs/capture/sources.md`](docs/capture/sources.md); the test-matrix table that backs it lives in [`docs/capture/test-matrix.md`](docs/capture/test-matrix.md).

## Code Exploration

Use codebase-memory (`mcp__codebase-memory-mcp__*`) first for symbol lookup, callers, and change impact in Rust and Python source: `search_graph` to find a symbol, `trace_path` for callers and callees. Codegraph misses calls written as qualified paths (`crate::claude_session::ingest_session_file`), so do not rely on `codegraph_callers` for completeness. Use `codegraph_explore` to read the source of an area in one call. Use `rg` and `Read` for artifacts the graphs do not index: `.plist`, TOML, YAML, SQL, and shell scripts.

## Style

- Rust: edition 2024, clippy clean, anyhow for errors, favor immutability and functional combinators
- Python: 3.15+, ruff for lint+format, uv for package management
- All timestamps: Unix epoch milliseconds (i64/INTEGER)
- SQLite: WAL mode, PRAGMA foreign_keys=ON, PRAGMA busy_timeout=5000 on every connection
- Vectors: 768d embeddings in the `knowledge_vectors` vec0 virtual table (`brain/src/hippo_brain/vector_store.py`); retrieval and RAG are in `retrieval.py`, `rag.py`, and `mcp.py`

## Observability / OTel

### Metric naming (OTel → Prometheus)

The OTel → Prometheus exporter appends a unit suffix to every instrument name:

| OTel unit | Prometheus suffix appended |
|---|---|
| `ms` | `_milliseconds` |
| `By` | `_bytes` |
| counter (any unit) | `_total` |
| `1` | `_ratio` ← avoid this |

**Do not use `unit="1"` for scores or raw counts.** Unit `"1"` produces a misleading `_ratio` suffix on the Prometheus side (e.g., a 0–100 health score or an alarm count would become `hippo_daemon_health_grade_ratio`, not `hippo_daemon_health_grade`). Use an explicit descriptive unit or omit the unit entirely.

For dashboard metric contracts and runtime drift checks, see [`docs/observability.md`](docs/observability.md#dashboards).

### Bench results

Bench results are surfaced via the self-contained HTML dashboard produced by `hippo-bench export-dashboard` (backed by the SQLite results datastore). There are no Grafana bench dashboards. Use `hippo-bench export-dashboard` to view leaderboard, per-run, and per-model results.
