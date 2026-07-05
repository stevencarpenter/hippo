# Confidence scoring (SNUG-126)

Explainable confidence on retrieval hits and evidence-backed responses. Implemented in `brain/src/hippo_brain/confidence_scoring.py`.

## Output shape

Each hit carries a `confidence` object:

| Field | Meaning |
|---|---|
| `level` | `high`, `medium`, `low`, or `insufficient` |
| `score` | Bounded composite in `[0, 1]` from named factors |
| `factors` | Weighted factor breakdown (`name`, `weight`, `score`, `contribution`, `detail`) |
| `explanation` | Agent-readable one-liner summarizing top factors |
| `withheld` | `true` when level is `insufficient` |

## Factors

| Factor | Weight | Inputs |
|---|---|---|
| `evidence` | 0.30 | Inspectable evidence packet count |
| `source_diversity` | 0.15 | Distinct `source_kind` families |
| `recency` | 0.15 | Hit `captured_at` age |
| `retrieval_match` | 0.20 | Hybrid/semantic/lexical retrieval score |
| `capture_health` | 0.15 | Inline `evidence[].freshness.status` |
| `context_alignment` | 0.05 | `cwd` / `git_branch` present |

## Limitations — when confidence is withheld or capped

- **No evidence → `insufficient`.** Hits without inspectable evidence packets are never rated high confidence, regardless of retrieval score.
- **Stale/failing capture → capped.** `stale`, `failing`, `expected_absent`, or `unknown` freshness on cited sources prevents `high` and often yields `low`.
- **Single weak packet → capped at medium.** One evidence packet needs very strong match + health to reach `high`.
- **No conflict detection yet.** This module does not detect contradictory newer evidence (see SNUG-127); do not treat `high` as contradiction-free.
- **Enrichment health excluded.** Queue depth and LLM enrichment status are intentionally omitted — capture health only.

## Surfaces

- Retrieval `SearchResult.confidence` (all `search()` modes)
- MCP `search_hybrid` / `search_knowledge` dicts
- `agent_query` hits
- RAG `/ask` sources (when hits flow through `_result_to_hit`)
