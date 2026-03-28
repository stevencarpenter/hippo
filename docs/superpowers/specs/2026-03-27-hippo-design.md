# Hippo — Design Specification

**Date:** 2026-03-27
**Status:** Approved for implementation

---

## Vision

Hippo is a local-first, privacy-preserving knowledge capture daemon for macOS. It observes developer activity in
real-time, builds a linked knowledge graph, and surfaces it through a fast CLI. Shell is the first capture source; the
architecture is designed to grow to cover IDE activity, filesystem events, browser activity, and any other producer.

Long-term vision: a fully local second brain — automated knowledge base, documentation, and retrieval — all on-device,
no cloud dependency.

**Name:** Hippo — loveable until angry, and shorthand for hippocampus.

---

## Constraints

- macOS only (forever)
- All compute runs locally — no cloud APIs
- Integrates with LM Studio (OpenAI-compatible `/v1` API)
- Lightweight background daemon — no perceptible shell impact
- Target hardware: M5 Max 128GB

---

## Architecture Overview

Two processes, one system:

```
hippo-daemon (Rust)                    hippo-brain (Python)
──────────────────────────────         ────────────────────────────
Shell hook → Unix socket               Polls enrichment queue
→ Redaction engine                     → Calls LM Studio API
→ SQLite writer (raw events)           → Generates embeddings
→ Enrichment queue                     → Synthesizes knowledge nodes
→ JSONL fallback on SQLite failure     → Updates knowledge graph
→ CLI socket handler
```

The daemon owns the hot path: capture, redact, store. The brain owns the slow path: enrich, embed, synthesize. They
share a SQLite database.

**Future producers** (FSEvents, IDE webhook, browser) add a new `EventPayload` variant. The storage and enrichment
pipeline is unchanged.

---

## Components

### hippo-daemon (Rust)

- **Shell hook** — zsh/bash `preexec`/`precmd` hooks in `~/.config/hippo/hippo.zsh`. Captures command, exit code, cwd,
  timing, git state. Fire-and-forget via background CLI call. Never blocks prompt.
- **Redaction engine** — compiled `RegexSet` from `~/.config/hippo/redact.toml`. Runs in-daemon before any data hits
  disk. ENV capture uses an explicit allowlist (~20 safe vars). Stores `redaction_count` per event; never stores what
  was redacted.
- **Event bus** — tokio async. Typed `EventEnvelope` with `EventPayload` enum (`Shell`, `FsChange`, `IdeAction`, `Raw`).
  Length-prefixed framing (4-byte u32 + JSON body) via `tokio-util::LengthDelimitedCodec`.
- **Storage writer** — batched SQLite writes (mpsc channel + 100ms flush timer). WAL mode. Falls back to JSONL on SQLite
  failure.
- **CLI handler** — same binary, subcommands via `clap`. Communicates with running daemon via Unix socket.

### hippo-brain (Python)

- **Enrichment worker** — polls enrichment queue, groups events into 30-second session bursts, calls LM Studio for
  entity extraction and knowledge node synthesis. Yields if a user query is in flight.
- **Embedding pipeline** — calls LM Studio (`/v1/embeddings`). Two surfaces per node: command signature (small model,
  384d) and knowledge summary (quality model, 2560d). Writes to LanceDB.
- **Graph synthesizer** — extracts entities and relationships, writes to SQLite junction tables.
- **Training exporter** — queries knowledge graph, formats high-quality session pairs as JSONL for `mlx_lm.lora`.
  Filters low-quality sessions (confused iteration, short duration, empty entities, any detected secrets).

### Storage Layer

- **SQLite** — `~/.local/share/hippo/hippo.db`. WAL mode. Single file, inspectable with any SQLite client.
- **LanceDB** — `~/.local/share/hippo/vectors/`. Two vector columns: `vec_command` (384d) and `vec_knowledge` (2560d).
- **JSONL fallback** — `~/.local/share/hippo/events/YYYY-MM-DD.jsonl`. Written when SQLite unavailable. Re-imported on
  daemon startup.

---

## Data Model

### SQLite Schema

All timestamps are INTEGER Unix epoch milliseconds throughout. WAL mode and foreign keys are set on every connection
open.

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE sessions (
    id          INTEGER PRIMARY KEY,
    start_time  INTEGER NOT NULL,
    end_time    INTEGER,
    terminal    TEXT,
    shell       TEXT    NOT NULL,
    hostname    TEXT    NOT NULL,
    username    TEXT    NOT NULL,
    summary     TEXT,
    created_at  INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
);

CREATE TABLE env_snapshots (
    id           INTEGER PRIMARY KEY,
    content_hash TEXT    NOT NULL UNIQUE,
    env_json     TEXT    NOT NULL,
    created_at   INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
);

