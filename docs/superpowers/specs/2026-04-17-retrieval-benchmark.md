# Retrieval Benchmark — sqlite-vec Hybrid vs LanceDB

**Date:** 2026-04-17
**Reviewer:** reviewer agent
**Branch:** `postgres`
**Eval set:** `brain/tests/eval_questions.json` (10 questions)

## Methodology

For each question we planned to run:

1. **OLD path (LanceDB semantic-only)** — the pre-consolidation retrieval path.
2. **NEW path (retrieval.search hybrid mode)** — RRF + MMR over vec0 + FTS5.

Top-5 results per path were to be scored subjectively: `relevant` / `partial` / `off-topic`.

## Scope limitation

The LanceDB write path was removed in this branch (spec "Open questions
answered" — cold-turkey switch, no dual-write) and the running brain on the
`main` branch still holds a live LanceDB index but cannot be reached from this
worktree without a costly re-embed. As a result, this benchmark reports only
the **NEW path** numbers and uses the question set as a sanity-check for the
new stack rather than a head-to-head comparison.

The acceptance criterion #12 ("hybrid ≥ LanceDB on 10-Q eval set") is marked
**partial** in the scorecard with this explicit limitation. A full A/B should
be rerun once an enrichment + embedding pass has populated the new SQLite DB
with a corpus comparable to what main has indexed in LanceDB.

## Dry-run observations (static — no live corpus)

Running `retrieval.search(..., mode="hybrid")` against a populated DB was not
possible at review time: the `postgres`-branch daemon has not yet re-enriched
a corpus (the backfill script `migrate-vectors.py` has not been run, and the
fresh DB has no historical content). The integration test
(`test_integration_sqlite_vec.py`) confirms the mechanical path — all 4 modes
return `[0, 1]`-scored, deduplicated results with `uuid` and `linked_event_ids`
present — but it does not exercise semantic relevance against the real
corpus.

## Known retrieval pathologies discovered during this review

These findings would distort a real benchmark and must be addressed before a
full A/B:

1. **FTS5 punctuation crash (task #8)** — Any question ending in `?` triggers
   `sqlite3.OperationalError: fts5: syntax error near "?"` in lexical or
   hybrid mode. Eight of the ten eval questions end with `?`; seven of them
   contain other FTS5-significant characters (`-` in "sqlite-vec", `.` in
   "rag.ask", etc.). **A benchmark run today would show the new stack
   returning zero results — or the degraded RAG path — for almost every
   question.** This is the single largest blocker for a meaningful benchmark.

2. **Project filter semantic mismatch (task #9)** — Not exercised by the eval
   questions (none specify a project filter) but would show empty or
   misleading results for any filter-scoped query.

## Recommendation

- Fix tasks #8 and #9 first.
- Run `migrate-vectors.py` to backfill the new DB from recent enrichment.
- Re-run this benchmark with both paths active (requires preserving a
  LanceDB-populated checkout or temporarily re-enabling the old write path
  for a one-shot dual-index run).

Until then, the acceptance of the retrieval direction is based on the
mechanical integration test + the shape assertions (scores in `[0, 1]`,
no duplicates, filters + uuids plumbed) — not on semantic quality.
