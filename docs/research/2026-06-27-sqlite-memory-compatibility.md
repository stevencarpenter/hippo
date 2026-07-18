# sqlite-memory compatibility assessment

Issue: [SNUG-131](https://linear.app/snugmarina/issue/SNUG-131/auto-memory-18-evaluate-sqlite-memory-and-chunking-strategies)

Upstream inspected: `sqliteai/sqlite-memory` tag `1.3.5` (released 2026-06-10).

## Decision

**Reject as a Hippo runtime dependency. Adapt its proven design ideas.**

The extension solves a similar problem, but adopting it would create a second
memory/retrieval subsystem inside Hippo. Its useful ideas—Markdown-heading
boundaries, content-hash no-ops, path-preserving identity, savepoint-scoped
replacement, deferred embeddings, and cleanup on directory reconciliation—fit
Hippo's existing SQLite architecture and should be implemented against Hippo's
own source, knowledge-node, FTS5, sqlite-vec, inference, and MCP contracts.

## Evidence

### What aligns

- Markdown parsing uses `md4c`, splits on semantic heading boundaries, and then
  enforces a configurable size/overlap budget.
- File ingestion hashes content, skips unchanged input, atomically replaces
  modified chunks, removes deleted files during directory sync, and supports
  distinct logical paths for identical content.
- Deferred indexing separates source durability from embedding availability.
- FTS5 and vectors are combined in one hybrid query surface.
- The extension supports a custom embedding-provider C callback, so its built-in
  llama.cpp/vectors.space providers are not the only theoretical integration path.

### What conflicts

| Concern | sqlite-memory 1.3.5 | Hippo contract / impact |
|---|---|---|
| Vector extension | Requires `sqlite-vector` for semantic search | Hippo already owns `sqlite-vec` tables, migrations, integrity checks, and retrieval |
| Storage schema | Owns `dbmem_*` settings/content/vault/FTS tables | Would duplicate `knowledge_nodes`, link tables, FTS5, queue state, and source provenance |
| Retrieval | Own virtual table and weighted vector/text merge | Hippo uses filter pushdown, RRF, MMR, source hydration, and existing MCP/RAG APIs |
| Embeddings | Built-in llama.cpp or vectors.space; custom provider requires C callbacks | Hippo uses configurable OpenAI-compatible local providers and 768d embeddings from Python |
| Revision history | Modified files replace prior content | SNUG-130 requires bounded revision metadata, summaries, and diffs |
| Source health | Directory sync state, not Hippo watchdog/probe semantics | Hippo requires independent source health, alarms, doctor output, and synthetic-row exclusion |
| Packaging | C/C++ dynamic extension; full local build includes llama.cpp/Metal and optional curl | Adds binaries, submodules, signing/loading, release, and cross-platform support burden |
| Schema stability | Upstream documents a rebuild requirement for databases created before 1.0 | Hippo requires ordered migrations and rollback-compatible ownership |

### License blocker

The upstream README says "MIT License," but tag `1.3.5`'s `LICENSE.md` is a
modified Elastic License 2.0. It grants free use when incorporated into an
OSI-licensed open-source project but requires a commercial license for
non-open-source or commercial production use and restricts managed-service use.
That mismatch is enough to reject direct dependency until upstream publishes a
single unambiguous license. Hippo should not advertise or inherit an MIT-only
dependency claim from the README.

### Maturity

At inspection time the repository showed 66 commits, 21 releases, approximately
74 stars, five forks, and version 1.3.5. Rapid releases are positive, but the
small adoption base, recent pre-1.0 schema break, and license contradiction make
it unsuitable as a foundational storage dependency today.

## Behavior matrix

| Behavior | Upstream evidence | Hippo spike evidence | Required Hippo behavior |
|---|---|---|---|
| Update | Hash check and savepoint replacement | Scratch replacement removes stale FTS chunks | Preserve old searchable projection until new enrichment commits |
| Delete | Directory cleanup removes stored file/chunks | Cascading delete removes FTS rows | Tombstone current document; retain bounded history |
| Rename | Explicit logical rename without reprocessing | Scratch rename preserves content and changes stable path IDs | Preserve document history only when identity is unambiguous |
| Duplicate path/content | Optional path-scoped hash | Two identical files remain separately searchable | Path identity is authoritative; content hash is revision identity |
| Transaction failure | Savepoint rollback | Injected failure restored old document/chunks | No partial source, chunks, vectors, or node links |
| Deferred embedding | Stores unindexed content pending later embedding | Deferred document is durable but not searchable | Keep last known-good projection visible during pending replacement |
| Revision history | Not provided | Outside scratch-index scope | Bounded metadata/summary/diff, explicit-history query only |

## Build limitation

The planned local clone/build could not start because the execution environment
could not resolve `github.com`. Source, Makefile, API, release, and license files
were inspected through the configured GitHub connector at tag `1.3.5`. No claim
is made that the extension compiled or loaded on this host.

## Sources

- [sqlite-memory repository](https://github.com/sqliteai/sqlite-memory)
- [1.3.5 API reference](https://github.com/sqliteai/sqlite-memory/blob/1.3.5/API.md)
- [1.3.5 parser](https://github.com/sqliteai/sqlite-memory/blob/1.3.5/src/dbmem-parser.c)
- [1.3.5 build](https://github.com/sqliteai/sqlite-memory/blob/1.3.5/Makefile)
- [1.3.5 license](https://github.com/sqliteai/sqlite-memory/blob/1.3.5/LICENSE.md)
- [SQLite FTS5](https://sqlite.org/fts5.html)
