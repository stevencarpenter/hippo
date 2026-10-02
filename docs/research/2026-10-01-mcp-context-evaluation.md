# Hippo MCP context evaluation

Hippo's MCP usefulness requires a source-supported improvement in an agent's
task result or effort. A retrieved UUID, successful tool call or plausible
summary establishes only part of that mechanism.

## Measurements

| Question | Measurement | Evidence |
| --- | --- | --- |
| Can the agent call Hippo? | Startup, tool inventory, output schema, actual host permission errors and call success | MCP transport records and agent RPC trace |
| Did Hippo return the needed context? | Known-support recall/rank, literal fact availability, provenance, conflicting or stale statements | Returned MCP payload matched to a prior original-source audit |
| Did the agent use it correctly? | Correct, incorrect and unknown decisions, supported reasons, cited source application and repeated errors | Agent output checked independently against original sources |
| Did it improve the task? | Paired completion and decision accuracy, elapsed time, input/cached/output tokens and tool calls | Identical fresh control/treatment workspaces and checker |
| How often is useful history available? | Present, absent and unknown actionable-history counts in consecutive real tasks | Source audit independent of retrieval and arm results |

Run the transport replay before the agent comparison. It isolates returned
context from subsequent model interpretation. Compare versions on the same
frozen corpus, arguments and source audit. The actual MCP response is the unit
of retrieval measurement, including compaction and evidence excerpts. An
in-process retrieval score cannot detect host denial or lost response fields.

Use the existing `evaluation.py` metrics. Known-support recall measures only
the explicitly audited support set, not total corpus recall. New candidates
remain unjudged. Complete graded rank metrics are withheld until all returned
candidates are judged. Literal fact availability requires both an audited
support node and an audited phrase in its returned content. It excludes the
echoed query. This is a context-availability proxy, not factual correctness.
Original-source adjudication is still required for semantic claims and agent
application. Source duplication measures repeated evidence references, not
semantic redundancy.

Record latency and serialized response bytes alongside accuracy. Report text
and structured content separately because MCP may transmit both representations.
Do not estimate model tokens from characters. Use the agent provider's reported
tokens, retaining cached tokens separately. Four calls support descriptive
timings, not a reliable latency distribution or throughput claim.

The MCP specification defines structured output and tool annotations.
Read-only annotations are hints, not permission grants. Verify the host policy
with actual model calls. [MCP Tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)

## Reusable runner

`brain/src/hippo_brain/bench/mcp_context.py` uses the installed MCP SDK over
stdio. It checks the source-audit and server-database hashes before launching,
records the tool inventory, preserves raw tool responses privately and reuses
the existing retrieval scorers. Startup and each call have protocol timeouts.
Explicit call and repetition limits bound execution. No task labels are sent
to the server or agent.

```sh
mise run bench:mcp-context \
  --server-config /private/server/config.toml \
  --cases /private/cases.json \
  --out /private/new-replay \
  --max-calls 4
```

The external case manifest contains `source_audit`, its SHA-256,
`snapshot_sha256` and `cases`. Each case freezes `id`, `family`, `tool`, exact
`arguments` and a UUID-to-grade `judgments` map. Optional `facts` contain an
identity, audited `node_ids` and literal `any_text` phrases. Supported tools
are `agent_query`, `search_knowledge` and `search_hybrid`. Artifact directories
are private and exclusive. All raw source text, traces and case labels stay
outside Git and Kaneo.

Direct SDK calls bypass the agent host's approval policy. A successful replay
therefore establishes server behavior only. The paired model-thread runner
separately requires explicit approvals for all 12 audited context tools and
verifies actual tool inventory and filesystem boundaries.

## Case selection and interpretation

Known-history cases must specify a source-bound fact, expected application,
independent checker and why starting context cannot supply that fact. Keep the
answer key out of both arms. Use one primary task per incident family. Multiple
queries, repeated runs and overlapping session summaries from the same incident
do not create independent samples.

