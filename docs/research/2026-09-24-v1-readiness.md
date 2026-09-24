# Hippo v1.0.0 readiness assessment

**Decision: NO-GO for v1.0.0 under the requested empirical standard.** Hippo demonstrates working local capture, inspectable historical recall, and substantial regression coverage. The evidence does not establish representative answer accuracy, reliable abstention, or improved agent task outcomes. The paired next-action pilot measured no improvement. Fixing the reproduced software defects does not close those evidence gaps.

Assessment date: September 24, 2026. Source baseline: [`317273a6de8b84767447bd6b4a93887a2c505fba`](https://github.com/stevencarpenter/hippo/commit/317273a6de8b84767447bd6b4a93887a2c505fba), five commits above the published v0.35.0/main baseline [`e9c2a2500bca476b839631ae846ef50254c138d8`](https://github.com/stevencarpenter/hippo/commit/e9c2a2500bca476b839631ae846ef50254c138d8). Rust and Python versions remain **0.35.0**. The live database schema is **25**. Corrective source changes are not deployed. No v1 tag or release has been created.

## Release gates

| Gate | Current evidence | Decision |
|---|---|---|
| Accurate, grounded answers and correct abstention | April labels recovered in 10/28 answerable top-10 pools; current pooled review has zero human grades; adversarial trust cases remain pending. | Not met. A frozen, independently judged representative evaluation must measure correct answers, unsupported claims, and correct abstentions. |
| Better agent decisions | Both pilot arms chose the expected action in 12/12 cases. No wins or losses from memory. Conflict detectors still make semantic errors. | Not met. The requested improvement needs paired task outcomes with independent success checks and cases that avoid the observed baseline ceiling. |
| Supported capture and operational behavior | Current shell/session capture and storage integrity are observed. Probe traffic confounds other source counters; monitoring history cannot establish continuous availability. | Not met for an unrestricted capture/reliability claim. Supported producers and candidate installation need direct end-to-end evidence, with activity separated from probes. |
| Privacy and security within a stated deployment boundary | Reproduced browser-origin and filesystem defects have source fixes. Core dependency scans passed; optional containers have untriaged findings. | Conditional for a trusted single-user workstation. See [Validation record](#validation-record) for candidate verification. No approval for untrusted local users or the entire optional stack. |
| Tested, installable, publishable final candidate | Published v0.35.0 artifacts install in isolation. Corrective changes pass the full local suite, including upgrade rollback checks. The release workflow can publish despite failed CI. | Final release-SHA Linux/macOS CI remains required. Passing these checks alone does not satisfy the first two gates. |

No new numeric accuracy or utility threshold was chosen after seeing these results. An acceptance evaluation must declare its workload, tolerated failure types, adjudication policy, and decision rule before inspecting the candidate's outcomes. Existing benchmark thresholds apply to their documented benchmark contracts, not automatically to whole-product readiness.

## Council judgments and product strengths

The council used separate retrieval, capture, security, decision-utility, release, and operational evidence reviews. A separate reviewer challenged the combined fixes and reproduced an additional conflict-reporting regression. These are expert judgments about different contracts, not votes to average into a score.

| Dimension | Established strength | Constraint on the claim | Judgment |
|---|---|---|---|
| Retrieval and accuracy | Hybrid SQLite/FTS5/vector retrieval returns source identities, excerpts, timestamps, and freshness metadata. | Ranking is not answerability; representative correctness and abstention remain unmeasured. | Withhold v1 acceptance. |
| Capture and persistence | Transactional event/queue insertion, replay deduplication, fallback recovery, and an independent watchdog. | Capture is best effort before persistence; live browser/tool coverage was not established. | Conditional NO-GO. |
| Agent decision utility | Deterministic agent-query composition supplies evidence without another synthesis call; auto-memory has explicit revisions. | No action-selection gain in the pilot; conflict and lesson quality remain limited. | NO-GO under the empirical qualification requested. |
| Security and privacy | Loopback HTTP, stdio MCP, parameterized filters, configured external inference, and redaction boundaries are inspectable. | Local clients are trusted; browser content can retain secrets; optional containers are not cleared. | Conditional GO for the core on a trusted single-user host after validation. |
| Release engineering | Published arm64 artifacts, checksums, signatures, locked Python installation, and established CI. | Publication is not coupled to passing final-SHA CI; isolated candidate checks are recorded in [Validation record](#validation-record). | Hold v1 publication. |
| Operations and evidence quality | Store consistency, recent source links, queue states, and health metrics can be inspected directly. | Several metrics conflate distinct states; availability and RAG observations are incomplete. | Insufficient evidence for an operational readiness claim. |

The supported product demonstrated here is a macOS activity recorder and historical evidence retrieval service. Current-state truth, general learning from past mistakes, and autonomous resolution of contradictory history are stronger capabilities than these measurements establish. A bounded historical-recall v1 could have a narrower contract, but that would not satisfy the requested demonstration of improved agentic decisions.

## 1. Capture, persistence, and provenance

At the sampled live snapshot, the database contained **30,022 knowledge nodes**, with matching vector and FTS row counts. `PRAGMA quick_check` returned `ok`; the foreign-key check returned zero violations. These establish structural consistency at that snapshot, not semantic accuracy or complete ingestion.

| Observation | Denominator and result | Interpretation |
|---|---|---|
| Recent shell source links | 138/139 sampled preceding-day non-probe events linked to knowledge | Strong recent linkage; one event was unlinked at sampling. This is not a lifetime capture-recall estimate. |
| Recent agentic source links | 42/42 sampled events linked | The sampled recent agentic events reached knowledge. |
| Claude file offsets | 167/167 currently present session JSONL files had stored offsets at least as large as their file sizes | No current file showed missing offsets or unprocessed growth. Historical offset records numbered 10,272. |
| Source-less eligible nodes | 4,352/30,022 nodes, 14.5% | A material set can be eligible without a direct source link. Matching storage counts do not imply complete provenance. |
| Auto-memory documents | 0 in the inspected live store | The implemented revision contract has test evidence, but this deployment provides no populated live adoption sample. |
| Browser activity | 0 non-probe events in the preceding 24 hours; last non-probe event approximately 19.4 days old | No live browser producer coverage was demonstrated. |
| Claude-tool activity | 0 non-probe events in the preceding 24 hours; last non-probe event approximately 151 days old | No live Claude-tool producer coverage was demonstrated. |

The last two sources each reported 105 preceding-day events in `source_health`, while their non-probe tables reported zero. Synthetic probes advanced counts and timestamps. A green health row therefore proves neither actual user activity nor complete producer coverage. There is no evidence that browser or tool activity occurred and was lost.

The durable boundary is well designed. [`storage.rs`](../../crates/hippo-core/src/storage.rs) inserts shell/browser events and enrichment-queue entries in one transaction. Unique envelope IDs prevent replay from inserting duplicate events or queue work. Probe rows are excluded from enrichment. Fallback recovery separates recoverer and appender locks, claims files by rename, resumes interrupted recovery, and retains artifacts after partial failure. WAL mode, foreign keys, and busy timeout are configured at the shared database boundary.

Before that boundary, [`daemon.rs`](../../crates/hippo-daemon/src/daemon.rs) deliberately uses fire-and-forget socket ingest and a bounded in-memory buffer. The default capacity is 200 events. Existing tests characterize crash-before-flush loss and drop-at-capacity. This is an explicit best-effort capture contract, not durable acknowledgment of every event.

Three source-level concerns constrain stronger reliability claims without establishing observed data loss:

1. [`watch_claude_sessions.rs`](../../crates/hippo-daemon/src/watch_claude_sessions.rs) scans before subscribing, drops notifications when its 256-event channel is full, and suppresses events during cooldown. Its settling sweep revisits already-ingested rows rather than reconciling every file. A final write can await another notification or restart. Current file-offset measurements were clean.
2. A timed-out blocking watcher task continues after its join handle is dropped. Idempotent inserts reduce duplicate-record risk, but the timeout does not enforce a hard bound on active parsing/database work. No runaway workload was observed.
3. Synchronous SQLite/filesystem operations inside async handlers can block during contention. Existing contention tests pass; this review did not reproduce executor starvation or sustained-load data loss.

## 2. Retrieval, answer accuracy, and evaluation validity

### Current live retrieval baseline

The council ran all 40 existing questions using the configured embedding model against a read-only SQLite transaction. Synthesis and model judging were disabled. The run began at the source baseline, before the score-contract correction.

| Measurement | Result |
|---|---:|
| Questions with labeled answers / original coverage gaps | 28 / 12 |
| Distinct expected UUIDs still in the live store | 69 / 70 |
| Questions recovering any expected UUID in the top 10 | 10 / 28 (35.7%) |
| Mean recall@10 over labeled questions | 0.1679 |
| Mean reciprocal rank | 0.1320 |
| Mean nDCG@10 | 0.1071 |
| Original gap questions returning nonempty results | 12 / 12 |
| Retrieval errors | 0 / 40 |
| Median / p95 retrieval latency | 482 / 660 ms |
| Total evaluation time | 20.3 seconds |

The labels date to April and are incomplete. These scores measure recovery of those UUIDs. They do not measure all relevant evidence in the larger September corpus. The original gap questions may now be answerable, so their 12 nonempty result sets are not a measured 100% abstention failure. Because 69 of 70 expected UUIDs still exist, missing rows cannot explain all the weak recovery. Updated independent judgments are needed to separate ranking failures from obsolete labels.

### A reproduced false-confidence defect

One adversarial regression supplied an unrelated terminal-font node, no lexical matches, cosine distance 2.0, and `min_score=0.99`. Semantic retrieval returned no result. Hybrid retrieval normalized its best result to 1.0 and reported high confidence 0.7825 and zero coverage gap. The node's rank within its pool was being interpreted as absolute relevance.

The source correction preserves hybrid ranking and adds explicit `score_semantics`. Relative ranks no longer contribute relevance confidence or pass an absolute-relevance threshold; their nonempty coverage estimate is undefined. Confidence is capped below the existing high boundary and declares `relevance_calibrated=false`. RAG text labels rank scores and exposes the confidence explanation and node UUID. This removes a verified misleading contract. It does not calibrate correctness or implement answerability detection. See [`retrieval.py`](../../brain/src/hippo_brain/retrieval.py), [`confidence_scoring.py`](../../brain/src/hippo_brain/confidence_scoring.py), and the [evaluation reference](../eval-harness-design.md).

[`rag.py`](../../brain/src/hippo_brain/rag.py) deterministically abstains when retrieval is empty, then attempts synthesis for every nonempty pool. Relative-rank normalization cannot prove that such a pool supports an answer.

### What the existing benchmarks establish

| Evidence | Measured result | Permitted claim |
|---|---|---|
| September 22 inspected known-target comparison | Hit@5: retrieval 26/50, Jev 32/50, cached local reranker 33/50; designated target present in only 33/50 pools | Reranking improved recovery within known candidate pools. The report explicitly excludes release acceptance. |
| Current pooled retrieval review | 1,545 rows; zero grades/human labels | A review corpus exists. Judged acceptance evidence is absent. |
| Assistant review of 100 questions | 80 complete, 17 partial, 3 insufficient; zero added human labels | Diagnostic review, not independent human adjudication. |
| August 13 historical hybrid benchmark | 100 questions; hit@10 0.84, MRR 0.6397 | Historical performance on that corpus/configuration, not current production accuracy. |
| Trust fixture | 14 cases: 9 active, 5 pending; both adversarial cases pending | Regression infrastructure exists. Negative-answer acceptance is not enforced. |

The older 100-question corpus includes questions authored from captured material and 99 golden events grafted into its 299-event corpus. This documented construction supports targeted recall testing. It does not represent naturally sampled production work.

A final enrichment-fidelity check confirmed one derived factual error. A freshly captured node from this audit (`ab80a801-9c94-434e-8e5b-19bdb565b761`) retained the withdrawn website-installer hypothesis and claimed a repair to `site/integrations/copy-public-assets.ts`. The linked source explicitly corrected that hypothesis, and the file's HEAD and worktree blobs were identical. The actual installer edit addressed upgrade rollback. This demonstrates that available corrective source context can be lost in enrichment while a nonexistent action is asserted. It is one selected self-captured example, excluded from the pilot and holdout evidence; it establishes no population error rate. The private archive contains the source comparison and Git proof as `enrichment-fidelity-diagnostic.md`.

The newer [`knowledge_corpus.py`](../../brain/src/hippo_brain/bench/knowledge_corpus.py) harness protects frozen input hashes, task-family splits, temporal boundaries, human review, complete pooled judgments, and cluster bootstrap comparisons. It prohibits holdout tuning and limits claims to fully judged pooled unions. This is a substantial strength. Infrastructure and unfilled acceptance data remain separate facts.

The older [`evaluation.py`](../../brain/src/hippo_brain/evaluation.py) harness also has material diagnostic limits. Ranking metrics use one retrieval while synthesis retrieves again and may rerank. The judge sees truncated source summaries rather than all the answer model's context and uses the same query model. Keyword presence is not factual verification. Its CLI has no quality-failure gate. [`trust_eval.py`](../../brain/src/hippo_brain/trust_eval.py) skips pending cases, permits a pending negative case to satisfy coverage, and its CLI validates corpus shape. These tools should not be treated as already-passing release acceptance.

## 3. Agent decisions, conflicts, and lessons

### Paired next-action pilot

Two fresh agent contexts received the same authored tasks and response schema. One received no memory; one received frozen Hippo evidence. Labels were independently adjudicated before treatment retrieval. The cases comprised eight historical incident choices and four conflict, stale-evidence, scope, and injection controls.

| Outcome | No memory | Hippo memory |
|---|---:|---:|
| Historical incident choices | 8 / 8 correct | 8 / 8 correct |
| Controls | 4 / 4 correct | 4 / 4 correct |
| All cases | 12 / 12 correct | 12 / 12 correct |
| Paired wins / losses from memory | n/a | 0 / 0; both correct in 12 / 12 |

**The pilot measured no action-selection gain.** The perfect baseline creates a ceiling. It is not evidence that Hippo cannot help harder tasks, and the perfect treatment is not evidence of general reliability. Mean self-reported confidence on the eight incidents rose from 0.7225 to 0.96375 without a change in correctness. That increase does not establish calibrated confidence.

Queries used the complete task prompt without answer options, one frozen lexical pass, top five, and the existing `project=hippo` substring filter. A pre-September-24 cutoff covered node creation, linked session end times, result capture times, and every evidence packet timestamp. No query was tuned after retrieval. Every query returned five hits. A designated historical source appeared in 8/12 tasks and 6/8 incidents; alternate valid supporting material existed for some designated-source misses. The conflict control missed its designated correction source but recovered alternate correction evidence.

The original draft's implausible distractors and label-informed query terms were corrected before retrieval or model outcomes. One transport deviation occurred: the memory arm's initial input read was truncated, so the same blinded agent resumed in chunks without labels or outcome feedback. Only its final 12 responses were graded.

The pilot is small, authored, and partly dependent because controls reuse incident sources. Historical labels came from captured session text containing reports and tool activity, not independent execution traces proving each historical fix succeeded. Both the task and common instructions warned that the injection control's content was untrusted. Passing it therefore does not establish resistance to an unannounced production injection. This pilot measures choice selection, not executed repair success, elapsed time, tool-call savings, or repeated-mistake prevention.

### Conflict detection remains a warning heuristic

| Diagnostic | Baseline | Interpretation |
|---|---:|---|
| Decision-conflict detector | 12/22 unique cases correct; 7 false conflicts, 3 missed conflicts | Two orders produced 24/44 decisions, not 44 independent cases. |
| Outcome-conflict detector | 14/22 unique cases correct; 7 false conflicts, 1 missed conflict | Two orders produced 28/44 decisions. Explicit recovery can be labeled a conflict. |
| Offline lexical-rule ranking | 14/28 scored decisions correct; nDCG 0.82593 | 15 unique cases, one all-zero case excluded, two dependent orders. |
| Offline verification rules | 4/68 correct decisions, 64 abstentions, zero false supports | Exact-text/empty-source controls establish narrow safe behavior, not broad semantic verification. |
| Historical conflict replay | 528/528 saved responses validated | Artifact integrity passed. Historical outcome judgments still missed genuine conflicts. |

The audit fixed a default-mode omission in [`agent_query.py`](../../brain/src/hippo_brain/agent_query.py): design decisions are now retained during conflict analysis in all modes, then omitted from compact responses where appropriate. It also synchronized capped confidence levels, numeric scores, and explanations.

Independent review then showed that exposing all decisions to the existing detector made unrelated choices appear contradictory in ordinary queries. The follow-up in [`conflict_detection.py`](../../brain/src/hippo_brain/conflict_detection.py) limits comparisons to distinct nodes with matching project/branch context and opposed explicit alternatives. Its same-corpus diagnostic rose to **13/22 correct**, with **1 false conflict and 8 missed conflicts**. Outcome detection was unchanged. This trades fewer false warnings for more missed contradictions. The diagnostic is not an independent holdout, and an absent warning is not evidence of consistency.

Source capture freshness is also not factual freshness. A healthy collector can return an old decision whose validity has changed. Ordinary knowledge nodes do not have a general supersession/validity model. The July consolidation documents are specifications; the reviewed runtime does not implement their proposed synthesis sources, project digests, entity profiles, or salience. Those features cannot be counted as delivered capabilities.

### Lessons are unevenly actionable

Seventeen of 29 live lessons had blank tool/rule identity, a path-only summary, and no fix hint. [`workflow_enrichment.py`](../../brain/src/hippo_brain/workflow_enrichment.py) forms lesson summaries from the cluster key rather than retaining the diagnostic and corrective context. Hundreds of occurrences do not make that output useful guidance.

Capture-alarm lessons are stronger. [`capture_alarm_lessons.py`](../../brain/src/hippo_brain/capture_alarm_lessons.py) reports auto-resolution fraction and median duration, with live examples at 97% or 98% auto-resolution. [Issue #264](https://github.com/stevencarpenter/hippo/issues/264) remains open, but its requested distinction is implemented. It is not evidence that the old unqualified alarm summaries remain current.

Auto-memory's explicit document/revision identities, transactional projection updates, separate history surface, and last-known-good behavior are useful design strengths. They protect revisions from stale publication. They do not generalize automatically to every ordinary knowledge node, and the inspected live deployment had no memory documents.

## 4. Operations and measurement integrity

| Observation | Result | Consequence |
|---|---|---|
| Classification state | 3,063 ready; 26,155 pending; 803 skipped; 1 failed | The pending count includes recent backfill. Oldest pending age was approximately 2.5 hours, so count alone does not prove a stalled worker. |
| Available exporter scrapes | 2,032 / 5,760 expected in 24 hours, 35.3% | Observed scrape availability only. Sleep, collection interruptions, and service downtime were not separated. This is not a measured application uptime percentage. |
| RAG telemetry | Zero observations in seven days despite observed MCP calls | The telemetry does not establish query volume or service quality. Zero observations cannot be interpreted as zero use. |
| Recall probe | Disabled | No continuous recall-quality evidence from that probe. |
| Inference latency | Mean 13.7 seconds; reported p95 reached the 10-second histogram ceiling | The histogram cannot resolve the upper tail. The displayed p95 must not be treated as a precise tail-latency estimate. |
| Alarm states | 4,932 total; 146 unacknowledged; 99 unresolved; 0 both | Exporter active=99 conflicts with the watchdog's active definition. These are not 99 unattended active violations. |
| Fallback directory | 27 entries: 24 completed archives, 2 locks, 1 old partial; 0 pending recovery files | The raw entry-count gauge is not a recovery backlog. The partial artifact was approximately 178 days old. |

The source of the alarm discrepancy is inspectable in [`watchdog.rs`](../../crates/hippo-daemon/src/watchdog.rs): active requires both unacknowledged and unresolved. The daemon's fallback gauge counts directory entries, while the canonical recovery selector accepts only pending/recovering files. These measurement defects matter because a release decision based on the unlabeled numbers would reach the wrong conclusion.

The benchmark store also contained test contamination: 142 runs included 140 model rows named `m1` and 28 named `m2` from tests. Model rows are not run counts and must not be summed as separate runs. The identified test fixtures wrote through the default results location. They now set temporary `XDG_DATA_HOME`, with 23 focused tests passing and an assertion that the expected run lands in the temporary database. Historical records were preserved. See [`test_bench_e2e.py`](../../brain/tests/test_bench_e2e.py) and [`test_bench_orchestrate.py`](../../brain/tests/test_bench_orchestrate.py). The August benchmark remains historical evidence; generic test-model rows are not model evaluations.

## 5. Security and privacy

The defensible deployment boundary is a **trusted single-user workstation**. The brain binds loopback, but loopback TCP does not authenticate an OS account. Synthetic unauthenticated requests could read seeded knowledge and invoke control handlers. A local process can still do so after Host/Origin and filesystem fixes. MCP uses stdio and trusts its launching client; it has no per-consumer project/source authorization. [README privacy documentation](../../README.md#privacy-and-security) states this boundary.

Two reproduced defects have source corrections:

1. Browser-origin requests with foreign Host/Origin values reached read/control handlers. [`server.py`](../../brain/src/hippo_brain/server.py) now requires an exact loopback Host and either no Origin or one same-origin Origin, before dispatch. Tests reject duplicate/malformed headers, foreign/null origins, and scheme/port mismatches while preserving CLI requests and IPv6 loopback. This is browser-origin protection, not native-client authentication. No real-browser DNS-rebinding exploit was executed.
2. The deployment used data directory mode 0755 and database mode 0644 under a home directory traversable by permitted group members. New Rust/Python startup boundaries secure the dedicated data directory to 0700; Rust secures database/fallback files and sockets to 0600. Descriptor ownership/final-symlink checks and broad-directory rejection prevent changing unrelated parent directories. Tests cover new and existing files, sidecars, snapshots, owner mismatch, and symlink rejection. Live permissions were not changed during the assessment. See [`storage.rs`](../../crates/hippo-core/src/storage.rs) and [`__init__.py`](../../brain/src/hippo_brain/__init__.py).

SQLite remains unencrypted. Shell/session redaction reduces known-format secrets, but browser titles and extracted content deliberately bypass that engine. Improved patterns do not scrub existing records. Captured secrets can therefore persist or reach configured general inference. Jev is an explicit external option with recursive redaction, a request-size ceiling, timeouts, bounded concurrency, a fixed HTTPS endpoint, strict responses, and disabled redirects/environment proxy trust. Deployed configuration used local general inference with Jev classification/reranking enabled. This is not an unconditional no-egress product. See [`jev.py`](../../brain/src/hippo_brain/jev.py).

Retrieved content remains untrusted. RAG grounding instructions are not an enforced evidence/instruction boundary. The review did not demonstrate a model compromise, and the warned injection pilot does not establish general resistance. Agents consuming MCP evidence must preserve their normal action authorization boundaries.

Dependency and baseline secret scans ran on September 24, 2026 against the stated source baseline. Their results describe the scanned versions and advisory databases on that date. They do not establish an absence of unknown vulnerabilities or cover later edits.

| Scan scope | Finding | Qualified conclusion |
|---|---|---|
| Cargo: 318 dependencies | No reported advisories | Clean against the scanned advisory database. |
| Python lock: 69 packages | No reported advisories | Lockfile scan passed. |
| Site lock: 553 packages | No reported advisories | Lockfile scan passed. |
| Canonical extension npm lock | Two `image-size` 2.0.2 denial-of-service advisories | Developer lint path through `web-ext`/`addons-linter`; no runtime extension import. Locally adjusted severity is low. |
| Unused extension Bun lock | 36 package/advisory pairs across 10 packages | Stale alternate dependency graph; canonical install uses npm. |
| Optional Grafana arm64 image | 268 distinct package/version/advisory tuples, 182 advisory IDs | Package presence established; most vulnerable paths untriaged. Four other optional images were not scanned. |
| Secret scan at baseline | Zero matches in `origin/main..HEAD`; 21 synthetic fixture/documentation matches across 680 tracked files | No live credential found within this scope. Full history and later edits are not covered by that scan. |

The extension findings are [GHSA-5p2g-fcmc-qvqq](https://github.com/advisories/GHSA-5p2g-fcmc-qvqq) and [GHSA-w3rx-r6r6-pgpr](https://github.com/advisories/GHSA-w3rx-r6r6-pgpr). Registry dependency inspection identified `web-ext` 10.7.0 resolving `image-size` 2.0.4. Extension dependencies were not upgraded in this audit.

Grafana's loopback publication and required authentication constrain exposure but do not prove vulnerable functions unreachable. One critical OpenSSL advisory requires 32-bit execution and is inapplicable to the inspected arm64 image; other critical entries remain untriaged. Counts were deduplicated across repeated binaries. Neither a verified critical Hippo exploit nor an unconditional stack-wide security approval follows from the scan.

Mutable build action/image tags, broad build-job release permissions, and an unpinned build tool/backend remain supply-chain concerns. Python CI disables `pip-audit`, and no tracked Dependabot configuration was found; the successful lockfile-native OSV scan supplies evidence for this assessment, not a persistent CI gate.

## 6. Packaging, validation, and release control

Both [published v0.35.0 artifacts](https://github.com/stevencarpenter/hippo/releases/tag/v0.35.0) passed SHA-256 verification. The daemon identified as 0.35.0, Mach-O arm64, and passed `codesign --verify`. The actual brain tarball installed into an isolated directory using Python 3.14.7 and 67 locked dependencies. Its relocated entrypoint, server import, and version lookup worked. These checks establish published-package viability without starting production services.

The installer previously deleted the usable brain before dependency sync/import checks. A failing dependency operation reproduced loss of the old environment. [`install.sh`](../../scripts/install.sh) now stages a locked, non-editable, relocatable environment and verifies it before promotion. Rollback preserves the prior daemon, brain, and receipts across the tested failure paths. [`test-install-brain-upgrade.sh`](../../tests/shell/test-install-brain-upgrade.sh) covers dependency failure, staged and relocated import failures, receipt failure, post-commit backup-cleanup failure, and successful upgrade. A further review caught a mixed-version path when backup cleanup failed after promotion; committing the transaction before cleanup prevents a daemon-only rollback. It does not prove crash-atomic replacement across components or concurrent-installer safety.

The published release also exposes a process defect. Main's [Python CI run 35837006589](https://github.com/stevencarpenter/hippo/actions/runs/35837006589) failed with 1,665 passes and one expected failure, while the [release run at the same commit](https://github.com/stevencarpenter/hippo/actions/runs/35837041107) succeeded. The failed retrieval-eligibility test used a module-import timestamp against a moving 90-second settle window. A 179.4-second suite could age the fixture into eligibility. Freezing the test's runtime clock corrected the test; this finding did not establish a production retrieval regression.

The [release workflow](../../.github/workflows/release.yml) publishes matching tags without checking tests, main ancestry, manifest/tag agreement, or CI conclusions. The main ruleset requires a PR and linear history but has no required-status-check rule and requires zero approvals. Final merged-SHA Linux and macOS CI therefore require explicit verification before any tag. PR-only checks cover Linux.

Release binaries are built with `--no-default-features`, excluding the daemon OTel exporter present in source installs. A v1 telemetry claim must match the actual artifact. The installer treats service/doctor failures as warnings and can exit zero, so exit status alone does not establish a working installation. Version-pinned installer URLs still select latest internally; they are not a supported downgrade mechanism. Generated release notes also omit a behavioral change list and compatibility contract.

### Corrective changes versus deployed behavior

| Reproduced problem | Source correction | Evidence status |
|---|---|---|
| Relative rank presented as relevance | Score semantics, confidence/coverage correction, explicit text rendering | Retrieval/RAG/MCP focused suite: 287 passed. Ranking order preserved. |
| Ordinary query modes omitted decisions; confidence fields disagreed | Analyze decisions before response compaction; synchronize confidence caps | Initial focused tests passed; scoped conflict follow-up has 24 passing tests. |
| Follow-up exposed unrelated decisions as contradictions | Require matching context and opposed explicit alternatives | Independent-review reproducer addressed; detector remains semantically limited. |
| Browser-origin requests reached local handlers | Exact Host/same-origin guard | Security subset: 169 passed, 1 existing expected failure. |
| Permissive capture file creation | Dedicated private directory and file/socket modes | Rust targeted checks and Python startup checks passed; final startup subset: 13 passed. |
| Idle socket client blocked shutdown | Cancel idle reads while allowing buffered/in-flight frame completion; bound incomplete-frame drain | Included in final validation below. The earlier one-second global abort approach was replaced. |
| Failed upgrade removed usable components | Stage, verify, promote, and restore old components/receipts on tested errors | Canonical installer failure/success checks passed. |
| Time-sensitive eligibility test | Freeze its runtime clock | 7 focused tests passed. |
| Tests polluted benchmark history | Temporary XDG results directory | 23 focused tests passed; old records preserved. |

These are source fixes. They are not claims that the running deployment has changed.

### Validation record

The baseline `mise run test` passed **1,670 Python tests**, with **1 expected failure** and **85% coverage**. Rust passed **757 unique tests**; the workflow performed **1,146 executions**, including **389 repeated `hippo_daemon` library tests**. Both `cargo test -p hippo-daemon --lib` and `cargo test -p hippo-daemon --tests` execute that library suite. The repeated executions are not additional unique coverage.

An integrated run after the initial fixes passed **1,711 Python tests**, with **1 expected failure** and **85% coverage**. CodeRabbit's first review found three issues: a startup test bypassed the private-directory boundary, shutdown-test timeout cleanup could itself hang, and the one-second connection deadline could cut off a slow in-flight frame. Those fixes were applied. The second CodeRabbit review completed with **zero findings**. Independent review also prompted the scoped conflict correction.

The local `mise run test` for candidate `c4ac94f` passed **1,716 Python tests**, with **1 expected failure** and **85% coverage**. Rust passed **760 unique tests** across **1,151 executions**, including **391 repeated daemon-library tests**. Clippy, Ruff, formatting, and canonical installer checks passed. The intervening integration run exposed two sidecar expectations for the old false-conflict behavior; their labels and corpus were preserved while expectations were corrected, and all 61 related tests passed before the full rerun. The latest completed diff secret scan found zero leaks within its scope. Subsequent review corrected the installer cleanup path and a dashboard description that incorrectly inferred worker liveness from a flat queue count. Targeted follow-up on `f85aefc` passed all six installer regression scenarios, including post-commit cleanup failure. The candidate installer's component transaction also completed a fresh installation and forced replacement of the published v0.35.0 daemon and brain under synthetic HOME/XDG paths with real uv and dependencies. Both installed versions, relocated brain imports and CLI entrypoint, daemon checksum, and both artifact receipts passed checks. Downloads alone were redirected to the existing published artifacts; service installation was not invoked. This verifies same-version replacement, not a cross-version migration or deployment of candidate source. The first evidence-driver attempt failed because macOS `head` rejects `-n -1`; correcting the driver resolved that setup failure without a product change.

The completed continuation on candidate `7436399` passed canonical Rust/brain builds, focused regressions, and **10/10 named live software-validation scenarios**: native/same-origin HTTP access with foreign Host/Origin rejection; empty-store queries; seeded conflicts in all four query modes; private daemon storage/socket permissions; rejection of HOME and symlink storage targets without permission changes; completion of a delayed frame; bounded incomplete-frame drain; classification export and overwrite refusal; real package replacement with ordinary failures; and Grafana rendering. The incomplete-frame process exited successfully after 5.02 seconds. The export exercised an empty classification table, not populated export accuracy. Actual archive corruption restored prior component state, and a genuine backup-directory permission failure left committed components and receipts consistent. These checks supplement, rather than replace, the earlier controlled failure scenarios.

Disposable Grafana 12.4.2 rendered all six decision panels using read-only existing Prometheus metrics. Five panels displayed metrics; classification backlog displayed **No data**. Desktop and narrow screenshots verified the corrected queue-population tooltip. This supersedes the earlier isolated datasource-resolution failure but does not demonstrate a populated candidate backlog gauge. The continuation records removal of the successful fixture and closure of the browser session. The earlier isolated fixture, `hippo-v1-gate-01m39-grafana`, remained until the user approved its cleanup on 2026-09-24. It was then stopped and removed, and Docker confirmed its absence. No production services, data, configuration, or permissions were changed.

The verified report is `/tmp/hippo-v1-audit-20260924/verified-live-scenarios.json`. Its seven private artifacts remain under `/Users/carpenter/.no-mistakes/evidence/01M39JNBRPT4ZGDTF6FT6B4VR5/`: `real-upgrade-current.log`, `current/real-upgrade-adversarial.log`, `current/live-results.json`, `current/recall-results.json`, `current/private-paths.json`, `grafana-current-tooltip.png`, and `grafana-current-narrow.png`. No full suite or lint checks were rerun in that test continuation. These are local corrective software-validation results, not final release-SHA CI, production deployment verification, or empirical product certification. The empirical v1 decision remains **NO-GO**. The user subsequently approved replacing the inherited source-text metric tests. Their replacement collects Python and Rust OpenTelemetry measurements at runtime and verifies the shared metric-name/type contract against parsed dashboard and alert references. Decision counters, token counts, duration sums, and database-backed queue gauges have value assertions. Histogram components must correspond to collected histograms, and unsupported units fail the check. The parser-specific production comment and implementation-source parsers were removed. This establishes metric-emission contracts, not correctness of every application workflow that records those metrics. The focused follow-up passed 72 Python tests and the new Rust runtime contract test, with Python/Rust lint and formatting checks passing. Five mutations of collected Python metric data were rejected. Two isolated Rust source mutations, a counter rename and a health gauge changed to unit `1`, also failed the contract as expected while the unmodified control passed. Follow-up evidence is archived under the private audit directory in `metric-followup/`.

At candidate `c8b98e89e664c6943e80c548752d662b7e238b44`, the test continuation executed the Python collector as a subprocess with the installed OpenTelemetry SDK, production instrument handles, `record_decision_metrics`, and `create_app` callbacks over isolated seeded SQLite. It collected 31 instruments; inventory and dashboard/alert validation passed, and a renamed instrument was rejected. Decision values and all eight duration sums matched the candidate assertions, including populated classification and enrichment gauges. The selector validator accepted harmless descriptions and label values while rejecting actual `service_namespace` matchers. All 15 focused checks passed. Commands, outputs, collected metrics, and the three passing scenario records are archived under `/Users/carpenter/.no-mistakes/evidence/01M3AAF9X77AESV1TKTM55XSDX/metric-current-turn/`, with `report.json` recording the tested SHA. These synthetic inputs establish the changed executable metric contract, not real-inference accuracy, every application recording path, or populated Grafana backlog rendering. No production, installer, or Grafana scenario was rerun; empirical v1.0.0 remains **NO-GO**.

The preceding documentation/lint pass passed `mise run lint`, `mise run fmt:check` (209 Python files), release-feature Clippy (`--no-default-features --all-targets -p hippo-daemon -- -D warnings`), shell syntax checks, the configured shell-secret guardrail, dashboard JSON parsing, and `git diff --check`. Python lint and the edited docstring's formatting were rechecked after the documentation change. That pass ran no tests or product scenarios and changed only documentation and a docstring.

The exact evidence commands included:

```sh
mise run test
mise run test:install
mise run bench:decisions -- --repeats 2 --reverse
mise run bench:conflicts -- --repeats 2 --reverse
mise run bench:conflicts:replay
cargo audit --json
```

OSV Scanner checked the Python, extension, and site lockfiles. Gitleaks checked the baseline commit delta and a copied tracked-file snapshot. Read-only SQLite connections used `mode=ro` and `PRAGMA query_only=ON`; live retrieval held one read transaction. Production records, capture services, inference configuration, and permissions were not modified for the measurements.

Private captured text and raw audit artifacts remain outside Git at `~/.local/share/hippo-bench/v1-readiness/2026-09-24`, with directory mode 0700 and file mode 0600. The archive excludes the disposable package-smoke environment; its results are recorded in the release council evidence.

The local generated website installer initially appeared stale, but it is ignored output copied from the canonical script during Astro build. The deployed script lacked the suspected deletion, so that finding was withdrawn. Open issue status likewise was not used as proof of a current defect. The separate GUI repository is outside this daemon/brain release scope.
