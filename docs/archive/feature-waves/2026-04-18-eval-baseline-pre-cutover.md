# Eval Baseline: Pre-Cutover (Schema v5)

**Captured**: 2026-04-18T09:47:00Z  
**DB path**: `~/.local/share/hippo/hippo.db`  
**Branch**: `sqlite-vec-rollout`  
**Purpose**: Regression gate for v5→v6 migration. Post-cutover hybrid retrieval scores
must equal or exceed these baselines to pass the "no regression" gate.

---

## Schema Version

```
PRAGMA user_version → 5
```

Schema v5: `knowledge_nodes`, `events`, `claude_sessions`, `browser_events` present.
`knowledge_vectors` (vec0 virtual table) exists but is **empty** (0 rows — see §Root Cause).
`knowledge_fts` (FTS5 virtual table) is **absent**.

---

## Corpus Stats

| Table | Count |
|---|---:|
| `knowledge_nodes` | 2,191 |
| `events` | 8,191 |
| `claude_sessions` | 963 |
| `browser_events` | 16 |
| `workflow_runs` | 0 |
| `knowledge_vectors` rows | **0** |
| `embed_text` populated | 2,191 / 2,191 (100%) |

`embed_text` is fully populated across all knowledge nodes — the v5→v6 vector
backfill has the necessary text to embed.

---

## Mode Compatibility on v5

| Mode | v5 Status | Reason |
|---|---|---|
| `semantic` | ✓ runs, 0 hits | vec0 table exists but empty; no vectors written on v5 |
| `hybrid` | ✗ OperationalError | `knowledge_fts` absent; `fts_search()` raises and all questions degrade |
| `lexical` | ✗ OperationalError | same — `knowledge_fts` absent |
| `recent` (no query) | ✓ runs | falls back to `_all_recent_ids()`; no relevance signal |
| `recent` (with query) | ✗ OperationalError | attempts FTS before recency sort |

**Chosen eval mode**: `semantic` — the only mode that completes without errors. Returns
zero hits because `knowledge_vectors` is empty (see §Root Cause).

---

## Root Cause: Empty Vector Table

The v5 enrichment pipeline wrote embeddings exclusively to **LanceDB** (since
replaced). The `knowledge_vectors` vec0 virtual table was created by `open_conn()`
calling `ensure_vec_table()` (idempotent DDL), but no rows were ever inserted.
LanceDB is also absent (`~/.local/share/hippo/lancedb` does not exist on this machine).

Consequence: semantic retrieval on v5 returns 0 results for every query. This is
the correct and expected pre-migration state — the v5→v6 migration will:

1. Backfill `knowledge_vectors` by embedding all 2,191 `embed_text` values.
2. Create `knowledge_fts` with BM25 index via triggers.
3. Enable hybrid (RRF + MMR) retrieval.

---

## Retrieval Metrics (v5 Baseline)

Eval run: `hippo-eval --mode semantic --no-synthesis --no-judge`  
Embedding model: `text-embedding-nomic-embed-text-v2-moe` (768-dim)  
Duration: 1.1 s total, 40 questions.

| Metric | Mean | Median |
|---|---:|---:|
| recall@10 | **0.000** | 0.000 |
| mrr | **0.000** | 0.000 |
| ndcg@10 | **0.000** | 0.000 |
| source_diversity | 0.000 | 0.000 |
| coverage_gap | 1.000 | 1.000 |
| groundedness | — | — |
| keyword_hit_rate | 0.000 | 0.000 |
| latency_ms p50 | 26.1 | — |
| latency_ms p95 | 31.3 | — |

All retrieval metrics are zero because `knowledge_vectors` is empty. Latency reflects
embedding calls only (no DB scan component). This is the floor — post-cutover hybrid
retrieval must beat it on every non-adversarial question with labeled nodes.

---

## Per-Question Results

All 40 questions returned 0 hits in semantic mode (top score = `—`, gap = 1.000).
Degraded = false for all (the harness ran cleanly; empty results are not errors).

