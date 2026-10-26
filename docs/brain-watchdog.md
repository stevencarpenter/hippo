# Enrichment queue watchdog

Mitigates the enrichment-queue wedge risk: on 2026-04-17 the live corpus had
**417 queue rows all locked by one worker at one timestamp** and held for 30+
minutes because the inference backend returned HTTP 400 and the claim loop got
stuck, while pending work grew behind it. The watchdog removes the three
failure-mode legs that made that possible.

## What it does

1. **Reaper** (`hippo_brain.watchdog.reap_stale_locks`) runs at the top of every
   enrichment loop iteration, across all four queues
   (`enrichment_queue`, `claude_enrichment_queue`, `browser_enrichment_queue`,
   `workflow_enrichment_queue`). Rows where
   `status='processing' AND locked_at <= now - lock_timeout` are swept back to
   `pending`, `retry_count` is incremented, and rows that hit `max_retries` are
   promoted to `failed` so a permanently bad payload can't loop forever.

2. **Inference preflight** (`preflight_inference`) runs before each claim. It
   calls `/v1/models` against the configured OpenAI-compatible backend
   (LM Studio, oMLX, ollama, vLLM, …) and returns one of
   `ok | fallback | unreachable | no_models | model_missing`. On any non-ok
   reason the cycle skips claiming and WARNs — the claim queue stays intact
   instead of being poisoned by a wedge. The decision is also mirrored into
   `source_health['brain-preflight']`, where the Rust-side watchdog reads
   `consecutive_failures` to fire **I-12** when preflight is stuck for
   more than ~1 minute (`> 12` failures at the brain's 5 s poll).

3. **Claim-batch cap** — each `claim_pending_*` function accepts
   `max_claim_batch`. The cap is enforced as `LIMIT ?` on the UPDATE's inner
   SELECT, so one bad batch can't claim (and then hold) an entire backlog.

Structured failure logs include `queue_name`, `stage`, `exception_type`,
`enrichment_model`, `claim_count`, and `claim_age_ms` — the fields a human or
alert rule needs to diagnose a wedge without tailing the full log.

## Pause controls

Two independent pauses exist:

- **Bench pause**: `POST /control/pause` and `/control/resume`, used by hippo-bench. It is a full quiesce held in memory. It also rejects queries with 503 and stops the embed reaper and classification.
- **Operator pause**: `hippo enrichment pause|resume`, persisted in `<data_dir>/enrichment-state.json`. It stops only the enrichment loop. Capture, embeddings, queries, and classification continue.

The hourly agent-eval commit linkage job stops only under the bench pause. Neither pause clears the other. Enrichment runs only when both are inactive. While the operator pause is active, the watchdog suppresses I-12 and I-19, and the brain resets `brain-preflight.consecutive_failures` on pause transitions. While either pause is active, the loop skips the inference preflight and claims no work. The stale-lock reaper keeps running under both pauses, so a lock that expires during a pause is still released. The embed orphan reaper stops only under the bench pause. A batch already in flight finishes, so no row is cancelled or retried because of the pause. Capture-alarm lesson sync also waits until resume; it is cursor-based, so nothing is lost.

## Config knobs

`[brain]` section of `~/.config/hippo/config.toml`:

| Key                 | Default | Effect                                                              |
| ------------------- | ------- | ------------------------------------------------------------------- |
| `max_claim_batch`   | `10`    | Max rows claimed per queue per cycle. Per-queue, not global.        |
| `lock_timeout_secs` | `600`   | A `processing` lock older than this is considered stale and reaped. |

Metrics: `hippo.brain.enrichment.reaped{queue_name=...}` and
`hippo.brain.enrichment.preflight_skipped{reason=...}` — watch these to see
the watchdog actually firing.

## Tracking model worker crashes (LM Studio-specific)

LM Studio's qwen-MoE worker process can die mid-inference (JIT eviction when
another client requests a different model, or Metal allocator failures under
context-cache pressure). Until the worker reloads, the API returns HTTP 4xx
with body `{"error": "The model has crashed without additional information.
(Exit code: null)"}`. The brain's queue-level retry transparently re-runs the
work against a freshly-restarted model, so end-to-end enrichment succeeds —
but the underlying instability was previously visible only in the workflow
path's logs (which lack the in-process retry wrapper that claude/shell/browser
have).

Metric: `hippo.brain.inference.crashes` — incremented by the inference HTTP
client's `_raise_with_body` helper whenever a 4xx body matches `"model has
crashed"` (case-insensitive). The substring match is LM-Studio-specific
(other OpenAI-compatible backends — oMLX, ollama, vLLM — report crashes
differently and won't increment this counter; re-check on LM Studio
upgrades). Path-agnostic: chat, embed, and list_models contribute equally.
Crashes are a strict subset of `hippo.brain.inference.errors`, so a single
crash increments both counters; dashboards graphing `crashes / errors` see
the LM Studio-specific share of failures over time.

