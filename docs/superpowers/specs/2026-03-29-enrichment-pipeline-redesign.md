# Enrichment Pipeline Redesign

## Context

Hippo's enrichment pipeline produces low-quality knowledge nodes. The summaries are generic ("Claude Code edited a Rust file"), tags are vague ("editing", "rust", "success"), and the same description repeats across dozens of distinct events. The root causes are:

1. **Starved enrichment prompt** — only sees command, exit code, duration, cwd. No stdout/stderr.
2. **Arbitrary batching** — events are grabbed N-at-a-time regardless of session, mixing unrelated work.
3. **Semantic search not wired** — embeddings exist in LanceDB but `/query` does substring search only.
4. **Weak enrichment model** — nemotron-3-nano-4b (4B params) produces generic structured output.

Models have been updated in config (not yet deployed): `qwen/qwen3.5-35b-a3b` for enrichment, `text-embedding-nomic-embed-text-v2-moe` for embeddings (768d, matches existing `EMBED_DIM`).

## Decisions

- **Stdout/stderr capture**: Add to shell hook. First 50 + last 100 lines, configurable.
- **Session-based grouping**: Enrich by session, chunk long sessions at natural breakpoints.
- **Prompt redesign**: Richer input, more specific output schema, drop `relationships`.
- **Semantic search default**: `hippo query` does vector search; `--raw` for lexical.
- **Clean slate**: Nuke LanceDB + knowledge_nodes, re-enrich all events with new pipeline.

## 1. Stdout/Stderr Capture

### Shell Hook Changes

**File**: `shell/hippo.zsh`

The `preexec` hook sets up output capture before each command using `script -q` (macOS) or process substitution to tee stdout+stderr to a temp file (`/tmp/hippo-output.$$`). The `precmd` hook reads the temp file, applies head/tail truncation, and includes the result as the `output` field in the event JSON sent to the daemon. The temp file is cleaned up after each capture.

Note: `script -q /tmp/hippo-output.$$ -c "$1"` replaces the raw command execution in preexec. This wraps the command in a pty so both stdout and stderr are captured. The `-q` flag suppresses script's own header/footer lines.

Truncation: keep first N lines + last M lines (configurable). Default: first 50, last 100. If total output is <= N+M lines, send it all. If truncated, insert a `... (X lines omitted) ...` marker between the head and tail sections.

### Schema Change

**File**: `crates/hippo-core/src/schema.sql`

Add nullable column to the `events` table:

```sql
ALTER TABLE events ADD COLUMN output TEXT;
```

This is a migration step. Existing events get NULL output.

### Event Protocol Change

The `ShellEvent` struct in `crates/hippo-core/src/types.rs` gains an `output: Option<String>` field. The shell hook's JSON payload includes `"output": "..."` when available.

### Config

```toml
[daemon]
output_head_lines = 50    # First N lines of stdout/stderr to capture
output_tail_lines = 100   # Last N lines of stdout/stderr to capture
```

These are read by the shell hook (exported as env vars by the daemon or read from config directly).

### Wire Size

With first 50 + last 100 lines at ~120 chars/line average, worst case is ~18KB per event. Fine for a local Unix socket with length-prefixed framing.

## 2. Session-Based Enrichment Grouping

### Current Behavior

The enrichment loop polls `enrichment_queue` for N pending events (`enrichment_batch_size`), regardless of session. Events from different sessions, different projects, and different time periods get enriched together.

### New Behavior

**File**: `brain/src/hippo_brain/enrichment.py`

The enrichment loop:

1. Queries pending events grouped by `session_id`
2. Skips sessions that are still active (last event < `session_stale_secs` ago, default 120s)
3. For each stale session with pending events:
   a. Claims all pending events for that session
   b. If count <= `max_events_per_chunk` (default 30), enriches as one unit
   c. If count > threshold, splits into chunks at natural breakpoints:
      - Primary: time gaps > 60 seconds between consecutive events
      - Fallback: split at the cap boundary if no natural gaps
   d. Each chunk → one LLM enrichment call → one knowledge node