Cover recorded constraints, prior failed approaches, multi-session facts,
changed decisions, stale/conflicting history and abstention when context is
absent. LongMemEval's extraction, multi-session reasoning, temporal reasoning,
knowledge updates and abstention capabilities provide useful case categories.
Its benchmark results do not establish performance on this developer corpus.
[LongMemEval](https://arxiv.org/abs/2410.10813)

Deliberate known-history diagnostics measure conditional retrieval/application.
Independently audited absent controls measure unsupported claims and lookup
overhead. Consecutive real tasks measure actionable corpus coverage and practical
average benefit. Preserve retrieval misses, zero tool use, failures and unknown
source labels in that population. Report guided use and ordinary tool
availability separately. Reuse the bounded HIPO-50 study and family-aware
analysis rather than creating an independent acceptance protocol.

## October 1 regression

The authorized fix validation uses the unchanged Pi policy-recovery task,
unchanged five-decision answer key and pre-task snapshot with 29,626 knowledge
nodes. It remains one deliberately selected incident family. Eight direct MCP
calls replay the original query and three previously denied follow-up queries
against frozen old and patched servers. One new control/treatment pair has
600 seconds per arm, 1,200 summed seconds and two million reported tokens.
Amendment D has its own contract, identity and reservation. Older attempts and
resource use remain unchanged.

The patched MCP path applies configured reranking before response compaction,
preserves bounded recorded decisions and failures, and prefers recorded
assistant work over session environment scaffolding in excerpts. `agent_query`
provides an actual structured result and output schema. All query tools declare
read-only annotations. The new isolated host configuration explicitly approves
the complete audited context-tool surface.

### Agent result

| Measurement | Control | Hippo treatment |
| --- | --- | --- |
| Correct / incorrect / unknown decisions | 0 / 0 / 5 | 5 / 0 / 0 |
| Full task checker | Fail | Pass |
| Elapsed seconds | 34.609 | 68.757 |
| Reported total tokens | 92,907 | 302,843 |
| Cached input tokens | 77,184 | 266,624 |

Treatment made the exact prescribed `agent_query` and four successful
`search_knowledge` follow-ups. No approval denial occurred. Follow-up results
delivered all four audited support nodes. The agent cited `codex-18524` for
local/Git/object acceptance, pin validation and theme checks, and `codex-18345`
for removing fixed preference assertions. Both references were actually
returned. Their original-source hashes and selected records were rechecked.
All five output decisions and their reasons agree with those original records.
The recorded prior failures also agree with the local-package rejection and
stale-provider assertion. Proposals remained unchanged.

This is a verified context-delivery, correct-application and full-task win on
the selected recovery diagnostic. Control's unknown decisions are appropriate
abstention, not false policy claims. Treatment added 34.148 seconds and 209,936
reported total tokens. Summed use was 103.366 seconds and 395,750 tokens. It
improved decision recovery, not execution speed or token use. There is no
measured dollar-cost result. These are agent-provider tokens. Auxiliary
embedding and reranking tokens are not included in those totals.

The earlier treatment scored two correct, one incorrect and two unknown.
The new treatment scored five correct. These are single historical runs with
multiple changes, not an ablation that attributes the improvement to one fix.
The new paired comparison supplies the direct control-versus-Hippo result.
One repeated incident family cannot establish a population effect, confidence
interval, ordinary-task coverage or the HIPO-55 acceptance gate.

### Transport result and reranking diagnosis

| Measurement | Frozen old server | Patched server |
| --- | --- | --- |
| Successful direct calls | 4 / 4 | 4 / 4 |
| First query's audited support nodes | 1 / 4 | 1 / 4 |
| First audited support rank | 5 | 5 |
| Follow-up known-support recall | 100%, 100%, 75% | 100%, 100%, 75% |
| Median call milliseconds | 455.883 | 958.160 |
| Observed p95 milliseconds (four calls) | 465.605 | 979.861 |
| Startup milliseconds | 5,416.903 | 6,003.972 |
| Initial serialized response bytes | 40,250 | 71,192 |
| Initial text-content characters | 36,857 | 39,071 |
| `agent_query` structured output/schema | Absent | Present |
| Read-only tool annotations | Absent | 12 / 12 |

The patched first response reported `rerank_enabled=true` with
`fallback_reason=JevUnavailable` and `stop_reason=assessment_failed`.
Startup logs identify a five-second 1Password credential-read timeout.
Reranking was therefore configured and invoked, but did not improve this
ordering. Follow-up retrieval already found the required records in the old
server. The old agent trial's host approval policy prevented access to them.
The paired regression establishes that the corrected host allows those calls.

Both first responses contain literal pinning and preference-removal phrases,
yet the earlier agent misapplied the pinning policy. This demonstrates why
literal availability alone cannot score useful or correct context application.
The patched response preserves the more explicit decision notes. The source
excerpt now begins with recorded assistant work rather than environment
instructions. Additional structured serialization increases wire bytes;
those bytes are not a measured model-token count. Four calls are descriptive
timings from one family. The new candidates include unjudged records, so graded
NDCG and total-corpus recall remain undefined.

A private direct credential probe succeeded in 5.042 seconds. The startup
allowance was subsequently raised to ten seconds, within the isolated host's
twenty-second server startup budget. A separate four-call transport check still
reported unavailable reranking and a credential timeout. Its startup took
11,052.046 milliseconds. Further bounded credential probes timed out with both
closed and open stdin and with minimal and SDK-inherited environments. These
observations do not establish a timeout-only or stdin-inheritance cause.
The larger allowance supplies margin for the measured successful read; it has
not resolved the intermittent authentication failure.

1Password's desktop integration requires an unlocked app and CLI
authentication. The executor requested that unlock and authorization. The
owner replied "Unlocked; proceed" and authorized the verification to continue.
[1Password CLI app integration](https://www.1password.dev/cli/app-integration)

The subsequent four-call replay returned HTTP 200 for all four Jev requests.
The first query reports `stop_reason=single_pass` and no fallback. Runtime
startup completed in 4,375.465 milliseconds. The known-history first-query
measurements changed as follows:

| Measurement | Frozen old server | Patched server after unlock |
| --- | --- | --- |
| Audited support recovered in the first five hits | 1 / 4 | 2 / 4 |
| First audited support rank | 5 | 1 |
| Literal audited fact groups available | 2 / 4 | 4 / 4 |
| First call milliseconds | 465.605 | 1,283.451 |
| First serialized response bytes | 40,250 | 70,240 |

All three follow-ups recovered four of four audited support nodes. Their
first-support ranks were 2, 1 and 1. Median call latency was 1,108.1155
milliseconds; the observed p95 was 1,283.451 milliseconds. These are four
calls from the same selected family. The four support nodes contain overlapping
history, so recovering every node is not required to supply every needed fact.
The fact-group availability measure captures that distinction.

This verifies healthy reranking and improved first-response context on these
queries. No additional agent pair was launched after unlock. The earlier paired
task win occurred with unavailable reranking. Its improvement therefore cannot
be attributed to the healthy reranker. Authentication availability remains a
measured operational dependency: when credentials were unavailable, the tool
served fallback results and explicitly reported the degradation.

### Evidence identities

Implementation commit: `d58eea2`. Paired study identity:
`e565304a1b3921fdc828663350d2072a43387bbf43b58f29cbdf3ae98c6f3032`.
Canonical paired spec digest:
`76eecedb06eacb69269b15d4055154b933074d0b9a2ef00a7dc9a91b7603bbe4`.
Corpus file SHA-256:
`640b2f3aeca670dad569b0beacc2e72b624cdeea48262ae0c493cf617926870b`.

Private evidence is under
`~/.local/share/hippo-bench/agent-benefit/2026-09-30-v2-execution/`:
`mcp-replay-old/`, `mcp-replay-new/`, `mcp-replay-credential-fix/`,
`mcp-replay-unlocked/` and `mcp-regression-run/`. The latter contains the immutable runner report and
separate hash-bound `mechanism-audit.json`. The source audit verifies five
supported decisions and a full-task win. The runner's initial qualification
fields remain unchanged. No human acceptance review or cohort expansion is
qualified. Raw records and captured history remain private.
