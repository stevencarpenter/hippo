# Claude Code auto-memory

Hippo treats Claude Code auto-memory Markdown as an external, read-only source. It never writes, renames, or deletes Claude's files. Redaction runs before content or content-derived hashes enter SQLite or the local inference pipeline.

## Configure one file

Add an explicit source to `~/.config/hippo/config.toml`:

```toml
[auto_memory]
enabled = true

[[auto_memory.sources]]
path = "/absolute/path/to/MEMORY.md"
repository = "owner/repository"
logical_path = "MEMORY.md"
```

`repository` and `logical_path` form the stable identity. Use a stable local repository name when the file is outside Git. Hippo expands `~` but does not infer or scan home-directory paths in this initial slice.

The brain checks configured files during its normal polling cycle. An unchanged redacted content hash is a no-op. New content creates an immutable revision, deterministic Markdown-heading chunks, and a local enrichment queue item.

For a one-off ingest without changing configuration:

```sh
mise run ingest:auto-memory -- \
  --file /absolute/path/to/MEMORY.md \
  --repository owner/repository \
  --logical-path MEMORY.md
```

The command prints JSON containing the stable document UUID, revision, chunk count, and whether content changed.

## Query

Auto-memory is an always-on source: like shell, Claude, and browser activity, its enriched nodes participate in the default knowledge base, so `hippo ask` and `search_knowledge` surface them without any special flag. Pass `source="claude-auto-memory"` only to *scope* results to memory:

```text
search_knowledge(query="busy timeout", mode="lexical", source="claude-auto-memory", project="owner/repository")
```

For a dedicated read-only memory contract (current projected chunks, explicit history, provenance without local paths by default), use:

| Surface | Current memory | Revision history |
|---------|----------------|------------------|
| MCP | `query_memory(...)` | `query_memory_history(...)` |
| HTTP | `POST /memory/query` | `POST /memory/history` |
| CLI | `hippo-memory-query "term" --repository owner/repo` | `hippo-memory-query --history --repository owner/repo --logical-path MEMORY.md` |

**Claude Code / Codex / opencode (MCP):**

```json
{"tool": "query_memory", "arguments": {"query": "auth design", "repository": "owner/repo", "category": "project", "limit": 10}}
```

**Human CLI:**

```sh
uv run --project brain hippo-memory-query "busy timeout" --repository owner/repository --limit 5
uv run --project brain hippo-memory-query --history --repository owner/repository --logical-path MEMORY.md
```

Filters: `repository`, `category`, `logical_path`, `document_uuid`, `since` (e.g. `7d`). Default results include only active, successfully projected chunks (`projection_status` ready/stale). Use `include_non_queryable=true` to list pending/failed/unavailable status stubs. Use `include_source_path=true` only for local diagnostics.

Results include `repository`, `logical_path`, `document_uuid`, `chunk_id`, `content_hash`, `source_mtime_ms`, `enriched_at`, `evidence_excerpt`, `memory_categories`, and `memory_links`. Absolute `source_path` is omitted unless explicitly requested.

Results from `search_knowledge` include `source`, `source_path`, `repository`, `logical_path`, `content_hash`, and capture time. Only the active revision is ever queryable: superseding a revision replaces its knowledge node (and vector), and a revision superseded before its enrichment finishes is discarded rather than published, so stale memory content never appears in answers.

## Revision history

Each content update stores bounded summary/diff metadata on superseded revisions and clears their redacted bodies. Historical revisions never appear in normal `search_knowledge` results; use explicit history instead:

```sh
mise run ingest:auto-memory -- \
  --history \
  --repository owner/repository \
  --logical-path MEMORY.md
```

Retention defaults (override in `[auto_memory]`):

- `max_revision_count = 20`
- `max_revision_age_days = 90`
- `absence_confirm_polls = 2` (configured file missing on consecutive polls before tombstone)

