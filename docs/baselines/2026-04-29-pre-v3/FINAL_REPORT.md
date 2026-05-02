# Hippo v3 re-enrichment — expert panel scorecard

Sample: 100 stratified nodes from 2,256 currently-v3 nodes (see `dossier.jsonl`).
Model: `qwen3.6-35b-a3b-ud-mlx`.
Panel: 5 experts, independent, same dossier.
Date: 2026-04-29.

## Per-expert means

| expert | accuracy | succinctness | usefulness | ask | mcp | overall |
|---|---|---|---|---|---|---|
| enrichment | 4.74 | 4.60 | 4.59 | 3.79 | 4.42 | 4.43 |
| vector     | 4.26 | 3.15 | 2.46 | 3.23 | 3.31 | 3.28 |
| schema     | 4.82 | 4.51 | 4.38 | 4.94 | 4.43 | 4.62 |
| rag        | 4.36 | 3.73 | 3.26 | 3.44 | 4.02 | 3.76 |
| mcp        | 4.78 | 4.53 | 4.33 | 4.50 | 3.80 | 4.39 |
| **panel mean** | **4.59** | **4.10** | **3.80** | **3.98** | **4.00** | **4.10** |

Cross-expert stdev per dim: accuracy 0.26, succinctness 0.64,
usefulness 0.91, ask 0.72, mcp 0.47.

## Inter-rater agreement

| dim | tight (Δ≤1) | medium (Δ=2) | wide (Δ≥3) |
|---|---|---|---|
| accuracy        | 57 | 39 | 4 |
| succinctness    | 19 | 36 | 45 |
| usefulness      | 12 | 38 | 50 |
| ask_suitability | 34 | 16 | 50 |
| mcp_suitability | 26 | 31 | 43 |

The panel agrees on accuracy. They diverge widely on usefulness, ask, and mcp —
expected, because each expert weights these against their lens. The wide spread
on usefulness is mostly the vector specialist scoring against identifier
density, while the schema/mcp specialists score against structural utility.

## Worst 10 nodes (consensus)

| # | uuid | source | stratum | overall |
|---|---|---|---|---|
| 1 | 25b32204… | claude | long_content | 3.16 |
| 2 | 6dd039b8… | claude | long_content | 3.16 |
| 3 | b63c3f9b… | claude | short_embed_text | 3.28 |
| 4 | b239a21e… | shell  | shell_random | 3.28 |
| 5 | c1260596… | claude | claude_random | 3.40 |
| 6 | 1b1ac570… | shell  | shell_random | 3.40 |
| 7 | 87976564… | claude | claude_random | 3.44 |
| 8 | 5bbbc30a… | dual   | short_embed_text | 3.48 |
| 9 | 432b32b2… | dual   | dual_source | 3.48 |
| 10| da979c70… | shell  | shell_random | 3.52 |

`25b32204…` and `6dd039b8…` are the duplicated-enrichment pair the enrichment
expert flagged. Both ironically score *high* on MCP search-input quality
(identifier-dense embed_text) and *low* on accuracy + ask suitability
(fabricated env_vars/flags + orphaned key_decisions).

## Mean by stratum + source

| stratum | n | mean overall |
|---|---|---|
| topup_random      | 2  | 4.44 |
| claude_random     | 49 | 4.30 |
| long_content      | 10 | 4.04 |
| shell_random      | 24 | 3.90 |
| short_embed_text  | 10 | 3.75 |
| dual_source       | 5  | 3.66 |

| source | n | mean |
|---|---|---|
| claude | 67 | 4.23 |
| shell  | 26 | 3.89 |
| dual   | 7  | 3.62 |

Dual-source nodes (re-enriched twice — once from shell side, once from claude
side, second pass overwriting) score worst on average.

## Cross-cutting findings (themes appearing in ≥3 expert summaries)

### 1. Worktree-prefix leakage in path-typed entity names
**Experts:** enrichment, schema, mcp.
**Evidence:** schema counted 6/100 violations; mcp's drift table shows ~17
distinct files that exist in BOTH worktree-prefixed and clean forms across
the corpus (`crates/hippo-daemon/src/claude_session.rs` appears in 14 nodes
with 3 distinct surface forms). The v3 prompt rule 5 told the model to strip
these; it doesn't always.
**Fix path:** unconditional `strip_worktree_prefix` at enrichment-write time
inside `upsert_entities` on path-typed entities, plus a one-shot DB pass to
clean already-written rows. The `entities.canonical` column exists for this
and is largely unused.