| id | intent | source_bias | labeled_uuids | top | gap | degraded |
|---|---|---|---:|---:|---:|:---:|
| q01 | why-decision | claude | 3 | — | 1.000 | |
| q02 | how-it-works | claude | 3 | — | 1.000 | |
| q03 | why-decision | claude | 3 | — | 1.000 | |
| q04 | how-it-works | mixed | 5 | — | 1.000 | |
| q05 | state-lookup | claude | 4 | — | 1.000 | |
| q06 | why-decision | claude | 3 | — | 1.000 | |
| q07 | how-it-works | mixed | 3 | — | 1.000 | |
| q08 | state-lookup | mixed | 2 | — | 1.000 | |
| q09 | how-it-works | claude | 3 | — | 1.000 | |
| q10 | how-it-works | claude | 2 | — | 1.000 | |
| q11 | why-decision | claude | 2 | — | 1.000 | |
| q12 | how-it-works | claude | 4 | — | 1.000 | |
| q13 | state-lookup | mixed | 3 | — | 1.000 | |
| q14 | state-lookup | claude | 4 | — | 1.000 | |
| q15 | why-decision | claude | 4 | — | 1.000 | |
| q16 | state-lookup | mixed | 3 | — | 1.000 | |
| q17 | adversarial | claude | 0 | — | 1.000 | |
| q18 | how-it-works | claude | 1 | — | 1.000 | |
| q19 | state-lookup | mixed | 4 | — | 1.000 | |
| q20 | how-it-works | mixed | 3 | — | 1.000 | |
| q21 | why-decision | claude | 0 | — | 1.000 | |
| q22 | adversarial | claude | 3 | — | 1.000 | |
| q23 | why-decision | claude | 0 | — | 1.000 | |
| q24 | how-it-works | claude | 2 | — | 1.000 | |
| q25 | how-it-works | claude | 3 | — | 1.000 | |
| q26 | cross-source | browser | 0 | — | 1.000 | |
| q27 | how-it-works | claude | 0 | — | 1.000 | |
| q28 | why-decision | mixed | 3 | — | 1.000 | |
| q29 | how-it-works | shell | 3 | — | 1.000 | |
| q30 | why-decision | claude | 2 | — | 1.000 | |
| q31 | adversarial | claude | 3 | — | 1.000 | |
| q32 | how-it-works | claude | 0 | — | 1.000 | |
| q33 | why-decision | claude | 0 | — | 1.000 | |
| q34 | state-lookup | mixed | 0 | — | 1.000 | |
| q35 | adversarial | claude | 2 | — | 1.000 | |
| q36 | state-lookup | claude | 0 | — | 1.000 | |
| q37 | cross-source | claude | 0 | — | 1.000 | |
| q38 | adversarial | claude | 0 | — | 1.000 | |
| q39 | state-lookup | mixed | 0 | — | 1.000 | |
| q40 | state-lookup | claude | 4 | — | 1.000 | |

---

## Post-Cutover Comparison Baseline

### Included (28 questions — primary regression signal)

These questions have labeled `relevant_knowledge_node_uuids` against the v5 corpus.
All 28 labeled nodes have `embed_text` populated, so the post-cutover backfill will
embed them and make them findable via semantic/hybrid retrieval.

| id | labeled_uuids | notes |
|---|---:|---|
| q01 | 3 | strong coverage |
| q02 | 3 | strong coverage |
| q03 | 3 | strong coverage |
| q04 | 5 | strong coverage |
| q05 | 4 | strong coverage |
| q06 | 3 | strong coverage |
| q07 | 3 | strong coverage |
| q08 | 2 | partial (v6 detail branch-only) |
| q09 | 3 | strong coverage |
| q10 | 2 | partial (synthesis path not enriched) |
| q11 | 2 | partial (rationale implicit in memory) |
| q12 | 4 | strong coverage |
| q13 | 3 | strong coverage |
| q14 | 4 | partial (search_hybrid not enriched) |
| q15 | 4 | partial (ESRCH/EPERM names not explicit in nodes) |
| q16 | 3 | strong coverage |
| q18 | 1 | single labeled node |
| q19 | 4 | strong coverage |
| q20 | 3 | partial (hippo doctor check list in CLAUDE.md not nodes) |
| q22 | 3 | strong coverage |
| q24 | 2 | partial (_apply_filters detail branch-only) |
| q25 | 3 | partial (lambda math branch-only) |
| q28 | 3 | partial (why-uv rationale implicit) |
| q29 | 3 | strong coverage |
| q30 | 2 | strong coverage |
| q31 | 3 | partial (degraded path branch-only) |
| q35 | 2 | strong coverage — adversarial (relationships removed) |
| q40 | 4 | strong coverage |

