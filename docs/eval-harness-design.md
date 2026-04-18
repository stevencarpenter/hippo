# Retrieval Evaluation Harness — Design

**Status:** Active
**Author:** metrics-designer (ported from the sqlite-vec consolidation branch)
**Ported:** 2026-04-17

## Motivation

Hippo has a retrieval pipeline (LanceDB cosine-NN on `main`) but no
quantitative way today to answer:

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
- CLI `hippo-eval` that emits a Markdown scorecard
- Unit tests for each metric + small end-to-end integration test

**Out of scope**

- A/B comparison vs. the sqlite-vec consolidation branch (covered there)
- Relevance labels curated by hand against every node (expensive; deferred —
  harness supports labels as they accrue)
- Continuous benchmarking / CI integration (follow-up)
- Retraining or fine-tuning the embedding model

## Retrieval-mode coverage on main

This harness ships against `main`, where retrieval is LanceDB cosine-NN only.
Only `--mode semantic` is functional here. `hybrid`, `lexical`, and `recent`
remain in the CLI surface so existing invocations don't error, but they
produce a degraded `QuestionResult` with a clear `"not available on this
branch"` message until the sqlite-vec + FTS5 migration lands.

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

| Bucket | Delivered | Examples |
|---|---:|---|
| Past technical decisions (why-decision) | 11 | "Why sqlite-vec over LanceDB?", "Why the grandparent-PID fix?" |
| Known bug fixes / how-it-works | 13 | "How do we handle null LLM responses in enrichment?" |
| Project state lookups | 10 | "What schema version is live?", "Which domains does the Firefox extension allowlist?" |
| Cross-source queries | 2 | Questions that should pull from shell + claude + browser together |
| Adversarial / hard | 4 | Punctuation-heavy questions (FTS5 trap), intentional ambiguity, multi-hop |

`relevant_knowledge_node_uuids` is empty on questions where the corpus
demonstrably cannot answer — those questions still exercise the
coverage-gap metric.

`acceptable_answer_keywords` is the *minimum* grounding heuristic — the
generated answer must mention at least one of them to count as on-topic.

## Metrics

### Quantitative (pure functions, no LLM)

Implemented in `brain/src/hippo_brain/evaluation.py`.

| Function | Signature | Definition |
|---|---|---|
| `recall_at_k` | `(retrieved: list[str], relevant: set[str], k: int) -> float` | \|retrieved[:k] ∩ relevant\| / \|relevant\|; returns NaN if \|relevant\|=0 |
| `mrr` | `(retrieved: list[str], relevant: set[str]) -> float` | 1/rank of first relevant hit, 0 if none; NaN if relevant empty |
| `ndcg_at_k` | `(retrieved: list[str], relevance: dict[str,float], k: int) -> float` | DCG@k / IDCG@k; log2 discount, relevance score from dict (default 0) |
| `source_diversity` | `(sources_per_hit: list[list[str]]) -> float` | Shannon entropy of source-type distribution (shell/claude/browser/workflow), normalized to [0, 1] |
| `near_duplicate_density` | `(vectors: list[list[float]]) -> float` | Mean pairwise cosine similarity across returned hits; higher = more duplication = worse |
| `coverage_gap_score` | `(scores: list[float], threshold: float = 0.5) -> float` | Fraction of top-K scores below `threshold`. 0 = strong evidence, 1 = all weak |

All functions handle edge cases (empty input, missing vectors) without
raising; they return `float('nan')` where a metric is undefined.

### Qualitative

| Function | Signature | Notes |
|---|---|---|
| `groundedness` | `async (answer: str, sources: list[dict], lm_client, model) -> float` | LLM-judge prompt: "score 0.0–1.0 — every factual claim in answer backed by a source". Parse float from first line of response. Returns `nan` on any error. |
| `summary_coherence` | `(summary: str, entities: list[str]) -> bool` | Heuristic — case-insensitive substring match of at least one entity in the summary. Cheap, no LLM. |
| `embedding_cohesion` | `(conn, project: str, sample: int = 200) -> float` | Mean cosine between vectors for a project divided by background mean cosine. **Requires the sqlite-vec `knowledge_vectors` table; returns `nan` on main.** |