CREATE TABLE events (
    id               INTEGER PRIMARY KEY,
    session_id       INTEGER NOT NULL REFERENCES sessions(id),
    timestamp        INTEGER NOT NULL,
    command          TEXT    NOT NULL,
    stdout           TEXT,
    stderr           TEXT,
    stdout_truncated INTEGER DEFAULT 0,
    stderr_truncated INTEGER DEFAULT 0,
    exit_code        INTEGER,
    duration_ms      INTEGER NOT NULL,
    cwd              TEXT    NOT NULL,
    hostname         TEXT    NOT NULL,
    shell            TEXT    NOT NULL,
    git_repo         TEXT,
    git_branch       TEXT,
    git_commit       TEXT,
    git_dirty        INTEGER,
    env_snapshot_id  INTEGER REFERENCES env_snapshots(id),
    enriched         INTEGER NOT NULL DEFAULT 0,
    redaction_count  INTEGER NOT NULL DEFAULT 0,
    archived_at      INTEGER,                      -- set when event is archived; NULL = active
    created_at       INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
);

CREATE TABLE entities (
    id          INTEGER PRIMARY KEY,
    type        TEXT    NOT NULL CHECK (type IN (
                    'project','file','tool','service','repo','host','person','concept'
                )),
    name        TEXT    NOT NULL,
    canonical   TEXT,
    metadata    TEXT,
    first_seen  INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    last_seen   INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    created_at  INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    UNIQUE (type, canonical)
);

CREATE TABLE relationships (
    id             INTEGER PRIMARY KEY,
    from_entity_id INTEGER NOT NULL REFERENCES entities(id),
    to_entity_id   INTEGER NOT NULL REFERENCES entities(id),
    relationship   TEXT    NOT NULL,
    weight         REAL    NOT NULL DEFAULT 1.0,
    evidence_count INTEGER NOT NULL DEFAULT 1,
    first_seen     INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    last_seen      INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    UNIQUE (from_entity_id, to_entity_id, relationship)
);

CREATE TABLE event_entities (
    id         INTEGER PRIMARY KEY,
    event_id   INTEGER NOT NULL REFERENCES events(id),
    entity_id  INTEGER NOT NULL REFERENCES entities(id),
    role       TEXT    NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    UNIQUE (event_id, entity_id, role)
);

CREATE TABLE knowledge_nodes (
    id                  INTEGER PRIMARY KEY,
    uuid                TEXT    NOT NULL UNIQUE,
    content             TEXT    NOT NULL,
    embed_text          TEXT    NOT NULL,
    node_type           TEXT    NOT NULL DEFAULT 'observation',
    outcome             TEXT,
    tags                TEXT,
    enrichment_model    TEXT,
    enrichment_version  INTEGER NOT NULL DEFAULT 1,
    created_at          INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    updated_at          INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
);

CREATE TABLE knowledge_node_entities (
    knowledge_node_id INTEGER NOT NULL REFERENCES knowledge_nodes(id),
    entity_id         INTEGER NOT NULL REFERENCES entities(id),
    PRIMARY KEY (knowledge_node_id, entity_id)
);

CREATE TABLE knowledge_node_events (
    knowledge_node_id INTEGER NOT NULL REFERENCES knowledge_nodes(id),
    event_id          INTEGER NOT NULL REFERENCES events(id),
    PRIMARY KEY (knowledge_node_id, event_id)
);

CREATE TABLE enrichment_queue (
    id            INTEGER PRIMARY KEY,
    event_id      INTEGER NOT NULL UNIQUE REFERENCES events(id),
    status        TEXT    NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','processing','done','failed','skipped')),
    priority      INTEGER NOT NULL DEFAULT 5,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    max_retries   INTEGER NOT NULL DEFAULT 3,
    error_message TEXT,
    locked_at     INTEGER,
    locked_by     TEXT,
    created_at    INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    updated_at    INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
);

