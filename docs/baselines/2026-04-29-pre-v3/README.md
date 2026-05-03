# Baseline: 2026-04-29 pre-v3 vs v3-partial

A snapshot of intrinsic enrichment quality (5-expert panel) and end-to-end retrieval (`hippo-eval`), captured before the `qwen3.6-35b-a3b-ud-mlx` re-enrichment job touches the labeled gold corpus.

This is the **before** half of an A/B comparison. To get the **after**, re-run the panel + hippo-eval after the re-enrich job completes (or after force-prioritizing the 70 gold uuids).

## When this was captured

- Date: 2026-04-29 09:06 UTC
- Re-enrich progress at capture time: 2,319 / 6,485 nodes (~36%, newest-first)
- Re-enrich target version: `enrichment_version=3` (PR #108 added the `env_var` entity bucket)
- v3 nodes in DB at capture: 2,256
- Gold uuids at v3: **0 / 70** — gold corpus hasn't been re-enriched yet
- Enrichment model: `qwen3.6-35b-a3b-ud-mlx`
- Embedding model: `text-embedding-nomic-embed-text-v2-moe`

## What's in this directory

### Panel artifacts (intrinsic node quality)

- `FINAL_REPORT.md` — top-level scorecard, cross-cutting findings, worst nodes
- `RUBRIC.md` — the rubric all 5 experts scored against
- `dossier.jsonl` — the 100-node sample (stratified random) the panel saw. **Same uuids must be used in the after-snapshot for a fair comparison.**
- `panel_scorecard.jsonl` — per-node aggregated scores (panel mean, each expert's scores, each expert's note) — 100 rows
- `scores_<expert>.jsonl` — raw 100-row scorecard from each of 5 experts (enrichment, vector, schema, rag, mcp)
- `summary_<expert>.md` — each expert's narrative summary

### Hippo-eval artifacts (end-to-end retrieval)

- `hippo-eval-retrieval-only.md` — full hippo-eval scorecard, retrieval-only (no synthesis, no judge)
- `per_question.json` — per-question recall@10 / mrr / nDCG / source_diversity / coverage_gap

### Reproduction scripts

- `build_dossier.py` — re-build the same 100-node stratified sample (uses `random.seed(42)`)
- `aggregate.py` — recompute the cross-expert aggregate from `scores_*.jsonl`
- `dump_eval.py` — re-run hippo-eval and dump per-question metrics as JSON

## Headline numbers

### Panel (1–5 Likert, n=100)

| dimension | mean | stdev across 5 experts |
|---|---|---|
| accuracy | 4.59 | 0.26 |
| succinctness | 4.10 | 0.64 |
| usefulness | 3.80 | 0.91 |
| ask_suitability | 3.98 | 0.72 |
| mcp_suitability | 4.00 | 0.47 |

### Hippo-eval (40 questions, hybrid k=10)

| metric | mean | median |
|---|---|---|
| recall@10 | 0.210 | 0.000 |
| mrr | 0.213 | 0.000 |
| ndcg@10 | 0.167 | 0.000 |
| source_diversity | 0.890 | 0.971 |
| coverage_gap | 0.000 | 0.000 (broken in hybrid mode — see caveats) |

13/40 questions have any gold-uuid hit; 15/40 have recall@10 = 0; remaining 12 are adversarial-with-empty-gold.

## How to compare after re-enrich finishes

### Option A: same nodes, after corpus is fully v3

```bash
# 1. Confirm the 100 dossier uuids are now at v3
python3 - <<'PY'
import json, sqlite3
from pathlib import Path
uuids = [json.loads(l)["uuid"] for l in open("dossier.jsonl")]
conn = sqlite3.connect(Path.home() / ".local/share/hippo/hippo.db")
ph = ",".join("?"*len(uuids))
rows = conn.execute(
    f"SELECT enrichment_version, COUNT(*) FROM knowledge_nodes "
    f"WHERE uuid IN ({ph}) GROUP BY enrichment_version", uuids
).fetchall()
print(rows)
PY

# 2. Re-build dossier from current DB (same uuids, fresh content)
python3 build_dossier.py  # edit to read uuids from existing dossier.jsonl

# 3. Re-run the 5-expert panel against the new dossier
#    (use the agent dispatch pattern from the original session)

# 4. Re-aggregate
python3 aggregate.py
```

### Option B: hippo-eval re-run

```bash
# The labeled gold uuids are stable; hippo-eval just runs against current DB.
uv run --project brain hippo-eval --no-synthesis --no-judge --out v3-after.md
# For groundedness + keyword_hit (slower):
uv run --project brain hippo-eval --out v3-after-full.md
```

### Option C: force-prioritize gold uuids (fastest path to v3-after)

```bash
# Extract the 70 gold uuids
python3 - <<'PY'
import json
qs = json.load(open("../../../brain/tests/eval_questions.json"))
data = qs.get("questions", qs) if isinstance(qs, dict) else qs
golds = sorted({u for q in data for u in q.get("relevant_knowledge_node_uuids", [])})
print("\n".join(golds))
PY > gold_uuids.txt

# Modify re-enrich-knowledge-nodes.py to accept --uuids gold_uuids.txt
# OR raise the priority of those 70 nodes in the candidate query.
# Then re-run hippo-eval.
```

## Cross-cutting findings (from the panel — for v3 evaluation criteria)

The "after" comparison should look for these specific signals to confirm whether v3 actually fixed each:

1. **Render plumbing**: 66/100 nodes have orphan `key_decisions`/`problems_encountered`. Fix is in `brain/src/hippo_brain/rag.py::_hit_lines`, NOT in re-enrichment. Re-running won't change this.
2. **Hallucinated env_vars/versions**: 8/100 nodes. Should drop with a verbatim-validator post-LLM, NOT with re-enrichment alone. Re-running may not fix.
3. **Worktree-prefix leakage**: 5–6/100 nodes. Should drop only if `upsert_entities` is fixed; re-enrichment alone won't strip already-written rows.
4. **Long-session coverage drop**: median 0.53 of source identifiers survive. Should drop with per-segment chunking.
5. **Tool-bucket pollution**: 19/100 nodes have `cargo clippy` instead of `cargo`. Fix in `upsert_entities`.
6. **Filler-opening summaries**: 10/100 nodes. Could improve with prompt tuning.
7. **Empty design_decisions when alternatives weighed**: 6/100 nodes. Could improve with prompt tuning.

For each, "v3-after - v3-pre" delta on the relevant counts is the actionable comparison.

## Known issues with hippo-eval (caveats from the harness itself)

- **R-03 FTS5 phrase-wrap**: lexical mode wraps multi-word queries in a single phrase, so recall on long natural-language questions is pathologically low.
- **R-07 RRF normalization**: hybrid scores normalize to top=1.0 per query. `coverage_gap` is therefore identically 0.000 in hybrid mode — it's a broken metric in this configuration.
- **R-02 vec0 brute-force**: no ANN index. Hybrid latency is O(N).
- **events.git_repo NULL**: project filtering falls back to cwd-prefix.

## Why this lives on PR #102

PR #102 ships hippo-bench-v2 — a benchmark framework. This baseline is what such a framework would consume as the **before** snapshot. Saving it here means:
- The same branch that introduces the framework also introduces the first calibration data
- Whoever runs the bench-v2 against v3-after has a known reference point
- The comparison can be used to validate whether bench-v2's metrics actually move when enrichment quality changes