## Source-type derivation

Retrieval hits do not carry a `source_type` field. We derive it from the
linked tables in SQLite:

- If `knowledge_node_events` links → counts as `shell`
- If `knowledge_node_claude_sessions` links → counts as `claude`
- If `knowledge_node_browser_events` links → counts as `browser`
- If `knowledge_node_workflow_runs` links → counts as `workflow`

A node can belong to multiple sources; for `source_diversity` we count each
observed source once per hit (multiset over the top-K).

## CLI

**Entry point:** `hippo-eval` (wired in `brain/pyproject.toml [project.scripts]`).

**Usage:**

```
hippo-eval                              # run full suite, print to stdout
hippo-eval --questions path.json        # override Q/A file
hippo-eval --out scorecard.md           # write to file
hippo-eval --mode semantic              # retrieval mode (semantic only on main)
hippo-eval --limit 10                   # top-K
hippo-eval --no-synthesis               # skip rag.ask() — retrieval metrics only
hippo-eval --no-judge                   # skip LLM-judge groundedness
hippo-eval --subset q01,q02,q03         # run only these ids
```

Config sourcing: same `~/.config/hippo/config.toml` loader that `hippo-mcp`
uses (`_load_config`), so `db_path`, `lmstudio_base_url`, `embedding_model`,
and `query_model` all come from the one source of truth.

**Graceful degradation:**

- If LM Studio is unreachable → skip synthesis + judge, report retrieval-only
- If LanceDB is unreachable → synthesis + semantic retrieval both no-op
  cleanly with a `degraded=True` marker on each result
- If a question has no `relevant_knowledge_node_uuids` → report NaN for
  recall/MRR/NDCG
- If `--mode` is one of `hybrid|lexical|recent` → exit 0 with a stderr
  message explaining the mode is unavailable on this branch

## Output shape

Markdown scorecard with these sections:

1. **Header** — run time, duration, mode, limit, models.
2. **Corpus** — row counts for `knowledge_nodes`, `events`, `claude_sessions`.
3. **Summary table** — mean + median per metric plus `latency_ms_p50` / p95.
4. **Stratified by enrichment_model** — per-vintage metric rollup so model
   regressions are visible across enrichment generations.
5. **Caveats** — honest limitations of the current run.
6. **Per-question table** — id, intent, top score, diversity, groundedness,
   keyword hit, degraded marker.
7. **Coverage gaps** — top-10 weakest questions (highest `coverage_gap_score`).
8. **Errors** — any per-question retrieval/synthesis errors.

## How to run

```bash
# default: scorecard against the live corpus
uv run --project brain hippo-eval

# dry-run without LM Studio:
uv run --project brain hippo-eval --no-synthesis --no-judge

# save scorecard:
uv run --project brain hippo-eval --out scorecard-$(date +%F).md

# focused smoke:
uv run --project brain hippo-eval --subset q01,q02,q03 --mode semantic
```

## Validation plan

- `pytest brain/tests/test_evaluation.py -v` — exercise each metric on
  synthetic fixtures + a 3-question end-to-end smoke with a canned retriever
- `ruff check` + `ruff format --check` clean
- Semgrep scan clean on the new file
- Manual smoke: `hippo-eval --subset q01,q02,q03 --mode semantic` on the
  live DB — output is well-formed Markdown

## Non-goals / explicit pushback

- **No auto-grading of answer quality beyond keywords + LLM-judge.** A stricter
  rubric (exact reference match, span-level citation accuracy) would require
  hand-annotated reference answers, which don't exist today.
- **No regression gate.** The scorecard is reported; no CI threshold is
  proposed. Set thresholds after the first backfill run produces a baseline.
- **No fancy ranking metrics like BPref.** NDCG is sufficient for the label
  density we have.
