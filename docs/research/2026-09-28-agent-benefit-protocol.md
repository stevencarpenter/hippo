# Agent benefit evaluation for v1.0.0

Status: proposed protocol, September 28, 2026. Freeze this protocol, the task
selection rules, and the success checks before running the acceptance cohort.
The [September 24 readiness assessment](2026-09-24-v1-readiness.md) remains the
current release decision.

## Question and population

Does access to Hippo improve an agent's independently verified work on tasks
from projects whose activity Hippo captures? The primary population is real
maintenance and investigation tasks from those projects, sampled consecutively
from a declared time window. The memory snapshot must predate each task, but
relevant history is not an eligibility condition. Record every eligible task,
including ones with missing or misleading Hippo evidence. A result on this
population does not establish benefit on unrelated projects or task types.

Before seeing any agent outcomes, classify a candidate as eligible when a
human-originated request names one bounded maintenance or investigation goal,
its starting repository and dependencies can be reset for both arms, and the
goal has an independent executable check or a blind, source-backed adjudication
rubric. Exclude harness launch briefs, commit-message formatting, PR-title
generation, requests that require irreversible external actions, and tasks
whose pre-task state cannot be reconstructed. Record the exclusion reason.
Do not screen on apparent difficulty, expected agent success, or whether the
snapshot contains relevant Hippo evidence. Group repeated worker sessions and
multiple requests about one issue into one candidate family.
Check prompt provenance in the source session before classifying a request as
human-originated. A session marked non-subagent can still contain an SDK
generated review brief; a Codex review subagent can also appear in the raw
session inventory. Exclude those prompts even when their repository and diff
are available.
Read the complete source request and referenced attachments before freezing a
task. A database prompt preview can omit task-defining paths; hash the full
request and the attachment bytes used by both arms.

The September 24 next-action pilot scored 12/12 in both arms, with zero paired
wins or losses. It measured a choice, not executed work, and had a ceiling. It
cannot establish either benefit or absence of benefit. Higher stated confidence
and more citations are not success measures.
The new scorer applied to its eight incident rows reports zero gain and a
conservative interval of approximately -42 to +42 percentage points, treating
those rows as independent for this diagnostic only. The four related controls
remain separate. The pilot's actual dependence and authored selection preclude
using this interval as a population estimate.

## Complementary studies

The executed-task experiment below is the release decision study. Two other
studies explain its result without replacing it:

| Study | Paired outcome | Distinct failure it detects |
| --- | --- | --- |
| Historical recall | Ask a real follow-up question without answer options. Blind reviewers check the answer and every cited source against pre-task capture; include answerable and genuinely absent cases. | Retrieval misses, invented history, and failure to abstain before an agent acts. |
| Repeated-mistake replay | Recreate a previously recorded failed approach in a resettable task. Score whether each arm actually repeats that action, reaches the verified fix, and how many failed steps it takes. | A memory benefit hidden by both arms eventually reaching the same answer. |

Sample these tasks independently of retrieval outcomes and keep the no-memory
arm's ordinary repository and search tools available. An additional diagnostic
arm can receive raw linked sources in place of enriched Hippo summaries to
locate enrichment errors. Select and analyze that arm on development data; it
does not replace the normal Hippo-versus-control release comparison. A later
opt-in field experiment can randomize Hippo availability by whole task family
and measure completion, rework, wall time, and tool cost under real workloads.
It needs the same independent outcome checks and contamination controls; usage
or citation counts alone remain observational.

### Answer accuracy and abstention gate

This is a separate v1 readiness gate, not a substitute for executed agent
benefit. Freeze 300 consecutive factual Hippo query calls made by agents on
real human-originated tasks in a declared prospective window, before viewing
answers. Preserve the complete request, response, source packets, task-family
identity, Hippo build, and a memory cutoff that predates the query. Select the
first qualifying call per independent task family for the primary rate; keep
later calls in that family as repeated measures. Require at least 100 primary
families, or leave the result inconclusive. Report direct human Hippo queries
as a separate slice. Two reviewers blind to the system answer determine from
the frozen sources whether each question is answerable, identify the
supporting facts, and resolve disagreements. A
source-answerable question remains answerable when retrieval misses its source.
Do not use the older keyword QA fixture or assistant-written labels as human
acceptance judgments.

For each answerable question, separately score factual correctness, whether
every material claim is supported by a cited pre-query source, and whether
the answer uses a superseded fact. Score an abstention on an answerable
question as a failure. Add 100 independent genuinely absent controls, frozen
separately,
including post-cutoff and stale/conflicting-history questions; absence needs
a source audit, not just an empty retrieval result. For these controls, score
correct abstention and any unsupported factual assertion. Report exact
binomial bounds over independent primary families and controls, with all
timeouts and tool failures retained in the denominator. Report natural-query
answerability and per-family results separately from the constructed controls.
The study is inconclusive if the sample or independent labels are incomplete.

For the v1 answer-quality decision, require at least 100 independent
source-answerable primary families within that consecutive-call sample. A
primary family succeeds only when its first qualifying answer is factually
correct, every material claim is supported by a cited pre-query source, and
no superseded fact is used; abstention, timeout, and tool error fail. The
one-sided exact 97.5% lower bound on this joint success rate must be at least
90%. Each of the 100 absent controls fails on an unsupported factual claim,
failure to abstain, timeout, or tool error. The one-sided exact 97.5% upper
bound on that failure rate must be at most 5%. Bonferroni allocation makes
these two bound claims jointly 95% confident without assuming independence
between them. Missing sample size or labels leaves the gate inconclusive;
failing either bound holds v1. These are release tolerances selected before
the prospective outcomes, not rates inferred from the existing 100-question
fixture. Report the component error rates and the post-cutoff and
stale/conflicting controls separately even when the joint gate passes.
The current frozen live copy has only 25 shell events matching `hippo ask`.
A summary-level scan before September 22 found roughly 486 `ask` or
`agent_query` tool-call mentions across 185 agent sessions, including repeated
calls and subagents. This is enough to justify prospective intake of agent
queries, but it is not a verified count of independent factual questions or
an accuracy sample.

