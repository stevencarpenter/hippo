# Agent-Eval Schema and Ingest Contract: Design

**Status:** proposed, awaiting the HIPO-63 gate decision
**Date:** 2026-10-25
**Ticket:** HIPO-63 (epic HIPO-57). Implementation ticket: HIPO-64.
**Related:** HIPO-7 (derived-index exclusion), HIPO-9 (append-only feedback rows), HIPO-13 (payload expiry), HIPO-65 (producers), HIPO-66 (commit linkage), HIPO-67 (scorecards), HIPO-68 (private corpus)
**Producers (read-only reference):** `~/projects/agents/skills/blind-review/scripts/review_log.py`, `~/projects/agents/skills/blind-review/bench/bench.py`

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
  exist on this machine yet, so no legacy rows need a backfill.
- `bench/results.jsonl` holds 2 recorded runs.
- The mechanical reviewer runs `claude -p ... --no-session-persistence`
  (`mechanical_review.py::reviewer_argv`). The per-unit reviewer process
  writes no transcript, so hippo never has an `agentic_sessions` row for it.
- Claude Code exports `CLAUDE_CODE_SESSION_ID` to tool subprocesses (seen in
  this session's Bash environment).
- `.semgrep.yml` exists but no CI workflow or `mise` task runs it. Its header
  says so.
- The live schema is v27 (`storage.rs::EXPECTED_VERSION`). Phase 1 of the
  epic takes v28.

## 2. Decision: hippo tables, not a sidecar database

**Recommendation: put the five `eval_*` tables in `hippo.db`.**

A sidecar database has a precedent: `hippo-bench` keeps its results in
`~/.local/share/hippo-bench/bench-results.db` (`brain/src/hippo_brain/bench/paths.py`).
That choice was correct for hippo-bench because its rows join to nothing in
`hippo.db`. Eval rows are different.

Reasons for hippo tables:

1. **Joins are the product.** Scorecards (HIPO-67) join findings to
   `agentic_sessions` and `agentic_session_facts` (agent, model, tokens,
   cost). Commit linkage (HIPO-66) attributes PRs through
   `workflow_runs.head_sha`. A sidecar needs `ATTACH` on every reader
   connection, and cross-file foreign keys do not exist in SQLite.
2. **One privacy regime.** Redaction (`RedactionEngine`), payload expiry,
   `deletion_receipts`, and `secure_delete=ON` already exist for `hippo.db`
   (HIPO-13). A sidecar must copy all four and keep them in step.
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
EXISTS`, `PRAGMA user_version` bump. The same DDL goes into
`crates/hippo-core/src/schema.sql`. `EXPECTED_SCHEMA_VERSION` and
`ACCEPTED_READ_VERSIONS` in `brain/src/hippo_brain/schema_version.py` change
in the same commit. No existing table changes.

Conventions in all five tables:

- The primary key is the producer-generated record id (`TEXT`). It is the
  idempotency key (section 4.4).
- All timestamps are Unix epoch milliseconds (`INTEGER`).
- `ingested_at` is the time hippo wrote the row. Producer times have their own
  columns.
- `payload_expiry` follows HIPO-13: `ingested_at + 7776000000` (90 days).
  Only tables with private free text carry it.
- Columns marked "redacted" pass through `RedactionEngine::redact` before the
  insert.
- Enumerated values (`severity`, `confidence`, `verdict`) are validated by the
  ingest code, not by `CHECK`. A new value then needs a contract version, not
  a table rebuild (the v13 and v17 rebuilds show that cost). `CHECK` is used
  only for values that cannot grow: `side` and `auto`.

### 3.2 `eval_review_runs`

One row per review invocation (one `review_log.py log` call). Expected volume
is tens of rows per week. Write pattern: insert once, never updated.

```sql
CREATE TABLE IF NOT EXISTS eval_review_runs (
    run_id         TEXT    PRIMARY KEY,
    agent          TEXT    NOT NULL,
    mode           TEXT    NOT NULL,
    model_id       TEXT    NOT NULL DEFAULT '',
    effort         TEXT,
    cli_version    TEXT    NOT NULL DEFAULT '',
    repo           TEXT    NOT NULL,
    base           TEXT    NOT NULL,
    head_commit    TEXT,
    input_sha256   TEXT    NOT NULL,
    units_total    INTEGER NOT NULL DEFAULT 0,
    units_failed   INTEGER NOT NULL DEFAULT 0,
    session_id     TEXT,
    harness        TEXT,
    started_at     INTEGER NOT NULL,
    finished_at    INTEGER NOT NULL,
    ingested_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_review_runs_agent_started_at
    ON eval_review_runs (agent, started_at);
CREATE INDEX IF NOT EXISTS idx_eval_review_runs_session_id_harness
    ON eval_review_runs (session_id, harness) WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_eval_review_runs_repo_started_at
    ON eval_review_runs (repo, started_at);
```

- `agent` is the reviewing agent name, for example `blind-review`.
  `mode` is for example `mechanical`.
- `model_id` is the sorted, comma-joined set of served model ids, the same
  form `mechanical_review.py` writes per unit.
- `head_commit` is `git rev-parse HEAD` in the reviewed repo at review time.
  It is NULL when the repo has no commits. Commit linkage looks only at
  commits after it.
- `input_sha256` is the SHA-256 of the sorted per-unit `input_sha256` values.
- `units_failed` keeps a failed review distinct from a clean one with zero
  findings. `review_log.py log` already treats that difference as an error.
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
- `repo` repeats the run's value so that linkage can use
  `idx_eval_findings_repo_path` without a join.
- `line_hash` is defined in section 3.7.

### 3.4 `eval_verdicts`

Append-only, one row per verdict event. Expected volume is 0 to 3 rows per
finding. Write pattern: insert only. Triggers reject `DELETE` and any
`UPDATE` except the payload-expiry update of `note`. This is the
`retrieval_events` pattern from HIPO-9.

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
BEFORE UPDATE OF verdict_id, finding_id, verdict, commit_sha, auto, decided_at, ingested_at
ON eval_verdicts BEGIN
    SELECT RAISE(ABORT, 'eval_verdicts are append-only');
END;
```

- Redacted: `note`.
- Allowed `verdict` values in contract version 1: `accepted`,
  `false-positive`, `not-useful`, `duplicate`, `likely-addressed`.
- `auto = 1` marks a machine verdict (`review_log.py reconcile`, the HIPO-66
  linkage job). `auto = 0` marks a human verdict (`review_log.py mark`).

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
manual and rare (a few per month). Write pattern: insert once, then the
payload-expiry partial `UPDATE` of `note`.

```sql
CREATE TABLE IF NOT EXISTS eval_escapes (
    escape_id      TEXT    PRIMARY KEY,
    repo           TEXT    NOT NULL,
    path           TEXT    NOT NULL,
    line           INTEGER NOT NULL,
    fix_commit     TEXT,
    note           TEXT,
    recorded_at    INTEGER NOT NULL,
    ingested_at    INTEGER NOT NULL,
    payload_expiry INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_escapes_repo_path
    ON eval_escapes (repo, path);
CREATE INDEX IF NOT EXISTS idx_eval_escapes_payload_expiry
    ON eval_escapes (payload_expiry) WHERE note IS NOT NULL;
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
- `scores_json` and `per_case_json` store the producer objects as they are.
  Scores change with the bench version, and two rows do not justify one
  column per score. Readers use `json_extract`.
- No column is redacted. Bench data is synthetic and already public in the
  `agents` repo.

### 3.7 Links: sessions, commits, PRs

**Reviewing session.** The mechanical reviewer processes run with
`--no-session-persistence`, so the session that hippo can see is the agent
session that ran the review. The producer reads `CLAUDE_CODE_SESSION_ID` and
writes it to the run record with `harness = 'claude-code'`. Both columns are
NULL when the review ran outside an agent harness. The join is:

```text
eval_findings.run_id -> eval_review_runs.(session_id, harness)
                     -> agentic_sessions (session_id, harness)        all segments
                     -> agentic_session_facts (session_id, harness)   one row
```

This is a soft link with no foreign key, for two reasons. First,
`agentic_sessions` is keyed by `(session_id, harness, segment_index)`, so no
single parent row exists. Second, the eval record can arrive before the
watcher ingests the session. A query that finds no session row reports the
finding as unattributed. It does not drop the finding.

**Commits.** `eval_verdicts.commit_sha` holds the commit that addressed a
finding (automatic `likely-addressed`) or that a human cites. The JSON field
is `commit`. The column is `commit_sha` because `COMMIT` is an SQL keyword.
`eval_escapes.fix_commit` holds the commit that fixed an escape.

**Raw-line hash.** Commit linkage (HIPO-66) matches a finding to a later
commit on `(repo, path)` plus `line_hash`. It never matches on `line_text`.
Redacted text is unusable as a match key: a line with a credential is
stored as `[REDACTED]` text, and a later change to `redact.toml` changes the
stored form of new rows but not old ones.

- The producer sends `line_text` as the raw source line at the reviewed
  revision: the working-tree line for side `+`, the `base` line for side `-`.
  This is not the comment-stripped unit row that `review_log.py` keeps
  today (section 6).
- At ingest, before redaction, hippo computes
  `line_hash = lower_hex(sha256(utf8(normalize(line_text))))`.
  `normalize` removes a trailing `\r` and then leading and trailing
  whitespace. The same function, in one place in `hippo-core`, serves the
  ingest and the linkage job.
- Hippo stores the redacted `line_text` and the hash. It never stores the raw
  line.
- If `line_text` is absent, `line_hash` is NULL and linkage skips the
  finding.
- Limit: a plain hash lets someone who holds the database confirm a guessed
  full line. For a line that holds a credential, the guess requires the
  credential, so the hash adds no practical exposure. This is an inference,
  not a proof.

**PRs.** No eval table stores a PR. A session can have several PR links
(`agentic_session_facts.pr_links_json` is an array), and `pr-link` records
exist in only 48 transcript files. PR attribution therefore comes from the
commit: `eval_verdicts.commit_sha` and `eval_escapes.fix_commit` join to
`workflow_runs.head_sha` (indexed by `idx_workflow_runs_sha`) at query time.
The session PR links are a secondary source when the commit has no workflow
run.

**Linkage writer constraints (for HIPO-66).** The linkage job is a new
writer. It reads open findings and runs `git` outside any transaction. It
writes automatic verdicts in `BEGIN IMMEDIATE` transactions of at most 100
rows. Its `verdict_id` is deterministic:
`auto-` + the first 16 hex characters of `sha256(finding_id | commit_sha | verdict)`.
A rerun then inserts nothing new.

## 4. Ingest contract: `hippo ingest agent-eval <file.jsonl>`

### 4.1 Command

A new `IngestSource::AgentEval { path }` in `crates/hippo-daemon/src/cli.rs`.
The command opens the database with `storage::open_db` and writes directly,
the same way the other `hippo ingest` subcommands reach SQLite. It is
implemented in Rust because redaction must use `RedactionEngine`. It is not
a capture source: it writes no `source_health` row, adds no watchdog
invariant, and writes no fallback JSONL. The producer's own JSONL file is the
durable copy, and a rerun of the command is safe.

### 4.2 Line format

- The file is UTF-8 JSONL. Each non-blank line is one JSON object. Blank
  lines are ignored and are not counted.
- Every line has `schema_version` (integer) and `type` (string). Contract
  version 1 accepts `schema_version: 1` only.
- `type` is one of `review_run`, `finding`, `verdict`, `escape`, `bench_run`.
- Every line has `id` (string, 1 to 64 characters from `[A-Za-z0-9._:-]`).
  The id becomes the table's primary key.
- All `*_at` fields are integer epoch milliseconds.
- Keys not defined for the type are ignored. The command logs one `warn` per
  file that lists them.
- Text caps: `claim` and `note` 4,000 characters, `line_text` 1,000
  characters. Hippo redacts first and truncates second, so that a cut cannot
  split a credential and hide it from a pattern.
- A line longer than 1 MiB is rejected.

### 4.3 Fields per type

| `type` | Required | Optional | Table |
|---|---|---|---|
| `review_run` | `id`, `agent`, `mode`, `repo`, `base`, `input_sha256`, `started_at`, `finished_at` | `model_id`, `effort`, `cli_version`, `head_commit`, `units_total`, `units_failed`, `session_id`, `harness` | `eval_review_runs` |
| `finding` | `id`, `run_id`, `repo`, `path`, `side`, `line`, `severity`, `confidence`, `claim`, `logged_at` | `line_text`, `category` | `eval_findings` |
| `verdict` | `id`, `finding_id`, `verdict`, `auto`, `decided_at` | `commit`, `note` | `eval_verdicts` |
| `escape` | `id`, `repo`, `path`, `line`, `recorded_at` | `fix_commit`, `note` | `eval_escapes` |
| `bench_run` | `id`, `bench`, `recorded_at`, `repo_commit`, `model`, `reps`, `cases`, `cases_sha256`, `brief_sha256`, `scores` | `cli_version`, `model_ids`, `effort`, `failed_reviews`, `tokens`, `per_case` | `eval_bench_runs` |

Value rules: `side` is `+` or `-`. `severity` and `confidence` are `high`,
`medium`, or `low`. `verdict` is one of the five values in section 3.4.
`auto` is a JSON boolean. `harness` is a value that the `agentic_sessions`
`CHECK` accepts. `session_id` without `harness` is rejected.

### 4.4 Example lines (synthetic values)

```json
{"schema_version":1,"type":"review_run","id":"run-7f3a9c1e2b4d","agent":"blind-review","mode":"mechanical","model_id":"claude-haiku-5-5","effort":"low","cli_version":"2.1.310 (Claude Code)","repo":"/home/dev/example-repo","base":"main","head_commit":"0a1b2c3d4e5f60718293a4b5c6d7e8f901234567","input_sha256":"5e0f4c3b2a1908f7e6d5c4b3a2918070f6e5d4c3b2a19080706f5e4d3c2b1a09","units_total":4,"units_failed":0,"session_id":"00000000-0000-4000-8000-000000000001","harness":"claude-code","started_at":1792900000000,"finished_at":1792900042000}
{"schema_version":1,"type":"finding","id":"a1b2c3d4e5","run_id":"run-7f3a9c1e2b4d","repo":"/home/dev/example-repo","path":"src/app.py","side":"+","line":42,"line_text":"    if user.is_admin or True:","severity":"high","confidence":"medium","category":"security","claim":"The condition is always true, so every user passes the admin check.","logged_at":1792900043000}
{"schema_version":1,"type":"verdict","id":"v-9d8c7b6a5f4e","finding_id":"a1b2c3d4e5","verdict":"accepted","commit":"1122334455667788990011223344556677889900","note":"Fixed in the same branch.","auto":false,"decided_at":1792903600000}
{"schema_version":1,"type":"escape","id":"e-4f5e6d7c8b9a","repo":"/home/dev/example-repo","path":"src/db.py","line":17,"fix_commit":"aabbccddeeff00112233445566778899aabbccdd","note":"Off-by-one in the page size; no review flagged it.","recorded_at":1792990000000}
{"schema_version":1,"type":"bench_run","id":"bench-3c2b1a0f9e8d","bench":"blind-review-mechanical","recorded_at":1792800000000,"repo_commit":"686e67b","cli_version":"2.1.310 (Claude Code)","model":"haiku","model_ids":["claude-haiku-5-5"],"effort":"low","reps":3,"cases":15,"cases_sha256":"15c4bffbf0dc","brief_sha256":"393aabd28d31","failed_reviews":0,"tokens":104732,"scores":{"recall":1.0,"context_required_recall":1.0,"safe_false_alarm":0.0,"extra_findings_per_run":0.044,"trap_stability":1.0,"finding_jaccard":0.889,"input_identical":true},"per_case":[{"case":"c01-py-path-guard-removed","reps":3,"trap_hits":{"guard-removed":3},"extra":0}]}
```

### 4.5 Processing, idempotency, and transactions

1. Lines are processed in file order.
2. Each record inserts with `INSERT ... ON CONFLICT (<id column>) DO NOTHING`.
   A conflict counts as **skipped (id exists)**. Records are immutable, so
   this is the case that AP-12 permits: the key identifies the whole row. A
   correction is a new verdict record, never an edit.
3. A `finding` whose `run_id` is absent, or a `verdict` whose `finding_id` is
   absent, is rejected with reason `unknown_parent`. A rerun after the parent
   arrives accepts it. Producers write the parent first in the same file.
4. Writes go in `BEGIN IMMEDIATE` transactions of at most 100 lines, with the
   repository's `busy_timeout=5000`. Validation and redaction run before the
   transaction opens. If a transaction fails, the command reports the error,
   counts its lines as rejected with reason `db_error`, and continues with the
   next batch.

### 4.6 Rejection, output, and exit codes

- A rejected line prints to stderr as
  `line <n>: rejected: <reason>`. Reasons: `invalid_json`, `not_an_object`,
  `unknown_schema_version`, `unknown_type`, `missing_field:<name>`,
  `invalid_field:<name>`, `line_too_long`, `unknown_parent`, `db_error`.
  Processing continues with the next line.
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
and can show the text.

### 5.3 Enforcement in code

1. **Python guard test** (the enforcing control, because it runs in CI):
   `brain/tests/test_eval_isolation.py`. It reads every `.py` file under
   `brain/src/hippo_brain/` and fails when the text matches
   `\beval_(review_runs|findings|verdicts|escapes|bench_runs)\b`, except in an
   explicit allowlist. The pattern names the five tables exactly. A looser
   `eval_` prefix would match existing identifiers such as `eval_qa_version`
   in `bench/results_store.py`. No file matches the exact pattern today. The allowlist holds the
   modules that HIPO-66 and HIPO-67 add (the linkage job and the scorecard
   query module) and `schema_version.py`. The test also asserts that
   `ENRICHMENT_SOURCES` names exactly the six sources listed in section 5.2.
   A seventh source then needs a deliberate test change.
2. **Semgrep rule** `eval-table-in-derived-path` in `.semgrep.yml`, scoped
   to `enrichment*.py`, `embeddings.py`, `classification.py`, `jev.py`,
   `retrieval.py`, `rag.py`, and `mcp_queries.py`. It flags any SQL string
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

The private free text follows the HIPO-13 90-day policy. The structured facts
stay.

- At `payload_expiry` (ingest time plus 90 days), GC sets
  `eval_findings.line_text`, `eval_findings.claim`, `eval_verdicts.note`, and
  `eval_escapes.note` to NULL. It writes one `deletion_receipts` row per
  cleared field, with reason `payload_expired` and a locator such as
  `eval_findings/a1b2c3d4e5/claim`.
- GC uses the HIPO-13 mechanics: an explicit `--confirm` command, batches of
  at most 100 rows, receipts in the same transaction, `secure_delete=ON`, and
  `wal_checkpoint(TRUNCATE)` after the sweep. HIPO-64 extends the existing
  payload GC command with the eval tables. It does not add a second command.
- Kept with no expiry: every other column, including `path`, `line`,
  `line_hash`, severity, category, verdicts, commits, and all of
  `eval_review_runs` and `eval_bench_runs`. Scorecards measure trends over
  months, and these columns hold no code text. `path` has the same status as
  the paths in `agentic_sessions.cwd` and `events.cwd`, which are kept.
- `line_hash` is kept after the text expires. It cannot reproduce the line.
  Linkage stays possible for late fixes.
- As with HIPO-13, expiry does not erase copies outside `hippo.db`: the
  producer's own ledger, backups, and logs.

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
7. Run the ingest again. Assert `written 0, skipped 3, rejected 0` and exit
   code 0.

A brain-side test, `brain/tests/test_eval_isolation.py`, loads the same
fixture database and runs every claim function in `ENRICHMENT_SOURCES`. It
asserts that no claim returns a row. It does not need `knowledge_vectors`,
because the Rust test proves that no node exists to embed.

## 6. Producers

The producers live in `~/projects/agents/skills/blind-review/`. HIPO-65
changes them. Each subcommand keeps its local JSONL as the audit trail and
calls `hippo ingest agent-eval` when hippo is installed.

| Producer | Emits today (local file) | Emits for hippo |
|---|---|---|
| `review_log.py log --out DIR` | `finding` events, one per finding; no run event | One `review_run`, then one `finding` per finding |
| `review_log.py mark ID STATUS` | `status` event, `auto: false`, no id | One `verdict`, `auto: false` |
| `review_log.py reconcile` | `status` events `likely-addressed`, `auto: true`, no id | One `verdict` per changed finding, `auto: true`, `commit` absent |
| `review_log.py escape` | `escape` event, no id, no commit | One `escape` |
| `bench.py run --record` | One line in `bench/results.jsonl`, no id | One `bench_run` |

Producer changes the contract needs:

1. **`log`.** Emit a `review_run` record. Its `id` replaces today's
   `run_id = out.name`, which is not unique when an out dir is reused. Derive
   it as the first 16 hex characters of
   `sha256(repo | base | input_sha256 | started_at)`. Read `session_id` from
   `CLAUDE_CODE_SESSION_ID` and set `harness` to `claude-code` when the
   variable is set. Record `head_commit`, `units_total`, and `units_failed`.
   Send the raw source line as `line_text`. Keep the current 10-hex finding
   id, which hashes the repo, path, unit, side, line, and claim.
2. **`mark` and `reconcile`.** Add an `id` to each status event when it is
   written. A random UUID is enough, because the id lives in the ledger line
   and a re-ingest reads the same id. Map `status` to `verdict` and `at` to
   `decided_at`.
3. **`escape`.** Add an `id` and an optional `--commit` argument for
   `fix_commit`.
4. **`bench.py run --record`.** Add `id` (first 16 hex characters of the
   SHA-256 of the canonical record JSON) and `bench`. Map `scores` and
   `per_case` as they are. The two existing lines get ids by the same rule
   when they are ingested.
5. **All producers.** Convert ISO 8601 times to epoch milliseconds, and
   write `schema_version: 1`.

The `summary` subcommand only reads, so it emits nothing. `bench.py summary`
also only reads.

## 7. Changes from the starting data model

| Change | Reason |
|---|---|
| `eval_verdicts.commit` becomes column `commit_sha` (JSON field stays `commit`) | `COMMIT` is an SQL keyword. |
| Added `eval_findings.line_hash` | Commit linkage needs a key that redaction does not change (HIPO-63 review input). |
| `line_text` is the raw source line, not the comment-stripped unit row | Hippo hashes it, and the linkage job reads raw file contents at later commits. |
| Added `eval_review_runs.session_id`, `harness` | The reviewer process has no transcript. The invoking session is the only one that hippo can join. |
| Added `eval_review_runs.head_commit`, `units_total`, `units_failed` | Linkage needs a start point. A failed unit must not read as a clean review. |
| Added `eval_bench_runs.bench`; scores stay JSON | A second benchmark needs no new table, and two rows do not justify per-score columns. |
| Effective verdict prefers human over automatic | Plain "latest wins" lets a heuristic override a human judgment. |
| Added `payload_expiry` on the three tables with private text | HIPO-13 alignment. |
| Each record carries an explicit `id`; escapes, verdicts, and bench runs get ids they lack today | Ids are the idempotency key. |
| No PR column anywhere | Sessions can have several PRs and `pr-link` is sparse. PR attribution goes through the commit. |

## 8. Open questions

1. **Accepted findings and the 90-day expiry.** HIPO-68 builds a private
   corpus from accepted findings and escapes, and needs `claim` and
   `line_text`. Either HIPO-68 copies them within 90 days, or accepted
   findings get a pin that exempts them from expiry. Evidence that settles
   it: the HIPO-68 design, specifically whether its corpus builder runs on a
   schedule shorter than 90 days.
2. **Repository identity.** `repo` is the local absolute path, as
   `review_log.py` writes it. A moved or second clone splits one repository
   into two keys. `auto_memory_ingest.py::derive_repository_identity` already
   maps a path to a sanitized remote. Evidence that settles it: whether the
   HIPO-67 scorecards group by repository across clones. If they do, store
   the derived identity in a second column at ingest.
3. **Session id for other harnesses.** `CLAUDE_CODE_SESSION_ID` covers Claude
   Code only. Evidence that settles it: the environment that Codex, Cursor,
   and opencode give tool subprocesses, checked when a non-Claude reviewer
   is added. Until then, `session_id` is NULL for those runs.

## 9. Gate

This document is a decision gate. The controller posts **proceed** or
**shelve** on HIPO-63 before HIPO-64 starts. A proceed decision accepts the
recommendation in section 2 (hippo tables), the DDL in section 3, and the
contract in section 4, or names the changes it requires. A shelve decision
leaves HIPO-64, HIPO-65, and HIPO-66 blocked.
