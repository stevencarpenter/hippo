# Claude auto-memory storage and chunking spike

Issue: [SNUG-131](https://linear.app/snugmarina/issue/SNUG-131/auto-memory-18-evaluate-sqlite-memory-and-chunking-strategies)

## Recommendation

1. Keep Claude auto-memory in Hippo's existing SQLite database.
2. Do not adopt `sqlite-memory`; adapt its transaction, hashing, and
   Markdown-boundary ideas.
3. Use **adaptive Markdown chunking**: preserve a short file as one chunk;
   otherwise split on Markdown headings, then split an oversized section into
   bounded overlapping windows. Keep limits configurable and record the
   chunker/version on every revision.
4. Treat logical path as document identity and content hash as revision
   identity. Identical content at different paths remains distinct.
5. Keep source durability separate from active search projection. A failed or
   pending replacement must not hide the previous successfully enriched result.

This is a HITL decision. SNUG-132 should not start until the maintainer accepts
or changes the recommendation.

## Corpus observations

A metadata-only scan found 165 local Claude auto-memory Markdown files totaling
316,363 bytes. Median size was 1,519 bytes, p90 was 4,027 bytes, and maximum was
13,729 bytes. The Hippo memory directory contained 45 files totaling 100,516
bytes. These sizes do not justify a second datastore.

The committed spike corpus is entirely synthetic. It includes a `MEMORY.md`
index, linked topic files, headings, lists, code, short/long files, repeated
content under distinct paths, and expected retrieval questions. No real Claude
memory content or absolute home path is committed.

## Reproduction

```bash
mise run bench:auto-memory-spike
uv run --project brain pytest brain/tests/test_auto_memory_spike.py -v
```

The generated measurement table lives in
`docs/research/2026-06-27-auto-memory-spike-results.md`.

## Results

All three strategies achieved Hit@1 1.000 and MRR 1.000 on the initial
five-query lexical corpus:

| Strategy | Chunks | Hit@1 | MRR |
|---|---:|---:|---:|
| Whole file | 6 | 1.000 | 1.000 |
| Markdown headings | 13 | 1.000 | 1.000 |
| 48-token windows with 12-token overlap | 12 | 1.000 | 1.000 |

The correct conclusion is not that chunking is irrelevant. The corpus proves
that whole-file retrieval is sufficient for small, well-separated memories and
that extra chunks add index cost without improving these lexical queries. The
schema must therefore support one-or-many chunks without requiring eager
splitting. Heading-aware boundaries are preferable when a file is large enough
to split because they preserve author structure; token windows are a fallback
for an oversized heading section, not the primary semantic boundary.

The scratch mutation tests also prove:

- unchanged or duplicate content can remain path-distinct;
- update replacement removes stale FTS rows;
- an injected transactional failure restores the prior index;
- rename changes logical chunk IDs without changing content;
- delete removes active search rows; and
- deferred content remains durable but not searchable.

Semantic/vector quality was not measured because the spike deliberately avoids
the live inference service and the upstream extension could not be built in the
DNS-restricted execution environment. SNUG-132 should retain a configurable
chunking strategy and add live-model acceptance coverage before declaring exact
production thresholds permanent.

## Frozen minimal contract for SNUG-132

The names below are conceptual; the migration may adjust names to match existing
schema conventions, but it must preserve these identities and boundaries.

### Current document

`memory_documents`

- stable integer/UUID identity;
- `source_kind = 'claude-auto-memory'`;
- repository identity plus logical path, unique together;
- private local source path stored separately from broad query output;
- current redacted content and content hash;
- source mtime/size and observed timestamp;
- current source revision ID;
- active projected revision ID;
- projection status/error/attempt timestamps; and
- tombstone state/timestamp.

### Revision

`memory_revisions`

- document ID and immutable content hash;
- change kind (`create`, `update`, `rename`, `delete`);
- redacted current-content snapshot only while it is the current source revision;
- bounded historical summary and diff after supersession;
- source and observed timestamps;
- chunker name/version/config; and
- enrichment model/version/status.

### Chunk

`memory_chunks`

- revision ID plus stable ordinal, unique together;
- heading path and byte/character span;
- redacted chunk text, chunk hash, and estimated token count;
- no vector ownership in this table; vectors remain attached to projected
  `knowledge_nodes` through Hippo's existing sqlite-vec contract.

### Projection links

- a junction table links `knowledge_nodes` to memory chunk IDs;
- only the active projected revision participates in normal retrieval;
- FTS/vector/node replacement commits atomically;
- historical revisions never appear in ordinary search; and
- explicit history queries read bounded revision records, not stale knowledge
  projections.

## Chunking policy for the first vertical slice

- Empty content: store the document, create no searchable chunk.
- Small file/section: one chunk, preserving the original Markdown text.
- Larger file: split at Markdown heading boundaries and carry the heading path
  into each chunk.
- Oversized section: split into configurable bounded windows with overlap.
- Index files such as `MEMORY.md`: preserve links and list context; do not
  dereference or merge topic-file bodies into the index chunk.
- Store strategy/version/config so a future rechunk is explicit and repeatable.

Exact production limits remain configurable. The upstream defaults of 400
estimated tokens and 80-token overlap are reasonable starting benchmark values,
not a frozen interoperability contract.

## Risks carried into SNUG-132

- Markdown token estimation is not model tokenization.
- Rename inference can be ambiguous when multiple paths share a content hash.
- Redaction changes content hashes; the source hash and persisted redacted hash
  may need distinct fields without retaining plaintext secrets.
- Large diffs may cost more than compressed historical snapshots; retention
  policy needs measured bounds.
- FTS5 triggers and sqlite-vec rows have different integrity behavior and must
  be swapped in one application transaction.

## Related assessment

See [sqlite-memory compatibility](2026-06-27-sqlite-memory-compatibility.md).