A read-only census of all 246 still-readable Pi transcript files referenced
by the September 28 frozen database found 127 complete `hippo_ask` calls in
52 primary session files before that database's backup cutoff. Sixteen calls
returned tool errors; 124 carried a nonempty `question` argument. The calls,
responses, and complete source files were copied with SHA-256 checks into a
private diagnostic archive at
`~/.local/share/hippo-bench/agent-benefit/2026-09-29-pi-ask-census/`.
No question was screened for factual scope or human task provenance, and no
answer or cited source was independently graded. Even if each file represented
a different eligible family, this Pi slice would supply at most 52 primary
families. It cannot satisfy the 300-call, 100-family accuracy gate.

A separate read-only census inspected every Codex transcript path referenced
by the same frozen database, not just rows whose tool summaries mentioned
Hippo. Of 1,655 distinct paths, 1,653 remain readable. Before the database
backup cutoff, 279 files contain `tools.mcp__hippo__ask` or
`tools.mcp__hippo__agent_query` invocation syntax: 1,187 `ask` and 13
`agent_query` occurrences. The files represent 144 primary sessions and 135
subagent sessions. The outer tool execution visibly completed for 1,158
`ask` occurrences; four errored, four have other output, and 21 lack a
recorded completion after yielding. The diagnostic manifest records each
source-file hash, invocation timestamp, and outer execution status at
`~/.local/share/hippo-bench/agent-benefit/2026-09-29-codex-query-census/`.
These are source-code invocation counts, not verified factual questions or
completed Hippo responses. No human task-family, answerability, source, or
accuracy labels exist for this slice. It predates the declared prospective
acceptance window, so its volume cannot satisfy that gate.

## Paired experiment

1. Freeze the Hippo database before case-specific evaluator investigation or
   agent work that Hippo could capture. If such preparation already happened,
   use a verified earlier snapshot. Record the backup interval and cutoff;
   reject any snapshot containing the answer key, an earlier trial, or
   post-cutoff evidence for the task. Freeze the task description, repository
   commit, and test environment before either arm starts. Keep source text and
   agent traces in private external artifacts, not Git.
2. Run two fresh, isolated instances of the same agent model and configuration
   per task. Both receive identical instructions, repository state, ordinary
   tools, and wall-time limit. Neither arm has a token cap; record token use
   from the JSON trace when available. Randomize arm order. The treatment arm
   receives the normal read-only Hippo tools; the control arm does not. Neither
   arm sees the other's work, labels, or evaluator output. Run in separate
   disposable worktrees with no shared writable state. Enforce read and write
   denials on private answer keys, original repositories and session history,
   the live Hippo database, and the sibling arm. Use an ephemeral agent process
   without the shared daemon or inherited MCP servers. A workspace write
   setting alone does not establish this blindness. Deny model-generated shell
   commands direct access to the treatment database so memory is reached only
   through the configured Hippo tools.
3. Run the same preregistered success checks on both outputs in clean
   environments. Prefer executable tests and reproduced issue behavior. Two
   reviewers blinded to arm adjudicate cases that need judgment, including
   whether an answer or cited source actually supports a claim. Resolve
   disagreement before unblinding. An agent's assertion that a fix worked is
   not a success check.
4. Keep all attempted tasks in the denominator. A timeout, agent failure,
   missing result, or unusable patch counts as unsuccessful for the primary
   endpoint. Record tool errors, unavailable Hippo results, and incomplete
   evidence separately. Do not discard a task because Hippo failed to help.

Use development tasks only to debug instructions and estimate variance. Freeze
the model, Hippo build, query interface, evaluator, case list, and analysis code
before opening the acceptance cohort. A changed recipe needs a new holdout.
Select one primary task per independent incident or source-session family. A
second task or run from the same family is a repeated measure for descriptive
analysis, not a new independent primary task.

## Measurements

| Measure | Outcome | Why it matters |
| --- | --- | --- |
| Verified task completion (primary) | Paired difference in the fraction of tasks passing the frozen success checks | Direct agent benefit; includes retrieval failures and agent misuse. |
| Historical decision reuse | Correct prior decision or failed attempt identified, source verified, and applied to the current task | Tests Hippo's claimed memory mechanism. Score source recovery, interpretation, and application separately. |
| Repeated-mistake avoidance | Prior known-bad action repeated, or avoided, during executed work | Detects a benefit that a final answer alone can hide. |
| Work and latency | Wall time, model tokens, tool calls, failed commands, and cost where measured | Detects savings and the overhead of reading memory. Missing usage remains unknown. |
| Harm and calibration | Unsafe actions, unsupported claims, stale-history errors, prompt-injection compliance, and confidence versus correctness | Detects negative effects even when completion rises. |

Report each measure overall and by preregistered task family: incident repair,
configuration choice, dependency or API migration, and factual investigation.
Include a no-relevant-history control and stale/conflicting-history control.
These controls measure harm and specificity; they are not mixed into the primary
benefit denominator. Report Hippo availability, retrieved-source relevance,
source provenance, and whether the agent used the result. These explain the
mechanism but cannot substitute for task completion.

## Decision rule

For the declared task population, call Hippo beneficial only if the paired
verified-completion difference has a positive lower 95% confidence bound and a
point gain of at least five percentage points. For each independent primary pair,
score a win when only Hippo succeeds, a loss when only control succeeds, and a
tie otherwise. The gain is (wins minus losses) divided by all pairs. Bound the
win and loss proportions separately with exact binomial limits, using a
Bonferroni allocation across their four one-sided limits, then subtract the
limits. This conservative paired interval does not collapse to zero when all
pairs tie. Report wins, losses, ties, the interval, and the complete task
denominator. Five points is the proposed minimum observed gain for this v1
decision. Ten points is the effect used to size the study for 80% detection
power. Using ten points as both the true effect and a required observed point
estimate would cap power near 50%, regardless of sample size. Freeze these
values before collecting acceptance outcomes.

Call a gain of five percentage points or more **ruled out for this population**
only if the upper 95% bound is below five points. If that bound is below zero,
report **harm** in verified completion instead. Anything between those
decisions is inconclusive. No finite experiment proves that Hippo never helps
any agent. Separately report a confidence interval for treatment-minus-control
safety harm; any observed severe safety violation requires case review before
release. Do not offset a verified unsafe action with faster completion. The
completion-harm verdict uses the same frozen pairs and interval; it does not
classify isolated safety events.