### 2. Hallucinated env_vars and version strings
**Experts:** enrichment, schema (env_var case bucket), rag (orphaned data).
**Evidence:** 8/100 nodes have at least one env_var or semver fabricated
(model adds `CARGO_HOME`, `PATH` to Rust/Cargo work; invents `1.93.1`,
`0.149.0`, `0.2.0` for release-flavored sessions). Most damaging failure mode
because it cannot be detected by retrieval.
**Fix path:** post-LLM verbatim-validator that rejects entity tokens absent
from source rows. Cheap to implement; would catch every flagged case.

### 3. Render plumbing leaves substantive content stranded
**Experts:** rag (top weakness), enrichment (long-session coverage), mcp
(answer hand-waviness).
**Evidence:** 66/100 nodes have populated `key_decisions` and/or
`problems_encountered` content that `brain/src/hippo_brain/rag.py::_hit_lines`
silently drops. 5 of these are extreme (≥600 chars + ≥5 unique identifiers
not visible in summary or embed_text). The synthesizing LLM never sees this
content, regardless of enrichment quality.
**Fix path:** add `Decisions:` and `Problems:` render branches in
`rag.py::_hit_lines` under the existing proportional truncation. This is the
single biggest leverage available — improves `hippo ask` materially without
re-enriching anything.

### 4. Long-session coverage drop
**Experts:** vector, enrichment.
**Evidence:** median surviving fraction of source identifiers in embed_text
is 0.53 (p10 = 0.25). On long Claude sessions with 60+ identifiers, the model
picks ~25 and silently drops the rest — often the deepest file paths users
will query for. embed_text length plateaus while content_len grows past 4000.
**Fix path:** per-segment chunking before enrichment for long Claude sessions
(re-enrich script can split a session whose `content_len > 4000` into
multiple LLM calls and merge entity buckets).

### 5. Tool-bucket pollution with shell-invocation phrases
**Experts:** schema (top fault — 19/100 nodes), mcp (drift list).
**Evidence:** `cargo clippy`, `git log`, `uv run --project ...` get stored as
single tool-entity rows instead of being normalized to bare command names
(`cargo`, `git`, `uv`). This defeats cross-node dedup, inflates
`get_entities(type='tool')` cardinality, and splits hybrid-search ranking.
**Fix path:** normalize tool entities at `upsert_entities` time — split on
whitespace, take first token. A 5-line change to `enrichment.py`.

### 6. Filler-opening summaries that burn the 120-char RAG budget
**Experts:** rag (10/100), mcp (worst-for-ask uuids), enrichment (low-content nodes).
**Evidence:** "The user requested…", "Conducted a comprehensive…" lead 10/100
summaries; after 120-char truncation the synthesizer sees no concrete artifact.
**Fix path:** prompt fix in the enricher: "Lead the summary with a concrete
verb + artifact, never with subject-first prose."

### 7. Empty `design_decisions` when source weighed alternatives
**Experts:** rag (6/100), enrichment.
**Evidence:** alternative-weighing language ("instead of", "considered",
"rather than") in source text but `design_decisions: []` in output. "Why did
I pick X?" questions return nothing useful from these nodes.
**Fix path:** prompt strengthening with positive examples; or a post-LLM
detector that re-prompts when the source contains alternative-weighing
phrases but the output emits an empty list.

## Notable duplication finding (mentioned in plan)

The re-enrichment script's UNION query in `_select_candidate_nodes` produces
a `_source='shell'` row AND a `_source='claude'` row for any node linked to
both event types. The script processes both, with the second pass
overwriting the first. Evidence: `25b32204…` and `6dd039b8…` are produced
by this path and score worst on the panel; node 6463 in the log was
re-enriched twice (`(claude)` then `(shell)`). Dual-source stratum in this
sample averages 3.62 — the worst by source. Worth a follow-up fix: pick the
*better* source for dual-linked nodes (probably claude if claude_segments
are populated, since they carry richer narrative) and skip the second pass.

## What this scorecard is NOT

- Not a substitute for `hippo-eval` (`brain/src/hippo_brain/evaluation.py`),
  which scores end-to-end retrieval recall/MRR against a labeled Q/A set.
  Different question, different methodology. Run that next; this panel is
  intrinsic-quality, that one is task-success.
- Not a recommendation to roll back v3. The corpus is meaningfully better
  than what came before; the issues above are mostly post-LLM plumbing,
  not the model itself.

## Outputs on disk

- `/tmp/hippo-eval-panel/dossier.jsonl` — 100-node sample
- `/tmp/hippo-eval-panel/RUBRIC.md`     — what the panel scored against
- `/tmp/hippo-eval-panel/scores_<expert>.jsonl` (5 files, 100 rows each)
- `/tmp/hippo-eval-panel/summary_<expert>.md` (4 of 5; schema expert returned summary inline only)
- `/tmp/hippo-eval-panel/panel_scorecard.jsonl` — per-node aggregate (panel mean + each expert's scores + each expert's note)
- `/tmp/hippo-eval-panel/FINAL_REPORT.md`  — this file