Mitigations live in LM Studio settings, not hippo: set
`unloadPreviousJITModelOnLoad: false` to stop competing-client evictions, and
cap `defaultContextLength` to a fixed value (e.g. 32768) instead of `"max"`
to reduce Metal allocator pressure.

## Stack-wide health grade

The daemon exports `hippo.daemon.health.grade` (0–100) as an OTel observable
gauge. The score derives from the count of currently-active
`capture_alarms` rows: `100 - 10 * active_alarm_count`, floored at 0. The
`Stack Health Grade` stat panel at the top of the
`Hippo Overview` Grafana dashboard surfaces it with green/yellow/red
thresholds at 90 / 70 / 0. A companion gauge,
`hippo.daemon.health.active_alarms`, exposes the raw count for drill-down.

## Agent-eval commit linkage

The brain runs `hippo_brain.eval_linkage.run_once` once an hour (`_eval_linkage_loop`, first pass one hour after start, skipped during the bench pause). A brain that restarts more often than once an hour never runs the pass; use the CLI then. It also runs once from the CLI: `uv run --project brain python -m hippo_brain.eval_linkage --once [--db PATH]` or `mise run eval-linkage`. The CLI prints `{"open_findings": N, "linked": L, "still_open": S}`.

For each open finding (no verdict of any kind) with side `+` and a non-NULL `line_hash`, the job:

1. Skips the finding when `repo` or `repo/.git` is gone, or when the file is absent at `HEAD`.
2. Keeps the finding open when any line of `HEAD:path` still has the finding's `line_hash`. A line that moved but stayed byte-identical therefore stays open.
3. Picks a start commit. When `head_sha` exists, the start is `git merge-base <head_sha> HEAD`: `head_sha` itself when `HEAD` descends from it, else the fork point of a squash-merged branch. The walk is `git rev-list --reverse --topo-order --ancestry-path <start>..HEAD -- <path>`, so a mainline commit merged into the reviewed branch is never taken as the fix. When `head_sha` was pruned, the start is `git rev-list -1 --before=<started_at seconds> HEAD`, and the walk is `git rev-list --reverse --topo-order --ancestry-path <start>..HEAD -- <path>`, which excludes the start, so a commit in the review's start second counts once, as the start. Unrelated history (`merge-base` exit 1) and any other `merge-base` failure keep the finding open with a reason.
4. Returns the first walked commit whose version of the file lacks the hash, counted from the start commit when its file holds the line or, for a line reviewed before it was committed, from the first walked commit that contains it. The commit must also have a true parent (`git rev-parse <commit>^@`) whose file holds the line, because the walk flattens history and a sibling-branch commit that never held the line can follow the commit that added it. A commit that recreates a deleted file without the line also counts when an earlier walked commit that held the line is its ancestor (`git merge-base --is-ancestor`), so a sibling branch that adds the file without the line is not credited. When the walk starts at the merge-base (`HEAD` does not contain `head_sha`), a removal committed before the review's `started_at` is a mainline edit the reviewed branch never had, so the job treats the line as not yet seen and continues; the finding can then reach step 5. This is an observed fix, and the verdict `note` stays NULL.
5. If no walked commit ever contains the line (it is absent at the start commit and at `HEAD`), returns the first walked commit that touches the path and whose committer time is at or after the review's `started_at`, and sets the verdict `note` to `inferred: line absent at walk start; first commit touching the path`. This is hippo's default flow: review the uncommitted tree on a branch, fix, commit, squash-merge. The flagged line is gone at `HEAD`, so `likely-addressed` holds; only the commit is a guess. If no walked commit touches the path, the finding stays open with reason `line absent at walk start and no later commit touches the path`; if only commits from before the review start touch it, the reason is `line absent at walk start; only commits before the review touch the path`. When `HEAD` does not contain `head_sha`, inference also requires that the reviewed branch is gone: if `git for-each-ref --contains <head_sha> refs/heads refs/remotes` lists any ref, the finding stays open with reason `reviewed branch still exists; not merged`. Every inference then requires that no ref tip still holds the line: for each ref under `refs/heads` and `refs/remotes` (symbolic refs such as `origin/HEAD` skipped), the job reads `git cat-file blob <ref>:<path>` and hashes its lines as in step 2, caching each `(ref, path)` for the pass. A missing path at a ref counts as absent. The first ref whose tip holds the line keeps the finding open with reason `flagged line still present on <refname>`. Observed fixes (step 4) are not affected by these guards.
6. Writes an `eval_verdicts` row: `verdict = 'likely-addressed'`, `auto = 1`, that 40-hex `commit_sha`, `decided_at = ingested_at = now`, `verdict_id = auto-` + the first 16 hex characters of `sha256(finding_id|commit_sha|likely-addressed)`.