CREATE INDEX idx_events_session        ON events (session_id);
CREATE INDEX idx_events_timestamp      ON events (timestamp DESC);
CREATE INDEX idx_events_git_repo       ON events (git_repo) WHERE git_repo IS NOT NULL;
CREATE INDEX idx_events_enriched       ON events (enriched) WHERE enriched = 0;
CREATE INDEX idx_entities_type_name    ON entities (type, name);
CREATE INDEX idx_entities_canonical    ON entities (canonical) WHERE canonical IS NOT NULL;
CREATE INDEX idx_relationships_from    ON relationships (from_entity_id, relationship);
CREATE INDEX idx_relationships_to      ON relationships (to_entity_id, relationship);
CREATE INDEX idx_event_entities_entity ON event_entities (entity_id, event_id);
CREATE INDEX idx_kn_entities_entity    ON knowledge_node_entities (entity_id);
CREATE INDEX idx_queue_pending         ON enrichment_queue (status, priority) WHERE status = 'pending';
```

### LanceDB Schema

```python
schema = pa.schema([
    pa.field("id",                 pa.string()),           # = knowledge_nodes.uuid
    pa.field("session_id",         pa.string()),
    pa.field("captured_at",        pa.timestamp("us", tz="UTC")),
    pa.field("commands_raw",       pa.string()),           # JSON array
    pa.field("cwd",                pa.string()),
    pa.field("git_branch",         pa.string()),
    pa.field("git_repo",           pa.string()),
    pa.field("outcome",            pa.string()),
    pa.field("tags",               pa.list_(pa.string())),
    pa.field("entities_json",      pa.string()),
    pa.field("embed_text",         pa.string()),
    pa.field("summary",            pa.string()),
    pa.field("vec_knowledge",      pa.list_(pa.float32(), 2560)),
    pa.field("vec_command",        pa.list_(pa.float32(), 384)),
    pa.field("enriched",           pa.bool_()),
    pa.field("enrichment_model",   pa.string()),
    pa.field("enrichment_version", pa.int32()),
])
```

Notes:

- Do not create IVF_PQ index until table has 5000+ rows
- Create FTS index explicitly on `embed_text` and `summary` (string columns only — not the `tags` list field)
- Run `table.compact_files()` periodically in a maintenance job
- `vec_command` uses the small embedding model (384d); `vec_knowledge` uses the quality model (2560d)
- **Dimension lock:** LanceDB enforces exact dimension match at insert time. If the embedding model in config changes,
  existing records cannot be updated in-place — use `enrichment_version` to track and re-embed. Document this
  prominently in the config.

---

## Capture Pipeline

### Shell Hook (Tier 1, v1)

Command metadata only. Stdout/stderr capture is a future opt-in Tier 2 feature via a `h <cmd>` PTY wrapper.

`HIPPO_SESSION_ID` is generated once at login (sourced from `~/.config/hippo/hippo-env.zsh` via `zshenv`) and exported.
Survives subshells.

Git state is cached per-cwd and refreshed only when the directory changes or 5 seconds elapse. Commands in large repos
will not cause prompt lag.

The CLI call is always backgrounded and disowned. `SO_SNDTIMEO` is set to 100ms on the socket. If the daemon is not
running, `connect()` fails immediately and the CLI exits silently.

### Enrichment Prompt

```
System: You are a developer activity analyst. Extract structured knowledge
        from shell session data. Output ONLY valid JSON.

User:   Time: {timestamp}
        Directory: {cwd}
        Git: {git_branch} @ {git_commit}
        Duration: {duration_seconds}s

        Commands (with exit codes):
        {commands_list}

        Key output (truncated to 2KB):
        {truncated_output}

        Extract:
        - summary (1-2 sentences, past tense)
        - intent
        - outcome: success | failure | partial | exploratory
        - entities: {projects, tools, files, services, errors}
        - relationships: ["entity-A verb entity-B"]
        - tags: [3-6 items]
        - embed_text: 150-250 word retrieval-optimized prose
```

`embed_text` is written by the LLM for retrieval, not display. This is the field that gets embedded into LanceDB.

### Query Pipeline

```
User query
  → Query expansion via LLM (cheap call, adds technical synonyms)
  → Embed expanded query with quality model
  → LanceDB: keyword pre-filter on tags + entities (FTS)
  → Vector similarity search on filtered set (top-20)
  → Metadata time filter
  → Return top-5 to LLM for synthesis
  → hippo query output
```

---

## Error Handling & Resilience

**Capture is sacrosanct. The intelligence layer is optional.**

### Storage Fallback

```
Normal:    event → redaction → SQLite + enrichment queue
Fallback:  SQLite unavailable → YYYY-MM-DD.jsonl
Recovery:  on startup, re-import unprocessed .jsonl files → SQLite
```

JSONL files are structurally identical to SQLite event rows. No capture-path event is silently lost. Only the Unix
socket send (100ms timeout, background process) can drop — unavoidable tradeoff for shell responsiveness.

### Enrichment Degradation

- LM Studio unavailable → queue accumulates, brain retries with exponential backoff (1s → 2s → 4s, cap 5min), max 3
  retries then `failed`
- LM Studio busy → enrichment yields, retries in 5s
- Queue depth > 100 → pause enrichment, log warning, keep capturing

### Query Degradation

- LM Studio offline → falls back to keyword/SQLite-only search with clear notice
- LanceDB index missing → flat vector scan
- Model not loaded → clear error with model name

---

## Models & Configuration

Model selection is fully user-controlled. Hippo never loads or manages models — that is LM Studio's responsibility. Any
model available in LM Studio can be configured.

```toml
# ~/.config/hippo/config.toml

