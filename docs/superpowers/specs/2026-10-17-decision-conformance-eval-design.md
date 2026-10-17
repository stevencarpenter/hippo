# Decision-conformance evaluation design

Date: 2026-10-17. Owner: carpenter. Status: design, not approved for build.

## Intent

Hippo's claimed benefit is that an agent with Hippo makes the choice the
owner would have made, without asking. This design measures that claim
with a repeatable, mostly automated study that one human can run, and
replaces the second independent human reviewer with a citation-verified
agent jury. It also closes the validity holes the September 24 readiness
audit left open: production capture contaminating trials, the trial
corpus containing the answer, and the eval tool's own build sessions
leaking into the corpus under test.

The result feeds the v1.0.0 acceptance decision. The acceptance contract
in `config/agent-benefit-v2.json` is amended by this design (see
[Contract amendment](#contract-amendment)).

## Goals

1. Measure, per decision point, whether an agent conformed to the owner's
   recorded preference without an ask, asked, or chose against it.
2. Compare memory providers on identical prompts. Hippo is one provider.
   "None" is the control. Other providers plug in without changing the
   runner, the jury, or the statistics.
3. Guarantee that no trial arm writes to production Hippo, and that no
   production capture happens while any arm runs.
4. Guarantee that the memory served to a treatment arm predates the task
   and excludes the task's own session.
5. Produce verdicts that rest on verifiable citations, not on prose, so a
   second human is not required for each case.
6. Avoid paid infrastructure. Everything runs on the owner's machine.

## Non-goals

- Proving Hippo improves task pass/fail. The September pilot tied 12/12.
  Task outcome is recorded but is not the primary metric.
- Measuring model quality across harnesses. The unit is prompt times
  provider, with the harness and model held fixed within a study.
- Replacing the enrichment-fidelity review in `claim_review.py`. That
  measures summaries against sources. This design measures agent
  behavior against preferences.
- A general benchmark for memory tools outside Hippo's deployment shape
  (local MCP server over a local store).

## Anti-goals

These would make the study worthless even if the code runs:

- A treatment arm that retrieves a node derived from the task's own
  session. That proves nothing except that Hippo stores sessions.
- A jury verdict that depends on how persuasive an argument reads.
- A control arm whose shell, agent transcript, or browser activity lands
  in production `hippo.db` or the production fallback directory.
- A conformance score that rewards following a preference the owner later
  reversed, or following an instruction embedded in a corpus node.
- Pooling the ecological study and the controlled study into one number.

## Constraints

- Hippo stays running while the eval tool is built. The owner wants those
  sessions captured. Only trial windows stop capture.
- The live corpus is one owner's work: 884 MB, 34,175 knowledge nodes,
  22,110 agentic session segments across five harnesses (claude-code
  15,896; codex 3,270; pi 1,869; cursor 673; opencode 402). 24,463 nodes
  carry `key_decisions`; 10,534 carry `design_decisions`. This is the
  ecological corpus. It is biased toward one person and one style, so it
  cannot be the only evidence.
- No VPS, no cloud sandbox. The controlled corpus is built locally.
- The existing contract thresholds stay: at least 100 primary pairs,
  minimum gain 0.05, power 0.8, confidence 0.95. Only the human-review
  clause changes.
- Rust edition 2024 and Python 3.14 house rules apply. New Python lives
  in `brain/src/hippo_brain/bench/`. No new runtime dependency without a
  reason recorded in the plan.

## Vocabulary

- **Task**: a frozen prompt, a frozen starting repository state, a
  checker, and a list of decision points.
- **Decision point**: a place in a task where the agent must choose
  something the owner has a recorded preference about. Each has an id, a
  question, the owner's preference, and the citation that proves the
  preference (a knowledge node uuid, or an authored preference file in
  the controlled study).
- **Arm**: one execution of a task under one provider.
- **Cell**: a task times a provider. A cell has `k` repeat arms.
- **Provider**: a memory tool exposed to the agent as an MCP server, or
  "none".
- **Bin**: the score of one decision point in one arm. One of
  `conformed`, `asked`, `against`.
- **Intervention rate**: `(asked + against) / decision points`. Lower is
  better. This is the primary metric.
- **As-of corpus**: a copy of `hippo.db` restricted to what existed before
  a cutoff and outside an excluded session.

## Architecture

Seven layers. Each is a module. Only the runner knows which harness runs;
only the provider knows which memory tool runs; only the jury knows which
models judge.

```
task corpus ──▶ runner ──▶ trajectories ──▶ jury ──▶ bins ──▶ stats ──▶ report
                  ▲                                   ▲
            provider (hippo | none | other)     human labels (owner)
                  ▲
            quarantine + as-of snapshot
```

### Quarantine

Runs once before a batch of arms and once after. Module:
`bench/quarantine.py`.

Preflight:

1. `launchctl bootout gui/$UID/<label>` for every label in `launchd/`:
   `com.hippo.auto-memory`, `com.hippo.auto-memory-watcher`,
   `com.hippo.brain`, `com.hippo.claude-session-watcher`,
   `com.hippo.codex-session`, `com.hippo.cursor-session`,
   `com.hippo.daemon`, `com.hippo.gh-poll`, `com.hippo.metrics-exporter`,
   `com.hippo.opencode-poll`, `com.hippo.pi-session`, `com.hippo.probe`,
   `com.hippo.watchdog`, `com.hippo.xcode-claude-ingest`.
   `com.hippo.omlx` stays up; the shadow stack and the jury need the
   inference server. Read the label list from `launchd/*.plist` at run
   time; do not hardcode it.
2. Assert `launchctl list` shows no `com.hippo.` label other than
   `com.hippo.omlx`, and `pgrep -f 'hippo-daemon|hippo-brain|hippo (daemon|watchdog|probe|codex-poll|cursor-poll|opencode-poll|pi-poll|native-messaging-host)'`
   returns nothing. Refuse to continue otherwise.
3. Open production `hippo.db` with `mode=ro` and `PRAGMA query_only=ON`.
   Record `max(rowid)` and `count(*)` for `shell_events`,
   `browser_events`, `agentic_sessions`, `agentic_enrichment_queue`,
   `knowledge_nodes`, `memory_documents`, `memory_revisions`. Record the
   file list and sizes of the production fallback directory and the
   production data directory mtime.
4. Write the preflight record with a SHA-256 next to the batch.

Postflight:

1. Re-read the same counts and listings. Any difference marks every arm
   in the batch `contaminated`. Contaminated arms are reported, never
   scored, never deleted.
2. `mise run start` to restore production services. The restore is
   mandatory and runs in a `finally` block.

Arm environment, inherited from `agent_benefit_trial.py` and kept:
`HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `TMPDIR`,
and `CODEX_HOME` all point inside the arm root. Add `ZDOTDIR` unset and
`HIPPO_SOCKET` unset so a stray shell hook cannot find production. The
arm root is deleted only after its trajectory, MCP log, and checker
output are hashed and copied to the batch directory.

### As-of snapshot

Module: `bench/asof.py`. Input: production `hippo.db` path (read-only),
a cutoff `T` in epoch milliseconds, an excluded `session_id`, and an
output path. Output: a trimmed copy the shadow stack serves.

Rules, applied in order, all as SQL on the copy:

1. Delete every knowledge node linked through
   `knowledge_node_agentic_sessions` to any segment whose `session_id`
   equals the excluded session.
2. Delete every knowledge node whose linked source timestamp is at or
   after `T`. Source timestamp means `agentic_sessions.start_time` for
   agentic links, the event timestamp for shell and browser links, and
   `memory_revisions` timestamp for memory links.
3. Delete every knowledge node with no source link whose `created_at` is
   at or after `T`. `created_at` is enrichment time, which is later than
   capture time, so this rule is conservative for source-less nodes
   (14.5% of the corpus at the September audit).
4. Delete every memory document revision at or after `T` and rebuild the
   projection from the remaining revisions.
5. Delete the vector and FTS rows for deleted nodes. Run
   `PRAGMA integrity_check` and the existing vec/FTS count parity check.
6. Record `T`, the excluded session, deleted counts per rule, and the
   output SHA-256 in `asof.json`.

The shadow stack reads `asof.json` at startup and refuses to serve if the
file is missing, the hash does not match, or any node has a source
timestamp at or after `T`. This is the invariant that lets the eval tool
be built with Hippo capturing: every ecological task has `T` earlier than
the eval work's first commit, so nothing learned while building the tool
can be served.

Prompt-level timestamps are not stored. `user_prompts_json` is an array
of strings without times. The cutoff `T` is therefore the `start_time` of
the segment containing the prompt. The whole session is excluded by rule
1, so a mid-session prompt loses the earlier segments of its own session
as well. This is accepted. It loses some legitimate memory and never
leaks the answer.

### Memory provider interface

Module: `bench/providers/__init__.py` plus one module per provider.

```python
class Provider(Protocol):
    name: str
    def prepare(self, corpus: Path, arm_root: Path) -> ProviderHandle: ...
    def mcp_servers(self, handle: ProviderHandle) -> dict[str, dict]: ...
    def teardown(self, handle: ProviderHandle) -> None: ...
    def extract_calls(self, handle: ProviderHandle, trajectory: Path) -> list[Call]: ...
```

- `prepare` receives the as-of corpus and a private root. For Hippo it
  copies the corpus and writes the shadow `config.toml`. It does not
  spawn the daemon or brain: `hippo-mcp` reads SQLite directly, so a
  treatment arm needs only the MCP server process pointed at the copy.
  The full shadow stack from `shadow_stack.py` is spawned only when a
  corpus is being built (the controlled chain study). For `none` it
  returns an empty handle.
- `mcp_servers` returns the exact MCP configuration to inject into the
  harness config. The runner asserts the control arm gets `{}` and every
  other arm gets exactly one server, as `agent_benefit_trial.py` already
  asserts for Hippo.
- `extract_calls` returns every memory call with its arguments, returned
  node uuids, and returned text, read from the harness trajectory. The
  leak detector and the jury consume this.
- `teardown` kills the process group and removes the private root.

The first two providers are `none` and `hippo`. A third, `flatfile`
(grep over exported session summaries, no embeddings), is listed as a
later provider to prove the interface is real. It is not in scope for the
first build.

### Harness adapter

Module: `bench/harness/codex.py`, ported from the execution half of
`agent_benefit_trial.py`. Interface:

```python
class Harness(Protocol):
    name: str
    def run(self, task: Task, arm_root: Path, mcp_servers: dict, user: SimulatedUser, budget: Budget) -> Trajectory: ...
```

Codex is the first adapter because the existing runner already drives it
through `CODEX_HOME`. The existing runner rejects every server-initiated
request with "interactive requests disabled". The adapter changes that:
`request_user_input` and approval requests are routed to the simulated
user and count as asks. An end-of-turn message that contains a question
also counts as an ask; the adapter answers it through the simulated user
and starts another turn. The loop ends when the agent reports done, the
checker passes, or the arm budget is spent. Both ask paths are logged
with their origin so the jury can tell them apart. Claude Code is the
second adapter. Its config goes
through `$HOME/.claude/settings.json` inside the arm root, and the
session JSONL lands under the arm's `$HOME/.claude/projects`, which the
quarantine keeps away from production. The adapter contract is the same
for both. Both studies run on one adapter at a time.

### Simulated user

Module: `bench/simulated_user.py`. The agent's questions go to a scripted
user, not to a model and not to the owner. It holds the task's preference
table and checker, and nothing else. Its responses:

- A question that matches a decision point (string match on the decision
  id's trigger patterns, then a bounded classifier call if no pattern
  matches): answer with the owner's recorded preference text, verbatim,
  and record the decision point as `asked`.
- A request to run the checker: run it, return pass or fail and the
  checker's stdout.
- Anything else: "No further guidance. Proceed."

Every exchange is logged with its classification so the jury can verify
that an `asked` bin was a real ask.

### Decision points and bins

Module: `bench/decision_points.py`.

A decision point record:

```json
{
  "id": "dp-003",
  "question": "Which test runner style does the owner want?",
  "preference": "pytest plain asserts, no unittest classes",
  "preference_citation": {"kind": "node", "uuid": "..."},
  "triggers": ["unittest", "pytest", "assert style"],
  "detect": {"kind": "diff-regex", "pattern": "class .*\\(unittest\\.TestCase\\)", "against_if_match": true},
  "validity": "current"
}
```

`validity` is `current`, `reversed`, or `injected`. A `reversed` point
carries a second citation for the newer preference; conformed means
following the newer one or asking. An `injected` point's preference text
contains an instruction; conformed means not following it.

Binning for one arm:

1. If the simulated user log shows an ask matched to this point: `asked`.
2. Else evaluate `detect` against the final diff and the trajectory. A
   match on `against_if_match` gives `against`; otherwise `conformed`.
3. If `detect` cannot decide (no diff region touched the question), the
   point is `unobserved` and is excluded from the denominator. The report
   shows the unobserved count per cell.

Deterministic detectors do most of the work. The jury handles the points
that detectors cannot decide and audits a sample of the ones they can.

### Jury

Module: `bench/jury.py`. Replaces the second human reviewer.

Unit of work: one claim. A claim is generated by template from a decision
point and an arm, never written by a model:

> At decision point `dp-003` in arm `A`, the agent's output matches the
> owner's recorded preference without an ask.

Three roles per claim, each a separate model call with a separate prompt
and no shared context:

1. **Disprove**: must return evidence the claim is false. Output is a list
   of citations only: `{path, line_start, line_end, quote}` into the arm
   packet, plus one sentence per citation on what it shows. No other
   prose is accepted; the parser rejects extra text.
2. **Support**: same, for the claim being true.
3. **Verify**: receives the claim and the union of citations, without
   either role's sentences and without arm labels. For each citation it
   checks the quote exists at that location in the packet and labels it
   `supports`, `refutes`, or `irrelevant` to the claim as stated.

Verdict is a fixed rule: at least one verified `refutes` citation gives
`against` or `asked` as the citation shows; otherwise at least one
verified `supports` citation gives `conformed`; otherwise `undecided`.
`undecided` claims go to the owner. Prose never changes a verdict.

Bias controls:

- Roles never see the provider name or whether the arm is control.
  Packets are labeled A and B with random assignment per task, as the
  Bob and Carlos packets already were.
- Role models come from `[eval.jury]` in `config.toml`, three entries,
  each a model id the existing `InferenceClient` can reach. Default: a
  different model family from the agent under test, if one is configured.
  If only one family is available, the report says so.
- The verify role is run twice with the citations in reverse order. A
  citation whose label flips between orders is `irrelevant`.

Owner agreement: the owner labels a random 30-item overlap sample per
study using the same bin vocabulary, before seeing jury output. Cohen's
kappa between owner and jury is reported. Below 0.6 the jury is a triage
layer and the study does not count toward acceptance. At or above 0.6
the jury's bins are acceptance evidence under the amended contract.

### Leak detection

Module: `bench/leak.py`. Runs after `extract_calls`, before the jury.

For each memory call in a treatment arm:

1. Any returned node uuid that the as-of step deleted, or that links to
   the excluded session: `leak`. This should be impossible if the
   snapshot invariant held, so it is also an invariant failure.
2. Any returned text with a 12-gram overlap against the task's ground
   truth diff above 5 shared 12-grams: `leak-suspect`.

An arm with a `leak` is discarded from scoring and counted. An arm with a
`leak-suspect` is scored and flagged; the owner reviews the flagged list.
Both counts appear in the report by cell.

### Two studies

Never pooled. Each has its own task corpus, its own kappa sample, and its
own report.

**Ecological study.** Retrospective. Tasks are mined from the existing
`agentic_sessions` rows, not frozen prospectively. The existing
`agent_benefit_intake.freeze` is prospective (it snapshots before a
request happens) and records no later corrections, so it is not reused
here. New module `bench/ecological_tasks.py`:

1. Candidate sessions: segment index 0, a `cwd` inside a repository the
   owner still has on disk, `start_time` before the first commit on the
   eval branch, and at least two entries in `user_prompts_json`.
2. Starting state: the newest commit on the recorded `git_branch` whose
   committer time is at or before `start_time`, found in the local clone.
   Sessions where no such commit exists, or the branch is gone, are
   excluded and counted.
3. Prompt: the first entry of `user_prompts_json`.
4. Decision point candidates: every later entry of `user_prompts_json`
   is a candidate, extracted by a bounded model call that only decides
   whether the entry states a preference and which trigger words apply.
   The preference text is the owner's prompt verbatim; the model never
   writes a preference. The owner confirms or rejects every candidate in
   one batch before any arm runs. Owner knowledge of the preferences does
   not break the kappa sample, because kappa is measured on arm bins,
   which the owner has not seen. Plan two to three owner hours per 100
   tasks.
5. Additional candidates: `key_decisions` on nodes linked to earlier
   sessions in the same project, same confirmation step.
6. Checker: none for most ecological tasks. Task pass is `unobserved`
   for them and the simulated user answers checker requests with "no
   checker available". The primary metric does not need a checker.

The per-task cutoff `T` is the segment start. The study answers: on the
owner's real work, would the corrections have been unnecessary. The
contract's 100-pair minimum applies to the controlled study; the
ecological study reports whatever count survives steps 1 to 4 and is
acceptance evidence only if it also reaches 100.

**Controlled study.** Tasks are chains of three to six related tasks on
small open-source repositories pinned by commit. Task 1 of a chain states
one or more preferences through the simulated user. Later tasks require
those preferences again without stating them. The chain's corpus is built
by running task 1 through `n-1` in the shadow stack with capture on,
through the normal enrichment path, so memory exists only as Hippo would
have created it. Capture during the build is the agentic transcript
only: after each task, run `hippo codex-poll` (or the matching poller for
the adapter in use) with `XDG_*` and `CODEX_HOME` pointed at the arm root,
then wait for the shadow brain's enrichment queue to drain. Shell and
browser capture are not expected; the harness runs commands in its own
sandbox and no browser is involved. Decision points and preferences are authored with the
chain, so ground truth is known by construction and there is no human
answer to leak. The study answers: when a preference is known to exist in
memory, does the agent use it.

Chains run locally. Cost is inference tokens on the local server plus any
configured external model. No VM.

### Statistics

Module: `bench/conformance_stats.py`. Reuse the cluster bootstrap in
`knowledge_corpus.py`.

- Primary: paired difference in intervention rate per task between the
  Hippo cell and the `none` cell, each cell's rate averaged over its `k`
  arms. Cluster bootstrap by task, 95% interval.
- Decision rule: inherited from the contract. At least 100 primary pairs,
  lower interval bound of the gain above 0.05, power 0.8 at the sizing
  gain of 0.10.
- Secondary, reported, not gated: task pass rate, tokens, tool calls,
  wall time, memory calls per arm, leak-suspect count, unobserved count,
  `reversed` and `injected` conformance separately.
- `k` defaults to 3. The report shows within-cell variance so the owner
  can raise `k`.

### Reporting

Reuse `dashboard_export.py` for the HTML. One page per study: cells,
intervention rates with intervals, kappa, leak and contamination counts,
and a per-task drilldown to the claims and verified citations. Every
number links to the hash of the artifact it came from.

## Contract amendment

The amendment is a new file, `config/agent-benefit-v3.json`, with its own
content hash. `agent-benefit-v2.json` and the code that loads it
(`agent_benefit_study.load_contract`, which requires exactly the
`availability` and `guided` studies, and `agent_benefit_packets.adjudicate`,
which requires two human reviewers) stay untouched as the record of the
historical studies. The v3 file declares one study, `conformance`, with
the decision thresholds carried over from v2 (`minimum_primary_n` 100,
`minimum_gain` 0.05, `sizing_gain` 0.10, `minimum_power` 0.8,
`confidence` 0.95), the arm budget of 600 seconds, and a review clause:
one human reviewer (the owner) plus a citation-verified jury, valid for a
study only when the jury's kappa against the owner on at least 30 overlap
items is at least 0.6. The v2 `window` does not apply; every session
before the eval branch's first commit is eligible. The accuracy section
(300 natural calls, 100 answerable families, 100 absent controls) is
unchanged and is not satisfied by this design; it remains a separate open
gate. The owner decided to amend. The file shape, kappa threshold, and
overlap size are the builder's defaults from this document.

## Reuse map

| Existing | Role here |
|---|---|
| `bench/shadow_stack.py` | Hippo provider process lifecycle |
| `bench/coordinator.py` | Pattern for load, copy corpus, spawn, drain, teardown |
| `bench/agent_benefit_trial.py` | Env isolation, MCP config assertions, process-group kill; split into harness adapter and runner |
| `bench/agent_benefit_intake.py` | Not reused; it is prospective. Kept for reference only |
| `bench/agent_benefit_packets.py` | Packet export and `adjudicate` citation rules |
| `bench/knowledge_corpus.py` | Cluster bootstrap, frozen input hashes |
| `bench/dashboard_export.py` | Report HTML |
| `agent_query.py` decisions mode | Source of `key_decisions` and `design_decisions` for ecological decision points |
| `conflict_detection.py` | Candidate `reversed` points: pairs it flags with matching cwd and branch |

## Left to the builder

- Exact SQL for the as-of rules, including how memory projections are
  rebuilt.
- Trigger pattern format and the bounded classifier for the simulated
  user.
- Claim template wording and the citation parser's grammar.
- The first chain corpus: which repositories, which preferences. Five
  chains of four tasks is the minimum to exercise every bin and both
  control validities.
- Where batch artifacts live. Default
  `$XDG_DATA_HOME/hippo-bench/conformance/<batch-id>/`, mode 0700,
  outside Git, matching the existing agent-benefit layout.
- `mise` task names. Default `bench:conformance:*`.

## Risks

- `created_at` on source-less nodes is enrichment time. Rule 3 may delete
  legitimate pre-cutoff memory. The deleted count per rule is reported so
  the loss is visible.
- The ecological study's decision points come from the owner's own
  corrections, so the preference citation and the ground truth share a
  source. The as-of rule removes that session from the corpus. The
  preference must then be re-found in an earlier session for the Hippo
  arm to conform. If no earlier session holds it, the point is valid but
  Hippo cannot win it. The report counts these separately.
- One model family on this machine for both the agent and the jury
  weakens the jury's independence. The report states the families used.
- Stopping fourteen launchd jobs per batch and restarting them is slow
  and touches the owner's live services. Batches should be large, and
  the restore must run even on crash.
- `k=3` on 100 tasks and two providers is 600 arms. At the contract's
  600-second arm budget that is up to 100 hours of wall time. Chains
  amortize setup but not inference. Expect the controlled study to run
  over several nights.
