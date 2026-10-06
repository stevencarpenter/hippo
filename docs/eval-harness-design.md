# Retrieval Evaluation Harness

**Status:** Live reference. This document describes the current `hippo-eval` harness as it ships against `main`.

## Motivation

Hippo's retrieval pipeline (sqlite-vec + FTS5 hybrid since v0.20) needs quantitative answers to:

1. Does retrieval return the right nodes for a question? (Recall@K, MRR, NDCG)
2. Are results diverse across capture source types? (source diversity)
3. Do `ask()` answers stay grounded in sources, or hallucinate? (LLM-judge groundedness)
4. For an individual query, does the top-K evidence look strong or weak? (coverage gap score)

`hippo-eval` runs a labeled Q/A set against the live corpus and reports all of the above.

## CLI

`hippo-eval` is a single-command CLI (defined as the `hippo-eval` console script in `brain/pyproject.toml`, dispatching to `hippo_brain.evaluation:main`). It runs the full labeled set in one pass and writes a scorecard:

```bash
uv run --project brain hippo-eval                        # run with defaults
uv run --project brain hippo-eval --mode hybrid          # pick retrieval mode
uv run --project brain hippo-eval --subset q01,q02       # subset of question ids
uv run --project brain hippo-eval --no-synthesis         # skip ask() synthesis
uv run --project brain hippo-eval --no-judge             # skip LM-judge groundedness
uv run --project brain hippo-eval --questions <path>     # override questions file
uv run --project brain hippo-eval --out scorecard.md    # write Markdown scorecard
```

All flags (verbatim, source: `_parse_args` in `evaluation.py`):

| Flag | Default | Notes |
|---|---|---|
| `--questions` | [Default question set](#question-set) | Path to an explicit labeled question set; overrides the default. |
| `--mode` | `hybrid` | One of `hybrid`, `semantic`, `lexical`, `recent`. |
| `--limit` | `10` | Top-K size for retrieval. |
| `--out` | `""` | Write the Markdown scorecard to this file; its parent directory must exist. Otherwise print to stdout. |
| `--subset` | `""` | Comma-separated question ids; empty = all. |
| `--no-synthesis` | off | Skip `ask()` synthesis (retrieval-only). |
| `--no-judge` | off | Skip LM-judge groundedness scoring. |

There is no `run` / `baseline` / `compare` subcommand surface. To compare two runs, write separate Markdown scorecard files with `--out` and diff them externally.

## Question set

The authoritative default question set is `brain/tests/eval_questions.json`. Wheel builds map its exact bytes to `hippo_brain/_fixtures/default_eval_questions.json`; `_DEFAULT_QUESTIONS` uses that installed resource, falling back to the tests file in a source checkout. The separate historical `_fixtures/eval_questions.json` is not the default corpus. Packaging does not change the 40 questions or their labels. Explicit `--questions` selection remains supported.

The file is a JSON object whose `questions` array contains the labeled entries; each entry is loaded by `load_questions` into the `Question` dataclass (`brain/src/hippo_brain/evaluation.py`):

```json
{
  "id": "q01",
  "question": "Why did we replace LanceDB with sqlite-vec?",
  "intent": "why-decision",
  "relevant_knowledge_node_uuids": [
    "e4397aa3-520d-4d5e-a1ab-56f9411bba2b"
  ],
  "acceptable_answer_keywords": ["sqlite-vec", "consolidation"],
  "source_bias": "claude",
  "coverage_gap_reason": ""
}
```

Field meanings (from the file's own `schema` block):

| Field | Meaning |
|---|---|
| `id` | Stable unique id (e.g. `q01`). |
| `question` | Natural-language user query. |
| `intent` | One of `why-decision`, `how-it-works`, `state-lookup`, `cross-source`, `adversarial`. |
| `relevant_knowledge_node_uuids` | Known-good node UUIDs, labeled against the live corpus on the `labeled_at` date. |
| `acceptable_answer_keywords` | At least one MUST appear in a good answer (drives the `keyword_hit` boolean). |
| `source_bias` | `shell`, `claude`, `browser`, or `mixed`. |

The file's `schema` block also documents a `coverage_gap_reason` field for entries where `relevant_knowledge_node_uuids` is empty. `load_questions` loads this labeling annotation into `Question`; the Markdown scorecard summarizes nonempty annotations as reason counts. The annotation does not determine the numeric coverage-gap score.

Targets 30–50 questions drawn from hippo's own development history. Adding a question:

1. Pick a real recent activity that produced retrievable nodes.
2. Write the question as a user would ask it.
3. With the brain server running, use `uv run --project brain hippo-brain-api query "<text>" --mode lexical`; inspect `nodes[].uuid` and label the relevant nodes.
4. Append to `eval_questions.json` under `questions`.
5. Run `uv run --project brain hippo-eval --subset <new-id>` to confirm metrics.

## Metrics

Per-question (computed in `evaluation.py`):

- **Recall@K** — fraction of `relevant_knowledge_node_uuids` present in the top-K retrieved hits.
- **MRR** — mean reciprocal rank of the first expected hit.
- **NDCG@K** — normalized discounted cumulative gain.
- **Source diversity**: normalized Shannon entropy of linked source-type occurrences across top-K hits, in `[0, 1]` (`source_diversity` in `evaluation.py`). 0 means no source types or one type; 1 means an even distribution among the observed types.
- **Coverage gap score**: fraction of semantic mode scores below a threshold (default 0.5). This is a distance-score heuristic, not an answerability measurement. Hybrid, lexical, and recent modes report an undefined value for nonempty results because relative ranks cannot establish corpus coverage. Empty retrieval reports 1.0.
- **Groundedness** — LM-judge 0/1 score for whether `ask()`'s answer is supported by the retrieved sources (skipped under `--no-judge`).
- **Keyword hit** — boolean: at least one of `acceptable_answer_keywords` appears in the synthesized answer.

The Markdown scorecard reports means and medians for recall, MRR, NDCG, source diversity, coverage gap, groundedness and keyword-hit rate, excluding undefined measurements. It also reports latency percentiles and per-enrichment-model aggregates when model labels are available. Per-question rows include the intent; there are no per-intent or per-source-bias aggregate sections.

`near_duplicate_density` and `embedding_cohesion` are standalone metric helpers. The CLI does not measure or emit them.

Retrieval results expose `score_semantics`. Hybrid RRF, lexical, and recent results use `relative_rank`; semantic results use `recency_adjusted_cosine`. The `min_score` setting applies only to semantic mode. Ranking order and normalized hybrid scores remain unchanged. The [confidence reference](capture/confidence-scoring.md) owns confidence interpretation and caps.

## Degradation

`hippo-eval` exits with a non-zero code if `--subset` matches no questions. It does not currently enforce a minimum recall floor or fail on missing UUIDs. For diagnostic comparisons across runs, write separate Markdown scorecard files with `--out` and diff them externally.

## Implementation

Lives at `brain/src/hippo_brain/evaluation.py`. Entry point: the `hippo-eval` console script in `brain/pyproject.toml`, which dispatches to `hippo_brain.evaluation:main`. There is no separate `eval/` package or `eval/cli.py` module today.

Tests: `brain/tests/test_evaluation*.py` and adjacent metric-function tests.

## See also

- [`brain/README.md`](../brain/README.md) — brain HTTP server + MCP server
- [`docs/capture/`](capture/) — capture-reliability stack (the data the harness queries)
- Historical LanceDB-era design record: [`docs/archive/feature-waves/2026-04-17-eval-harness-design.md`](archive/feature-waves/2026-04-17-eval-harness-design.md)
