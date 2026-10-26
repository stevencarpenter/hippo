# Agent-Eval Schema and Ingest Contract: Design

**Status:** proposed, awaiting the HIPO-63 gate decision
**Date:** 2026-10-25
**Ticket:** HIPO-63 (epic HIPO-57). Implementation ticket: HIPO-64.
**Related:** HIPO-7 (derived-index exclusion), HIPO-9 (planned append-only feedback rows), HIPO-13 (payload expiry), HIPO-65 (producers), HIPO-66 (commit linkage), HIPO-67 (scorecards), HIPO-68 (private corpus)
**Producers (read-only reference):** `~/projects/agents/skills/blind-review/scripts/review_log.py`, `scripts/mechanical_review.py`, `bench/bench.py`

## 1. Goal and scope

Hippo has no record type for review findings, verdicts, escaped defects, or
benchmark runs. The blind-review tools in the `agents` repo already produce
these facts as JSONL. This design adds five `eval_*` tables and one typed
ingest command, `hippo ingest agent-eval <file.jsonl>`.

In scope: table DDL, keys, indexes, redaction, the JSONL record contract, the
links to `agentic_sessions` and to commits, privacy enforcement, retention,
and the canary test. Out of scope: the linkage algorithm (HIPO-66), the
scorecard queries (HIPO-67), and producer code (HIPO-65). This document states
only the contracts those tickets depend on.

Evidence used, all checked on 2026-10-25:

- `review_log.py` writes `finding`, `status`, and `escape` events to
  `~/.local/share/blind-review/findings.jsonl`. That ledger file does not
  exist on this machine yet, so no legacy rows need a backfill, and the id
  and record formats can change now without a migration.
- `bench/results.jsonl` holds 2 recorded runs.
- The mechanical reviewer runs `claude -p ... --no-session-persistence`
  (`mechanical_review.py::reviewer_argv`). The per-unit reviewer process
  writes no transcript, so hippo never has an `agentic_sessions` row for it.
- A mechanical out dir records no run times, no effort, and no commit SHAs.
  `manifest.json` holds `repo`, `base` (a ref name), `mode`, and `files`.
  `results/*.json` holds `unit`, `ok`, `findings`, `usage`, `model_id`,
  `cli_version`, `input_sha256`, and `error`.
