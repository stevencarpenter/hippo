# TypeSafe experiments for Hippo

Recommend an optional TypeSafe reranking pilot, followed by an advisory claim-verification pilot. The synthetic results justify comparison against Hippo's existing local model. They do not establish a production accuracy or latency improvement.

Executed September 18, 2026 (America/Denver), using the TypeSafe skill and the exported `TYPESAFE_API_KEY`. The API returned `jev-1.13.0` for all 88 requests. All requests succeeded and produced 200 typed judgments. Only authored synthetic text was sent; no captured sessions or production database contents were submitted.

| Experiment | Initial screen | Follow-up stress cases | Combined median / p95 request latency |
| --- | --- | --- | --- |
| Reranking | Best passage first in 7/7 answerable queries | Best passage first in 2/2 answerable queries | 247 / 403 ms |
| Claim verification | 23/24 three-way labels correct | 7/8 labels correct | 243 / 334 ms |
| Query routing | 20/20 labels correct | 7/8 labels correct | 238 / 304 ms |

## Method and interpretation

`cases.json` contains the initial labels, fixed before requests. `holdout.json` contains follow-up stress cases written after inspecting initial failures, with unchanged prompts. This follow-up is not an independently curated holdout. Expected labels and relevance grades were excluded from every API payload. Each recorded response includes its exact request, token usage, model, and elapsed request time. Four requests ran concurrently; latency includes HTTP/network overhead, excludes executor queue time, and is not end-to-end Hippo latency.

Reranking used four candidates per query, one Score per candidate, and an independent Noul answerability question in the same request. All 14 candidate sets were also evaluated in reverse order. The top passage identity stayed unchanged in all 14 sets. Across the nine queries with an actual answer, top-1 was correct in both orders (18/18 requests, nine unique queries). Lower ranks and numeric scores changed with ordering.

On the initial seven answerable queries, NDCG@4 rose from 0.722 in the authored candidate order to 0.996 after reranking. Original-order top-1 was 1/7. This is an artificial ordering baseline, not Hippo hybrid retrieval or its existing local reranker. Four-candidate results also do not measure performance at Hippo's configured candidate-pool size.

Verification distinguished `supports`, `contradicts`, and `unsupported`. It correctly accepted all nine supported claims and rejected all 23 unsupported or contradicted claims as non-supporting. Its two three-way errors were unsupported claims labeled contradicted: a migration proposal treated as disproving implementation (confidence 0.86), and an increased timeout treated as disproving faster requests (0.73). A confidence cutoff of 0.8 therefore did not eliminate all classification errors.

The routing error was “What new decisions were recorded today?” Expected `recent`, returned `decisions` at confidence 0.44. Both intents are present. The error exposes the limits of mutually exclusive routing as well as model behavior. Five action-bearing requests selected `none`; this small sample is not an authorization or safety evaluation.

## Answerability finding

The original Noul was positive on all five candidate sets labeled as lacking the requested fact, in both orders (10/10). The initial Android-release case scored 0.75 and 0.62 despite only macOS background and unrelated Android content. Follow-up cases lacked a latency measurement, approver identity, current health observation, or main-branch fix identity.

There is a material rubric ambiguity: the question permits evidence correcting a false premise, while labels require the requested fact or an explicit factual refutation. “No approver was recorded” is useful evidence for an honest answer but does not identify the approver. These results do not isolate model error from that mismatch. Neither this Noul nor a relevance Score should gate assertions that the requested fact is known. A relevance score also rated a quoted search phrase 2.71/3, illustrating that topical relevance does not establish factual support.

## Proposed changes, ranked

1. **Add an opt-in TypeSafe backend at `brain/src/hippo_brain/rerank.py:rerank_results`.** Batch one relevance Score per candidate, preserve original retrieval scores, and retain existing order on service or validation failure. Keep the current local path as the default. The existing call in `rag.py` already places reranking between retrieval and synthesis. Evaluate against that local path on identical labeled candidate pools before selecting a default. Pin the model version and make external transmission of retrieved text explicit.
2. **Add advisory verification after synthesis in `brain/src/hippo_brain/rag.py`.** Evaluate each explicit claim against its cited evidence and expose non-supporting verdicts for inspection. Preserve source links. The experiment covers pre-split claims, not extracting atomic claims or resolving citations from generated prose; those operations must be evaluated separately. Do not discard answers or automatically rewrite them based on this screen.
3. **Defer automatic routing in `brain/src/hippo_brain/agent_query.py`.** Its existing explicit `known`, `evidence`, `recent`, and `decisions` modes need no classifier. If natural-language mode suggestions are added, preserve explicit caller choices and keep time filters separate from intent. The mixed-intent failure does not support replacing that interface with a single mandatory Choice.

No production integration, dependency, configuration, or service was changed.

## Usage and reproducibility

The 88 successful requests reported 53,058 input tokens and 5,226 output tokens. At the published $0.042 per million input tokens, estimated cost is **$0.00223**, excluding any account-specific billing terms. Output tokens are listed as free in the [model documentation](https://docs.typesafe.ai/models.md). The console invoice was not inspected.

Replay the initial results without an API key or network calls:

```sh
python3 docs/research/typesafe-2026-09-18/run.py
```

Replay the follow-up results:

```sh
python3 docs/research/typesafe-2026-09-18/run.py \
  --cases docs/research/typesafe-2026-09-18/holdout.json \
  --results docs/research/typesafe-2026-09-18/holdout-results.json
```

Append `--live` to either command to issue new paid requests and replace that run's result file. `--self-check` runs the offline metric, ordering, and label-exclusion checks. Replay also checks response keys, output ranges, and Choice probability sums. The harness uses Python's standard library and has no retry loop; service errors remain visible in saved results.

API design follows the live [HTTP contract](https://docs.typesafe.ai/api.md), [reranking cookbook](https://docs.typesafe.ai/cookbooks/rerank_typesafe.md), and [citation-checking cookbook](https://docs.typesafe.ai/cookbooks/citation_check.md). Raw records are in `results.json` and `holdout-results.json`; corresponding `*-summary.json` files contain per-case metrics and confidence values.
