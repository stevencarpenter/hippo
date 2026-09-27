# Confidence scoring (SNUG-126)

Confidence is an evidence-quality heuristic, not a probability of correctness or an answerability measurement. Implemented in `brain/src/hippo_brain/confidence_scoring.py`.

## Output shape

Each hit carries a `confidence` object:

| Field | Meaning |
|---|---|
| `level` | `high`, `medium`, `low`, or `insufficient` |
| `score` | Bounded composite in `[0, 1]` from named factors |
| `factors` | Weighted factor breakdown (`name`, `weight`, `score`, `contribution`, `detail`) |
| `explanation` | Agent-readable one-liner summarizing top factors |
| `withheld` | `true` when level is `insufficient` |
| `score_semantics` | Retrieval scale; see the [score contract](../eval-harness-design.md#metrics) |
| `relevance_calibrated` | `false`; relevance has not been calibrated |
| `uncapped_score` | Original score retained when conflict analysis changes the confidence level |

## Factors

| Factor | Weight | Inputs |
|---|---|---|
| `evidence` | 0.30 | Inspectable evidence packet count |
| `source_diversity` | 0.15 | Distinct `source_kind` families |
| `recency` | 0.15 | Hit `captured_at` age |
| `retrieval_match` | 0.20 | Semantic distance score; relative ranks contribute zero |
| `capture_health` | 0.15 | Inline `evidence[].freshness.status` |
| `context_alignment` | 0.05 | `cwd` / `git_branch` present |

## Limitations — when confidence is withheld or capped

- **Relative rank cannot yield high confidence.** Hybrid, lexical, and recent scores contribute no relevance confidence; their composite is capped below the high boundary. Relative ranking does not establish relevance or answerability.
- **No evidence → `insufficient`.** Hits without inspectable evidence packets are never rated high confidence, regardless of retrieval score.
- **Stale/failing capture → capped.** `stale`, `failing`, `expected_absent`, or `unknown` freshness on cited sources prevents `high` and often yields `low`.
- **Single weak packet → capped at medium.** One evidence packet needs very strong match + health to reach `high`.
- **Conflict caps (SNUG-127).** `conflict_detection.analyze_conflicts` surfaces outcome disagreements, decision contradictions, and stale-only evidence; `apply_conflict_confidence_caps` lowers per-hit confidence when conflicts are unresolved, aligning its numeric score and explanation with the capped level. See [agent-query conflict semantics](../mcp-reference.md#tool-selection-guide). Do not treat `high` as contradiction-free until conflicts are reviewed.
- **Enrichment health excluded.** Queue depth and LLM enrichment status are intentionally omitted — capture health only.

## Surfaces

- Retrieval `SearchResult.confidence` (all `search()` modes)
- MCP `search_hybrid` / `search_knowledge` dicts
- `agent_query` hits
- RAG `/ask` sources (when hits flow through `_result_to_hit`)