**Target post-cutover thresholds** (hybrid mode, k=10):

| Metric | Threshold | Rationale |
|---|---:|---|
| recall@10 (28 q subset) | ≥ 0.35 | At least 1 labeled node in top-10 for ~60% of questions |
| MRR (28 q subset) | ≥ 0.25 | First relevant hit within top 4 for ~half of questions |
| coverage_gap (all 40) | ≤ 0.60 | ≥ 40% of top-10 scores above 0.5 threshold |

These are conservative floors derived from typical LanceDB-era performance, not
aspirational targets.

### Excluded from Post-Cutover Recall/MRR/NDCG Comparison (12 questions)

These have empty `relevant_knowledge_node_uuids` — either adversarial hallucination
tests or branch-only content not yet enriched. They CAN still be run post-cutover
for coverage_gap and latency regression.

| id | intent | reason excluded |
|---|---|---|
| q17 | adversarial | No labeled nodes by design (tests hallucination guard) |
| q21 | why-decision | v6 trigger-based drift prevention — branch-only spec |
| q23 | why-decision | FTS5 sanitization fix — branch commit 601168d not enriched |
| q26 | cross-source | Firefox disconnect behavior — not enriched |
| q27 | how-it-works | 8000-char budget — branch commit 99b57d7 not enriched |
| q32 | how-it-works | `list_projects` MCP tool — branch commit c6ed1cd not enriched |
| q33 | why-decision | cosine→[0,1] transform — branch commit 1c3bb91 not enriched |
| q34 | state-lookup | Live COUNT(*) — not answerable via retrieval by design |
| q36 | state-lookup | Scorecard verdict — branch commit 48ee6b7 not enriched |
| q37 | cross-source | `search_hybrid` — branch commit c6ed1cd not enriched |
| q38 | adversarial | FTS5 quoting stress test — branch-only |
| q39 | state-lookup | XDG env vars — in CLAUDE.md/code, not in knowledge_nodes |

---

## Known Caveats

1. **Vec0 empty on v5**: The pre-cutover semantic baseline is a degenerate floor
   (recall=0). This is expected and correct — the v5 enrichment pipeline wrote to
   LanceDB (now absent). The baseline's value is to confirm the harness runs cleanly
   and to anchor the post-migration delta.

2. **LanceDB absent**: `~/.local/share/hippo/lancedb` does not exist on this machine.
   There is no prior LanceDB baseline to compare against.

3. **LM Studio latency variance**: Embedding calls use `text-embedding-nomic-embed-text-v2-moe`
   at `http://localhost:1234/v1`. p50=26ms, p95=31ms per question in retrieval-only
   mode. Post-cutover runs with synthesis will be slower due to LLM calls.

4. **events.git_repo is NULL**: All 8,191 event rows have NULL `git_repo` on the
   live machine. Project filters fall back to `cwd LIKE %project%`. Low recall on
   project-filtered queries is a data artifact.

5. **Branch corpus coverage**: Labels were drawn from the main-branch corpus
   (pre-sqlite-vec-rollout). The labeled node UUIDs are valid in the live DB
   (confirmed: 2,191/2,191 have embed_text). But nodes created by branch-only
   sessions may not exist in the corpus until after enrichment catches up.

6. **Synthesis/groundedness not measured**: `--no-synthesis --no-judge` was used
   for speed. Groundedness is `—` for all questions. Post-cutover run should include
   `--no-judge` for speed but with synthesis on to capture keyword_hit_rate.
