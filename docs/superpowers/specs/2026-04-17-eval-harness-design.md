# Retrieval Evaluation Harness — Design

**Status:** Wave 2 deliverable (team `hippo-sqlite-vec`, task #10)
**Author:** metrics-designer
**Date:** 2026-04-17
**Branch:** `postgres` (throwaway experimental)

## Motivation

Wave 1 delivered a substantively new retrieval pipeline (sqlite-vec + FTS5 +
RRF + MMR). The reviewer scorecard (`2026-04-17-sqlite-vec-consolidation-scorecard.md`)
flagged criterion #12 ("benchmark hybrid ≥ LanceDB on a fixed eval set") as
**partial**: an eval set exists (`brain/tests/eval_questions.json` — 10 topic
questions), but has no ground-truth labels and no quantitative metrics.

Concretely, the project has no way today to answer:

1. Does retrieval actually return the right nodes for a question? (Recall@K, MRR, NDCG)
2. Are results diverse across sources, or do they collapse onto one Claude
   session? (source diversity, near-duplicate density)
3. Do `ask()` answers stay grounded in sources, or hallucinate? (LLM-judge
   groundedness)
4. Does the corpus have structural cohesion — do nodes from the same project
   cluster in embedding space? (embedding cohesion)
5. For an individual query, does the top-K evidence look strong or weak?
   (coverage gap score)

This spec delivers a harness (`hippo-eval`) that answers all of the above from
a labeled Q/A set against the live corpus.

## Scope

**In scope**

- 30–50 labeled questions drawn from hippo's development history
- Pure-function quantitative metrics (Recall@K, MRR, NDCG, diversity,
  near-duplicate density, coverage gap)
- Qualitative LLM-judge metric (groundedness) + heuristic coherence
- Embedding cohesion computed over existing vec0 vectors
- CLI `hippo-eval` that emits a Markdown scorecard
- Unit tests for each metric + small end-to-end integration test

**Out of scope**

- A/B comparison vs. LanceDB (impossible — removed on this branch; covered in
  scorecard §12 as a known limitation)
- Relevance labels curated by hand against the 1,878 real nodes (expensive;
  deferred — harness supports labels as they accrue)
- Continuous benchmarking / CI integration (follow-up)
- Retraining or fine-tuning the embedding model

## Labeled Q/A set

**File:** `brain/tests/eval_questions.json`

**Schema (per question):**

```json
{
  "id": "q01",
  "question": "...",
  "intent": "why-decision | how-it-works | state-lookup | cross-source | adversarial",
  "relevant_knowledge_node_uuids": ["..."],
  "acceptable_answer_keywords": ["term1", "term2"],
  "source_bias": "shell | claude | browser | mixed"
}
```

**Curation methodology**

Questions are drawn from memory files + recent specs + scorecard follow-ups.
We group them in five buckets:

| Bucket | Target count | Examples |
|---|---:|---|
| Past technical decisions | 10 | "Why sqlite-vec over LanceDB?", "Why the grandparent-PID fix?" |
| Known bug fixes | 8 | "How do we handle null LLM responses in enrichment?" |
| Project state lookups | 8 | "What schema version is live?", "Which domains does the Firefox extension allowlist?" |
| Cross-source queries | 6 | Questions that should pull from shell + claude + browser together |
| Adversarial / hard | 8 | Punctuation-heavy questions (FTS5 trap), intentional ambiguity, multi-hop |

`relevant_knowledge_node_uuids` is left empty by default — labels will be
filled in progressively as corpus-analyst (#12) surfaces concrete nodes.
Today the harness treats empty labels as "no recall ground truth available"
and reports metrics that don't require labels (source diversity, near-dup
density, coverage gap, groundedness).

`acceptable_answer_keywords` is the *minimum* grounding heuristic — the
generated answer must mention at least one of them for the answer to count
as on-topic. This is complementary to the LLM-judge groundedness score.

## Metrics

### Quantitative (pure functions, no LLM)

Implemented in `brain/src/hippo_brain/eval.py`.

| Function | Signature | Definition |
|---|---|---|
| `recall_at_k` | `(retrieved: list[str], relevant: set[str], k: int) -> float` | \|retrieved[:k] ∩ relevant\| / \|relevant\|; returns NaN if \|relevant\|=0 |
| `mrr` | `(retrieved: list[str], relevant: set[str]) -> float` | 1/rank of first relevant hit, 0 if none; NaN if relevant empty |
| `ndcg_at_k` | `(retrieved: list[str], relevance: dict[str,float], k: int) -> float` | DCG@k / IDCG@k; log2 discount, relevance score from dict (default 0) |
| `source_diversity` | `(hits: list[SearchResult] \| list[dict]) -> float` | Shannon entropy of source_type distribution (shell/claude/browser/unknown), normalized to [0, 1] |
| `near_duplicate_density` | `(vectors: list[list[float]]) -> float` | Mean pairwise cosine similarity across returned hits; higher = more duplication = worse |
| `coverage_gap_score` | `(scores: list[float], threshold: float = 0.5) -> float` | Fraction of top-K scores below `threshold`. 0 = strong evidence, 1 = all weak |

All functions handle edge cases (empty input, missing vectors) without
raising; they return `float('nan')` where a metric is undefined.

### Qualitative

| Function | Signature | Notes |
|---|---|---|
| `groundedness` | `async (answer: str, sources: list[dict], lm_client, model) -> float` | LLM-judge prompt: "score 0.0–1.0 — every factual claim in answer backed by a source". Parse float from first line of response. Returns `nan` on any error. |
| `summary_coherence` | `(summary: str, entities: list[str]) -> bool` | Heuristic — case-insensitive substring match of at least one entity in the summary. Cheap, no LLM. |
| `embedding_cohesion` | `(conn, project: str, sample: int = 200) -> float` | Mean cosine between vectors whose joined event.git_repo or cs.project_dir matches `project`, divided by mean cosine of a random sample. Ratio > 1 means the project cluster is tighter than background. |

## Source-type derivation

`SearchResult` does not carry a `source_type` field today. We derive it from
the linked tables:

- If `linked_event_ids` non-empty → counts as `shell`
- If any `knowledge_node_claude_sessions` row matches → counts as `claude`
- If any `knowledge_node_browser_events` row matches → counts as `browser`

A node can belong to multiple sources; for `source_diversity` we count each
observed source once per hit (multiset over the top-K), so a mixed-source
node contributes to all of its source buckets.

## CLI

**Entry point:** `hippo-eval` (wired in `pyproject.toml [project.scripts]`).

**Usage:**

```
hippo-eval                              # run full suite, print to stdout
hippo-eval --questions path.json        # override Q/A file
hippo-eval --out scorecard.md           # write to file
hippo-eval --mode hybrid                # retrieval mode (hybrid|semantic|lexical|recent)
hippo-eval --limit 10                   # top-K
hippo-eval --no-synthesis               # skip rag.ask() — retrieval metrics only
hippo-eval --no-judge                   # skip LLM-judge groundedness
hippo-eval --subset q01,q02,q03         # run only these ids
```

Config sourcing: same `~/.config/hippo/config.toml` loader that `hippo-mcp`
uses, so `db_path`, `lmstudio_base_url`, `embedding_model`, and `query_model`
all come from the one source of truth.

**Graceful degradation:**

- If LM Studio is unreachable → skip synthesis + judge, report retrieval-only
- If a question has no `relevant_knowledge_node_uuids` → report NaN for
  recall/MRR/NDCG and note "no labels"
- If the corpus is empty → abort with an actionable error

## Output shape

Markdown scorecard with four sections:

1. **Summary table** — one row per metric, reporting mean + median across all
   questions (NaN rows excluded).
2. **Per-question table** — id, question, retrieval mode, top score, diversity,
   groundedness, pass/fail against keyword heuristic.
3. **Coverage gaps** — top-10 questions with highest `coverage_gap_score`,
   highlighting where the corpus is weakest.
4. **Notes** — corpus stats, timing, config values, degradation reasons.

## How to run

```bash
# default: scorecard against the live corpus
uv run --project brain hippo-eval

# dry-run without LM Studio:
uv run --project brain hippo-eval --no-synthesis --no-judge

# save scorecard:
uv run --project brain hippo-eval --out scorecard-$(date +%F).md
```

## Relationship to corpus-analyst (#12)

Any concrete corpus anomalies corpus-analyst surfaces (e.g. known orphan
locks, empty relationships, overly noise-heavy nodes) can be added as
adversarial questions — the harness design supports progressive enrichment of
the Q/A set without code changes.

## Validation plan

- `pytest brain/tests/test_eval.py -v` — exercise each metric on synthetic
  fixtures + a 3-question integration against an in-memory tmp DB
- `ruff check` + `ruff format --check` clean
- Semgrep scan clean on the new file
- Manual smoke: `hippo-eval --subset q01,q02,q03 --no-synthesis --no-judge`
  on the live DB — output is well-formed Markdown

## Non-goals / explicit pushback

- **No auto-grading of answer quality beyond keywords + LLM-judge.** A stricter
  rubric (exact reference match, span-level citation accuracy) would require
  hand-annotated reference answers, which don't exist today.
- **No regression gate.** The scorecard is reported; no CI threshold is
  proposed. Set thresholds after the first backfill run produces a baseline.
- **No fancy ranking metrics like BPref.** NDCG is sufficient for the label
  density we have.
