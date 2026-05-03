# Hippo Evaluation Scorecard

- Run at: 2026-04-29 09:06:32Z
- Duration: 196.7s
- Mode: `hybrid`  |  Limit: 10
- Synthesis: off  |  Judge: off
- Embedding model: `text-embedding-nomic-embed-text-v2-moe`
- Query model: `qwen3.6-35b-a3b-ud-mlx`

## Corpus
- **knowledge_nodes**: 6568
- **events**: 22085
- **claude_sessions**: 2324

## Summary

| Metric | Mean | Median |
|---|---:|---:|
| recall@k | 0.210 | 0.000 |
| mrr | 0.213 | 0.000 |
| ndcg@k | 0.167 | 0.000 |
| source_diversity | 0.890 | 0.971 |
| coverage_gap | 0.000 | 0.000 |
| groundedness | — | — |
| keyword_hit_rate | 0.000 | 0.000 |
| latency_ms_p50 | 4918.6 | — |
| latency_ms_p95 | 5037.3 | — |

## Stratified by enrichment_model

| model | n | mean recall@k | mean gap | mean ground |
|---|---:|---:|---:|---:|
| `google/gemma-4-26b-a4b` | 8 | 0.274 | 0.000 | — |
| `google/gemma-4-31b` | 39 | 0.210 | 0.000 | — |
| `gpt-oss-120b` | 16 | 0.241 | 0.000 | — |
| `gpt-oss-120b-mlx-crack` | 20 | 0.280 | 0.000 | — |
| `qwen/qwen3.6-27b` | 1 | — | 0.000 | — |
| `qwen3.5-35b-a3b` | 20 | 0.178 | 0.000 | — |
| `qwen3.6-35b-a3b-ud-mlx` | 37 | 0.211 | 0.000 | — |

## Caveats

- **FTS5 phrase-wrap (R-03)**: lexical mode wraps multi-word queries in a single phrase, so recall on long natural-language questions is pathologically low — not a ranking bug.
- **RRF normalization (R-07)**: hybrid scores are normalized to top=1.0 per query. Absolute score thresholds are not comparable across queries.
- **vec0 brute-force (R-02)**: there is no ANN index on `knowledge_vectors`. Latency is O(N); hybrid≥LanceDB will stop holding once the corpus grows well past ~2K nodes.
- **events.git_repo is NULL** across the live v5 corpus, so project filtering silently falls back to cwd-prefix. Low recall on project-filtered queries is a data bug, not a retrieval bug.
- **Branch corpus coverage**: on the `postgres` branch only ~1.7% of events have knowledge-node coverage (vs ~13.4% on main). Labels are drawn from the main-hippo corpus until backfill runs.

## Per-question

| id | intent | top | gap | div | ground | kw | degraded |
|---|---|---:|---:|---:|---:|:---:|:---:|
| q01 | why-decision | 1.000 | 0.000 | 1.000 | — | ✗ |  |
| q02 | how-it-works | 1.000 | 0.000 | 1.000 | — | ✗ |  |
| q03 | why-decision | 1.000 | 0.000 | 1.000 | — | ✗ |  |
| q04 | how-it-works | 1.000 | 0.000 | 0.971 | — | ✗ |  |
| q05 | state-lookup | 1.000 | 0.000 | 1.000 | — | ✗ |  |
| q06 | why-decision | 1.000 | 0.000 | 0.783 | — | ✗ |  |
| q07 | how-it-works | 1.000 | 0.000 | 0.971 | — | ✗ |  |
| q08 | state-lookup | 1.000 | 0.000 | 1.000 | — | ✗ |  |
| q09 | how-it-works | 1.000 | 0.000 | 0.971 | — | ✗ |  |
| q10 | how-it-works | 1.000 | 0.000 | 0.971 | — | ✗ |  |
| q11 | why-decision | 1.000 | 0.000 | 1.000 | — | ✗ |  |
| q12 | how-it-works | 1.000 | 0.000 | 0.000 | — | ✗ |  |
| q13 | state-lookup | 1.000 | 0.000 | 0.783 | — | ✗ |  |
| q14 | state-lookup | 1.000 | 0.000 | 0.881 | — | ✗ |  |
| q15 | why-decision | 1.000 | 0.000 | 0.971 | — | ✗ |  |
| q16 | state-lookup | 1.000 | 0.000 | 0.881 | — | ✗ |  |
| q17 | why-decision | 1.000 | 0.000 | 0.971 | — | ✗ |  |
| q18 | how-it-works | 1.000 | 0.000 | 1.000 | — | ✗ |  |
| q19 | state-lookup | 1.000 | 0.000 | 0.469 | — | ✗ |  |
| q20 | how-it-works | 1.000 | 0.000 | 0.971 | — | ✗ |  |
| q21 | why-decision | 1.000 | 0.000 | 1.000 | — | ✗ |  |
| q22 | adversarial | 1.000 | 0.000 | 0.881 | — | ✗ |  |
| q23 | why-decision | 1.000 | 0.000 | 0.971 | — | ✗ |  |
| q24 | how-it-works | 1.000 | 0.000 | 0.971 | — | ✗ |  |
| q25 | how-it-works | 1.000 | 0.000 | 0.971 | — | ✗ |  |
| q26 | cross-source | 1.000 | 0.000 | 0.971 | — | ✗ |  |
| q27 | how-it-works | 1.000 | 0.000 | 0.722 | — | ✗ |  |
| q28 | why-decision | 1.000 | 0.000 | 1.000 | — | ✗ |  |
| q29 | how-it-works | 1.000 | 0.000 | 0.722 | — | ✗ |  |
| q30 | why-decision | 1.000 | 0.000 | 1.000 | — | ✗ |  |
| q31 | adversarial | 1.000 | 0.000 | 0.722 | — | ✗ |  |
| q32 | how-it-works | 1.000 | 0.000 | 0.971 | — | ✗ |  |
| q33 | why-decision | 1.000 | 0.000 | 0.971 | — | ✗ |  |
| q34 | state-lookup | 1.000 | 0.000 | 0.971 | — | ✗ |  |
| q35 | adversarial | 1.000 | 0.000 | 0.881 | — | ✗ |  |
| q36 | state-lookup | 1.000 | 0.000 | 0.971 | — | ✗ |  |
| q37 | cross-source | 1.000 | 0.000 | 0.971 | — | ✗ |  |
| q38 | adversarial | 1.000 | 0.000 | 0.881 | — | ✗ |  |
| q39 | state-lookup | 1.000 | 0.000 | 0.469 | — | ✗ |  |
| q40 | state-lookup | 1.000 | 0.000 | 0.971 | — | ✗ |  |

## Coverage gaps (weakest 10 questions)

- `q01` (gap=0.000): Why did we replace LanceDB with sqlite-vec?
- `q02` (gap=0.000): How does the enrichment pipeline run shell, claude, and browser sources concurrently?
- `q03` (gap=0.000): What fixed the Claude session hook PID problem?
- `q04` (gap=0.000): How does the Firefox browser source send events to the daemon?
- `q05` (gap=0.000): What runs in the OTel observability stack locally?
- `q06` (gap=0.000): What is the north star vision for hippo?
- `q07` (gap=0.000): How are secrets redacted before storage?
- `q08` (gap=0.000): What is the current schema version and what changed in v6?
- `q09` (gap=0.000): How does hybrid retrieval combine vector and lexical scores?
- `q10` (gap=0.000): What does a degraded-mode response from rag.ask look like?
