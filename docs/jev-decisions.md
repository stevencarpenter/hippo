# Jev decisions: operator reference

Start with `mise run classification -- status --database /absolute/path/hippo.db`. This reads classification availability and queue state without starting inference or migrating the database.

The implementation provides optional Jev reranking, deterministic rules, controlled-topic classification, topic retrieval, and isolated evaluation. Every production feature starts disabled. Implementation tests do not establish the quality, latency, or human-review acceptance criteria in the [approved specification](superpowers/specs/2026-09-22-jev-retrieval-classification-design.md).

## Configuration and compatibility

Use the normal Hippo configuration file, `~/.config/hippo/config.toml`. These values show the defaults:

```toml
[retrieval]
rerank = false
rerank_backend = "local"
rerank_recipe = "multi-v1"
rerank_adaptive = false
rerank_pool = 30
decision_deadline_ms = 2000
topic_retrieval = false

[classification]
enabled = false
# recipe_path = "/absolute/path/calibrated-topics.json"
```

Enable a qualified Jev ranking recipe with `rerank = true` and `rerank_backend = "jev"`. Backend `"rules"` runs the existing declarative lexical decision table. Backend `"local"` retains local LLM ranking. Classification and topic retrieval have independent switches. Configuration changes require restarting the affected brain and MCP processes through the normal service workflow.

Supply `TYPESAFE_API_KEY` through the environment of each process that calls Jev. Setting it in a terminal does not automatically update an already running LaunchAgent. The client sends bounded, redacted input to `https://api.typesafe.ai/v1/systemone`, pins version `1.13.0` using API model name `jev-1.13.0`, and makes one HTTP attempt per assessment. It permits four concurrent requests per client and enters a 30-second cooldown after three endpoint failures. Missing credentials or endpoint failures preserve the last complete ranking; the fast path does not invoke the local LLM as a fallback.