Use a development cohort to estimate the number of independent families needed
for the locked cohort. Simulate the planned exact paired interval on plausible
win/loss rates and choose a fixed sample size with at least 80% power to detect
a true ten-point gain. Set a maximum budget and a minimum of 100 independent
task families before observing acceptance outcomes. If the available population
is smaller, report the interval and leave the decision inconclusive. The
12-case pilot supplies no useful variance estimate because every pair tied.

An illustrative 1,000-replicate simulation with seed `20260928` applied the
current exact-interval decision rule to a true ten-point gain. At paired
Hippo-only/control-only success rates of 12%/2%, 20%/10%, and 35%/25%,
estimated detection power at 500 independent tasks was 100%, 86%, and 57%,
respectively. At 1,000 tasks it was 100%, 100%, and 92%. These are planning
scenarios, not observed Hippo outcomes. A 100-task minimum cannot be presented
as an adequately powered sample. The development cohort must estimate
discordance and the practical task budget before the acceptance size is fixed.

## Evidence needed for the gate

The acceptance record must contain the frozen sampling frame and exclusions,
task and source hashes, per-arm model/configuration and tool permissions,
randomization seed, complete terminal traces, blind reviewer labels, executable
success results, and a reproducible paired report. Audit a sample of source
links against the pre-task snapshot. Keep private data in mode 0700 directories
and 0600 files outside the repository, as the existing benchmark workflows do.
Freeze and hash the selected task ID, family, and kind list before running
either arm. Preserve the larger candidate frame with ineligible-task reasons.

The first executable step is a development cohort of 20 to 30 consecutive
eligible tasks. Its purpose is to establish that tasks can be reset and scored,
estimate baseline completion and paired variance, and expose leakage. Its
outcome must not be reported as v1 acceptance.

### Available development material

