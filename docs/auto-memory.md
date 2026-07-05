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

Results include `source`, `source_path`, `repository`, `logical_path`, `content_hash`, and capture time. Only the active revision is ever queryable: superseding a revision replaces its knowledge node (and vector), and a revision superseded before its enrichment finishes is discarded rather than published, so stale memory content never appears in answers.

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

## Storage and rollback

Schema v19 adds only additive tables: `memory_documents`, `memory_revisions`, `memory_chunks`, `memory_enrichment_queue`, and `knowledge_node_memory_chunks`. Existing source and knowledge tables are unchanged.

To stop ingestion, set `auto_memory.enabled = false` and restart the brain. Existing memory knowledge remains queryable. For a full feature rollback, stop Hippo, back up `hippo.db`, delete knowledge nodes linked through `knowledge_node_memory_chunks`, then delete `memory_documents` rows; foreign-key cascades remove revisions, chunks, and queue rows. Do not reduce `PRAGMA user_version` or drop v19 tables while a v19 binary is installed.