[lmstudio]
base_url = "http://localhost:1234/v1"

[models]
enrichment = "lmstudio-community/LFM2-24B-A2B-MLX-8bit"
query      = "lmstudio-community/Qwen3-Coder-Next-MLX-6bit"
embedding  = ""  # required — set to whichever embedding model is loaded in LM Studio
                 # hippo will refuse to start enrichment if this is empty
```

**Default recommended stack (M5 Max 128GB):**

- Enrichment: LFM2-24B-A2B-MLX-8bit (~25.3GB, 2B active — always loaded)
- Query/synthesis: Qwen3-Coder-Next-MLX-6bit (~64.8GB — on demand, LM Studio auto-evicts)
- These cannot coexist; LM Studio handles the swap via auto-evict + JIT TTL

Swap models at runtime:

```
hippo config set models.enrichment "lmstudio-community/GLM-4.7-Flash-MLX-8bit"
```

---

## CLI Interface

```
# Daemon
hippo daemon start | stop | restart
hippo status
hippo doctor

# Sessions & events
hippo sessions [--today] [--since <duration>]
hippo events [--session <id>] [--since <duration>] [--project <name>]

# Query
hippo query "<natural language>"
hippo query --raw "<term>"

# Knowledge graph
hippo entities [--type project|file|tool|service|repo]
hippo graph show <entity>
hippo graph relate "<entity-a>" "<entity-b>"

# Fine-tuning export
hippo export-training [--out <dir>] [--since <duration>] [--min-quality <0-1>]

# Config
hippo config edit
hippo config set <key> <value>
hippo redact test "<string>"
```

---

## Daemon Management

Runs as a launchd `LaunchAgent` (user session, not root). `KeepAlive: true` so launchd restarts on crash — brief
restarts are invisible to the shell hook.

- Socket: `~/.local/share/hippo/daemon.sock`
- Database: `~/.local/share/hippo/hippo.db`
- Logs: `~/.local/share/hippo/hippo.log` (10MB rotation)
- Config: `~/.config/hippo/`
- JSONL fallback: `~/.local/share/hippo/events/`

---

## Rust Crate Stack

| Crate                            | Purpose                                                      |
|----------------------------------|--------------------------------------------------------------|
| `tokio` (full)                   | Async runtime                                                |
| `rusqlite` (bundled)             | SQLite — bundled avoids macOS system SQLite version issues   |
| `tokio-util`                     | `LengthDelimitedCodec` for Unix socket framing               |
| `serde` + `serde_json`           | Serialization                                                |
| `uuid`                           | Event/session IDs                                            |
| `chrono`                         | Timestamps — always `DateTime<Utc>`, display-only conversion |
| `clap` (derive)                  | CLI parsing + subcommand dispatch                            |
| `regex`                          | `RegexSet` for redaction, compiled once at startup           |
| `toml`                           | Config parsing                                               |
| `tracing` + `tracing-subscriber` | Structured logging to file                                   |
| `thiserror` / `anyhow`           | Typed errors (lib) / contextual errors (CLI)                 |
| `notify`                         | Future: FSEvents watcher                                     |

---

## Testing Approach

- **Unit**: redaction engine (pattern coverage, false positives), event serialization, config parsing
- **Integration**: daemon + SQLite write path, enrichment queue claim/release, JSONL fallback + recovery
- **No SQLite mocking** — tests use real in-memory or temp-file databases
- **Shell hook**: manual testing via test zsh session; no automated shell tests in v1

---

## Out of Scope for v1

- TUI (CLI-first; TUI is the next layer)
- Stdout/stderr capture (Tier 2, opt-in `h <cmd>` wrapper)
- IDE integration
- Multi-machine sync
- Automated fine-tuning scheduling
- Web UI or remote access

---

## Future Capture Sources

Each adds a new `EventPayload` variant. Core storage and enrichment pipeline unchanged.

| Source        | Mechanism                               |
|---------------|-----------------------------------------|
| Stdout/stderr | Opt-in PTY wrapper `h <cmd>`            |
| Filesystem    | FSEvents watcher via `notify` crate     |
| JetBrains IDE | Plugin posting to daemon HTTP endpoint  |
| Browser       | Native messaging extension              |
| Generic apps  | Accessibility API or app-specific hooks |
