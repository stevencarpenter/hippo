# Hippo

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![macOS](https://img.shields.io/badge/platform-macOS_arm64-lightgrey.svg)
[![Rust](https://img.shields.io/badge/rust-edition_2024-orange.svg)](https://blog.rust-lang.org/2025/02/20/Rust-1.85.0/)
[![Python](https://img.shields.io/badge/python-3.14%2B-blue.svg)](https://www.python.org/)

Local-first knowledge capture daemon for macOS. Hippo watches your shell activity, Claude Code sessions, and Firefox browsing, redacts known secret formats, enriches events with a local LLM, and builds a searchable second brain — all without sending data to third-party services. LLM inference runs through LM Studio on your machine; telemetry is off by default and points at localhost when on. See [Privacy and Security](#privacy-and-security) for the full data-flow story.

## Why hippo

Hippo solves a problem that shell history (`~/.zsh_history`, fish history, Atuin) and consumer "memory" tools (Rewind, Apple Continuity) don't:

- **Cross-source recall.** Your shell command from Tuesday, the Claude Code conversation that produced it, and the StackOverflow tab you had open are all linked into one searchable knowledge graph.
- **Semantic + lexical retrieval.** Ask `hippo ask "how did I fix that build error?"` and get a synthesized answer with cited sources, not a `grep` over command strings.
- **Local-only by default.** Your shell stdout, conversation transcripts, and browsing history never leave your machine. The LLM that summarizes them is local too.
- **MCP-native.** Claude Code can query your knowledge base mid-conversation through the MCP server, so the model can look up "what was I just doing" without you re-explaining.

If you want a faster `^R`, use Atuin. If you want hippo's setup, keep reading.

## Quick Install (Apple Silicon)

```bash
curl -fsSL https://github.com/stevencarpenter/hippo/releases/latest/download/install.sh | bash
```

The release binaries are **arm64-only**. Intel Macs need to build from source — see [Manual Installation](#manual-installation).

The installer downloads the daemon, brain, and GUI app with checksum verification, writes LaunchAgent plists, and prints a `hippo doctor` summary on completion. See the install script source at the link above for what it touches.

## Verify it's working

After install, generate ~5 minutes of shell + browser activity, then:

```bash
hippo doctor                                # green across the board?
hippo events --since 30m                    # are events landing?
hippo ask "what have I been working on"     # synthesized answer + cited sources
```

If `hippo events` returns nothing, see [Troubleshooting](#troubleshooting) below — the most common cause is the shell hook not being sourced.

## Architecture

```
+-----------+                  +--------------+                +--------------+
|  zsh      |  Unix socket     |              |  SQLite (WAL)  |              |
|  shell    | ---------------> |              | <------------> | hippo-brain  |
+-----------+                  |              |                | (Python)     |
+-----------+  FSEvents        | hippo-daemon |                +------+-------+
|  Claude   |  watcher         | (Rust)       |                       |
|  Code     | ---------------> |              |                +------+-------+
+-----------+                  |              |                |  hippo-mcp   |
+-----------+  Native Msg      |              |                | (MCP server) |
|  Firefox  | ---------------> |              |                +--------------+
|  ext.     |                  |              |                  ^         |
+-----------+                  +--------------+           stdio  |  SQLite |
                                                        (JSONL) | sqlite-vec |
                                                                |  LM API |
                                                          Claude Code / Desktop
```

| Component | Language | Role |
|-----------|----------|------|
| **hippo-daemon** | Rust | Captures events via Unix socket and Native Messaging. Applies secret redaction, stores to SQLite, serves CLI queries. |
| **hippo-brain** | Python | Polls enrichment queues, calls LM Studio for summarization, correlates browser research with shell activity, writes knowledge nodes + vector embeddings to SQLite via sqlite-vec. |
| **hippo-mcp** | Python | MCP server exposing the knowledge base over stdio. Claude Code queries your personal knowledge base mid-conversation. |

Five LaunchAgents run under `gui/$(id -u)`:

| Agent | Role |
|-------|------|
| `com.hippo.daemon` | Long-lived Rust daemon (KeepAlive) |
| `com.hippo.brain` | Long-lived Python brain server (KeepAlive) |
| `com.hippo.claude-session-watcher` | FSEvents watcher on `~/.claude/projects/**/*.jsonl` (KeepAlive); ingests Claude Code sessions |
| `com.hippo.watchdog` | Capture-reliability monitor; runs every 60 s, asserts I-1..I-10 invariants, writes `capture_alarms` rows |
| `com.hippo.probe` | Synthetic canary probes; runs every 5 min, round-trips a real event through each capture path and records latency in `source_health` |

`hippo daemon install --force` writes the plists and bootstraps all five. The capture-reliability stack (source health, invariants, watchdog, probes, alarms) is documented in [`docs/capture/`](docs/capture/) — start with [`architecture.md`](docs/capture/architecture.md). Review-blocker rules every contributor must follow live in [`docs/capture/anti-patterns.md`](docs/capture/anti-patterns.md).

## Prerequisites

| Dependency | Required | Notes |
|------------|----------|-------|
| macOS Apple Silicon (arm64) | Yes for binaries | Intel needs to build from source. Uses launchd for service management. |
| [Rust](https://rustup.rs/) | Yes | Edition 2024 (1.85+). No `rust-toolchain.toml` is shipped; CI runs against the latest stable. |
| [Python](https://www.python.org/) | Yes | 3.14+ |
| [uv](https://docs.astral.sh/uv/) | Yes | Python package manager |
| [LM Studio](https://lmstudio.ai/) | Yes | Local LLM inference. Default config targets `qwen3.6-35b-a3b-ud-mlx` (MoE, ~3 B active params). Plan on 32 GB+ unified memory for the default model; smaller models work with smaller machines — adjust the `[models]` section. |
| [mise](https://mise.jdx.dev/) | Recommended | Task runner; all workflows are defined in `mise.toml` |
| [glow](https://github.com/charmbracelet/glow) | Recommended | Renders `hippo ask` markdown output in the terminal |
| [Firefox Dev Edition](https://www.mozilla.org/en-US/firefox/developer/) | Optional | Browser activity capture (allowlisted developer domains) |

## Manual Installation

```bash
# Clone hippo and enter the repo
git clone https://github.com/stevencarpenter/hippo.git
cd hippo

# Build and install everything (release binary, LaunchAgents, config, symlink)
mise run install

# Add shell hooks to your zsh config
echo "source $(pwd)/shell/hippo-env.zsh" >> ~/.zshenv
echo "source $(pwd)/shell/hippo.zsh"     >> ~/.zshrc
exec zsh  # reload

# Configure your LM Studio model
hippo config edit
```

In the config editor, set the `[models]` section to match a model loaded in LM Studio. To list loaded models:

```bash
curl -s http://localhost:1234/v1/models | python3 -m json.tool
```

Then edit:

```toml
[models]
enrichment = "your-model-name"
enrichment_bulk = "your-model-name"
query = "your-model-name"
embedding = "text-embedding-nomic-embed-text-v2-moe"
```

Verify everything is wired up:

```bash
hippo doctor
```

If anything is red, run `hippo doctor --explain` for CAUSE / FIX / DOC per failure.

## Usage

```bash
hippo status                            # Daemon status
hippo sessions --today                  # List today's sessions
hippo events --since 2h                 # Recent shell events
hippo ask "how did I fix that build error"  # RAG query (synthesized answer)
hippo query --raw "cargo build"         # Raw lexical search (no brain needed)
hippo entities                          # Known projects, tools, files, env vars
hippo export-training --since 30d --out ./export  # Export training data
hippo redact test "password=hunter2"    # Test redaction patterns
```

Operational:

```bash
hippo doctor                            # Run diagnostic checks; non-zero exit on any [!!]
hippo doctor --explain                  # Same, with CAUSE/FIX/DOC for each failure
hippo alarms list                       # Unacknowledged capture alarms (exit 1 if any)
hippo alarms ack <id> --note "..."      # Acknowledge an alarm
hippo probe --source claude-session     # Run a synthetic probe for one source
hippo ingest claude-session <path>      # Manual one-shot import of a JSONL (recovery)
```

Capture-reliability operator runbook (recipes for "I ran a command but it's not in `hippo events`", "doctor shows red", "schema mismatch", etc.): [`docs/capture/operator-runbook.md`](docs/capture/operator-runbook.md).

## Troubleshooting

Common first-day failures, in rough order of frequency:

| Symptom | Likely cause | Fix |
|---|---|---|
| `hippo events` returns nothing after several minutes of shell activity | Shell hook not sourced — `~/.zshrc` / `~/.zshenv` weren't reloaded after install. | `exec zsh` or open a new terminal. Verify with `grep -l 'hippo.zsh' ~/.zshrc ~/.zshenv ~/.config/zsh/*.zsh 2>/dev/null`. |
| `hippo ask` returns "I don't have enough information" | Brain hasn't enriched yet, or LM Studio model isn't loaded. | `hippo doctor` to confirm. Open LM Studio and load the model in `[models].enrichment`. |
| `hippo doctor` says LM Studio isn't reachable | LM Studio app isn't running, or it's serving on a non-default port. | Open LM Studio. Check the API base URL in `~/.config/hippo/config.toml` (`[lmstudio].base_url`). |
| Daemon won't start; `hippo doctor` says schema mismatch | Daemon and brain have different `EXPECTED_SCHEMA_VERSION` constants — happens after partial upgrades. | `mise run install --clean` brings everything to the same version. |
| Brain queue backs up | LM Studio model unloaded or swapped for one not in `[models].enrichment`. | Load the configured model. The reaper handles transient locks; persistent backlog is operator-visible. See [`docs/brain-watchdog.md`](docs/brain-watchdog.md). |
| Firefox extension shows no recent visits | Native messaging manifest missing, or extension not loaded. | `hippo daemon install --force` rewrites the manifest. Reload the extension in `about:debugging`. See [`extension/firefox/README.md`](extension/firefox/README.md). |

For anything not in this table, run `hippo doctor --explain` and follow the DOC link in each failure block. The full operator runbook lives at [`docs/capture/operator-runbook.md`](docs/capture/operator-runbook.md).

## MCP Server

The MCP server lets Claude Code (or any MCP client) query your knowledge base mid-conversation.

| Tool | Description |
|------|-------------|
| `ask` | RAG query — synthesizes an answer from relevant knowledge nodes |
| `search_knowledge` | Semantic or lexical search over enriched knowledge nodes |
| `search_hybrid` | Hybrid sqlite-vec + FTS5 search with score fusion |
| `search_events` | Search raw events (shell commands, Claude sessions, browser visits) |
| `get_entities` | List extracted entities (projects, tools, files, env vars, concepts) |

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

The MCP server reads SQLite directly (vectors live in the same DB via sqlite-vec; no dependency on the brain HTTP server).

> **Trust boundary.** Granting Claude Code MCP access to hippo gives the model — and any prompt injection that reaches it through code or documents you ask Claude to read — read access to your shell history, Claude transcripts, and browser data. Grant deliberately.

## Firefox Extension (Optional)

```bash
# Build + install in one step (also called by `mise run install`)
mise run install:ext
```

This builds the extension and side-loads the `.xpi` into your Firefox Dev Edition `dev-edition-default` profile. For development / hot-reload, `extension/firefox/README.md` covers the manual `about:debugging` flow.

The native messaging host is installed by `hippo daemon install --force` (also called by `mise run install`).

## Configuration

| File | Purpose |
|------|---------|
| `~/.config/hippo/config.toml` | Runtime config (models, ports, browser allowlist, telemetry, GitHub token) |
| `~/.config/hippo/redact.toml` | Secret redaction patterns (regex-based) |

Created automatically by `mise run install`. Edit with `hippo config edit`. See [`config/config.default.toml`](config/config.default.toml) and [`config/redact.default.toml`](config/redact.default.toml) for the templates and inline documentation of each key.

The `[models]` section must be configured for enrichment to work. Set the model name to whatever LM Studio is serving — verify with `curl -s http://localhost:1234/v1/models | python3 -m json.tool`.

## Privacy and Security

Hippo captures shell commands (including stdout/stderr), Claude Code session transcripts, and browser visits from allowlisted domains. All data is stored locally in `~/.local/share/hippo/hippo.db` (SQLite, unencrypted — use macOS FileVault for full-disk encryption). No data is sent to Anthropic, OpenAI, or any cloud service.

**LLM calls are local.** Enrichment and RAG queries go to LM Studio at `http://localhost:1234/v1`. If you point LM Studio at a remote backend, your shell history and session transcripts travel that path.

**Redaction is best-effort.** Hippo redacts known secret formats (AWS keys, GitHub tokens, `password=` assignments, JWTs, PEM headers) before storage. Regex-based redaction cannot catch secrets in positional arguments, non-standard env-var names, or multi-line stdout payloads. Treat it as a noise filter, not a security guarantee. Test patterns with `hippo redact test "your candidate string"`.

**The SQLite database is accessible to any process running as your user.** Single-user assumption. Consider restricting `~/.local/share/hippo/` to `700` if you share the machine.

**MCP access is broad.** Adding hippo to Claude Code's MCP config grants the model read access to your shell history, Claude transcripts, and browser history — including via prompt injection through code or documents you ask Claude to read.

**Telemetry is off by default.** The OTel stack (`otel/`) is optional, points at `localhost:4317`, and only emits when `[telemetry] enabled = true`. See [`otel/README.md`](otel/README.md).

**Network exposure.** The brain HTTP server binds `127.0.0.1:9175`. The daemon's Unix socket lives at `~/.local/share/hippo/daemon.sock`.

**Uninstall.** Stop services with `mise run stop`, remove `~/.local/share/hippo/`, `~/.config/hippo/`, and unload the LaunchAgents (`launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.hippo.*.plist`, then delete the plists).

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
cargo build                                      # default features include `otel`
cargo build --no-default-features                # build without OTel instrumentation
cargo test --workspace --locked --no-fail-fast   # matches CI
cargo clippy --all-targets --locked -- -D warnings
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

See [`hippo-gui/README.md`](hippo-gui/README.md) for app-specific notes on Xcode, previews, package tests, and bundle builds. Note: GUI commands run from inside `hippo-gui/`, unlike the rest of the workflow which runs from the repo root.

### Service management

```bash
mise run start              # Start daemon + brain via launchd
mise run stop               # Stop both services
mise run restart            # Stop + start
mise run nuke               # SIGKILL everything (preserves data)
```

### Contributing

A dedicated `CONTRIBUTING.md` is tracked in [issue #114](https://github.com/stevencarpenter/hippo/issues/114). In the meantime: open issues for bugs, PRs welcome. Code-style notes live in `CLAUDE.md`. Review blockers for the capture layer are codified in [`docs/capture/anti-patterns.md`](docs/capture/anti-patterns.md) (AP-1 through AP-12); read them before touching `crates/hippo-daemon/src/{daemon,commands,storage}.rs` or `brain/src/hippo_brain/server.py`.

## Project Structure

```
crates/
  hippo-core/              Shared library (types, config, storage, redaction; SQL schema in src/schema.sql)
  hippo-daemon/            Binary (daemon + CLI + native messaging host)
brain/                     Python enrichment, query server, and MCP server
hippo-gui/                 SwiftUI macOS app (Swift Package)
extension/
  firefox/                 Firefox WebExtension for browser activity capture
shell/                     zsh hooks (preexec/precmd integration)
config/                    Default config templates
launchd/                   macOS LaunchAgent plist templates
scripts/                   Utility scripts (bulk enrich, re-embed, monitor, dedup-entities)
otel/                      OpenTelemetry observability stack (docker-compose)
docs/                      Live reference docs (capture/, RELEASE.md, eval-harness-design.md, brain-watchdog.md)
docs/superpowers/          In-flight feature plans + designs
docs/archive/              Historical plans, design records, post-mortems
tools/                     Developer utilities (SQL formatting)
```

## Data Storage

All paths follow XDG defaults. Override with `XDG_DATA_HOME` / `XDG_CONFIG_HOME`.

| Store | Path | Purpose |
|-------|------|---------|
| SQLite | `~/.local/share/hippo/hippo.db` | Events, sessions, browser visits, enrichment queue, knowledge nodes, entities, vector embeddings (sqlite-vec). Schema lives at [`crates/hippo-core/src/schema.sql`](crates/hippo-core/src/schema.sql). |
| Config | `~/.config/hippo/config.toml` | User configuration |
| Logs | `~/.local/share/hippo/*.log` | Daemon and brain logs |
| Fallback | `~/.local/share/hippo/*.fallback.jsonl` | Last-resort durability backstop when the daemon socket is unreachable; replayed on next daemon start |

Schema uses `PRAGMA user_version = N`. Daemon and brain handshake on this constant at startup; see [`docs/RELEASE.md`](docs/RELEASE.md) for the lockstep version contract.

## License

[MIT](LICENSE)
