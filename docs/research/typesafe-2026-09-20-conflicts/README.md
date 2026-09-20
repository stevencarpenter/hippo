# Jev conflict judgments in Hippo

Jev is a candidate for advisory semantic conflict analysis, but the measured
outcome misses do not support using it to clear Hippo's existing warnings.
The rules-first local LLM comparison is running. This record evaluates a proposed meaning
of unresolved conflict on synthetic examples, not production answer quality.

## Code paths examined

| Path | Existing operation | Evaluation decision |
| --- | --- | --- |
| [`conflict_detection.py`](../../../brain/src/hippo_brain/conflict_detection.py) | `_outcome_conflict` flags mixed success/failure labels; `_decision_conflict` compares the oldest and newest chosen strings. | Execute these functions unchanged as the `current` arm. Compare with semantic judgments and declarative rules. |
| [`agent_query.py`](../../../brain/src/hippo_brain/agent_query.py) | `run_agent_query` retrieves hits, calls `analyze_conflicts`, lowers per-hit confidence, and appends warnings. Decision fields are included in `decisions` mode. | False conflict flags and missed conflicts affect the information returned to agents. The default hit limit is ten. |
| [`models.py`](../../../brain/src/hippo_brain/models.py) | `_coerce_entity_list` calls `_infer_entity_key` for flat or untyped entity output, after LLM enrichment. | Adding Jev here adds inference rather than replacing the primary LLM. Fix deterministic parsing precedence before evaluating ambiguous entity classification. |
| [`claude_session.rs`](../../../crates/hippo-daemon/src/claude_session.rs) | The active watcher path splits sessions at a five-minute user-prompt gap or 12,000 characters, then computes segment identities. | Semantic segmentation needs an offline boundary-quality experiment. Adding inference inside capture would change latency and identity behavior. The Python extractor is not the active watcher. |

The entity fallback probe returned `files` for both `github.com` and
`api.example.org`: the file-suffix expression runs before the domain expression.
That observation is deterministic and does not establish a need for Jev.
`src/main.rs`, `HIPPO_DATA_DIR`, `Docker`, and `TimeoutError` returned `files`,
`env_vars`, `tools`, and `errors`, respectively. No entity parser was changed.

Project-scoped and cross-project Hippo recall found prior TypeSafe opportunity
analysis, the existing conflict detector review, and the earlier sidecar work.
Neither search returned a prior Jev evaluation of conflict detectors or ten-hit
conflict pools. The earlier answerability experiment remains evidence against
treating relevance as proof that the requested fact is known.

## Policy and protocol

The target label is `conflict` only when records make unresolved, incompatible
assertions about the same subject, project, environment and applicable time or
attempt. Explicit recovery or replacement is compatible chronology. Capture
timestamps alone establish neither recovery nor supersession. `compatible`
means that the supplied records establish no unresolved contradiction; it does
not establish current truth or freshness.

The inputs are enriched hit summaries and decision fields, not the underlying
source excerpts. Agreement between two summaries does not verify either
summary against its source. Retrieval quality, summary extraction errors,
source freshness and downstream answer correctness are unmeasured here.

This policy refines the existing product behavior. For example, the existing
`test_outcome_disagreement_conflict` intentionally flags a build failure
followed by a fix. The new semantic corpus labels an explicit failed attempt
followed by a successful retry `compatible`. That difference is documented in
each case's provenance; the benchmark does not declare the production test
incorrect.

[`cases.json`](cases.json) contains 44 synthetic cases, 22 per task. Each task
has 13 compatible cases and nine conflicts. Two repeats produce 88 decisions
per arm, with the second repeat reversing hit order while retaining identity
and capture times. The 528 planned records span six independent worker
processes: `current`, `jev`, `rules`, `rules_jev`, `llm`, and `rules_llm`.

Labels, rationales and provenance were fixed before any model calls and are
excluded from requests. The initial 40 cases were authored from code and tests;
four additional controls cover identical records, equal timestamps and coarse
outcome labels that conceal contradictory summaries. A separate review checked
all 44 labels against the policy before execution. This code-informed synthetic
set is not a representative production sample or an independent holdout.

The LLM arm is a new semantic comparator using the configured local query model,
`Qwen3.6-35B-A3B-Uncensored-Heretic-MLX-8bit`. There is no production LLM conflict
prompt to replay. Both models receive the same normalized hit fields and policy.
Jev uses a single Choice, pinned to `jev-1.13.0`.

Rules skip inference only for absent comparisons or identical substantive
records. Matching chosen strings or outcome labels alone do not qualify.
Every other case reaches the model with the complete original input. The
rules-first arms make fresh requests, allowing repeated identical requests
to reveal output variation. They do not reuse the model-only responses.

[`protocol.json`](protocol.json) records the pre-request corpus hash, rule hash,
dependency-lock hash, repository revision, model configuration and host details.
The run manifest hashes normalized inputs, labels and implementation sources.
All requests are single attempts, with pooled HTTP and a 60-second HTTPX timeout.
The existing local inference server shares compute and caches with production;
background load and warm-cache effects are not controlled. Arms execute
sequentially. Timing covers the client decision operation and HTTP call, not
end-to-end Hippo response time. Client CPU/RSS excludes model-server resources.

## Initial observed results

The completed Jev arms currently show:

| Task | Current | Local LLM | Jev | Rules then Jev |
| --- | --- | --- | --- | --- |
| Decision conflict, correct decisions | 24/44 | 44/44 | 44/44 | 44/44 |
| Outcome conflict, correct decisions | 28/44 | 39/44 | 40/44 | 39/44 |

