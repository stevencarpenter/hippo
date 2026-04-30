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

`hippo-eval` is a single-command CLI (defined as the `hippo-eval` console script in `brain/pyproject.toml`, dispatching to `hippo_brain.evaluation:main`). It runs the full labeled set in one pass and writes a scorecard:

```bash
uv run --project brain hippo-eval                        # run with defaults
uv run --project brain hippo-eval --mode hybrid          # pick retrieval mode
uv run --project brain hippo-eval --subset q01,q02       # subset of question ids
uv run --project brain hippo-eval --no-synthesis         # skip ask() synthesis
uv run --project brain hippo-eval --no-judge             # skip LM-judge groundedness
uv run --project brain hippo-eval --questions <path>     # override questions file
uv run --project brain hippo-eval --out <dir>            # write scorecard JSON
```

All flags (verbatim, source: `_parse_args` in `evaluation.py`):

| Flag | Default | Notes |
|---|---|---|
| `--questions` | `brain/tests/eval_questions.json` (i.e. `_DEFAULT_QUESTIONS`) | Path to the labeled question set. |
| `--mode` | `hybrid` | One of `hybrid`, `semantic`, `lexical`, `recent`. |
| `--limit` | `10` | Top-K size for retrieval. |
| `--out` | `""` | When set, writes the full scorecard JSON to this directory. |
| `--subset` | `""` | Comma-separated question ids; empty = all. |
| `--no-synthesis` | off | Skip `ask()` synthesis (retrieval-only). |
| `--no-judge` | off | Skip LM-judge groundedness scoring. |

There is no `run` / `baseline` / `compare` subcommand surface; "compare two runs" is an external diff over the JSON scorecards in `--out` directories.

## Question set

The labeled question set lives at `brain/tests/eval_questions.json` (resolved via `_DEFAULT_QUESTIONS = Path(__file__).parent.parent.parent / "tests" / "eval_questions.json"`). The file is a JSON object whose `questions` array contains the labeled entries; each entry is loaded by `load_questions` into the `Question` dataclass (`brain/src/hippo_brain/evaluation.py`):

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

The file's `schema` block also documents a `coverage_gap_reason` field for entries where `relevant_knowledge_node_uuids` is empty. As of v0.20, that field is informational only — `load_questions` and the `Question` dataclass in `evaluation.py` do not load it, and no metric consumes it. Treat it as a human-readable labeling note until the harness reads it explicitly.

Targets 30–50 questions drawn from hippo's own development history. Adding a question:

1. Pick a real recent activity that produced retrievable nodes.
2. Write the question as a user would ask it.
3. Run `hippo ask` (or `hippo query --raw <text>`) to find the relevant node UUIDs.
4. Append to `eval_questions.json` under `questions`.
5. Run `uv run --project brain hippo-eval --subset <new-id>` to confirm metrics.

## Metrics

Per-question (computed in `evaluation.py`):

- **Recall@K** — fraction of `relevant_knowledge_node_uuids` present in the top-K retrieved hits.
- **MRR** — mean reciprocal rank of the first expected hit.
- **NDCG@K** — normalized discounted cumulative gain.
- **Source diversity** — *normalized Shannon entropy* of `source_kind` distribution across top-K hits, in `[0, 1]` (`source_diversity` in `evaluation.py`). 0 means all hits share one source; 1 means uniform spread across all observed sources.
- **Near-duplicate density** — pairwise cosine-similarity density of top-K embeddings; high values flag duplicate-heavy retrievals.
- **Coverage gap score** — *fraction of top-K scores that fall below the configured threshold* (default 0.5; `coverage_gap_score` in `evaluation.py`). 0.0 means all hits are strong; 1.0 means none are.
- **Groundedness** — LM-judge 0/1 score for whether `ask()`'s answer is supported by the retrieved sources (skipped under `--no-judge`).
- **Keyword hit** — boolean: at least one of `acceptable_answer_keywords` appears in the synthesized answer.

Aggregate: macro-mean of each metric across the question set, plus per-`intent` and per-`source_bias` breakouts when emitted to `--out`.

## Degradation

`hippo-eval` exits with a non-zero code if `--subset` matches no questions. It does not currently enforce a minimum recall floor or fail on missing UUIDs; surfacing those as exit codes is open follow-up work. For diagnostic comparisons across runs, point `--out` at separate directories and diff the resulting JSON scorecards externally.

## Implementation

Lives at `brain/src/hippo_brain/evaluation.py`. Entry point: the `hippo-eval` console script in `brain/pyproject.toml`, which dispatches to `hippo_brain.evaluation:main`. There is no separate `eval/` package or `eval/cli.py` module today.

Tests: `brain/tests/test_evaluation*.py` and adjacent metric-function tests.

## See also

- [`brain/README.md`](../brain/README.md) — brain HTTP server + MCP server
- [`docs/capture/`](capture/) — capture-reliability stack (the data the harness queries)
- Historical LanceDB-era design record: [`docs/archive/feature-waves/2026-04-17-eval-harness-design.md`](archive/feature-waves/2026-04-17-eval-harness-design.md)