### Config Changes

```toml
[brain]
max_events_per_chunk = 30     # Max events per enrichment call
session_stale_secs = 120      # Seconds after last event before enriching a session
```

The existing `enrichment_batch_size` key is deprecated. If present and `max_events_per_chunk` is absent, read it as a fallback.

### Session Staleness Query

```sql
SELECT session_id, MAX(e.timestamp) as last_event_ts, COUNT(*) as pending_count
FROM enrichment_queue eq
JOIN events e ON eq.event_id = e.id
WHERE eq.status = 'pending'
GROUP BY session_id
HAVING last_event_ts < :stale_threshold
ORDER BY last_event_ts ASC
```

Process oldest sessions first.

## 3. Enrichment Prompt Redesign

### Current Prompt

Produces: summary, intent, outcome, entities (projects/tools/files/services/errors), relationships, tags, embed_text. Input is minimal (command + exit code + cwd).

### New Prompt

**Input context per chunk:**
- Session metadata: session type (shell/claude), start time, project directory
- Git context: branch name, commit messages created during the session
- For each event in the chunk:
  - Command string
  - Exit code
  - Duration
  - Working directory
  - **Output** (truncated stdout/stderr, when available)
- For Claude session events: tool names, file paths touched, assistant reasoning

**Output schema:**

```json
{
  "summary": "Specific description of what was accomplished, not what tools ran",
  "intent": "The goal driving this work",
  "outcome": "success | partial | failure | unknown",
  "key_decisions": ["Decision made and why, e.g. 'Chose build.rs over vergen for zero dependencies'"],
  "problems_encountered": ["Error/failure and resolution, e.g. 'LM Studio loaded embedding model as LLM, switched to nomic'"],
  "entities": {
    "projects": [],
    "tools": [],
    "files": [],
    "services": [],
    "errors": ["Actual error messages, not 'error encountered'"]
  },
  "tags": ["specific", "descriptive", "tags"],
  "embed_text": "A paragraph a developer would write in a work log. Specific file names, error messages, and outcomes. Written for semantic search retrieval."
}
```

**Dropped fields:**
- `relationships` — rarely useful, wastes tokens, the model produces low-quality relationships

**Prompt instructions emphasize:**
- Use actual file names, function names, error messages from the event data
- "Claude Code edited a Rust file" is bad. "Added build.rs to hippo-daemon that embeds git metadata into the version string via cargo:rustc-env" is good.
- The `embed_text` should be written as if for a developer journal, optimized for future search queries
- Attribute actions to the correct actor (developer vs AI agent)

### Token Budget

With the larger model (qwen3.5-35b-a3b), the context window is generous. A chunk of 30 events with ~150 lines of output each is still well within context. The system prompt + event data + JSON output fits comfortably.

## 4. Semantic Search in `/query`

### Current Behavior

`hippo query "text"` → brain `/query` → `LIKE %text%` on events.command and knowledge_nodes.content/embed_text.

`hippo query --raw "text"` → daemon socket → `LIKE %text%` on events.command only.

### New Behavior

**`hippo query "text"` (default, semantic):**

1. CLI sends POST to brain `/query` with `{"text": "...", "mode": "semantic"}`
2. Brain embeds the query text using the embedding model
3. Searches LanceDB `vec_knowledge` for top-K similar results (K=10)
4. For each hit, joins back to SQLite via `knowledge_node_events` to get:
   - The knowledge node summary, tags, key_decisions, problems_encountered
   - Linked event commands and working directories
   - Session metadata
5. Returns results ranked by cosine similarity

**`hippo query --raw "text"` (unchanged):**
- Existing substring search on events table via daemon socket

### Brain `/query` Endpoint Changes

**File**: `brain/src/hippo_brain/server.py`

The `/query` endpoint accepts an optional `mode` parameter:
- `"semantic"` (default): vector search via LanceDB
- `"lexical"`: current LIKE-based search

Semantic mode requires:
- Embedding model configured in config
- LM Studio reachable with the embedding model loaded