Jev misses `outcome-16` (embedded instruction), `outcome-17` (irrelevant neighbor),
and `outcome-20` (later capture of the same completed run). The first order of
`outcome-20` is wrong at confidence 0.91; the rules-first request is wrong at
0.85. A confidence threshold of 0.8 would retain both errors.

In `outcome-20`, both summaries name the same completed benchmark and attempt.
The failure statement was captured first, and the success statement was captured
one day later. Neither records a retry, correction or retraction. The benchmark
requires a conflict. Jev's answer was compatible in both orders. This is an
observed classification error under the supplied rubric; the API provides no
reasoning trace that establishes why it happened.

The local LLM missed the opposite accounts of the same production run recorded
on different documentation branches (`outcome-18`) and the temporal example
(`outcome-20`) in both orders. It also missed a middle conflicting record
(`outcome-15`) in reverse order. Jev and the local LLM each answered 19/22 unique
outcome cases correctly in both orders, despite different per-call scores.
One additional correct call is not evidence of a general accuracy advantage.

Overall accuracy also hides the outcome tradeoff. Among the 18 positive outcome
decisions, current code missed two, Jev missed four, and the local LLM missed
five. Current code raised 14 false outcome conflicts; both models raised zero.
Thus the models' higher total accuracy came with more missed genuine outcome
conflicts. Decision-conflict judgments had a different result: Jev and the local
LLM corrected all 14 false flags and six misses in the current detector.

The direct Jev decision-conflict median/p95 was 148/211 ms; the direct local
LLM was 10,385/17,112 ms. Outcome medians/p95s were 150/220 ms for Jev and
10,041/20,832 ms for the local LLM. The current deterministic code took roughly
0.003 to 0.004 ms at the median and made no inference requests. Adding Jev to
this production path would buy semantic judgments at added latency and remote
inference cost; it would not speed up the current detector. The model comparison
has different serving hardware and uncontrolled background load.

The remaining rules-first local LLM results will be recorded after its worker
completes. No prompts, rules or labels are being tuned during these runs.

An additional offline composition retains every current warning and consults
the saved Jev judgment only when current code reports compatibility. On the
initial corpus it detects all labeled conflicts in both orders, correcting
eight missed-conflict decisions, while retaining all 28 existing false alarms.
It would consult Jev on 32/88 inputs rather than 88/88. Its quality is 30/44
for each task because preserving warnings also preserves false alarms.

This is a post-hoc simulation of an additive policy, not an executed seventh
arm. The call count is simulated, latency was not measured, and fresh Jev
responses can differ. It supports considering Jev as an additional advisory
signal; it does not establish a production accuracy gain. Recall searches for
this composition returned the current experiment and earlier sidecar work,
not an earlier evaluation of the composition.

## Ten-hit stress protocol

[`pool-cases.json`](pool-cases.json) expands eight selected examples to ten hits
using explicitly unrelated documentation records. The original target evidence
and labels remain unchanged. There are four cases per task, each tested in both
orders against current code, Jev, rules and rules then Jev, for 64 records.

This follow-up was authored after inspecting the initial Jev results. It is a
stress test, not an independent holdout. Its local LLM comparator is unmeasured.
The main experiment's local LLM worker ran concurrently, so even client host
load is not controlled. [`pool-protocol.json`](pool-protocol.json) preserves
these conditions and its frozen corpus hash.

The stress run completed all 64 records with no errors. Decision-conflict
accuracy was 8/8 for Jev and rules then Jev, versus 4/8 for current code.
Outcome-conflict accuracy was 6/8 for both model paths, versus 4/8 for current
code. Rules abstained on every ten-hit input, so they saved no inference calls.

The outcome miss was again the later account of the same completed run
(`pool10-outcome-20`). Jev missed it in both orders at confidence 0.97 and 0.80;
rules then Jev missed it at 0.94 and 0.89. Thus the error persists with extra
context, and a high confidence score does not certify the absence of conflict.
The [per-case analysis](pool-analysis.json) and [raw run artifacts](pool-run/)
retain both correct and incorrect outputs.

## Reproduction

Run the current implementation and rules without network access:

```sh
mise run bench:conflicts
```

Verify saved hashes and replay the completed ten-hit results offline:

```sh
mise run bench:conflicts:replay -- docs/research/typesafe-2026-09-20-conflicts/pool-run
```

The replay checks corpus, labels, rules, source and request hashes, matching
model input states, and duplicate/unknown records. Its paired
`consistent_correct_wins` counts unique cases correct in every repeat for the
target but not every repeat for the comparator; losses reverse that condition.
This is a robustness criterion, not two independent statistical wins. Repeated
calls and reversed order are dependent observations. The experiment does not
estimate production prevalence, calibration or statistical significance.

Run the complete comparison with an existing TypeSafe credential:

```sh
mise run bench:conflicts -- \
  --arms current,jev,rules,rules_jev,llm,rules_llm \
  --llm-url http://127.0.0.1:42069/v1 \
  --llm-model Qwen3.6-35B-A3B-Uncensored-Heretic-MLX-8bit \
  --jev-model jev-1.13.0 --timeout 60 --repeats 2 --reverse
```

Run the ten-hit stress comparison:

```sh
mise run bench:conflicts -- \
  --cases docs/research/typesafe-2026-09-20-conflicts/pool-cases.json \
  --arms current,jev,rules,rules_jev --repeats 2 --reverse
```

The API contract and Choice question design follow TypeSafe's
[HTTP API](https://docs.typesafe.ai/api.md),
[Choice reference](https://docs.typesafe.ai/primitives/choice.md), and
[citation verification cookbook](https://docs.typesafe.ai/cookbooks/citation_check.md),
read on September 20, 2026. No SDK or other dependency was added. No production
database was opened, and no inference selection, warning policy, capture service
or model configuration was changed. Only synthetic text was sent to TypeSafe.