- Claude Code exports `CLAUDE_CODE_SESSION_ID` to tool subprocesses (seen in
  this session's Bash environment).
- `workflow_runs` has no PR column, and `raw_json` holds no `pull_requests`
  key on any live row. `workflow_runs.repo` is an `owner/repo` slug.
- `.semgrep.yml` exists but no CI workflow or `mise` task runs it. Its header
  says so.
- The live schema is v27 (`storage.rs::EXPECTED_VERSION`). Phase 1 of the
  epic takes v28. `retrieval_events` (HIPO-9) does not exist yet. The in-tree
  append-only precedent is `deletion_receipts` (`schema.sql`).

## 2. Decision: hippo tables, not a sidecar database

**Recommendation: put the five `eval_*` tables in `hippo.db`.**

A sidecar database has a precedent: `hippo-bench` keeps its results in
`~/.local/share/hippo-bench/bench-results.db` (`brain/src/hippo_brain/bench/paths.py`).
That choice was correct for hippo-bench because its rows join to nothing in
`hippo.db`. Eval rows are different.

Reasons for hippo tables:

1. **Commit linkage and attribution join hippo tables.** Linkage (HIPO-66)
   resolves a fix commit through `workflow_runs` and branch-matched
   `agentic_sessions` rows (section 3.7). Scorecards (HIPO-67) join a run to
   its orchestrating session in `agentic_sessions` and
   `agentic_session_facts`. Those joined values describe the orchestrator,
   not the reviewer, because the reviewer has no transcript. Reviewer model,
   effort, and tokens come from `eval_review_runs` itself. A sidecar needs
   `ATTACH` on every reader connection, and cross-file foreign keys do not
   exist in SQLite.
2. **One privacy regime.** Redaction (`RedactionEngine`), payload expiry with
   pins, `deletion_receipts`, and `secure_delete=ON` already exist for
   `hippo.db` (HIPO-13). A sidecar must copy all four and keep them in step.
3. **One backup and one doctor.** Operators back up, inspect, and migrate one
   file. A second file adds a second migration runner and a second
   version handshake.
4. **The write load is small.** A review run writes one run row and 0 to 50
   finding rows. Bench runs are one row each. The ingest uses short
   transactions (section 4.5), so the added `SQLITE_BUSY` exposure is
   bounded. HIPO-58 addresses the existing contention independently.

Costs accepted with this choice:

- Every eval schema change is a hippo schema version bump with the lockstep
  release (`docs/schema.md`, "Adding a new migration").
- Eval rows sit in the same file as derived indexes, so exclusion must be
  enforced in code (section 5.2). A sidecar would give physical separation.
  The guard test in section 5.3 replaces that separation.

Reasons that would reverse the decision: if HIPO-58 shows that any added
writer causes capture drops, or if the eval schema must change faster than the
hippo release cadence. Neither holds today.

## 3. Schema

### 3.1 Placement and version

One additive migration in `crates/hippo-core/src/storage.rs`, the first free
version after Phase 1 (v28 to v29 if Phase 1 lands first). It follows the
crash-safe pattern: `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT
EXISTS`, `CREATE TRIGGER IF NOT EXISTS`, `PRAGMA user_version` bump. The
DDL lives once in `crates/hippo-core/src/schema/agent_eval.sql`, which both the
fresh-install `SCHEMA` and the migration include (the v28
`schema/agentic_eval.sql` precedent), so the two cannot drift. `EXPECTED_SCHEMA_VERSION`
and `ACCEPTED_READ_VERSIONS` in `brain/src/hippo_brain/schema_version.py`
change in the same commit. No existing table changes.

Conventions in all five tables:

- The primary key is the producer-generated record id (`TEXT`). It is the
  idempotency key (section 4.5).
- All timestamps are Unix epoch milliseconds (`INTEGER`).
- `ingested_at` is the time hippo wrote the row. Producer times have their own
  columns.
- Commit columns used for linkage (`base_sha`, `head_sha`, `commit_sha`,
  `fix_commit`) hold full 40-character lowercase hex SHAs. Producers resolve
  them with `git rev-parse`. Ingest rejects any other form.
- `repo` keeps the local absolute path, which the linkage job needs to run
  `git`. `repo_identity` holds the slug from
  `crates/hippo-daemon/src/git_repo.rs::derive_git_repo`: `owner/repo` from
  the `origin` remote, else the top-level directory name, else NULL.
  Scorecards and joins to `workflow_runs.repo` use `repo_identity`.
- `payload_expiry` follows HIPO-13: `ingested_at + 7776000000` (90 days).
  Only `eval_findings` and `eval_verdicts` carry it (section 5.4).
- Columns marked "redacted" pass through `RedactionEngine::redact` before the
  insert.
- Enumerated values (`severity`, `confidence`, `verdict`) are validated by the
  ingest code, not by `CHECK`. A new value then needs a contract version, not
  a table rebuild (the v13 and v17 rebuilds show that cost). `CHECK` is used
  only for values that cannot grow: `side` and `auto`.

### 3.2 `eval_review_runs`

One row per review invocation (one mechanical out dir). Expected volume is
tens of rows per week. Write pattern: insert once, never updated.

```sql
CREATE TABLE IF NOT EXISTS eval_review_runs (
    run_id                TEXT    PRIMARY KEY,
    agent                 TEXT    NOT NULL,
    mode                  TEXT    NOT NULL,
    requested_model       TEXT,
    model_id              TEXT    NOT NULL DEFAULT '',
    effort                TEXT,
    cli_version           TEXT    NOT NULL DEFAULT '',
    repo                  TEXT    NOT NULL,
    repo_identity         TEXT,
    base                  TEXT    NOT NULL,
    base_sha              TEXT    NOT NULL,
    head_sha              TEXT    NOT NULL,
    input_sha256          TEXT    NOT NULL,
    units_total           INTEGER NOT NULL DEFAULT 0,
    units_failed          INTEGER NOT NULL DEFAULT 0,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    session_id            TEXT,
    harness               TEXT,
    started_at            INTEGER NOT NULL,
    finished_at           INTEGER NOT NULL,
    ingested_at           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_review_runs_agent_started_at
    ON eval_review_runs (agent, started_at);
CREATE INDEX IF NOT EXISTS idx_eval_review_runs_session_id_harness
    ON eval_review_runs (session_id, harness) WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_eval_review_runs_repo_identity_started_at
    ON eval_review_runs (repo_identity, started_at);
```

- `agent` is the reviewing agent name, for example `blind-review`.
  `mode` is for example `mechanical`.
- `requested_model` is the model argument (for example `haiku`). `model_id`
  is the sorted, comma-joined set of served model ids, the same form
  `mechanical_review.py` writes per unit. The column names match the Phase 1
  `agentic_sessions` columns.
- `base` is the ref name the review used. `base_sha` is that ref resolved at
  review start. `head_sha` is `HEAD` at review start. Both are required: a
  repository with no commits is not reviewable, and `mechanical_review.py`
  refuses to run when either SHA does not resolve. The reviewed tree can hold
  uncommitted changes on top of `head_sha`. Commit linkage looks only at
  commits after `head_sha`.
- `input_sha256` is the SHA-256 of the sorted per-unit `input_sha256` values.
- `units_failed` keeps a failed review distinct from a clean one with zero
  findings. `review_log.py log` already treats that difference as an error.
- The token columns are sums of the reviewer `usage` objects over all units,
  the same sum `bench.py` computes. They describe the reviewer processes.
- No column holds free text, so no column is redacted. `repo` is a local path,
  which hippo already stores in `agentic_sessions.cwd`.

### 3.3 `eval_findings`

One row per finding. Expected volume is 0 to 50 rows per run. Write pattern:
insert once. The only later write is the payload-expiry partial `UPDATE`.

```sql
CREATE TABLE IF NOT EXISTS eval_findings (
    finding_id     TEXT    PRIMARY KEY,
    run_id         TEXT    NOT NULL REFERENCES eval_review_runs (run_id),
    repo           TEXT    NOT NULL,
    repo_identity  TEXT,
    path           TEXT    NOT NULL,
    side           TEXT    NOT NULL CHECK (side IN ('+', '-')),
    line           INTEGER NOT NULL,
    line_text      TEXT,
    line_hash      TEXT,
    severity       TEXT    NOT NULL,
    confidence     TEXT    NOT NULL,
    category       TEXT    NOT NULL DEFAULT 'unknown',
    claim          TEXT,
    logged_at      INTEGER NOT NULL,
    ingested_at    INTEGER NOT NULL,
    payload_expiry INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_findings_run_id
    ON eval_findings (run_id);
CREATE INDEX IF NOT EXISTS idx_eval_findings_repo_path
    ON eval_findings (repo, path);
CREATE INDEX IF NOT EXISTS idx_eval_findings_payload_expiry
    ON eval_findings (payload_expiry)
    WHERE line_text IS NOT NULL OR claim IS NOT NULL;
```

- Redacted: `line_text`, `claim`.
- `line_text` and `claim` are NOT NULL at ingest. They become NULL after
  payload expiry, so the columns allow NULL.
- `repo` and `repo_identity` repeat the run's values so that linkage can use
  `idx_eval_findings_repo_path` without a join.
- `line_hash` is defined in section 3.7.
- The finding id includes the run id (section 6), so the same claim in two
  runs gives two rows.

### 3.4 `eval_verdicts`

Append-only, one row per verdict event. Expected volume is 0 to 3 rows per
finding. Write pattern: insert only. The only permitted `UPDATE` sets `note`
to NULL at payload expiry. The triggers copy the in-tree `deletion_receipts`
pattern (`schema.sql`), including its `no_replace` trigger. That trigger is
needed because `INSERT OR REPLACE` deletes the old row without firing a delete
trigger while `recursive_triggers` is off. HIPO-9 plans the same pattern for
`retrieval_events`.

```sql
CREATE TABLE IF NOT EXISTS eval_verdicts (
    verdict_id     TEXT    PRIMARY KEY,
    finding_id     TEXT    NOT NULL REFERENCES eval_findings (finding_id),
    verdict        TEXT    NOT NULL,
    commit_sha     TEXT,
    note           TEXT,
    auto           INTEGER NOT NULL CHECK (auto IN (0, 1)),
    decided_at     INTEGER NOT NULL,
    ingested_at    INTEGER NOT NULL,
    payload_expiry INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_verdicts_finding_id_decided_at
    ON eval_verdicts (finding_id, decided_at);
CREATE INDEX IF NOT EXISTS idx_eval_verdicts_commit_sha
    ON eval_verdicts (commit_sha) WHERE commit_sha IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_eval_verdicts_payload_expiry
    ON eval_verdicts (payload_expiry) WHERE note IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS eval_verdicts_no_delete
BEFORE DELETE ON eval_verdicts BEGIN
    SELECT RAISE(ABORT, 'eval_verdicts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS eval_verdicts_no_update
BEFORE UPDATE OF verdict_id, finding_id, verdict, commit_sha, auto, decided_at,
    ingested_at, payload_expiry
ON eval_verdicts BEGIN
    SELECT RAISE(ABORT, 'eval_verdicts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS eval_verdicts_note_clear_only
BEFORE UPDATE OF note ON eval_verdicts
WHEN NEW.note IS NOT NULL BEGIN
    SELECT RAISE(ABORT, 'eval_verdicts.note can only be cleared');
END;
CREATE TRIGGER IF NOT EXISTS eval_verdicts_no_replace
BEFORE INSERT ON eval_verdicts
WHEN EXISTS (
    SELECT 1 FROM eval_verdicts
    WHERE verdict_id = NEW.verdict_id
) BEGIN
    SELECT RAISE(ABORT, 'eval_verdicts are append-only');
END;
```

- Redacted: `note`.
- Allowed `verdict` values in contract version 1: `accepted`,
  `false-positive`, `not-useful`, `duplicate`, `likely-addressed`.
- `auto = 1` marks a machine verdict (`review_log.py reconcile`, the HIPO-66
  linkage job). `auto = 0` marks a human verdict (`review_log.py mark`).
- The `no_replace` trigger also aborts `INSERT ... ON CONFLICT DO NOTHING`
  for an existing id, because a `BEFORE INSERT` trigger runs before the
  conflict check. The ingest therefore checks for the id before it inserts
  (section 4.5).

**Effective verdict rule.** The latest human verdict wins. If a finding has no
human verdict, the latest automatic verdict wins. "Latest" orders by
`decided_at`, then `verdict_id` as a tie-break. A finding with no verdict is
open. Plain "latest wins" would let an automatic `likely-addressed` override
an earlier human `false-positive`. A human judgment is stronger evidence than
a line-change heuristic, so it takes precedence.

```sql
-- Effective verdict per finding.
SELECT finding_id, verdict, auto, commit_sha
FROM (
    SELECT v.*, ROW_NUMBER() OVER (
        PARTITION BY finding_id
        ORDER BY (auto = 0) DESC, decided_at DESC, verdict_id DESC
    ) AS rn
    FROM eval_verdicts v
)
WHERE rn = 1;
```

### 3.5 `eval_escapes`

One row per defect that reached review and that no finding reported. Rows are
manual and rare (a few per month). Write pattern: insert once, never updated.
Escape notes never expire (section 5.4), so the table has no
`payload_expiry`.

```sql
CREATE TABLE IF NOT EXISTS eval_escapes (
    escape_id      TEXT    PRIMARY KEY,
    repo           TEXT    NOT NULL,
    repo_identity  TEXT,
    path           TEXT    NOT NULL,
    line           INTEGER NOT NULL,
    fix_commit     TEXT,
    note           TEXT    NOT NULL,
    recorded_at    INTEGER NOT NULL,
    ingested_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_escapes_repo_path
    ON eval_escapes (repo, path);
```

- Redacted: `note`.
- An escape links to runs at query time: the runs in the same `repo` that
  reviewed `path` before `fix_commit`. No foreign key exists, because an
  escape is by definition not tied to a finding.

### 3.6 `eval_bench_runs`

One row per recorded `bench.py run --record`. Two rows exist today. Write
pattern: insert once, never updated, never expired.

```sql
CREATE TABLE IF NOT EXISTS eval_bench_runs (
    bench_run_id   TEXT    PRIMARY KEY,
    bench          TEXT    NOT NULL,
    recorded_at    INTEGER NOT NULL,
    repo_commit    TEXT    NOT NULL,
    cli_version    TEXT    NOT NULL DEFAULT '',
    model          TEXT    NOT NULL,
    model_ids_json TEXT    NOT NULL DEFAULT '[]',
    effort         TEXT,
    reps           INTEGER NOT NULL,
    cases          INTEGER NOT NULL,
    cases_sha256   TEXT    NOT NULL,
    brief_sha256   TEXT    NOT NULL,
    failed_reviews INTEGER NOT NULL DEFAULT 0,
    tokens         INTEGER NOT NULL DEFAULT 0,
    scores_json    TEXT    NOT NULL,
    per_case_json  TEXT    NOT NULL DEFAULT '[]',
    ingested_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_bench_runs_bench_recorded_at
    ON eval_bench_runs (bench, recorded_at);
```

- `bench` names the benchmark, for example `blind-review-mechanical`, so that
  a second benchmark needs no new table.
- `repo_commit` is the bench version label that `bench.py` writes: a short
  SHA of the `agents` repo with an optional `-dirty` suffix. It is not a
  linkage key, so the 40-hex rule does not apply to it.
- `scores_json` and `per_case_json` store the producer values with their
  content unchanged. Ingest re-serializes the parsed JSON, so object keys are
  stored in sorted order and whitespace is dropped; values and array order
  stay as sent. Scores change with the bench version, and two rows do not justify one
  column per score. Readers use `json_extract`.
- No column is redacted. Bench data is synthetic and already public in the
  `agents` repo.

### 3.7 Links: sessions, commits, PRs

**Reviewing session.** The mechanical reviewer processes run with
`--no-session-persistence`, so the session that hippo can see is the agent
session that ran the review (the orchestrator). The producer reads
`CLAUDE_CODE_SESSION_ID` and writes it to the run record with
`harness = 'claude-code'`. Both columns are NULL when the review ran outside
an agent harness, and for harnesses other than Claude Code (section 8). The
join is:

```text
eval_findings.run_id -> eval_review_runs.(session_id, harness)
                     -> agentic_sessions (session_id, harness)        all segments
                     -> agentic_session_facts (session_id, harness)   one row
```

This is a soft link with no foreign key, for two reasons. First,
`agentic_sessions` is keyed by `(session_id, harness, segment_index)`, so no
single parent row exists. Second, the eval record can arrive before the
watcher ingests the session. A query that finds no session row reports the
finding as unattributed. It does not drop the finding. Values read through
this join describe the orchestrator. Reviewer values come from
`eval_review_runs`.

**Commits.** `eval_verdicts.commit_sha` holds the commit that addressed a
finding (automatic `likely-addressed`) or that a human cites. The JSON field
is `commit`. The column is `commit_sha` because `COMMIT` is an SQL keyword.
`eval_escapes.fix_commit` holds the commit that fixed an escape. Both are
full 40-hex SHAs. A short SHA that a user types for `mark --commit` or
`escape --commit` is resolved by the producer with `git rev-parse` before it
is written.

**Raw-line hash.** Commit linkage (HIPO-66) and `review_log.py reconcile`
match a finding against later file contents on `(repo, path)` plus
`line_hash`. Neither matches on `line_text`. Redacted text is unusable as a
match key: a line with a credential is stored as `[REDACTED]` text, and a
later change to `redact.toml` changes the stored form of new rows but not old
ones. Both sides of every comparison are raw source lines.

- The producer sends `line_text` as the raw source line at the reviewed
  revision: the working-tree line for side `+`, the `base_sha` line for side
  `-`. It is not the comment-stripped unit row.
- `normalize(s)` removes one trailing `\r`, then removes leading and trailing
  ASCII spaces and tabs. `line_hash = lower_hex(sha256(utf8(normalize(line_text))))`.
  The definition uses only ASCII spaces and tabs because Python `str.strip()`
  and Rust `str::trim` disagree on some Unicode whitespace.
- Test vector, required in both the Rust and the Python test suites: input
  `"    if user.is_admin or True:  \r"` normalizes to
  `"if user.is_admin or True:"` and hashes to
  `d5474129f47594d84b580d2b1d5d10e2ddd523ce5b0668b5de867807203e676c`.
- At ingest, hippo computes `line_hash` from the raw `line_text` before
  redaction. It ignores any hash the producer sends. Hippo stores the
  redacted `line_text` and the hash. It never stores the raw line.
- If `line_text` is absent, `line_hash` is NULL and linkage skips the
  finding.
- Limit: raw-to-raw comparison counts a comment-only edit on the flagged line
  as a change. The comment-stripped comparison that `reconcile` uses today
  avoids that false positive, but it cannot run in hippo.
- Limit: a plain hash lets someone who holds the database confirm a guessed
  full line. For a line that holds a credential, the guess requires the
  credential, so the hash adds no practical exposure. This is an inference,
  not a proof.

**PRs.** No eval table stores a PR. `workflow_runs` has no PR column, and a
run exists only for a commit that was a push tip in a repo with Actions. PR
attribution for a commit (`commit_sha` or `fix_commit`) follows this chain:

1. Find `workflow_runs` rows with `repo = repo_identity` and
   `head_sha = <commit>` (index `idx_workflow_runs_sha`). Take their
   `head_branch`.
2. Find `agentic_sessions` rows on that `git_branch` in the same repository,
   and read `agentic_session_facts.pr_links_json` for those sessions. A
   session can have several PR links, so the result is a set.
3. If step 1 or step 2 finds nothing, HIPO-66 can resolve the PR at linkage
   time with a `gh` lookup of the commit. This lookup is off by default and
   is enabled only by explicit configuration. It is an outbound flow: it
   sends the repository slug and the commit SHA to GitHub. HIPO-66 defines
   where a resolved PR is stored and documents the flow in the privacy
   section of its own design.

**Linkage writer constraints (for HIPO-66).** The linkage job is a new
writer. It reads open findings and runs `git` outside any transaction. It
writes automatic verdicts in `BEGIN IMMEDIATE` transactions of at most 100
rows. Its `verdict_id` is deterministic:
`auto-` + the first 16 hex characters of `sha256(finding_id | commit_sha | verdict)`.
It checks for the id before the insert, as the ingest does, so a rerun
inserts nothing new.

**Linkage as implemented (HIPO-66).** `brain/src/hippo_brain/eval_linkage.py`
runs hourly in the brain and once through
`python -m hippo_brain.eval_linkage --once`. Three readings of this section
are fixed there. First, "open" means a finding with no verdict row at all, so
the job never adds a verdict after a human one. Second, the job links only
side `+` findings: a side `-` line is not in the reviewed tree. Third, the
flagged line counts as changed only when no line of `HEAD:path` carries its
`line_hash`; the commit recorded is the first one, in
`git rev-list --reverse --topo-order --ancestry-path <start>..HEAD -- path`
order, whose file lacks the hash. The start is `git merge-base head_sha HEAD`,
which is `head_sha` itself unless the reviewed branch was squash-merged; when
`head_sha` was pruned, the start is HEAD's last commit before `started_at`
and the walk uses `--since=<started_at>`. `git log -L` is not used because
the finding's line number refers to the reviewed working tree, not to
`HEAD`. A line that never appears in the walked history (fixed before its
first commit, or fixed on a branch before a squash merge, which is the
default hippo flow) is gone at `HEAD`, so `likely-addressed` holds. The job
attributes it to the first walked commit that touches `path` and sets the
verdict `note` to the fixed marker
`inferred: line absent at walk start; first commit touching the path`, so
scorecards and humans can separate inferred commits from observed ones. An
observed fix (the line present at the start, then absent) keeps a NULL
note. When no walked commit touches `path`, the finding stays open. A resolved PR
from the opt-in `gh` lookup is stored in the automatic verdict's `note` as
`PR <url>, ...`, after the `inferred:` marker and `; ` when both apply. It is redacted and expires with the note, and the job asks
only when the chain in steps 1 and 2 finds nothing. Operation and limits:
`docs/brain-watchdog.md`.

## 4. Ingest contract: `hippo ingest agent-eval <file.jsonl>`

### 4.1 Command

A new `IngestSource::AgentEval { path }` in `crates/hippo-daemon/src/cli.rs`.
The command opens the database with `storage::open_db` and writes directly,
the same way the other `hippo ingest` subcommands reach SQLite. It is
implemented in Rust because redaction must use `RedactionEngine` and
`repo_identity` must use `derive_git_repo`. It is not a capture source: it
writes no `source_health` row, adds no watchdog invariant, and writes no
fallback JSONL. The producer's ledger is the durable copy. It holds the same
v1 records that this command reads (section 6), so a rerun of the command on
the ledger is safe and recovers any record that a failed or skipped ingest
missed.

### 4.2 Line format

- The file is UTF-8 JSONL. Each non-blank line is one JSON object. Blank
  lines are ignored and are not counted.
- Every line has `schema_version` (integer) and `type` (string). Contract
  version 1 accepts `schema_version: 1` only.
- `type` is one of `review_run`, `finding`, `verdict`, `escape`, `bench_run`.
- Every line has `id` (string, 1 to 64 characters from `[A-Za-z0-9._:-]`).
  The id becomes the table's primary key.
- All `*_at` fields are integer epoch milliseconds.
- `base_sha`, `head_sha`, `commit`, and `fix_commit` match `^[0-9a-f]{40}$`.
- Keys not defined for the type are ignored. The command logs one `warn` per
  file that lists them.
- Text caps: `claim` and `note` 4,000 characters, `line_text` 1,000
  characters. Hippo hashes the raw `line_text` first, then redacts, then
  truncates, so that a cut cannot split a credential and hide it from a
  pattern.
- A line longer than 1 MiB is rejected.

### 4.3 Fields per type

| `type` | Required | Optional | Table |
|---|---|---|---|
| `review_run` | `id`, `agent`, `mode`, `repo`, `base`, `base_sha`, `head_sha`, `input_sha256`, `started_at`, `finished_at` | `requested_model`, `model_id`, `effort`, `cli_version`, `units_total`, `units_failed`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `session_id`, `harness` | `eval_review_runs` |
| `finding` | `id`, `run_id`, `repo`, `path`, `side`, `line`, `severity`, `confidence`, `claim`, `logged_at` | `line_text`, `category` | `eval_findings` |
| `verdict` | `id`, `finding_id`, `verdict`, `auto`, `decided_at` | `commit`, `note` | `eval_verdicts` |
| `escape` | `id`, `repo`, `path`, `line`, `note`, `recorded_at` | `fix_commit` | `eval_escapes` |
| `bench_run` | `id`, `bench`, `recorded_at`, `repo_commit`, `model`, `reps`, `cases`, `cases_sha256`, `brief_sha256`, `scores` | `cli_version`, `model_ids`, `effort`, `failed_reviews`, `tokens`, `per_case` | `eval_bench_runs` |

Value rules: `side` is `+` or `-`. `severity` and `confidence` are `high`,
`medium`, or `low`. `verdict` is one of the five values in section 3.4.
`auto` is a JSON boolean. `harness` is a value that the `agentic_sessions`
`CHECK` accepts. `session_id` without `harness` is rejected. `repo` must be
an absolute path; a relative one is rejected as `invalid_field:repo`, because
`derive_git_repo` would resolve it against the ingest's working directory. `repo_identity`
is not a record field: hippo derives it from `repo` at ingest, outside the
write transaction, once per distinct `repo` value.

### 4.4 Example lines (synthetic values)

```json
{"schema_version":1,"type":"review_run","id":"7f3a9c1e2b4d5a60","agent":"blind-review","mode":"mechanical","requested_model":"haiku","model_id":"claude-haiku-5-5","effort":"low","cli_version":"2.1.310 (Claude Code)","repo":"/home/dev/example-repo","base":"main","base_sha":"0a1b2c3d4e5f60718293a4b5c6d7e8f901234567","head_sha":"1b2c3d4e5f60718293a4b5c6d7e8f9012345678a","input_sha256":"5e0f4c3b2a1908f7e6d5c4b3a2918070f6e5d4c3b2a19080706f5e4d3c2b1a09","units_total":4,"units_failed":0,"input_tokens":5200,"output_tokens":830,"cache_read_tokens":41000,"cache_creation_tokens":0,"session_id":"00000000-0000-4000-8000-000000000001","harness":"claude-code","started_at":1792900000000,"finished_at":1792900042000}
{"schema_version":1,"type":"finding","id":"a1b2c3d4e5f60718","run_id":"7f3a9c1e2b4d5a60","repo":"/home/dev/example-repo","path":"src/app.py","side":"+","line":42,"line_text":"    if user.is_admin or True:","severity":"high","confidence":"medium","category":"security","claim":"The condition is always true, so every user passes the admin check.","logged_at":1792900043000}
{"schema_version":1,"type":"verdict","id":"0f4e8a2c-6b1d-4c3e-9a7f-2d5b8c1e0a94","finding_id":"a1b2c3d4e5f60718","verdict":"accepted","commit":"1122334455667788990011223344556677889900","note":"Fixed in the same branch.","auto":false,"decided_at":1792903600000}
{"schema_version":1,"type":"escape","id":"6c9d2e1f-3a4b-4d5e-8f70-1a2b3c4d5e6f","repo":"/home/dev/example-repo","path":"src/db.py","line":17,"fix_commit":"aabbccddeeff00112233445566778899aabbccdd","note":"Off-by-one in the page size; no review flagged it.","recorded_at":1792990000000}
{"schema_version":1,"type":"bench_run","id":"3c2b1a0f9e8d7c6b","bench":"blind-review-mechanical","recorded_at":1792800000000,"repo_commit":"686e67b","cli_version":"2.1.310 (Claude Code)","model":"haiku","model_ids":["claude-haiku-5-5"],"effort":"low","reps":3,"cases":15,"cases_sha256":"15c4bffbf0dc","brief_sha256":"393aabd28d31","failed_reviews":0,"tokens":104732,"scores":{"recall":1.0,"context_required_recall":1.0,"safe_false_alarm":0.0,"extra_findings_per_run":0.044,"trap_stability":1.0,"finding_jaccard":0.889,"input_identical":true},"per_case":[{"case":"c01-py-path-guard-removed","reps":3,"trap_hits":{"guard-removed":3},"extra":0}]}
```

### 4.5 Processing, idempotency, and transactions

1. Lines are processed in file order.
2. For each record, the command selects the row with the same id inside the
   write transaction. If no row exists, it runs a plain `INSERT`. If a row
   exists and its compared fields equal the record, the line counts as
   **skipped (id exists)**. If a row exists and a compared field differs,
   the line is rejected with reason `id_conflict`.
   - The compared fields are every column-backed field of the record
     (section 4.3) except the redacted ones: `claim`, `line_text`, and
     `note`. `schema_version` and `type` have no columns and are not
     compared. Fields map to columns as in sections 3.4 and 3.7: `commit` is
     stored in `commit_sha`, and `auto` is stored as INTEGER 0 or 1. Redacted
     fields are left out because a later `redact.toml` change alters their
     stored form.
   - An optional field that the record omits compares as its column default.
   - `scores`, `per_case`, and `model_ids` compare as parsed JSON values, not
     as strings. Numbers inside those values compare by `f64` value, so `1`
     and `1.0` are equal.
   - Columns that hippo generates or derives are never compared:
     `ingested_at`, `payload_expiry`, `line_hash`, and `repo_identity`.
     `repo_identity` changes when the `origin` remote changes, and the other
     three are not record fields.
   - Producers re-ingest the whole ledger after every write, so the
     acceptance tests must include a rerun: ingest the same file twice and
     expect `written 0`, `skipped N`, `rejected 0` on the second pass, where
     N is the count of non-blank lines.
3. This check replaces `ON CONFLICT DO NOTHING`. It makes a differing record
   visible instead of silently dropping it, and it works with the
   `eval_verdicts_no_replace` trigger, which aborts any insert of an existing
   id. Records are immutable: a correction is a new verdict record, never an
   edit.
4. A `finding` whose `run_id` is absent, or a `verdict` whose `finding_id` is
   absent, is rejected with reason `unknown_parent`. A rerun on the ledger
   after the parent arrives accepts it. Producers write the parent first.
5. Writes go in `BEGIN IMMEDIATE` transactions of at most 100 lines, with the
   repository's `busy_timeout=5000`. The single writer lock makes the
   select-then-insert in step 2 free of races. Validation, hashing,
   redaction, and `derive_git_repo` run before the transaction opens. If a
   transaction fails, the command reports the error, counts its lines as
   rejected with reason `db_error`, and continues with the next batch.

### 4.6 Rejection, output, and exit codes

- A rejected line prints to stderr as
  `line <n>: rejected: <reason>`. Reasons: `invalid_json`, `not_an_object`,
  `unknown_schema_version`, `unknown_type`, `missing_field:<name>`,
  `invalid_field:<name>`, `line_too_long`, `unknown_parent`, `id_conflict`,
  `db_error`. Processing continues with the next line.
- On completion, stdout prints one line:
  `written <W>, skipped <S>, rejected <R>`.
- Exit code 0 when at least one line was written or skipped (`W + S >= 1`).
- Exit code 1 when every line was rejected, or when the file is unreadable.
  A file with no non-blank lines exits 1 because no line was accepted or
  skipped.

## 5. Privacy

### 5.1 Redaction

`RedactionEngine::redact` runs at ingest on `eval_findings.line_text`,
`eval_findings.claim`, `eval_verdicts.note`, and `eval_escapes.note`. This
matches how `user_prompts` are handled in `claude_session.rs`. `line_hash` is
computed from the raw `line_text` before redaction (section 3.7). The other
columns are identifiers, enumerated values, hashes, paths, and counts.
`category` holds an enumerated label from the reviewer schema, so it is not
redacted. It is capped at 64 characters.

### 5.2 Exclusion from derived indexes and outbound flows

Rule: no code reads `eval_*` rows into `knowledge_nodes`, `knowledge_fts`,
`knowledge_vectors`, an enrichment prompt, entity extraction, or outbound
classification. This applies HIPO-7 to a stronger degree: eval text is not
cleaned for indexing, it is never indexed.

How the current code keeps eval rows out:

- **Enrichment.** The brain claims work only through
  `brain/src/hippo_brain/enrichment_sources.py::ENRICHMENT_SOURCES`: `shell`,
  `claude`, `browser`, `workflow`, `opencode`, and `claude-auto-memory`. The
  claim functions live in `enrichment.py`, `claude_sessions.py`,
  `browser_enrichment.py`, `workflow_enrichment.py`, `opencode_sessions.py`,
  and `auto_memory.py`. No source reads `eval_*`. The ingest enqueues
  nothing.
- **Embedding.** `brain/src/hippo_brain/embeddings.py::store_node_vectors`
  embeds `knowledge_nodes` rows only. Re-embedding and the orphan reaper use
  the same boundary.
- **FTS.** `knowledge_fts` is filled only by triggers on `knowledge_nodes`.
- **Classification.** `brain/src/hippo_brain/classification.py` and `jev.py`
  select from `knowledge_nodes` only. This is the only confirmed outbound
  flow.
- **Retrieval and MCP search.** `retrieval.py`, `rag.py`, `mcp_queries.py`,
  and `agent_query.py` read knowledge nodes and the source tables they link
  to. They do not read `eval_*`.

Because eval rows never become knowledge nodes, every path above excludes
them structurally. The guard in section 5.3 keeps it that way.

**MCP is an outbound flow.** An MCP tool result goes to the model that called
the tool, which may be a cloud model. The HIPO-67 scorecard tool therefore
returns aggregates and record ids by default. It does not return `claim`,
`line_text`, or `note`. The CLI report (`hippo agents report`) runs locally
and can show the text. The optional `gh` PR lookup (section 3.7) is the
other outbound flow, and it is off by default. When
`[eval_linkage] gh_pr_lookup = true`, it sends `repo_identity` and one
commit SHA per new automatic verdict to GitHub (`gh api
repos/<slug>/commits/<sha>/pulls`). It sends no eval text. The PR URLs it
returns are stored in that verdict's `note`.

### 5.3 Enforcement in code

1. **Python guard test** (the enforcing control, because it runs in CI):
   `brain/tests/test_eval_isolation.py`. It reads every `.py` file under
   `brain/src/hippo_brain/` and fails when the text matches
   `\beval_(review_runs|findings|verdicts|escapes|bench_runs)\b`, except in an
   explicit allowlist. It also fails when any `.py` or `.sh` file under
   `scripts/` or `brain/scripts/` matches, because re-embedding and
   maintenance scripts sit on the same boundary (section 5.2). The pattern
   names the five tables exactly. A looser
   `eval_` prefix would match existing identifiers such as `eval_qa_version`
   in `bench/results_store.py`. No file matches the exact pattern today.
   The allowlist holds the
   modules that HIPO-66 and HIPO-67 add (the linkage job and the scorecard
   query module) and `schema_version.py`. The test also asserts that
   `ENRICHMENT_SOURCES` names exactly the six sources listed in section 5.2.
   A seventh source then needs a deliberate test change.
2. **Semgrep rule** `eval-table-in-derived-path` in `.semgrep.yml`, scoped
   to `enrichment*.py`, `embeddings.py`, `classification.py`, `jev.py`,
   `retrieval.py`, `rag.py`, `mcp_queries.py`, and the Python files in
   `scripts/` and `brain/scripts/`. It flags any SQL string
   that names one of the five eval tables, the same shape as
   `unfiltered-event-table-select` (AP-6). `.semgrep.yml` is not run in CI
   today, so this rule is a local and review aid. The pytest guard is the
   control that blocks a merge.
3. **Rust test** in `hippo-daemon`: after an agent-eval ingest, the row counts
   of every `*_enrichment_queue` table are unchanged.
4. **Anti-pattern entry.** HIPO-64 adds AP-14 to
   `docs/capture/anti-patterns.md`: "Reading `eval_*` rows into a derived
   index, an enrichment prompt, or an outbound flow."

### 5.4 Retention

The private free text follows the HIPO-13 90-day policy, with two pinned
exemptions that HIPO-68 needs. The structured facts stay.

- **Swept:** at `payload_expiry` (ingest time plus 90 days), GC sets
  `eval_findings.line_text`, `eval_findings.claim`, and `eval_verdicts.note`
  to NULL, except on pinned rows.
- **Pinned findings:** GC skips every finding whose effective verdict
  (section 3.4) is `accepted`. This uses the in-tree pin mechanism:
  `storage.rs::event_payload_pins` derives the set of pinned event ids inside
  each GC transaction, and `gc_event_payloads` skips those ids. HIPO-64 adds
  `eval_payload_pins(tx)`, which derives the pinned finding ids from
  `eval_verdicts` in the same transaction. The pin also covers every verdict
  of a pinned finding, so the `note` that holds the human rationale stays
  with the corpus case. No pin table is stored, so a later human verdict that
  replaces `accepted` removes the pin at the next sweep. A finding accepted
  after its text expired stays without text.
- **Pinned escapes:** every escape is pinned. This is the case where the pin
  set is the whole table, so `eval_escapes` has no `payload_expiry` and GC
  does not visit it. HIPO-68 needs the escape `note`, and an escape without
  its note is only a repo, a path, and a line.
- **Receipts:** each cleared field gets one `deletion_receipts` row through
  the existing `storage.rs::payload_receipt`. The row holds `content_hash`
  (SHA-256 of the cleared text), `ts`, reason `payload_expired`, and a
  locator in `what`, such as `eval_findings/a1b2c3d4e5f60718/claim`.
- **Mechanics:** GC uses the HIPO-13 rules. It is an explicit `--confirm`
  command. Batches hold at most 100 rows, and receipts commit in the same
  transaction as the field updates. `secure_delete=ON` is set, and
  `wal_checkpoint(TRUNCATE)` runs after the sweep. A cursor moves past
  pinned rows, as in `gc_event_payloads`, so pins cannot stall the sweep.
  HIPO-64 extends the existing payload GC command with the eval tables. It
  does not add a second command.
- **Kept with no expiry:** every other column, including `path`, `line`,
  `line_hash`, severity, category, verdicts, commits, all of
  `eval_review_runs`, and all of `eval_bench_runs`. Scorecards measure trends
  over months, and these columns hold no code text. `path` has the same
  status as the paths in `agentic_sessions.cwd` and `events.cwd`, which are
  kept. `line_hash` cannot reproduce the line, and it keeps linkage possible
  for late fixes.
- As with HIPO-13, expiry does not erase copies outside `hippo.db`: the
  producer's ledger, backups, and logs.

### 5.5 Canary test

`crates/hippo-daemon/tests/agent_eval_privacy.rs`, run against a temporary
database. It is modeled on the HIPO-8 capture canary.

1. Build a fixture JSONL with one run and one finding. The finding `claim`
   holds the synthetic token `ghp_` followed by 36 characters of
   `[A-Za-z0-9]`. The finding `line_text` holds the same token. A verdict
   `note` holds it too.
2. Run the ingest. Assert `written 3, skipped 0, rejected 0` and exit code 0.
3. Assert that `claim`, `line_text`, and `note` contain `[REDACTED]` and do
   not contain the token.
4. Assert that `line_hash` equals the SHA-256 of the normalized raw line. The
   hash is computed from the raw text, not from the redacted text.
5. Assert that `knowledge_nodes`, `knowledge_fts`, and every
   `*_enrichment_queue` have the same row counts as before the ingest. Assert
   that an FTS `MATCH` for the token prefix returns no rows.
6. Run `PRAGMA wal_checkpoint(TRUNCATE)`. Read the bytes of the database file
   and of any `-wal` file, and assert that the token bytes are absent.
7. Run the ingest again on the same file. Assert `written 0, skipped 3,
   rejected 0` and exit code 0. This is the rerun acceptance test from
   section 4.5. A second fixture with all five record types gets the same
   two-pass assertion.

A brain-side test, `brain/tests/test_eval_isolation.py`, loads the same
fixture database and runs every claim function in `ENRICHMENT_SOURCES`. It
asserts that no claim returns a row. It does not need `knowledge_vectors`,
because the Rust test proves that no node exists to embed.

## 6. Producers

The producers live in `~/projects/agents/skills/blind-review/`. HIPO-65
changes them. After this change, each producer writes v1 records (section 4)
to its own JSONL file, so the local file and the hippo input have one format.
After each write, when `hippo` is on `PATH`, the producer runs
`hippo ingest agent-eval <file>` on the whole file. A ledger holds tens to
hundreds of records, and the ingest skips known ids, so a full-file ingest is
cheap. A record written while hippo is absent enters hippo on the next
ingest.

| Producer | Writes today | Writes after HIPO-65 (v1 records) | Target file |
|---|---|---|---|
| `mechanical_review.py` | `manifest.json`, `results/*.json`, no run times | Also `run.json` in the out dir (see below) | out dir |
| `review_log.py log --out DIR` | `finding` events; no run event | One `review_run` from `run.json` and `manifest.json`, then one `finding` per finding | `findings.jsonl` ledger |
| `review_log.py mark ID STATUS` | `status` event, `auto: false`, no id | One `verdict`, `auto: false` | ledger |
| `review_log.py reconcile` | `status` events `likely-addressed`, `auto: true`, no id | One `verdict` per changed finding, `auto: true` | ledger |
| `review_log.py escape` | `escape` event, no id, no commit | One `escape` | ledger |
| `bench.py run --record` | One line in `bench/results.jsonl`, no id | One `bench_run` | `bench/results.jsonl` |

Producer changes the contract needs:

1. **`mechanical_review.py`** writes `run.json` to the out dir. It holds
   `started_at` and `finished_at` (epoch ms around the whole review),
   `requested_model`, `effort`, `base` (the ref), `base_sha` and `head_sha`
   (both from `git rev-parse` at review start), and the summed reviewer
   token usage over all units: `input_tokens`, `output_tokens`,
   `cache_read_tokens`, and `cache_creation_tokens` (from the `usage` keys
   `input_tokens`, `output_tokens`, `cache_read_input_tokens`, and
   `cache_creation_input_tokens`), and `session_id` (from
   `CLAUDE_CODE_SESSION_ID` at review start; the key is absent when that
   variable is unset). `head_sha` read later at `log` time can
   point to a later commit, so `log` must not read it. If `base` or `HEAD`
   does not resolve to a SHA, `mechanical_review.py` refuses to run, so
   `run.json` always holds both SHAs.
2. **`log`** reads `run.json`, so a second `log` of the same out dir
   produces identical records. The run id is the first 16 hex characters of
   `sha256(repo | base_sha | head_sha | input_sha256 | started_at)`. All of
   these come from the out dir, never from `now()`. The id replaces today's
   `run_id = out.name`, which is not unique when an out dir is reused.
   `session_id` comes from `run.json`. Only when `run.json` has no
   `session_id` key does `log` fall back to `CLAUDE_CODE_SESSION_ID` in its
   own environment. `harness` is set to `claude-code` whenever a
   `session_id` is written. `line_text` is the raw source
   line (section 3.7).
3. **Finding id.** The first 16 hex characters of
   `sha256(run_id | path | unit_hash | side line | claim)`. Today's id omits
   the run, so the same claim in two runs collapses into the first run and
   undercounts the second (the bench measures `finding_jaccard` at 0.889, so
   repeated claims across runs are expected). Within one run each unit is
   reviewed once, so the new key identifies one reviewer output.
4. **`mark`.** Writes a `verdict` with a random UUID `id`. The id is written
   once into the ledger, so a re-ingest reads the same id. An optional
   `--commit` argument is resolved to a full SHA with `git rev-parse` before
   it is written as `commit`, the same rule as `escape --commit`.
5. **`reconcile`.** Compares raw to raw by `line_hash`. It reads the current
   file, hashes every line with the `normalize` and SHA-256 definition from
   section 3.7, and writes `likely-addressed` when the finding's hash is
   absent. It no longer calls `strip_context.strip_source`. The shared test
   vector in section 3.7 keeps the Python and Rust hashes equal. Each
   verdict gets a random UUID `id`.
6. **`escape`.** Writes an `id` (random UUID) and takes an optional
   `--commit` argument, which it resolves to a full SHA with `git rev-parse`
   before it writes `fix_commit`. `--note` stays required.
7. **`bench.py run --record`.** Writes a `bench_run` record with `id` (first
   16 hex characters of the SHA-256 of the canonical record JSON without
   `id`) and `bench`. `scores` and `per_case` keep their shape. `summary`
   reads the v1 records. HIPO-65 rewrites the 2 existing lines once into v1
   records, with ids from the same rule over their original content.
8. **All producers.** Write epoch milliseconds for times, `schema_version: 1`,
   and the v1 `type` names. `review_log.py summary` and `bench.py summary`
   read the v1 records and emit nothing.

## 7. Changes from the starting data model

| Change | Reason |
|---|---|
| `eval_verdicts.commit` becomes column `commit_sha` (JSON field stays `commit`) | `COMMIT` is an SQL keyword. |
| Added `eval_findings.line_hash`; `line_text` is the raw source line | Linkage and `reconcile` need a key that redaction does not change, compared raw to raw. |
| Added `session_id` and `harness` to `eval_review_runs` | The reviewer process has no transcript. The orchestrating session is the only one that hippo can join. |
| Added `base_sha`, `head_sha`, `requested_model`, `effort`, token sums, `units_total`, `units_failed` to `eval_review_runs` | Linkage needs exact commits. Reviewer cost must come from the run, not from the orchestrator session. A failed unit must not read as a clean review. |
| Added `repo_identity` to runs, findings, and escapes | Joins to `workflow_runs.repo` and cross-clone scorecards need the slug. `repo` keeps the path for `git`. |
| Run id and finding id redefined; finding id includes the run id | A stable run id needs stored times. The old finding id collapsed findings across runs. |
| Added `eval_bench_runs.bench`; scores stay JSON | A second benchmark needs no new table, and two rows do not justify per-score columns. |
| Effective verdict prefers human over automatic | Plain "latest wins" lets a heuristic override a human judgment. |
| `payload_expiry` on findings and verdicts; accepted findings and their verdicts pinned; escapes never expire | HIPO-13 alignment, and HIPO-68 needs the pinned text. |
| Each record carries an explicit `id`; the ledger itself holds v1 records | Ids are the idempotency key. One format lets the ledger be re-ingested. |
| Ingest checks the id before the insert and rejects a differing duplicate | `ON CONFLICT DO NOTHING` hid differing rows, and it cannot pass the `no_replace` trigger. |
| No PR column anywhere | `workflow_runs` has no PR data. Attribution goes through branch and session PR links, or an opt-in `gh` lookup. |

## 8. Open questions

None remain. Notes on settled items:

- **Session id for other harnesses.** `session_id` and `harness` stay NULL
  for reviews run under Codex, Cursor, opencode, or Pi. The evidence needed
  to fill them is the output of `env` from a tool call in each harness, to
  find the variable that carries the session id. Collect it when a
  non-Claude reviewer is added.
- **Repository identity.** Settled as the `repo_identity` column
  (section 3.1).
- **Accepted findings and expiry.** Settled as pins (section 5.4).

## 9. Gate

This document is a decision gate. The controller posts **proceed** or
**shelve** on HIPO-63 before HIPO-64 starts. A proceed decision accepts the
recommendation in section 2 (hippo tables), the DDL in section 3, and the
contract in section 4, or names the changes it requires. A shelve decision
leaves HIPO-64, HIPO-65, and HIPO-66 blocked.
