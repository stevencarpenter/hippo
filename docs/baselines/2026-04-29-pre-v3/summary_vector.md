# Vector-retrieval expert summary
Mean scores: accuracy 4.26 / succinctness 3.15 / usefulness 2.46 / ask 3.23 / mcp 3.31

Corpus stats (embed_text):
- identifier density per 100 chars: median 3.56, p10 1.68, p90 5.05
- 38% of nodes have density < 3.0 (target floor); 14% under 2.0 (prose territory)
- coverage of source identifiers: median 0.53, p10 0.25, p90 1.00
- prose-stopword ratio: median 0.00, p90 0.04
- front-240-char density (post-truncation): median 3.75, p10 1.67

## Top strengths (top 3, concrete)
- Strong front-loading on the dense majority: best exemplars (e.g. `1b1ac570-0ec9-4404-93c3-e1852b1a3333`, `e276ab79-158b-4e76-a35d-6fbc54d0706c`) front-density 6.1/8.3 per 100 chars and coverage 1.00/0.59 — these will retrieve cleanly from sqlite-vec and survive truncation.
- File-path retention is generally good: across the corpus, median coverage of source identifiers is 0.53; many nodes (e.g. `1b63fe7b-2dde-47cc-8647-ea2b9a889443`) preserve the deep `crates/...` and `brain/src/...` paths verbatim, which is exactly what `search_knowledge`'s FTS and the vec0 cosine want.
- Versions and env-var-style constants (UPPERCASE_WITH_UNDERSCORES) make it through with high fidelity in the strong nodes — the verbatim-preservation rule is mostly being honoured (e.g. `da979c70-f512-4dc3-ba51-2a93ba3f6935`).

## Top weaknesses (top 3, concrete)
- A long tail reverts to prose: 14% of nodes have <2 identifiers per 100 chars of embed_text — these read like sentences, not tag soup. Worst examples: `b63c3f9b-d9b5-4510-ad13-afe41d5a3435` (d=0.7, prose=0.00), `7a73195e-05ff-4138-adab-5f9f15006399` (d=0.7).
- Coverage drops sharply on dense Claude sessions: when source has dozens of identifiers, the median surviving fraction is 0.53; nodes like `c1260596-e479-4bc7-a9ff-6904f722fefd` (cov=0.33) drop high-value file paths and symbol names that users will absolutely query for.
- Under-densification on short_embed stratum: nodes with `embed_text_len < 200` rarely compensate with density. Several short rows (e.g. `e5978deb-5385-4bf6-8267-30820deba36e` len=104) carry only 3 identifiers — these will rarely surface in vec0 ranking against richer neighbours.

## Worst 5 nodes
| uuid | reason |
|---|---|
| b63c3f9b-d9b5-4510-ad13-afe41d5a3435 | density=0.7/100ch, coverage=0.20, prose=0.00, len=152, ids=1 |
| 7a73195e-05ff-4138-adab-5f9f15006399 | density=0.7/100ch, coverage=0.33, prose=0.05, len=144, ids=1 |
| c1260596-e479-4bc7-a9ff-6904f722fefd | density=0.6/100ch, coverage=0.33, prose=0.00, len=166, ids=1 |
| e5978deb-5385-4bf6-8267-30820deba36e | density=2.9/100ch, coverage=0.01, prose=0.07, len=104, ids=3 |
| 7b8d9699-2064-4698-84f2-ed5f0aa7dca3 | density=1.1/100ch, coverage=0.40, prose=0.00, len=175, ids=2 |

## Cross-cutting observation
The model mostly honours the tag-soup directive: median density 3.56 per 100 chars, with 62% of nodes clearing the 3-per-100 bar. The remaining failure mode is coverage on long Claude sessions (median 0.53) — when the source has 60+ identifiers, the model picks ~25 and silently drops the rest, often the deepest file paths. Front-loading is good (median front-240 density 3.75), so truncation hurts less than coverage does.