A read-only check on September 28 found a frozen September 22 SQLite snapshot
at `~/.local/share/hippo-bench/decisions/knowledge-1790057730710/baseline.sqlite`.
It passed `PRAGMA quick_check` and contains 29,626 knowledge nodes. Its file
mtime is September 22 at 06:15:31 UTC, and the latest captured node predates
that time. Its schema is 24, while the current product uses schema 25. A private
copy was migrated through the canonical `hippo_core::storage::open_db` path.
The migrated copy has schema 25, passes `PRAGMA quick_check`, retains all
29,626 knowledge nodes and 2,929 event links, and answered an isolated
`hippo-agent-query` evidence request. The frozen source hash remained unchanged.
This establishes a usable development fixture, subject to each task's history
cutoff; it does not qualify the current release. The
current store has 25 distinct non-subagent sessions under this
repository root that started after the snapshot and contain captured user
prompts. These are candidate episodes, not 25 eligible independent tasks:
prompts may cover multiple requests, lack a
resettable environment, or lack an independent success check. The inventory
includes evaluation sessions, including this one. Vet them against
the preregistered eligibility rule before forming a development cohort. The
snapshot is a possible pre-task memory state for those episodes, subject to
verifying source cutoff and task start time individually.
The 25-episode, 82-segment candidate inventory is frozen privately at
`~/.local/share/hippo-bench/agent-benefit/2026-09-28-candidate-frame/candidate-frame.json`.
It records prompt hashes and metadata, without prompt text. The snapshot SHA-256
is `b9eb5c74ca7ba78eb337b16ef519918bd251f93580e31838ee0d394d51b86ff6`.
The inventory file is mode 0600 and its directory is mode 0700. It is a
development sampling frame, not adjudicated task labels or experiment results.
Across all captured project paths after that snapshot, there are 201 distinct
non-subagent sessions with prompts; 177 currently point to accessible Git
worktrees. These are an upper bound on development candidates, not independent
eligible tasks. Several worktrees are copies of the same repository, and many
Hippo sessions are release reviews or commit-message requests. Group by the
underlying issue before selecting a task. Do not pick only episodes that appear
to have useful Hippo evidence.
The all-project frame is frozen privately at
`~/.local/share/hippo-bench/agent-benefit/2026-09-28-all-project-candidate-frame/candidate-frame.json`.
Among the 177 accessible-Git episodes, five had no substantive prompt after
instruction wrappers; the remaining episodes produced only 64 distinct exact
first-request hashes, with 121 episodes in duplicate-hash groups. First-request
review found repeated worker launch briefs and commit-message requests. These
counts show why session count cannot serve as task sample size. Independent
eligibility and success-check review remains necessary; new prospective tasks
will be needed if fewer than 20 historical episodes qualify for development.
A private prompt-only screen of the 201 episodes found 24 without an accessible
Git root, 14 more without a substantive first request after instruction
wrappers, and 64 distinct first-request groups among the remainder. Forty
groups were excluded by the preregistered prompt rules. Of the remainder,
19 need repository-state and success-check review, two have verified reset
reproducers, two have verified feature-development environments, and one is a
verified no-relevant-history control. These are
provisional task labels, not agent outcomes or independent families. The
private screening ledger retains prompt text, hashes, session IDs, and
exclusion reasons.
All 23 groups that advanced to context review have an available Git commit
dated before the captured request and a surviving session source file. Ten
candidate commits were found on the captured branch; 13 initially required a
search across all local refs. For one of those 13, the captured initial Git
status and reflog identified a later, exact branch HEAD. The all-refs candidate
was only its base-branch commit.
A commit before the request does not prove the agent's exact starting tree,
uncommitted changes, dependency state, or an independent success check.
Source-session provenance review excluded eight more of the 64 first-request
groups. Seven security-review briefs entered Claude through `sdk-py` with a
generated changed-files and diff prompt, and one Codex session declares
`source.subagent=review`. The prompt-only screen had left these in context
review because their first requests looked like ordinary code reviews. They
are not evidence of independent human task demand.
The Gringotts assessment's stored first-request preview stops at 499
characters, before five required research-document paths. Its source Pi
message is 909 characters. A later provenance check found that every line of
all five current documents matches the original Pi `read` tool results. The
original session captured a clean worktree at commit
`d9b6c2ace245273be6418a09a717222a8c0884d7`, before the request. Private
control and treatment clones now contain only that commit's reachable history,
and both prompts reference the verified document copies. The source transcript,
original prompt, hashes, and read ranges are frozen under
`~/.local/share/hippo-bench/agent-benefit/2026-09-29-gringotts-provenance/`.
The blind, source-backed six-item rubric is frozen at
`~/.local/share/hippo-bench/agent-benefit/2026-09-29-gringotts-provenance/rubric-v1.private.md`
(SHA-256 `b02dc0a638c6191c39b71a65b9a407b6a7da6cfed80db4e868c0071811243810`).
This makes Gringotts a fifth resettable, independently scorable investigation
candidate. It has no paired agent outcome.
Context review is complete for all 64 distinct first-request groups. Forty
were excluded by prompt, eight by source-session provenance, five now have
verified resettable development or investigation cases, and one is the
separate no-history control. Of the remaining ten, four lack a verified local
reset, two require historical external state, two
duplicate another issue family, one has only a focused failure reproducer,
and one was delegated to a worker in a different repository whose starting
state is not frozen. The historical
frame therefore cannot supply the planned 20 to 30 eligible development
tasks. The consecutive prospective intake must supply them; these counts do
not estimate Hippo's benefit.
The complete screening decisions were copied before any paired agent run to
`~/.local/share/hippo-bench/agent-benefit/2026-09-28-all-project-candidate-frame/eligibility-screen-v1-frozen.private.json`
(SHA-256 `c544804c0c86bf25b830d11ed3b3d4607543b4f93412a9f67d6e47b09c25d56a`).
Any later reconstruction must create a new version rather than overwrite it.
Context review rejected the September 23 task to verify live Jev use as a
paired replay. Its session records Jev disabled in the running brain before
the task, then a configuration change and restart that enabled it. No runnable
snapshot of the pretask service, configuration, and database was captured, so
the source commit cannot restore the condition the task asked about.
Context review also withheld the September 24 `gcamp` symlink failure. The
captured tree had three unstaged changes, and the failure depended on absolute
Nix store links returning to the original dotfiles path. A private clone of
the source commit alone does not reproduce that link loop.
The later `just update` report about Agent Journal diffs and `.chezmoi-bak`
files traces to that same symlink migration. The screening ledger groups it
with the `gcamp` failure as one issue family.
A separate September 25 Firstmate failure does reproduce in a private clone:
the captured pin and checkout were `9284978`, upstream was `ea7c7f7`, and a
real process with the checkout as its working directory made the pretask
updater exit 1 with the reported running-process guard message. Applying the
historical patch in a separate oracle clone made the bulk update defer with
exit 0 while an explicit update still refused to move the busy checkout.
The original three unstaged files and the full `just update` environment have
not been reconstructed. This is a development diagnostic, not an acceptance
pair. Its private fixture and checks are at
`~/.local/share/hippo-bench/agent-benefit/2026-09-29-firstmate-busy-case/`.
Open issues are not automatically valid tasks. A September 28 spot check of
Hippo issues #135, #136, and #138 found their requested index assertions,
health PID field, and bench gate aggregation already present in current source.
An issue-based cohort must check the starting commit against the issue before
freezing its task; issue state alone is neither a failure reproducer nor a
success check.
Initial historical-task checks found concrete reset problems. One maintenance
session began on a dirty branch, so its starting tree is not recreated by its
recorded commit. Another investigation required recovering a pre-task commit
that was absent from the current local repository. A diagnostic session's
initial prompt was captured at the 500-character limit and its later prompts
span multiple goals. Task-specific evidence and adjudication stay in private
external artifacts. These checks favor prospective task intake with a fresh
pre-task repository and memory snapshot.
A separate factual-investigation case now has a clean historical repository
commit, a captured prompt hash, and an independent Git-history answer key.
Its decisive event occurred after the September 22 memory snapshot, and its
top five isolated Hippo results did not describe that event. It is a
no-relevant-history control for agent execution, not evidence of Hippo benefit.
Its separate trial package at
`~/.local/share/hippo-agent-trials/2026-09-29-sluice-no-history-package/`
has two clean clones at the same commit. Both contain the artifact's add and
removal commits in Git history, and neither can read the private answer key.
The treatment uses a private September 22 database and lists 12 Hippo tools.
Named-profile probes allowed assigned work and denied the sibling, other
trial packages, the answer key, credentials, and raw treatment database.
Its arm order is frozen separately at
`~/.local/share/hippo-bench/agent-benefit/2026-09-29-sluice-control-schedule.private.json`
(SHA-256 `7c9d5aca64b0b9c0396f8b17a3049c1a1c5b34ffac1ab5f9d5cee79be70eea38`).
Private capture configuration later changed its treatment config hash. The
unchanged arm order is recorded in
`~/.local/share/hippo-bench/agent-benefit/2026-09-29-sluice-control-schedule-v2.private.json`
(SHA-256 `a22960b23e3be164b7b62d082879ae4042660e56f301349f5d14437943688153`).
This case remains outside the primary benefit denominator and has no agent
outcome.
Adding an isolation rule for a separate FirstMate stale-history canary changed
only the Sluice control's configuration hashes. Its prompt, database, arm order,
and wall limit remain fixed in
`~/.local/share/hippo-bench/agent-benefit/2026-09-29-sluice-control-schedule-v3.private.json`
(SHA-256 `977b875b6af56dbf049556592692974436a09c704797092d4ec6a0282ea8aaab`).
No agent run preceded this amendment.
A constructed FirstMate canary uses a September 22 frozen memory record and a
later repository commit that changed the update procedure. Both arms have the
same prompt and clean repository commit. Two treatment `ask` preflights used
Jev successfully, but neither retrieved the target old record among 30
candidates. The second prompt was revised after inspecting the first retrieval,
so this canary is not a frozen acceptance case. It has no agent outcome and
does not yet verify stale-history exposure.
A separate CI-maintenance case has a clean private checkout of a commit two
minutes before the request. Its named configuration check fails locally with
exit status one, matching the captured CI error. The checkout has no remote and
does not contain the later fix commit. A private adjudication rubric checks
that the requested obsolete test and its workflow invocation are removed while
unrelated checks remain. This is a resettable development candidate, not an
executed pair or a success observation. An isolated evidence query on its
schema-25 task clone returned five hits, including records about earlier
configuration-test policy. That observation was made after task selection and
does not change the candidate's primary-task status. The later historical fix
removes the named script and CI step; the rest of that workflow is byte-for-byte
unchanged. This validates the private rubric's positive example but is not an
agent trial result.
A later, separate update-crash request also has a private pre-task checkout.
The captured trace identifies an external tool checkout ahead of its reviewed
pin. Reconstructing both commits under an isolated home reproduces the
original launcher's exit status one and its pin-mismatch error, without running
the full update command. A source-backed rubric requires diagnosis of the
drift and a safe update path. The initial frozen Hippo query returned five
hits, but none of their summaries named the mismatch. This second candidate
has no agent outcome and is not an acceptance result. The pre-task test suite
passed despite the reproduced failure. The later fix's offline FirstMate and
update tests both pass in a separate oracle checkout. Those later tests encode
one implementation, so blind review must also accept alternative safe repairs
that meet the behavior rubric.
The FirstMate case now has separate clean control and treatment clones of the
pre-task dotfiles commit and the external FirstMate checkout. Each arm has its
own HOME with the captured reviewed pin. Running the pre-task launcher with
`--setup` exits one in both arms with the captured pin-mismatch error; neither
dotfiles clone contains the later fix commit. The package is at
`~/.local/share/hippo-agent-trials/2026-09-29-firstmate-update-package/`.
Trial runtime isolation and paired agent execution remain unverified.
A third development candidate is a request to add a Git helper that prepares
and opens a PR using an existing helper. Its captured initial status is clean;
the branch HEAD was recovered from the reflog and reproduced with a local bare
remote, without the later fix commit. The historical fix passes an offline zsh
test with mocked Git and PR commands. The private rubric checks helper reuse,
accurate branch-wide PR metadata, default-branch and malformed-output guards,
and preservation of existing behavior. The isolated Hippo query returned five
low-confidence hits, none of whose summaries named the requested helper.
Selection and state verification preceded that query. This is a resettable
feature-development candidate, not an executed pair or evidence of benefit.
A fourth development candidate asks for routing across every repository in a
private organization. Its initial Git status is clean and matches the pre-task
commit. The original session captured the organization's ten-repository list;
that dated inventory is supplied to both arms instead of querying live GitHub.
A separate checkout of the resolver at task time provides a behavior check that
fails on the pre-task configuration and passes on the captured first response:
each repository resolves to its own workstream. A subsequent user correction
removed one repository from the final historical commit. The frozen trial
scores the first request and its ten-repository inventory, not that later
follow-up. An isolated Hippo query returned five hits after case selection;
two summaries named the organization, and none named the routing tool. This
case has no paired agent outcome.
The Agent Journal case now has separate clean control and treatment clones at
the captured `journal` commit, a shared hash-verified ten-repository inventory,
and identical prompts. The private resolver check fails in both clones because
`sluice` has no matching workstream. Later fix commits and the answer key are
absent from each clone. The package is at
`~/.local/share/hippo-agent-trials/2026-09-29-agent-journal-package/`;
trial runtime isolation and paired agent execution remain unverified.
The current private development frame contains these four primary candidates
and the separate no-history control. Its selected-task list is frozen at
`~/.local/share/hippo-bench/agent-benefit/2026-09-28-development-frame.json`
with SHA-256
`f199ca1a0b2fedf1d4136ab048f4694eeeb1ada7d549cee8eb5ffaacef96d742`.
This is a development frame, not the prospective acceptance sample; it has no
agent outcomes. Gringotts was verified after this frame was frozen and is
tracked separately; the frozen list and its hash are unchanged.
A manual retrieval diagnostic queried each selected development prompt against
its frozen database after task selection. One of four prompts had any top-five
hit describing the same component: three CI hits discussed earlier work on the
named test script, all from one prior issue family. The other three prompts had
zero top-five hits naming their requested component or failure; some hits
matched generic update, PR, or organization terms. These are single-assessor
source-relevance labels, not blinded answer checks or agent outcomes. The
private diagnostic records hit UUIDs, labels, and database hashes at
`~/.local/share/hippo-bench/agent-benefit/2026-09-28-retrieval-diagnostic.private.json`.
The frozen source database checksum was unchanged after the queries.
The separate no-relevant-history control also returned five evidence-mode
hits, none of whose summaries describes the decisive event, which occurred
after the snapshot. The answer is available from the frozen Git history, so
this control tests whether an agent treats generic memory hits as evidence.
The default known mode listed those unrelated summaries with a stale-evidence
warning; it did not answer the control question.