Install matching Rust and Python components through the normal deployment workflow before enabling classification. See [schema compatibility](schema.md#at-a-glance) for migration ownership and accepted database versions. Missing classification schema disables classification with a diagnostic. Do not apply experimental SQL directly to the production database. Classification CLI commands do not migrate it.

`single-v1` retains the original relevance rubric. `multi-v1` combines relevance, direct evidence, and scope fit with weights 0.50, 0.35, and 0.15. Adaptive mode is a development recipe: it fetches eligible source evidence for at most eight uncertain candidates and rescoring stops on stable order, sufficient separation, missing new evidence, or the budget. Online decisions admit at most 30 candidates, three passes, and four reserved attempts within a maximum 2,000 ms deadline, including client queue wait. Repeating identical input is not refinement.

## Classification and topic discovery

The built-in classification recipe has 14 topics, a development threshold of 0.8, and a maximum of five accepted topics. Those defaults are not a claim that publication precision has passed its gate. A calibrated recipe can override thresholds and limit publication to supported topics:

```json
{
  "taxonomy_version": "technical-topics-v1-calibrated-01",
  "thresholds": {"database-storage": 0.94},
  "publish_topics": ["database-storage"],
  "max_topics": 5
}
```

The number above demonstrates the file format; it is not a recommended calibrated threshold. Set `classification.recipe_path` to the reviewed file. Omitted topic thresholds retain 0.8. An empty `publish_topics` list publishes no memberships. Recipe model identity must match the pinned client. Recipes also support explicit topic definitions and input version; unsupported fields and invalid values are rejected.

HTTP, MCP, enrichment scripts, and classification commands load the same configured recipe. CLI `--recipe PATH` overrides it for an individual command. A different recipe changes the hash and makes prior results ineligible for that reader. Keep the recipe fixed throughout a run and use a new backfill cursor after changing it.

Source writers enqueue the latest node input in their transaction. The worker checks node UUID, input hash, revision, recipe, and lease before publication. It uses two concurrent assessments, three attempts per revision, and expiring leases. Invalid answers never publish a partial topic set. An empty accepted set is a valid result. Original tags, content, FTS, vectors, and source links remain unchanged.

Owned concept identities use `hippo/topic/` plus an explicit ownership marker. Source concepts using reserved names retain their display text and receive an escaped canonical identity. A pre-existing unowned identity collision causes abstention; classification does not take ownership of that entity. Changed nodes lose stale owned memberships even when classification is disabled.

After an approved migration and rollout, queue one bounded backfill page:

```sh
mise run classification -- backfill \
  --database /absolute/path/approved-working.sqlite \
  --cursor "$HOME/.local/share/hippo-bench/classification/backfill.json" \
  --limit 100
```

This command **writes queue state to the selected database**. Use an approved deployment or a writable experiment clone, never the immutable benchmark baseline. It does not call Jev. Repeat the same command until `complete` is true; the cursor binds the database, recipe, and initial node-ID high watermark. Replayed pages are idempotent. New nodes use the writer hooks, or a new backfill job. The enabled brain worker drains the queue. The worker claims two nodes per brain poll, about 22 nodes per minute at the default 5-second interval, so a 30,000-node corpus drains in about 22 hours. A 1,000-row page holds the SQLite write lock long enough to make concurrent worker claims fail with `database is locked`; a node can exhaust its three attempts during that window. Use the default 100-row limit while the worker runs.

Export classification state to Parquet for offline analysis:

```sh
mise run classification -- export \
  --database /absolute/path/hippo.db \
  --out "$HOME/.local/share/hippo-investigations/classification-$(date -u +%Y%m%dT%H%MZ)"
```

The command reads the database once, so the export is consistent even while the worker runs. It writes `classifications.parquet` with one row per classification record (nodes never enqueued are absent; status, attempts, timestamps, `latency_ms`, recipe and input identity, and raw probability and accepted-topic JSON) and `topic-probabilities.parquet` with one row per ready node and topic (`prob`, `accepted`). The output directory must be outside the repository and the Hippo data directory, and existing export files are never overwritten. Both files publish together or not at all. The Parquet writer is DuckDB from the brain's development dependency group, so run the command from a checkout synced with `uv sync --project brain`. Reclassification overwrites rows in place, so an export is the only record of a run's results after a recipe or threshold change.

Inspect current connections without inference:

```sh
mise run classification -- connections \
  --database /absolute/path/hippo.db \
  --node KNOWLEDGE_NODE_UUID --limit 20 --project /absolute/path/project
```

Optional filters are `--source`, `--branch`, `--entity`, `--memory-category`, and `--since-ms`. Connections show the exact shared topic and provenance for both current memberships. They express shared subject matter, not causal or evidential support.

`topic_retrieval = true` adds an independent retrieval channel using reviewed query aliases. It admits at most three query topics, 20 candidates per topic, and 40 additional candidates before bounded fusion. Existing source/project/time/exclusion filters still apply. This path uses current metadata and leaves semantic vectors unchanged.

## Capture and external artifacts

All generated corpora, labels, logs, clones, responses, manifests, and archives belong outside the checkout and outside Hippo's production data directory. The default root is `$XDG_DATA_HOME/hippo-bench/decisions`, or `~/.local/share/hippo-bench/decisions`. Output guards resolve symlinks and reject repository or production paths. Maintained code, handwritten fixtures, recipes, and documentation can remain in Git.

Set these environment variables on the serving process only when capture is intended:

| Variable | Output under the decision root | Behavior |
| --- | --- | --- |
| `HIPPO_QUERY_CAPTURE=1` | `query-captures/` | Captures ordinary queries and candidate identities even when reranking is disabled. |
| `HIPPO_DECISION_CAPTURE=1` | `captures/`, `decision-traces/` | Captures reranker inputs and decision diagnostics when their paths execute. |

Capture invokes no additional inference. Files are redacted, created with private permissions, limited to 128 KB each, and capped at 1,000 entries per directory. Query records identify their origin; benchmark and probe traffic is excluded from ordinary capture. Duplicate query inputs are skipped. Capture failure does not fail a query. Move completed captures to an external archive before starting another bounded collection.

## Corpus review and replay

The corpus target is 1,000 retrieval questions over at least 10,000 nodes, plus 3,000 independently sampled classification nodes. Preparation creates source-grounded authoring and review queues. Empty authoring slots are not completed questions, and model annotations are not human ground truth.

1. Freeze source data and inspect actual coverage:

   ```sh
   mise run bench:knowledge:corpus -- prepare \
     --source /absolute/path/hippo.db \
     --questions /external/path/questions.json \
     --captures /external/path/query-captures \
     --query-target 1000 --node-target 3000 \
   --out /external/path/corpus-draft
   mise run bench:knowledge:corpus -- validate /external/path/corpus-draft
   mise run bench:knowledge:corpus -- review-package /external/path/corpus-draft \
     --out /external/path/review-package
   ```

   `--questions` and `--captures` are optional. Preparation uses a consistent SQLite backup and records source/input hashes, code identity, embedding metadata, and actual population counts. It does not call inference. Inspect `manifest.json`, `questions.json`, `query-authoring.json`, and `classification-review.json`. The separate review package supplies source evidence, taxonomy definitions, blind judgment queues, and the review rubric; it does not create human judgments.

2. Have reviewers author missing questions, resolve answerability, and audit task families before tuning. Keep related sessions, duplicates, and paraphrases in one split. Family hashing produces approximate split counts; it does not force quotas by splitting a family. Record evidence for unanswerable cases and retain missing-source or missing-node cases as coverage failures. Publish reviewed questions to a new directory:

   ```sh
   mise run bench:knowledge:corpus -- finalize /external/path/corpus-draft \
     --questions /external/path/reviewed-questions.json \
     --family-auditor REVIEWER_NAME --out /external/path/corpus-reviewed
   ```

   Finalization requires actual `human_reviewed` question records and a named family audit. It does not perform that review. To correct family grouping, supply the reviewed common `task_family_id` on the affected input questions and run `prepare` into a new directory before finalization. The preparer unions their source families before assigning splits. Source-identity corrections also require a new freeze; do not edit a frozen manifest.

3. Freeze candidate pools and run ranking through the production boundary:

   ```sh
   mise run bench:knowledge:replay -- \
     --cases /external/path/cases.json \
     --questions /external/path/corpus-reviewed/questions.json \
     --db /external/path/corpus-reviewed/baseline.sqlite \
     --arms retrieval,rules,jev-single,jev-multi,jev-adaptive \
     --repeats 1 --max-requests 300 --max-tokens 50000000 \
     --out /external/path/ranking-run
   ```

   These explicit budgets demonstrate a bounded run and can stop before a large corpus finishes. Choose the budget before dispatch. `cases.json` is a JSON array of `{id, state: {query, candidates}}`; each candidate needs a stable `uuid` and frozen result fields. IDs/text must match `--questions`, with at most 30 candidates per case. Archived `knowledge_probe` inputs can use `--targets targets.json` to recover their original UUID mapping. Raw query captures are inputs for authoring; select and freeze their candidate pools before acceptance replay.

   Supported arms also include `local-original`, `local-optimized`, and `local-cached`. Fresh local arms require `--local-url` and `--local-model`. Cached local responses require `--cached-local` and are quality replay, not fresh timing. Adaptive replay requires the frozen database. Every arm requires `--db` to measure source-policy violations; without it, the measurement remains unknown and cannot pass the source-policy gate. The default `--concurrency 1` rotates arms sequentially. `--concurrency 4` runs one arm at a time in batches of four, preserving local-comparator isolation. Every terminal result is checkpointed, and requests/tokens are reserved before dispatch. Resume an unchanged run with the same arguments plus `--resume`; inspect `completion.json` for exhaustion, interruptions, or incomplete rows. Unknown usage remains unknown in the evidence and consumes its conservative budget reservation. Run the specified repeated latency slice separately with `--repeats 3` and compare single-query and concurrency-four measurements.

4. Pool all tested candidates for blind relevance review, then import completed labels:

   ```sh
   mise run bench:knowledge:corpus -- pool /external/path/corpus-reviewed \
     --rankings /external/path/ranking-run/results.jsonl --cutoff 30 \
     --out /external/path/relevance-review.json
   mise run bench:knowledge:corpus -- import-labels /external/path/corpus-reviewed \
     --labels /external/path/relevance-reviewed.json --out /external/path/relevance-imported.json
   mise run bench:knowledge:corpus -- import-node-labels /external/path/corpus-reviewed \
     --labels /external/path/topics-reviewed.json --out /external/path/topics-imported.json
   ```

   Relevance judgments require grade 0 to 3, evidence or a negative rationale, named annotator and type, rubric `relevance-0-3-v1`, and reviewed/adjudicated status. Preserve their semantic-query and node-input fingerprints. Changing question text, filters, as-of time, source IDs, or node state requires pooling and grading again; changing only annotation status preserves those fingerprints. Legacy labels without fingerprints cannot qualify release acceptance. Topic judgments must label every taxonomy topic as `present`, `absent`, or `insufficient_evidence`, with evidence. Disagreements require human adjudication. Test acceptance also checks human labels, family audit, coverage, and double review. An unjudged candidate is not an irrelevant candidate.

5. Report the locked recipe against retrieval and a selected local comparator:

   ```sh
   mise run bench:knowledge:corpus -- report /external/path/corpus-reviewed \
     --rankings /external/path/all-arms.jsonl --labels /external/path/relevance-imported.json \
     --candidate jev-multi --baseline retrieval --local local-optimized \
     --out /external/path/retrieval-report.json
   mise run bench:knowledge:corpus -- classification-report /external/path/corpus-reviewed \
     --predictions /external/path/topic-predictions.json --labels /external/path/topics-imported.json \
     --backend jev --recipe /external/path/calibrated-topics.json \
     --out /external/path/classification-report.json
   ```

   Ranking reports require one record per query/arm with complete identities and terminal status; keep repeated latency trials separate from that quality input. Use `--adaptive` when reporting the adaptive latency gate. `sweep --signals PATH --labels PATH --weights PATH --out PATH` recomposes recorded development signals without new inference. Select on development/calibration data, then evaluate the locked test set once. Review the complete [acceptance policy](superpowers/specs/2026-09-22-jev-retrieval-classification-design.md#acceptance-policy), including operational load, exact-topic connections, candidate discovery, and answer/citation checks. A report is evidence for its implemented checks, not blanket release approval.

Review the exact topics behind proposed connections separately from individual-node labels:

```sh
mise run bench:knowledge:corpus -- connection-review /external/path/corpus-reviewed \
  --predictions /external/path/topic-run/predictions.jsonl --limit 200 \
  --out /external/path/connection-review.json
mise run bench:knowledge:corpus -- connection-report /external/path/corpus-reviewed \
  --predictions /external/path/topic-run/predictions.jsonl \
  --pairs /external/path/connections-reviewed.json \
  --plan /external/path/connection-review.plan.json --out /external/path/connection-report.json
```

Reviewers judge the connecting topic and usefulness for a named discovery task separately. The default sample uses test-only eligible nodes and disjoint endpoints without materializing all node pairs. Preserve the adjacent immutable plan unchanged and annotate every planned pair. The report checks that coverage and uses family-aware uncertainty bounds; generating the queue does not supply its judgments. Legacy reports without a plan can score results but cannot pass the connection gate. Candidate-discovery acceptance uses recall at 30 with equal candidate pools of at most 30; a larger diagnostic pool does not qualify.

Classify the fixed node sample independently for each comparator:

```sh
mise run bench:knowledge:tags -- classify-corpus /external/path/corpus-reviewed \
  --backend jev --recipe /external/path/calibrated-topics.json \
  --limit 3000 --max-requests 3000 --max-tokens 50000000 \
  --out /external/path/topic-run
```

Use `predictions.jsonl` from that directory for `classification-report`. The other backends are `rules`, `stored`, and `local`; fresh local classification also requires `--model MODEL --base-url URL`. Run each into its own directory with the same frozen sample and recipe. The report checks backend/model/input/recipe identities; comparator results cannot qualify Jev publication. Benchmark classification commands use the built-in recipe when `--recipe` is absent, so pass the configured recipe explicitly for parity.

Re-running the unchanged `classify-corpus` command resumes its output directory after validating the manifest and checkpoints. It reserves request/token budget before dispatch. Interrupted reservations become terminal error records with unknown usage charged conservatively; they cannot qualify a complete run. Inspect `completion.json` instead of inferring completion from the existence of predictions.

The metadata-only clone command reuses production classification validation and publication:

```sh
mise run bench:knowledge:tags -- metadata /external/path/baseline.sqlite \
  /external/path/topic-run/predictions.jsonl --recipe /external/path/calibrated-topics.json \
  --out /external/path/metadata.sqlite
```

Each prediction requires `node_uuid`, current `input_hash`, and `recipe_hash`. Jev predictions require the complete typed `response`; comparator predictions retain their backend, model, and complete probabilities. Historical summary-only classifications cannot be relabeled as current source-aware predictions. The command verifies preservation and writes only a new external clone.

Run content/FTS and semantic-vector projections as separate ablations from the same untouched baseline:

```sh
mise run bench:knowledge:tags -- projection /external/path/baseline.sqlite \
  /external/path/topic-run/predictions.jsonl --recipe /external/path/calibrated-topics.json \
  --arm fts --out /external/path/fts.sqlite
mise run bench:knowledge:tags -- projection /external/path/baseline.sqlite \
  /external/path/topic-run/predictions.jsonl --recipe /external/path/calibrated-topics.json \
  --arm vectors --model SNAPSHOT_EMBEDDING_MODEL --base-url EMBEDDING_API_URL \
  --out /external/path/vectors.sqlite
```

The vector arm requires the embedding model recorded in the baseline. Each arm checks original tags, source identity, command vectors, and isolation from the other projection. A second disposable clone proves restoration of content, FTS postings, and vectors; inspect the adjacent completion record. These are explicit clone mutations with additional embedding requests in the vector arm. Runtime flags do not roll them back. Older experiments remain described in the [decision-sidecar reference](decision-sidecars.md); their results do not authorize production FTS or vector rewriting.

## Observability and rollback

`mise run brain:api:health` exposes classification availability, queue counts, oldest pending age, recent batch outcomes, claim-gate failures, and Jev cooldown diagnostics. Decision traces record recipe/input identity, raw typed judgments, rule decisions, usage, timing, and stop/fallback reasons. Existing telemetry exports `hippo.brain.decision.*` counters for decisions, actual HTTP dispatches, errors, tokens, and unknown measurements, plus a stage-duration histogram. Labels are bounded to backend/task/outcome/stage/direction, never node/query IDs. The brain also reports classification rows by status through `hippo_brain_classification_queue_depth`, separate from the enrichment queue gauge. Stage durations overlap and must not be summed. HTTP time is client wall time, not server inference time. Missing token usage is not zero, and latency ratios are not dollar-cost estimates.

`hippo doctor` reports classification and Jev endpoint health from the brain `/health` response. It fails when enabled classification has an unavailable, stopped, or erroring worker, or when the Jev client is in cooldown. It warns on failed rows, recent endpoint failures, or a pending row older than 24 hours. The *Decisions* row of the `hippo-enrichment` Grafana dashboard plots decision outcomes, stage latency, classification backlog, Jev tokens, error ratio, and HTTP dispatches.

The brain worker yields new claims while paused or while queries are in flight. HTTP and MCP queries share a file-lock gate with classification when classification is configured. The lock lives beside the canonical database as `DATABASE_NAME.classification-claims.lock`, with mode 0600. Queries hold shared locks; classification takes a nonblocking exclusive lock only around its short claim transaction and releases it before inference. Do not delete the lock file while processes use it. A gate failure allows the query to proceed, prevents a background claim, and increments process-local diagnostics. Disabled classification creates no gate file. Standalone MCP processes consume classification state but never start workers.

Classifier claim, publication, and failure writes use owned background-thread connections so SQLite busy waits do not block the brain event loop. Cancellation checks prevent pending transactions from publishing; a claim committed before cancellation can remain until its bounded lease expires.

Classification does not depend on local chat-model preflight. Already dispatched work remains bounded by its timeout; query arrival does not retroactively cancel it. Verify query latency under the declared deployment load before claiming the operational acceptance gate.

Disable a feature by changing its configuration and restarting affected processes:

1. Set `retrieval.rerank = false` to restore retrieval order, or select `rerank_backend = "local"` to retain local reranking.
2. Set `retrieval.topic_retrieval = false` to remove classification-driven candidate expansion.
3. Set `classification.enabled = false` to stop new background assessments. Valid state remains stored; source updates still invalidate stale memberships.
4. Remove the capture environment variables to stop capture. Preserve collected evidence in an external archive.

Metadata-only rollback does not require deleting original content or vectors. Do not delete all concepts or entity links to remove classification. FTS/vector experiment clones are separate artifacts; disabling a flag cannot remove words or embeddings already written by an experimental transformation.