If embedding fails (model not loaded, LM Studio down), falls back to lexical with a warning in the response.

### CLI Output Format

**File**: `crates/hippo-daemon/src/commands.rs`

Semantic results display:

```
[0.82] Session: hippo-versioning (2026-03-29)
       Built custom build.rs to embed git metadata into version string.
       Chose build.rs over vergen crate for zero-dependency approach.
       Files: crates/hippo-daemon/build.rs, cli.rs
       Tags: rust, versioning, build-system

[0.71] Session: enrichment-debugging (2026-03-28)
       Debugged LM Studio embedding model loading. Model was categorized as LLM.
       Switched from qwen3-embedding to nomic-embed-text-v2-moe.
       Tags: debugging, embedding, lmstudio
```

The score is cosine similarity (0-1, higher = more relevant).

### LanceDB Schema Update

**File**: `brain/src/hippo_brain/embeddings.py`

Add new fields to the LanceDB schema to match the enriched output:

```python
pa.field("key_decisions", pa.string()),        # JSON-encoded list
pa.field("problems_encountered", pa.string()), # JSON-encoded list
```

Remove the `enrichment_version` field (use `enrichment_model` for provenance).

## 5. Migration & Re-enrichment

### `mise run re-enrich` Task

**File**: `mise.toml`

A new task that:
1. Stops the brain service (`launchctl bootout` or `pkill`)
2. Deletes `~/.local/share/hippo/vectors/` (LanceDB directory)
3. Runs SQL against `~/.local/share/hippo/hippo.db`:
   ```sql
   DELETE FROM knowledge_node_events;
   DELETE FROM knowledge_nodes;
   UPDATE enrichment_queue SET status = 'pending', retry_count = 0, error_message = NULL
   WHERE status IN ('done', 'failed');
   ```
4. Restarts the brain service
5. Prints status: "Re-enrichment queued. X events will be re-processed."

### Schema Migration

The `events.output` column addition runs as part of the daemon's startup migration (existing pattern in `schema.sql` or the Rust schema init code). Uses `ALTER TABLE ... ADD COLUMN` which is idempotent in SQLite (wrap in try/catch or check column existence).

### Config Migration

The `enrichment_batch_size` key continues to work as a fallback for `max_events_per_chunk`. No breaking change.

## Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `shell/hippo.zsh` | Modify | Add stdout/stderr capture to preexec/precmd hooks |
| `crates/hippo-core/src/types.rs` | Modify | Add `output: Option<String>` to ShellEvent |
| `crates/hippo-core/src/schema.sql` | Modify | Add `output TEXT` column to events |
| `crates/hippo-core/src/storage.rs` | Modify | Persist output field in event inserts |
| `crates/hippo-daemon/src/commands.rs` | Modify | Update query display for semantic results |
| `crates/hippo-daemon/src/cli.rs` | Modify | Query subcommand: `--raw` already exists, semantic becomes default |
| `brain/src/hippo_brain/enrichment.py` | Modify | Session grouping, chunking, new prompt |
| `brain/src/hippo_brain/embeddings.py` | Modify | Updated LanceDB schema, new fields |
| `brain/src/hippo_brain/server.py` | Modify | Semantic search in `/query`, embed query text |
| `brain/src/hippo_brain/client.py` | No change | `embed()` method already works |
| `config/config.default.toml` | Modify | New config keys, updated model defaults |
| `mise.toml` | Modify | Add `re-enrich` task |

## Verification

1. Shell hook captures output: run a command, check `events` table for non-NULL output
2. Session grouping: create events across sessions, verify enrichment groups by session
3. Enrichment quality: compare old vs new knowledge_nodes for specificity
4. Semantic search: `hippo query "version embedding"` returns relevant results ranked by similarity
5. Re-enrich: run `mise run re-enrich`, verify queue resets and new enrichments appear
6. Fallback: stop LM Studio, verify `hippo query` falls back to lexical with a warning
7. All tests pass: `cargo test && uv run --project brain pytest brain/tests -v`