Renames are detected when exactly one prior document shares the same redacted hash, the old path is gone, and the new path is ingested. Deleted files move to `unavailable` on the first missing poll, then `tombstoned` after confirmation; tombstoned documents drop out of retrieval but keep bounded history.

## Categories and links

Claude's four-category filename convention is detected deterministically when present:

| Signal | Examples |
|--------|----------|
| `index` | `MEMORY.md` |
| `user` | `user.md`, `user_role.md` |
| `feedback` | `feedback_testing.md` |
| `project` | `project_auth.md` |
| `reference` | `reference_api.md` |

Files without a recognized prefix (for example `debugging.md`) remain uncategorized until the enrichment model adds optional `memory_categories` in its JSON output. Filename-derived categories are kept across content updates; model-derived categories are replaced on each successful enrichment.

`MEMORY.md` index Markdown links are reconciled to other files in the same memory directory. Unresolved, external, ambiguous, and circular links are stored with their resolution status but never invented.

Filter retrieval with `memory_category` (RAG `Filters.memory_category` or `search_knowledge(..., category="feedback")`). Results include `memory_categories` and `memory_links` provenance when the knowledge node projects an auto-memory document.

## Fleet discovery

When `[auto_memory]` is enabled and `[auto_memory.discovery] enabled = true` (default), Hippo discovers every local Claude Code memory directory under `~/.claude/projects/<slug>/memory/` plus trusted `autoMemoryDirectory` overrides read from Claude user/project settings (read-only; Hippo never writes Claude config).

- Explicit `[[auto_memory.sources]]` entries are merged with discovered files; explicit paths win on conflict.
- Worktrees that share one memory directory are deduplicated by resolved path.
- Dry-run inventory: `uv run --project brain hippo-auto-memory-inventory`
- Include/exclude glob patterns: `[auto_memory.discovery] include_patterns` / `exclude_patterns`
- FSEvents under `~/.claude/projects` triggers reconcile only for files that pass the same Python discovery policy (excluded projects are skipped with `outcome: skipped`). Custom `autoMemoryDirectory` paths outside the default layout are picked up on periodic poll, not live FSEvents.

## Continuous reconciliation

When `auto_memory.enabled = true`, `hippo daemon install` writes two LaunchAgents:

- `com.hippo.auto-memory-watcher` (KeepAlive) — FSEvents on each configured source's parent directory, debounced per file, then `hippo-auto-memory-reconcile` with stable-read gating before ingest.
- `com.hippo.auto-memory` (StartInterval) — periodic full reconcile fallback (`hippo-auto-memory-poll`).

Tuning knobs in `[auto_memory]`:

- `debounce_ms` — Rust watcher quiet period before spawning per-file reconcile (default 500).
- `reconcile_fallback_secs` — in-watcher full reconcile cadence (default 60).
- `stable_idle_ms` / `stable_sample_ms` / `stable_timeout_ms` — Python stable-read gate (mtime+size unchanged) before reading a file mid-write.
- `poll_interval_secs` — launchd interval for the periodic fallback poller (default 60).

During enrichment failure or pending retries, the last-known-good projection stays queryable (`projection_status = stale`). Poll/reconcile JSON reports `pending_enrichment` and `failed_enrichment` per source.

## Storage and rollback

Schema v19 adds only additive tables: `memory_documents`, `memory_revisions`, `memory_chunks`, `memory_enrichment_queue`, and `knowledge_node_memory_chunks`. Existing source and knowledge tables are unchanged.

To stop ingestion, set `auto_memory.enabled = false` and restart the brain. Existing memory knowledge remains queryable. For a full feature rollback, stop Hippo, back up `hippo.db`, delete knowledge nodes linked through `knowledge_node_memory_chunks`, then delete `memory_documents` rows; foreign-key cascades remove revisions, chunks, and queue rows. Do not reduce `PRAGMA user_version` or drop v19 tables while a v19 binary is installed.

