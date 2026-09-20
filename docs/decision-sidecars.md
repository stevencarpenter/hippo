# Isolated decision sidecars

Run the offline rules benchmark:

```sh
mise run bench:decisions
```

The runner prints its artifact directory under
`$XDG_DATA_HOME/hippo-bench/decisions/` (default
`~/.local/share/hippo-bench/decisions/`). Each arm runs in a separate process.
No worker opens `hippo.db`, claims enrichment work, changes models, pauses the
brain, or publishes a result into Hippo. Production inference selection stays
unchanged. No new dependencies are required.

The benchmark covers two semantic judgments:

1. **Reranking:** reuse Hippo's existing `build_rerank_messages` and
   `parse_ranking` for the local LLM. Jev asks one relevance Score per candidate
   in one request. Every arm sees the same query and candidate text, including
   Hippo's existing 300-character field budgets. Original candidate identities
   are retained across reversed repeats.
2. **Claim verification:** classify a supplied claim against supplied evidence
   as `supports`, `contradicts`, or `unsupported`. This is an experimental
   verifier using the configured local LLM as comparator. Hippo has no existing
   production verifier to replay. Claims must already be split and associated
   with their evidence.

Neither judgment generates summaries or answers. Relevance is not proof that
the requested fact is known. The earlier answerability experiment did not
justify an answerability gate.

Run all five arms on the bundled synthetic corpus:

```sh
# TYPESAFE_API_KEY must already be exported in this shell.
mise run bench:decisions -- \
  --arms llm,jev,rules,rules_jev,rules_llm \
  --llm-url http://127.0.0.1:42069/v1 \
  --llm-model YOUR_CURRENT_QUERY_MODEL \
  --jev-model jev-1.13.0 \
  --repeats 2 --reverse
```

An explicit inference URL and model are required for LLM arms. The runner does
not resolve or change the production configuration. Supplying production's
inference URL shares that server's compute and caches. A separate inference
server provides compute isolation. Arm execution is sequential by default;
`--parallel` runs workers concurrently and records that scheduling choice.
Use sequential runs when comparing latency without inter-arm contention.

Jev arms send the selected corpus text to TypeSafe. Capture alone is local;
only an explicit run containing `jev` or `rules_jev` makes those requests.
The default run is rules-only and makes no inference requests.

All inference requests are single attempts with a configurable `--timeout`
(60 seconds by default), temperature zero for the local LLM, and pooled HTTP
connections. There are no hidden retries. This compares decision prompts and
outputs, not the complete production service. Production's transport retry,
connection, and 300-second timeout behavior are not being timed here.
Failed calls remain in the artifacts and denominators. Run exit status is 3
for completed runs with case errors and 2 for setup or worker failures.
Ctrl-C and SIGTERM terminate and reap owned workers, retain partial artifacts,
and exit with status 130. Missing records remain visible in the summary.

The default fixture contains the 46 ranking and verification cases from the
[September research](research/typesafe-2026-09-18/README.md), plus three controls
for exact-text, empty-evidence, and exact-identifier rules. The research cases
are synthetic; the follow-up cases were authored after inspecting initial
failures. These are regression checks, not an independent production holdout.

The rules live in
[`decision_rules.json`](../brain/src/hippo_brain/_fixtures/decision_rules.json).
They are a declarative decision table evaluated in Python:

```json
{
  "id": "empty-evidence",
  "task": "verification",
  "priority": 100,
  "when": [{"fact": "empty_source", "op": "eq", "value": true}],
  "output": "unsupported"
}
```

Conditions within a rule use AND semantics. Multiple rules express alternatives.
The highest priority wins. Conflicting outputs at that priority, or no match,
produce an abstention. The engine validates fact names, operators, priorities,
and outputs before starting workers. Rules cannot execute code or mutate facts.
This is a decision table, not a forward-chaining Drools implementation.

Ranking facts are `token_overlap`, `query_tokens`, and `exact_query`.
Verification facts are `empty_source` and `identical_text`. Operators are
`eq`, `ne`, `ge`, and `gt`. Ranking outputs are relevance levels 0 through 3;
verification outputs are the three verdicts above. Exact matches are a limited
rules baseline; lexical overlap cannot establish semantic support.

Use `--rules PATH` to compare a changed rule set. Every result retains facts,
matched rule IDs, winning rule IDs, and conflicts. The `rules` arm exposes its
raw decisions and abstentions. `rules_jev` and `rules_llm` run the engine first
and invoke inference on abstention. For ranking, they also invoke inference
unless the best rule score exceeds the second by `--rules-margin` (default 2).
Inference receives the original complete candidate set; rules do not prune it.
These are separately executed paths, so their measured time and token usage
include the inference calls they actually make.

Capture inputs from an already enabled production reranker by setting
`HIPPO_DECISION_CAPTURE=1` in the brain process's environment. This hook runs
only when Hippo invokes `rerank_results`; it does not enable reranking.
It writes at most 1,000 unique inputs to `decisions/captures/`, with private
file permissions. Candidate counts and serialized input sizes are bounded.
Duplicate inputs are skipped. Capture errors are logged by exception type and
cannot change a query's result. The synchronous file write is opt-in and adds
local filesystem work to that query.

Replay the frozen captures locally:

```sh
mise run bench:decisions -- \
  --cases ~/.local/share/hippo-bench/decisions/captures \
  --arms llm,rules,rules_llm \
  --llm-url http://127.0.0.1:42069/v1 \
  --llm-model YOUR_CURRENT_QUERY_MODEL
```

Custom corpora can be JSON arrays, JSONL, or directories of captured JSON files.
Ranking cases have `id`, `task: "ranking"`, `query`, and `candidates` (strings
or objects with `summary`, `embed_text`, and `commands_raw`). Optional `grades`
are integers 0 through 3 in candidate order. Verification cases have `id`,
`task: "verification"`, `source`, `claim`, and optional `expected`. Capture
files place inference fields under `state`. Labels are separated before any
inference request. Unlabeled captures report agreement, latency, errors, and
coverage; they do not report accuracy.

Inspect or scrape a run while it is running or after it finishes:

```sh
mise run bench:decisions:summary -- /path/to/run
mise run bench:decisions:metrics -- /path/to/run --port 9836
curl http://127.0.0.1:9836/metrics
```

The metrics sidecar binds to loopback. Its metrics use
`hippo_decision_shadow_*` names and `service_namespace="hippo-bench"`.
Inference workers disable production OpenTelemetry exporters. Point a
Prometheus scrape job at the metrics sidecar explicitly; starting a benchmark
does not alter observability configuration or install a background service.

Each artifact directory contains:

1. `manifest.json`, `inputs.jsonl`, `labels.json`, and `rules.json`: frozen
   corpus, labels, rule set, hashes, model IDs, request policy, and scheduling.
2. `<arm>.jsonl`: exact requests and responses, input and request hashes,
   returned model version, token usage, rule traces, HTTP status, errors,
   monotonic request and end-to-end decision durations, and worker CPU/RSS.
3. `<arm>.log` and `completion.json`: worker failures and exit status.
4. `summary.json`: planned and observed counts, missing records, coverage,
   p50/p95 latency, token totals, paired agreement, and labeled quality.

The summary's `accuracy` and `ndcg` measure successful inference decisions over
all planned scoreable cases. Errors, missing worker records, and abstentions
count as zero. `effective_ranking_accuracy` and `effective_ndcg` additionally
score original-order fallback on failed calls. Jev's fallback is the proposed
shadow behavior, not a production integration. Ranking cases with all-zero
grades have no meaningful ideal ranking and are excluded from ranking quality
denominators, but remain in operational counts. Coverage uses planned cases,
so crashed or missing workers cannot look complete. Paired agreement uses
matching input hashes and reports the number of pairs with successful outputs;
agreement with the current LLM is not ground truth.

Token totals include only provider-reported usage. `usage_missing` counts
requests with unknown token usage; unknown tokens are not estimated from
character counts. CPU and memory are client-process measurements, excluding
the inference server. Peak RSS is the process lifetime high-water mark.
Latency excludes process startup and artifact serialization. The raw records
retain enough information to inspect order sensitivity and model probabilities.

Conflict experiments compare the existing `analyze_conflicts` implementation
with semantic judgments over identical knowledge-hit fields:

```sh
# Offline, current production functions versus the declarative rules table.
mise run bench:conflicts

# Live, using the same configured local model as the other decision experiments.
mise run bench:conflicts -- \
  --arms current,jev,rules,rules_jev,llm,rules_llm \
  --llm-url http://127.0.0.1:42069/v1 \
  --llm-model YOUR_CURRENT_QUERY_MODEL \
  --repeats 2 --reverse
```

The `current` arm supports `decision_conflict` and `outcome_conflict` tasks
only. The LLM arm for these tasks is an experimental semantic comparator;
production uses deterministic detectors, not an LLM conflict prompt. Both
models receive the same semantic policy. The policy distinguishes unresolved
same-scope contradictions from explicit recovery, replacement and unrelated
work. That is a proposed refinement of the current product behavior, not a
restatement of the existing unit-test contract. `compatible` means that these
records establish no unresolved conflict; it does not establish current truth.

Conflict cases supply `state.hits` and optional `expected` (`conflict` or
`compatible`). Each hit retains `summary`, `cwd`, `git_branch`, `captured_at`,
`outcome`, and `design_decisions` with `chosen`, `considered`, and `reason`.
The normalizer assigns opaque hit IDs and strips all other fields, including
annotations. Staleness, source freshness and evidence lookup are outside this
comparison. No production conflict capture hook is installed.

Conflict rules use `comparable_records` and `identical_records`. They return
`compatible` for absent comparisons or identical substantive records, then
abstain for semantic judgments. Matching chosen strings or outcome labels alone
do not skip inference. Reverse repeats reverse hit order while preserving IDs
and timestamps. Summaries pair these tasks against `current` when present and
report `false_conflicts` and `missed_conflicts`. Those two counts cover successful
verdicts; failures, missing records and abstentions remain separate and count
as zero accuracy. Repeats are not independent labeled examples.

New runs also save the relevant Python source files under `source/`, alongside
their manifest hashes. The [conflict research record](research/typesafe-2026-09-20-conflicts/README.md)
contains the fixed corpus, reviewed policy, recorded requests/responses and
offline analysis for the September 20 comparison.

Contracts and question design follow TypeSafe's
[HTTP API](https://docs.typesafe.ai/api.md),
[reranking cookbook](https://docs.typesafe.ai/cookbooks/rerank_typesafe.md), and
[citation verification cookbook](https://docs.typesafe.ai/cookbooks/citation_check.md).