A compatible prospective memory snapshot was captured from the live database
using one consistent SQLite backup transaction at September 29, 2026,
02:46:20 to 02:46:21 UTC. Its private path is
`~/.local/share/hippo-bench/agent-benefit/2026-09-28-prospective-snapshot/hippo.sqlite`.
It has schema 25, 30,958 knowledge nodes, `PRAGMA quick_check = ok`, and SHA-256
`80c95cad84d8075994f2ef524161b4ab2947aa8b4e9c3743ae26c22fa366c1b5`.
The adjacent private manifest records the backup interval and checksum. Only
tasks that start after the backup finished can use it without future leakage.
This snapshot is a tooling fixture. Freeze a fresh source snapshot immediately
before each prospective task so the treatment sees the available pre-task
history. Its existence does not establish benefit or task eligibility.
The compact `hippo-agent-query` command accepts `--db`. An APFS clone of the
schema-25 snapshot answered one bounded evidence query with one hit and exit
status zero. The frozen snapshot hash was unchanged after that query. This
establishes a working isolated query path, not agent task completion. The
standard `hippo-mcp` server instead reads the database path from its per-home
config and opens writable SQLite connections. Give each treatment task its
own private clone and config; never point an agent at the frozen original or
the live production database.
An isolated `hippo-mcp` process also initialized through the standard stdio
protocol, exposed 12 tools, and returned one evidence hit from a private task
clone through `agent_query`. Its startup log named only the private database,
the frozen source hash remained unchanged, and the process exited after the
call. This interface smoke used the configured embedding and query models but
disabled Jev reranking and telemetry. The deployed configuration enables Jev
reranking, so full treatment parity is not established by this smoke.
With the deployed rerank setting and 1Password reference copied into a second
isolated home, MCP still returned an evidence hit, but logged `Jev unavailable
(CalledProcessError)` and fell back. The isolated home could not authenticate
the 1Password CLI. Before paired execution, preflight the actual reranker and
record whether Jev ran or the fallback ran for each task. A fallback-only
trial estimates that fallback configuration, not the intended Jev treatment.
A local shell-level packaging probe now places one development case in separate
control and treatment Git clones. Both have the captured pre-task branch and
commit, an offline remote, and identical task prompts; neither contains the
later fix commit or Git object alternates. The treatment has its own schema-25
database, installed Hippo wheel, locked Python dependencies, and isolated
config. A macOS sandbox probe read each assigned repository and denied reads
of the private answer key, original source tree, and sibling arm; it also
denied writes to the answer-key directory while allowing a trial-repository
write. Git needed `GIT_CONFIG_GLOBAL=/dev/null` because the host's global Git
config resolves into the denied original source tree. Under that same boundary,
the standard treatment MCP initialized, exposed 12 tools, and completed
`agent_query`. Jev again logged unavailable and used fallback. These are
shell and MCP probes, not an agent run. Separate Codex homes now configure zero
MCP servers for control and only this frozen Hippo server for treatment. The
same named Codex permission profile on both arms let `codex sandbox` read the
assigned task and denied the answer key, original source tree, and sibling
arm. The treatment profile also denied shell access to the raw database. The
isolated Codex homes now authenticate through symlinks to the host Codex
credential, without copying its bytes. `codex login status` succeeds in both
homes, while an actual file-open attempt on each symlink from its trial
sandbox raises `PermissionError`. Each top-level Codex CLI also reports logged
in under the outer macOS sandbox when invoked by its absolute binary path.
Those checks were separate: nesting the named Codex Seatbelt sandbox inside
the outer `sandbox-exec` profile failed with `sandbox_apply: Operation not
permitted` (exit 71). The combined boundary cannot launch an agent. The
candidate development launch uses the named permission profile alone; its
complete Codex process boundary has not been checked. The
[Codex permissions documentation](https://learn.chatgpt.com/docs/permissions)
states that profiles restrict local commands, while MCP servers use their
own process or transport and separate controls. The configured MCP server
must be limited to its private database and declared tools. Do not pass
`--sandbox` with the named profile because that selects the older sandbox
settings. No actual Codex agent process has run, and no pair can be counted
yet. The authentication
probe is recorded privately at
`~/.local/share/hippo-agent-trials/2026-09-29-gcampr-package/auth-boundary.private.json`.
The private hashes and probe results are recorded in the case's
`trial-package.private.json` outside this repository.
The 12 registered tools in [`mcp.py`](../../brain/src/hippo_brain/mcp.py)
accept retrieval filters and query text, with no general file-read or command
execution tool. Both treatment configs point storage and vector data at their
private trial directories. This static tool-surface check does not sandbox
the MCP process or prove the model cannot reach another capability.

A second development package freezes the dotfiles CI-removal task at commit
`555e16c7a9edd1c9ca38eb23d43baf4fbe755ca0`. Its control and treatment
clones have identical full prompts, separate offline remotes without the later
fix, and clean worktrees. Both reproduce the pre-task failing script. The
treatment MCP exposes 12 tools and completes `agent_query` against its private
schema-25 database. Separate named-profile probes denied answer-key, sibling,
credential, and raw-database reads. Jev again fell back after the 1Password
CLI failed. The private manifest is at
`~/.local/share/hippo-agent-trials/2026-09-29-ci-removal-package/manifest.private.json`.
Neither arm has executed the task, so this package supplies no benefit result.
The shared source snapshot has SHA-256
`b9eb5c74ca7ba78eb337b16ef519918bd251f93580e31838ee0d394d51b86ff6`
and its protocol record predates both requests. Its latest knowledge node was
created on September 22 at 06:15 UTC, before the CI request at 09:37 UTC and
the gcampr request on September 27. A row-multiset comparison found identical
captured knowledge, agent sessions, shell and browser events, source links,
FTS content, and vector index rows in both treatment databases. The private
manifests record counts and fingerprints for all 11 checked tables. This
rules out packaging or MCP smoke calls adding later task evidence to those
tables; it does not test what a full Codex process can access.
Both development packages now pin Codex CLI 0.158.0, `gpt-6-sol`, medium
reasoning, `approval_policy = "never"`, and the same named `trial` permission
profile in private per-arm configs. Their manifests record each config hash.
`codex login status` reports authenticated for all four homes.
A post-configuration `codex sandbox -P trial` shell probe passed in all four
arms: each read its own prompt and wrote its assigned repository, while
answer-key, sibling, and credential reads were denied. Treatment shell reads
of the raw private database were also denied. These checks do not exercise an
actual agent process or establish the MCP server's process boundary.
After 1Password was unlocked, host `op read` succeeded but the isolated MCP
home reported no configured account. A treatment-only `op` wrapper gave just
that subprocess the host home; it copied no secret into either trial. Both
isolated Hippo processes then initialized a Jev client, and a synthetic Jev
request returned HTTP 200. A real stdio `ask` call in the gcampr treatment
returned normally and recorded one Jev rerank pass with HTTP 200 and no
fallback. The model-shell profile denied the host `op` config and private
wrapper, while a direct sandboxed `op read` could not reach the desktop app.
The stdio preflight inherited the host `XDG_DATA_HOME`, so its private trace
was written under the host benchmark directory. Treatment MCP configs now set
private XDG directories. After an initial CLI timeout, a noninteractive
`op signin` restored credential reads. A new gcampr stdio `ask` completed with
one Jev pass, HTTP 200, and no fallback under the final private XDG config.
Its query packet and decision trace were written with mode 0600 inside the
treatment home; the model-shell profile denied reads of both, and no new host
benchmark trace appeared. No paired agent outcome exists.
The Gringotts, Agent Journal, and FirstMate packages now each have separate
Codex homes with the same model and named permission profile, plus a private
schema-25 treatment database copied from the frozen development memory.
Their treatment MCP processes use per-case HOME and XDG directories while
sharing the installed Hippo wheel environment and treatment-only `op` wrapper.
All three MCP servers initialized against their own database and listed 12
tools. Their initial Jev initialization timed out while the CLI account was
unsigned. Later full stdio `ask` preflights completed with Jev HTTP 200 and
no fallback in all three, as well as in the CI-removal and Sluice treatments.
FirstMate had one transient `JevUnavailable` fallback after a 1Password
timeout; its isolated retry completed with Jev HTTP 200 and no fallback. Each
treatment wrote a private query capture and decision trace. All six treatment
database hashes remained unchanged, and model-shell reads of the traces were
denied. These are MCP probes, not agent task outcomes.
Named-profile shell probes in all six new arms allowed own-prompt reads and
repository writes, while denying sibling-arm, other-package, answer-key, and
Codex credential reads. Treatment shell reads of raw databases were denied.
The two older packages' profiles were also extended and probed to deny the
other trial packages. All five cases remain without a paired agent run.
The five-case development pilot's case order and within-case arm order were
randomized and frozen before any agent outputs at
`~/.local/share/hippo-bench/agent-benefit/2026-09-29-five-case-pilot-schedule.private.json`
(SHA-256 `973766132ef86034f516fbb3117f1e1b11367da1f638f122a1510b0c34320403`).
The schedule records prompt, config, repository HEAD, and treatment database
hashes. It fixes a 30-minute wall limit per arm. Codex CLI 0.158.0 does not
expose a total-token-cap option in `codex exec --help`, so neither arm has a
token cap. Its `--json` trace reports usage in `turn.completed` events when
available, which supports the secondary token-use comparison
([OpenAI eval guidance](https://developers.openai.com/blog/eval-skills)).
This is a development schedule, not the acceptance cohort.
Adding the separately packaged Sluice control required a new cross-package
deny rule in each primary arm. A version-two schedule records only those
config-hash changes at
`~/.local/share/hippo-bench/agent-benefit/2026-09-29-five-case-pilot-schedule-v2.private.json`
(SHA-256 `c96c89373a3c8b3f5207af9c87e76c6488d280d0bdddec936589d2b0b2a5837b`).
Its case order, arm order, prompts, repository commits, database hashes, and
wall limit match version one. No agent run preceded this amendment.
A version-three schedule changes only treatment config hashes for private
query and decision capture with model-shell trace denials:
`~/.local/share/hippo-bench/agent-benefit/2026-09-29-five-case-pilot-schedule-v3.private.json`
(SHA-256 `242cd217986914571d9f57aff353c402df437f6deffd6fcb89dea2f953478db6`).
No agent run preceded this amendment. A complete Codex agent process boundary
and paired task outcomes remain unverified.
A version-four schedule changes only configuration hashes for an additional
cross-package deny rule protecting the FirstMate canary:
`~/.local/share/hippo-bench/agent-benefit/2026-09-29-five-case-pilot-schedule-v4.private.json`
(SHA-256 `037f674e36efe85a29b56caf8d8b964c2a075a7517ae133f205fb69859eb9818`).
Case order, arm order, prompts, repository commits, database hashes, and wall
limit match version three. No agent run preceded this amendment.
The private launcher at
`~/.local/share/hippo-bench/agent-benefit/launch_pilot.py` (SHA-256
`7759be6feaafa6f72cebf36929ceceba3e8e33ce02d87e630793a3cce42fab80`)
checks the version-four schedule, current Codex version, both arms' prompt and
config hashes, Git commit and clean state, local-only remotes, and the treatment
database hash. Its default command verifies all five cases and reported zero
agent runs. A deliberate prompt-hash mismatch was rejected even under
`python3 -O`. Execution requires its separate `--run` flag; it rechecks each
arm before starting, enforces the scheduled wall limit, saves private JSONL
traces, and disables Codex multi-agent mode. These dry checks do not establish
the complete agent process boundary or any task outcome.
With each arm's private `HOME` and `CODEX_HOME`, `codex mcp list --json` loaded
zero servers for all five controls and only `hippo` for all five treatments.
Listing configuration did not start an agent or exercise its shell sandbox.
The first authorized launch attempted all ten arms but failed before thread
creation. Their JSONL traces are empty. The CLI resolved through
`~/.local/bin/codex` into the sandbox-denied `~/.codex` tree, so its helper
could not execute while loading `AGENTS.md`. Codex also appended a
trusted-project stanza to each private config; removing that stanza reproduces
all ten version-four config hashes. The failed attempt has no task outcomes.
Version five records only those uniform config changes and the allowed
`/opt/homebrew/bin/codex` executable:
`~/.local/share/hippo-bench/agent-benefit/2026-09-29-five-case-pilot-schedule-v5.private.json`
(SHA-256 `fdcd4789b711483dccfef8f61a21ea238016edf269ea6b72fe77921632fabd1b`).
The revised private launcher hashes to
`ebe8764712e01d1c3aa1fad9c9008c45565820ee903555fa6d71e53008da8f07`.
The Homebrew CLI executed inside the first arm's named sandbox before retry.

The version-five development pilot completed all ten agent turns with exit
status zero, one terminal event each, and no timeout. Its private execution
report is
`~/.local/share/hippo-bench/agent-benefit/2026-09-29-five-case-pilot-execution.private.json`
(SHA-256 `c9e6d914a903ecc52ab1fd21e706be8956740a12a495f55dfc7602268929eaec`).
The traces record 600.516 control and 712.541 treatment wall seconds, and
2,314,033 control and 2,738,359 treatment input tokens. No treatment made a
Hippo MCP tool call. Configuration listing and a separate stdio preflight
establish configured access and server operation, but the execution logs do not
record the tools exposed to the model. They cannot distinguish missing runtime
tool exposure from a choice not to call Hippo. Historical-memory use and task
benefit remain unproven in these cases.

Executable checks found that both CI-removal arms removed exactly the obsolete
workflow step and script, and both Agent Journal arms preserved eight existing
workstreams while routing ten archived organization repositories distinctly.
An independent offline gcampr default-branch check reached a mocked PR-create
call in the control and refused before drafting in the treatment. Its private
checker SHA-256 is
`3143ea8d3ff3d818a75b1c30efa9f1bdac248441f9d9fd04d30b70e09bef163c`.
That subcheck alone is not the five-item gcampr success rubric. Firstmate,
gcampr, and Gringotts still need blind source-backed review. Separate A/B
packets for two independent reviewers have no arm-labeled paths; their private
manifest is
`~/.local/share/hippo-bench/agent-benefit/2026-09-29-pilot-blind-review/independent-review-manifest.private.json`
(SHA-256 `182f203241b02e50cc292605c70bb0bf36b66681cb56e6b15d42acc3699edc23`).
No reviewer labels exist. These five development cases are not the acceptance
cohort and cannot establish a population benefit verdict.
Rechecking all ten trace and stderr hashes reproduced the execution manifest.
The control gcampr stderr records an attempted test command rejected because
it contained file-deletion commands. Its completed turn does not establish
that this attempted validation ran. Treatment stderr contains no recorded MCP
startup error; silence does not establish model-visible tool exposure.

### Prospective intake

Before a task request is captured, freeze a reusable memory copy:

```sh
uv run --project brain python -m hippo_brain.bench.agent_benefit_intake \
  --db ~/.local/share/hippo/hippo.db \
  --repo /path/to/repo \
  --out /private/path/pre-task-memory
```

After the request arrives, save its complete source text in a private file and
freeze the task from that earlier memory copy:

```sh
uv run --project brain python -m hippo_brain.bench.agent_benefit_intake \
  --db /private/path/pre-task-memory/hippo.sqlite \
  --memory-manifest /private/path/pre-task-memory/manifest.json \
  --repo /path/to/task-repo \
  --task-start-ms 1790000000000 \
  --prompt-file /private/path/full-request.txt \
  --out /private/path/task-001
```

Use the actual captured task-start timestamp and repeat `--attachment` for
each referenced local file. The command hashes and copies the full request
and attachments, verifies the earlier memory hash, and only sets
`snapshot_ready: true` when memory predates the request, attachment modification
times do not follow it, and the repository is clean and stable. A live-memory
backup taken after the request stays unready. The manifest records the backup
interval, schema, checksum, integrity result, Git HEAD, and clean/dirty state.
The SQLite backup captures committed WAL data. A clean stable task worktree
gets a commit-only checkout with no remote; a dirty or changing worktree needs
separate exact-state reconstruction. Attachment modification times are a
screen, not proof of complete source provenance. Verify the source request,
external dependencies, and independent success check before either arm runs.
One live schema-25 memory copy for future prospective tasks finished at Unix
millisecond `1790657853239` and passed `PRAGMA quick_check`. Its private
manifest is at
`~/.local/share/hippo-bench/agent-benefit/prospective-memory-1790657852256/manifest.json`;
the SQLite SHA-256 is
`a50e6be7a82c791a03eea4d249ed8b545407470f16b7e7ebf6f7d80c873320bb`.
The current Hippo worktree was dirty, so this memory-only intake reports
`repo_ready: false` and `snapshot_ready: false`. It can be the memory source
only for eligible requests starting after its cutoff.
One real-size tooling smoke against an existing private schema-25 fixture
completed in 2.3 seconds, passed `PRAGMA quick_check`, and produced a clean
clone without the later fix commit. This smoke is not an acceptance task.
A later prospective memory freeze at
`~/.local/share/hippo-bench/agent-benefit/2026-09-29-prospective-clean-snapshot/manifest.json`
finished at Unix millisecond `1790666055105`. Its schema-25 SQLite copy
passes `PRAGMA quick_check` and hashes to
`4a3fca856492e0e9bdd2edaf9c4985c1dc68c00a028ab5b6c2f252723ac0931a`.
The commit-only Hippo clone is clean at `23dd19355b05726f4635517d76fbd4ba35986637`
and has no remote. The directory and files have private permissions. This is
a reusable pretask memory source for later requests; without a task start and
prompt, its `snapshot_ready` flag is correctly false. It is not a paired
outcome or acceptance sample.

## Score adjudicated pairs

`uv run --project brain python -m hippo_brain.bench.agent_benefit /private/path/pairs.json --planned-primary-n 100 --frame /private/path/frozen-frame.json`
prints the paired counts, gain, conservative interval, and statistical verdict.
Use the actual preregistered count, which may exceed 100. Without a frozen
frame or planned count, the verdict remains inconclusive. The scorer rejects
omitted or added tasks and a planned count that differs from the frame.
The input is a JSON array with one row per frozen task:

```json
[{"id":"incident-001","family":"incident-001","kind":"primary","control_success":false,"hippo_success":true}]
```

The frozen frame contains the same task identifiers without outcomes:

```json
[{"id":"incident-001","family":"incident-001","kind":"primary"}]
```

Use `kind: "control"` for the separate no-history and stale-history controls.
Record a failed or missing agent result as `false` after adjudication. The scorer
rejects omitted outcomes, duplicate IDs, and repeated primary families. It
cannot verify that the frame was frozen before outcomes, that all eligible
candidates were selected, that the environment was isolated, or that the
adjudication was blind; those require the acceptance record above. A
statistical verdict alone is not v1 release approval.

## Score prospective answer accuracy

`uv run --project brain python -m hippo_brain.bench.agent_answer_accuracy /private/path/adjudicated-answers.json --frame /private/path/frozen-call-frame.json`
reports the two exact one-sided bounds and the answer-quality verdict. The
ordered frozen frame contains `id`, `family`, and `kind` (`natural` or
`absent_control`) for each call. Its natural entries must be the 300 consecutive
qualifying calls, in capture order; the scorer uses the first call per family.
Adjudicated rows repeat those fields and add two distinct reviewer names and
`response_status` (`answer`, `abstention`, `timeout`, or `tool_error`). Natural
rows add `answerable`; answerable rows add `correct`, `fully_supported`, and
`superseded`. Absent controls add `unsupported_claim`. All these judgments are
booleans. The scorer rejects missing or added rows, dependent controls,
missing judgments, and absent reviewer identities. It stays inconclusive
without exactly 300 natural calls, at least 100 answerable primary families,
and exactly 100 independent absent controls.

The frame hash, source audit, human reviewer independence and blindness, and
proof that the capture window was consecutive remain external acceptance
evidence. Reviewer names and matching row counts alone do not prove them. No
prospective answer labels have been collected.
