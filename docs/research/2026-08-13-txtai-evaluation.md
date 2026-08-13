# txtai evaluation — replace any of the brain's SQLite stack?

Upstream inspected: [neuml/txtai](https://github.com/neuml/txtai) v9.12.0
(released 2026-07-30); master at 9.13.0-dev, last commit 2026-08-12.

Question evaluated: could txtai beneficially replace SQLite — or the
sqlite-vec + FTS5 retrieval layer on top of it — anywhere in the brain stack?
Operationally, speed, accuracy, quality; and if not obviously, is a prototype
worth spinning?

## Decision

**Reject as a storage or retrieval-backend replacement. The SQLite stack
stays.** txtai is a genuinely good project — not vaporware — but it solves a
problem Hippo does not have (retrieval plumbing) at the cost of the one Hippo
has explicitly paid to solve twice already (single-source-of-truth storage).
Adopting it would reintroduce the two-store drift the 2026-04-17 LanceDB →
sqlite-vec consolidation eliminated.

**Adopt ideas, not the dependency.** Two of its retrieval techniques are worth
cheap, bench-gated experiments inside the existing stack (see "Worth
stealing"). A full txtai sidecar prototype is **not** justified today: at
current corpus scale the expected win is a wash, and the bench fixture that
would score it is anti-lexical by construction, so it cannot yet produce a
fair verdict.

## What txtai actually is

Not a database. The core `Embeddings` class is a Python composite of pluggable
components, each persisted as a separate file in an index directory:

- **ANN backends**: Faiss (default), hnswlib, annoy, NumPy, Torch, pgvector,
  milvus-lite — and notably `sqlite`, which is **the same sqlite-vec vec0
  virtual table Hippo already uses**, wrapped
  ([ann/dense/sqlite.py](https://github.com/neuml/txtai/blob/master/src/python/txtai/ann/dense/sqlite.py)).
- **Content store**: SQLite by default ([database docs](https://neuml.github.io/txtai/embeddings/configuration/database/)).
  The "replace SQLite with txtai" framing is half-ironic — txtai's own ground
  truth is SQLite files.
- **Keyword index**: its own BM25 term-postings store in a separate SQLite
  file, doc ids/lengths held in RAM, tombstone deletes
  ([scoring/terms.py](https://github.com/neuml/txtai/blob/master/src/python/txtai/scoring/terms.py)).
- **Hybrid fusion**: convex combination (default 0.5/0.5), RRF, or BB25
  Bayesian log-odds ([search/hybrid.py](https://github.com/neuml/txtai/blob/master/src/python/txtai/embeddings/search/hybrid.py)).
- **Extras**: SQL DSL with `similar()` clauses, NetworkX graph-RAG, ColBERT
  late interaction + MUVERA, reranker pipelines, agents, FastAPI/MCP service.

Health: Apache-2.0, ~12.9k stars, ~monthly releases, 6 open issues (fast
triage). ~96% of 2,054 commits are one person (David Mezzetti / NeuML) —
treat as a bus-factor-1 dependency.

## What it could and could not replace in Hippo

Could replace (brain-side retrieval only):

| Hippo today | txtai equivalent |
|---|---|
| `knowledge_vectors` vec0 KNN (`vector_store.knn_search`) | ANN backend (Faiss/HNSW — or its own sqlite-vec wrapper, i.e. what we have) |
| `knowledge_fts` FTS5 BM25 (`vector_store.fts_search`) | txtai BM25 terms index |
| Weighted-RRF fusion in `retrieval._hybrid` | `Embeddings.search(weights=...)` convex/RRF/BB25 |

Could **not** replace: every relational table (`events`,
`agentic_sessions`, `browser_events`, enrichment queues, `source_health`,
`capture_alarms`, six `knowledge_node_*` link tables), transactional
co-writes from the Rust daemon, and the single-file WAL backup story. txtai's
schema is fixed (`sections`/`documents`/`objects`); it is an index, not a
general store.

## Why replacement loses

| Concern | txtai 9.12 | Hippo contract / impact |
|---|---|---|
| Crash consistency | `save()` = sequential multi-file directory write; no atomic rename, no fsync discipline, no manifest. Mid-save crash leaves ANN / terms / content files from different generations, undetected | One WAL SQLite file; FTS5 synced by triggers inside the writer's transaction; vectors committed in the same DB. `cp hippo.db` is a backup |
| Multi-process access | "Writes must be synchronized"; embedded backends have **no** cross-process story — second process sees last-saved snapshot, save clobbers. Rust can only reach it via the FastAPI service (txtai.rs is an HTTP client) | Three+ processes (daemon, brain, N× MCP, CLI, watchdog) share one DB under WAL + busy_timeout=5000 |
| Rust-side invariants | Faiss/msgpack/terms blobs unreadable from Rust | Watchdog I-14 anti-joins `knowledge_vectors_rowids` from Rust with no extension loaded (`watchdog.rs:899`); doctor/schema handshake read the same file |
| Drift between index and truth | Second consistency domain (index dir vs DB); enrichment churn accumulates tombstones (Faiss `remove_ids`, HNSW `mark_deleted`, BM25 tombstone lists) until full `reindex()` | The LanceDB era already produced exactly this failure — "same node written twice in two shapes… vector count mismatches" (2026-04-17 consolidation design, Motivation #2) — and got an orphan-reaper + I-14 invariant built to clean up the aftermath |
| Filter pushdown | `similar()` SQL DSL filters only over txtai's own content DB; Hippo's eligibility predicates (probe_tag AP-6, journal/stub/settle-window exclusion, project/branch/source/entity/category joins) live in the relational schema | `_apply_filters` joins six link tables post-fusion; a replacement must return ≥3000 candidates cheaply or absorb all predicates |
| Dependency footprint | Base install pulls torch + transformers + faiss (multi-GB). `txtai_minimal` (9.9.0, 2026-05) avoids that but then requires external vectorization — which is the part Hippo already has | Brain is a uv project on Python 3.14; torch/faiss wheels for 3.14 unverified |
| Embedding pipeline | Wants to own vectorization (sentence-transformers et al.); external-vector mode is the escape hatch | Hippo embeds via the local OpenAI-compatible server with an un-batched workaround for a real oMLX null-vector bug and an `EmbedDriftError` model guard — none of which txtai replicates |

## Speed

No expected win at Hippo's scale. The corpus is ~10.4k knowledge nodes × two
768-d vectors ≈ 64 MB. sqlite-vec's brute-force scan at that size is
single-digit milliseconds; Faiss IVF earns its complexity in the millions of
vectors. The one retrieval cost that *was* measured as dominant — pure-Python
MMR over a large pool — was already fixed in-repo (`_MMR_POOL_CAP=500`,
incremental-max, pre-normalized dot products), and txtai would not remove MMR
anyway since fused-candidate diversification happens above the index. NeuML's
own BM25-vs-FTS5 benchmark (notebook 47) shows wins on 400k-doc corpora and
memory-vs-rank-bm25 — impressive, but two orders of magnitude past Hippo's
size. The claim of Elasticsearch parity is the author's internal testing,
unverified.

## Accuracy / quality

Hippo's retrieval already implements the things txtai's hybrid search is
praised for, plus several it lacks:

- three-arm weighted RRF (knowledge vec + FTS5 + command vec) with per-arm
  weights in `[retrieval]` config,
- recency prior with half-life + floor,
- MMR diversification,
- entity-alias query expansion from the `entities` table,
- optional LLM listwise rerank with fail-open fallback,
- FTS query sanitization tuned on a real regression (phrase-only matching
  silently degrading hybrid to vector-only).

NeuML's BEIR-subset numbers for hybrid-vs-single-arm (NDCG@10 0.31→0.35 on
nfcorpus, etc.) validate hybrid *as a concept* — which Hippo already ships.
The residual deltas txtai offers are fusion-strategy variants (convex
combination, BB25 normalization) and heavier machinery (SPLADE sparse
vectors, ColBERT/MUVERA, graph-RAG). The former are ~20-line changes testable
in the existing stack; the latter require GPU-adjacent models the brain
deliberately avoids, and graph-RAG would duplicate the existing
`entities`/link-table graph rather than replace it.

Current baseline to beat: hybrid MRR ≈ 0.4 / Hit@1 ≈ 0.35 on the 100-item QA
fixture — with the caveat that the fixture's anti-leakage rubric strips
verbatim tokens, so it under-scores lexical arms by design and cannot fairly
judge a BM25 swap either way.

## Community evidence

<!-- COMMUNITY_EVIDENCE -->

## Worth stealing (bench-gated, no new dependency)

1. **Convex-combination fusion as a `Tuning` mode.** Hybrid fusion today is
   rank-only RRF; txtai defaults to normalized score interpolation and offers
   BB25 log-odds normalization. Both are small additions to
   `retrieval._hybrid` behind config, A/B-able via
   `downstream_proxy.run_downstream_proxy_pass(search_fn=...)` + the BT-29
   determinism gate (MRR/Hit@1 delta ≤ 0.02 noise floor).
2. **A fair-to-lexical QA fixture.** Any fusion experiment first needs a QA
   set that doesn't strip verbatim tokens; without it, no fusion change can
   be honestly scored. This blocks (1) and was already a known bench gap.

## If a prototype is ever wanted anyway

The seams already exist, so the cost is bounded: implement the two-method
`_Backend` protocol (`retrieval.py:180`) over a txtai `Embeddings` index fed
from `knowledge_nodes`, inject it via `downstream_proxy`'s `search_fn`, run
three BT-29 runs, and compare per-mode Hit@K/MRR/NDCG@10. Acceptance bar:
beat the sqlite-vec baseline by more than the 0.02 determinism noise floor on
a fair fixture. Expected outcome at 10k nodes: a wash on speed, small or no
accuracy delta from fusion differences — which is why this is filed as
"optional experiment," not roadmap.

## Sources

- [txtai repository](https://github.com/neuml/txtai) ·
  [releases](https://github.com/neuml/txtai/releases) ·
  [PyPI](https://pypi.org/project/txtai/)
- Docs: [ANN configuration](https://neuml.github.io/txtai/embeddings/configuration/ann/),
  [database](https://neuml.github.io/txtai/embeddings/configuration/database/),
  [scoring](https://neuml.github.io/txtai/embeddings/configuration/scoring/),
  [index format](https://neuml.github.io/txtai/embeddings/format/),
  [query DSL](https://neuml.github.io/txtai/embeddings/query/),
  [install](https://neuml.github.io/txtai/install/)
- Source verified: [embeddings/base.py](https://github.com/neuml/txtai/blob/master/src/python/txtai/embeddings/base.py),
  [ann/dense/sqlite.py](https://github.com/neuml/txtai/blob/master/src/python/txtai/ann/dense/sqlite.py),
  [ann/dense/faiss.py](https://github.com/neuml/txtai/blob/master/src/python/txtai/ann/dense/faiss.py),
  [scoring/terms.py](https://github.com/neuml/txtai/blob/master/src/python/txtai/scoring/terms.py),
  [search/hybrid.py](https://github.com/neuml/txtai/blob/master/src/python/txtai/embeddings/search/hybrid.py)
- NeuML benchmarks: [notebook 47 (BM25)](https://github.com/neuml/txtai/blob/master/examples/47_Building_an_efficient_sparse_keyword_index_in_Python.ipynb),
  [notebook 48 (hybrid)](https://github.com/neuml/txtai/blob/master/examples/48_Benefits_of_hybrid_search.ipynb)
- In-repo precedent: `docs/archive/feature-waves/2026-04-17-sqlite-vec-consolidation-design.md`
  (LanceDB → sqlite-vec motivation),
  `docs/research/2026-06-27-sqlite-memory-compatibility.md` (sqlite-memory
  rejection), `docs/superpowers/specs/2026-05-18-embedding-orphan-reaper-design.md`
  (cost of index/truth drift)
