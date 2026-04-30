# Retrieval Evaluation Harness

**Status:** Live reference. This document describes the current `hippo-eval` harness as it ships against `main`.

## Motivation

Hippo's retrieval pipeline (sqlite-vec + FTS5 hybrid since v0.20) needs quantitative answers to:

1. Does retrieval return the right nodes for a question? (Recall@K, MRR, NDCG)
2. Are results diverse across sources, or do they collapse onto one Claude session? (source diversity, near-duplicate density)
3. Do `ask()` answers stay grounded in sources, or hallucinate? (LLM-judge groundedness)
4. Does the corpus have structural cohesion — do nodes from the same project cluster in embedding space? (embedding cohesion)
5. For an individual query, does the top-K evidence look strong or weak? (coverage gap score)

`hippo-eval` runs a labeled Q/A set against the live corpus and reports all of the above.

## CLI

```bash
hippo-eval run --questions <path>                  # run all questions, emit metrics
hippo-eval run --questions <path> --mode <mode>    # restrict to one retrieval mode
hippo-eval baseline                                # capture a baseline for diff comparisons
hippo-eval compare <baseline> <current>            # diff two runs
```

`--mode` values: `semantic`, `lexical`, `hybrid`, `recent`. All four are functional against the live `main` (sqlite-vec for `semantic`, FTS5 for `lexical`, score fusion for `hybrid`, recency-only for `recent`).

## Question set

The labeled question set lives in `brain/eval/questions.jsonl`. Each entry:

```json
{
  "id": "q-123",
  "question": "...",
  "expected_node_uuids": ["uuid-1", "uuid-2"],
  "tags": ["claude-session", "rag-quality"]
}
```

Targets 30–50 questions drawn from hippo's development history. Adding a question:

1. Pick a real recent activity that produced retrievable nodes.
2. Write the question as a user would ask it.
3. Run the question through `hippo ask --raw` to find the relevant node UUIDs.
4. Append to `questions.jsonl`.
5. Run `hippo-eval run` to confirm the new question's metrics.

## Metrics

Per-question:

- **Recall@K** — fraction of expected UUIDs in the top-K retrieved hits.
- **MRR** — mean reciprocal rank of the first expected hit.
- **NDCG@K** — normalized discounted cumulative gain.
- **Source diversity** — number of distinct `source_kind` values in top-K.
- **Coverage gap** — heuristic: distance between top-K's mean similarity and the threshold considered "strong evidence" for that question type.
- **Groundedness** (when `--llm-judge` is set) — LM-Studio-judged 0/1 score for whether `ask()`'s answer is supported by the retrieved sources.

Aggregate: macro-mean of each metric across the question set, plus per-tag breakouts.

## Degradation

`hippo-eval run` exits non-zero if:

- Any expected node UUID is missing from the live DB (the question set has stale references — fix by re-running step 3 above).
- Aggregate Recall@5 falls below the configured floor (default 0.6).
- The brain HTTP server is unreachable.

For diagnostic purposes (e.g., "did the prompt change tank recall on a specific tag?"), `hippo-eval compare` prints per-question deltas between two runs.

## Implementation

Lives at `brain/src/hippo_brain/eval/`. Entry point: `brain/src/hippo_brain/eval/cli.py`.

Tests: `brain/tests/test_eval_*.py`.

## See also

- [`brain/README.md`](../brain/README.md) — brain HTTP server + MCP server
- [`docs/capture/`](capture/) — capture-reliability stack (the data the harness queries)
- Historical LanceDB-era design record: [`docs/archive/feature-waves/2026-04-17-eval-harness-design.md`](archive/feature-waves/2026-04-17-eval-harness-design.md)