Git runs outside any transaction, with `--no-optional-locks` so it never takes the user's index lock. Verdicts are written in `BEGIN IMMEDIATE` transactions of at most 100 rows on an autocommit connection with `busy_timeout=5000`. Each insert is conditional on the finding having no verdict and the id being unused, so a human verdict ingested during the pass wins, and a rerun inserts nothing.

Skip reasons are logged once per finding: the current set lives in `<data_dir>/eval-linkage-notes.json` (`{finding_id: reason}`), and a reason is logged only when it is new or changed. A skipped finding is re-evaluated every pass.

Every reason the job records:

| Reason | When |
|---|---|
| `repository missing` | `repo` or `repo/.git` does not exist. |
| `file deleted at HEAD` | `HEAD:path` does not exist. |
| `HEAD shares no history with head_sha` | `git merge-base <head_sha> HEAD` exits 1. |
| `git merge-base failed (exit N)` | `git merge-base` exits with any other nonzero status N. |
| `head_sha not in repository and no commit before the review` | The `head_sha` object is missing and `HEAD` has no commit at or before `started_at`. |
| `reviewed branch still exists; not merged` | The line never reached the walked history, `HEAD` does not contain `head_sha`, and a ref under `refs/heads` or `refs/remotes` still contains it. |
| `flagged line still present on <refname>` | A commit would be inferred, but the tip of `<refname>` (first match under `refs/heads` or `refs/remotes`) still holds the line. |
| `git for-each-ref failed (exit N)` | The branch-gone check (run only when a commit would be inferred and `HEAD` lacks `head_sha`) or the ref-tip check (run only when a commit would be inferred) exits with nonzero status N. |
| `line absent at walk start and no later commit touches the path` | The line never reached the walked history and no walked commit touches `path`. |
| `line absent at walk start; only commits before the review touch the path` | The line never reached the walked history and every walked commit that touches `path` has a committer time before `started_at`. |
| `no walked commit removed the line` | The line was seen in the walk but no walked commit lacks it; `HEAD` lacks it, so this is not expected in practice. |
| `git object unavailable: <rev>:<path>` | The path exists at that revision but its blob is not stored locally (for example an offline partial clone). The finding stays open rather than reading the line as absent. |
| `git failed: <ExceptionType>` | A git call raised `OSError` or `subprocess.SubprocessError` (for example a timeout). |

Limits: matching is raw line to raw line, so a comment-only edit counts as a change. A flagged line that has a byte-identical twin elsewhere in the file stays open until every copy is gone. A file rename reads as a deletion. An inferred commit (step 5) is wrong when a review was discarded and an unrelated commit later touched the file, or when a mainline commit touched the file after the review started and before a squash merge; the `inferred:` note marks every such verdict so scorecards can separate them from observed ones. The review-start cutoff compares committer time in seconds with `started_at` in milliseconds, so a candidate committed in the review's start second but after its start millisecond is excluded; a rewritten commit (rebase, cherry-pick, amend) gets a new committer time and stays a candidate. The ref-tip check reads ref tips only: a line committed on a branch and then fixed there no longer blocks, and a line still on any branch tip, including a stale branch or a remote-tracking ref whose fix is not yet fetched, keeps the finding open until that ref moves or is deleted. A line reviewed and never committed anywhere cannot be told apart from a fixed one, so a later unrelated edit to the file can still be inferred (the `inferred:` note marks it). The marker lives in `note`, so payload GC clears it at `payload_expiry` (90 days, unless the finding is pinned); after that an inferred verdict reads like an observed one. The branch-gone guard reads refs only: after a squash merge, a stale remote-tracking ref such as `origin/feat` keeps the finding open until `git fetch --prune` removes it, and a separate clone that never fetched the branch lacks the `head_sha` object, which the rule treats as a gone branch, so a mainline commit touching the file there can be inferred (the `inferred:` note marks it). A removed worktree or clone reads as `repository missing`; the job does not fall back to another checkout with the same `repo_identity`.

`[eval_linkage] gh_pr_lookup = true` in `config.toml` enables the opt-in PR lookup (default `false`). For each new verdict whose commit the local PR chain (a `workflow_runs` row for the commit, then a session on that branch in the same repository with `pr_links_json`) does not attribute, the job runs `gh api repos/<repo_identity>/commits/<sha>/pulls` and stores `PR <url>, ...` in the verdict `note` (after the `inferred:` marker and `; ` when both apply) (redacted, swept at `payload_expiry` like any note). This sends the slug and the SHA to GitHub. With the flag off, the job makes no network call: every git command runs with `GIT_NO_LAZY_FETCH=1`, so a partial clone never fetches a missing object from its promisor remote; it reports `git object unavailable` instead.
