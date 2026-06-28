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

Use the existing `search_knowledge` MCP tool with lexical mode while applying filters:

```text
search_knowledge(query="busy timeout", mode="lexical", source="claude-auto-memory", project="owner/repository")
```

Results include `source`, `source_path`, `repository`, `logical_path`, `content_hash`, and capture time. Only the active, successfully enriched revision is returned by the auto-memory source filter.

## Storage and rollback

Schema v19 adds only additive tables: `memory_documents`, `memory_revisions`, `memory_chunks`, `memory_enrichment_queue`, `knowledge_node_memory_chunks`, `memory_categories`, and `memory_links`. Existing source and knowledge tables are unchanged.

To stop ingestion, set `auto_memory.enabled = false` and restart the brain. Existing memory knowledge remains queryable. For a full feature rollback, stop Hippo, back up `hippo.db`, delete knowledge nodes linked through `knowledge_node_memory_chunks`, then delete `memory_documents` rows; foreign-key cascades remove revisions, chunks, and queue rows. Do not reduce `PRAGMA user_version` or drop v19 tables while a v19 binary is installed.

