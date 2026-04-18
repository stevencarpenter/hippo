# Design: Wave B — sqlite-vec Rollout & Productionization

**Status:** Draft for approval
**Author:** Claude (Sonnet 4.6) + Steven
**Date:** 2026-04-18
**Branch:** `sqlite-vec-rollout` (renamed from `postgres`; historical commits still reference the old name)
**Predecessor:** `2026-04-17-sqlite-vec-consolidation-design.md` (Wave A / Wave 1)

> **Revision 2026-04-18 (post main merge):** After merging `origin/main` into this branch, **R-22 (enrichment watchdog, #23)** and **R-23 (events.git_repo backfill, #25)** are already landed. This reduces the Phase 1 merge-blocker set from five to **three**: R-01, R-06, R-16. Tasks 1.4 and 1.5 in the plan collapse to audit-and-verify.

## Motivation

Wave A (Wave 1 in prior naming) landed the sqlite-vec + FTS5 storage engine, hybrid
RRF+MMR retrieval, filter-aware MCP surface, and `ask()` reliability fixes on the
`postgres` branch. Wave 2 produced a corpus health report, 40-question eval harness,
and risk register. All artifacts live at `docs/superpowers/specs/2026-04-17-*.md`.

The branch is **not yet merged to main.** Wave A is architecturally sound but has
six HIGH-severity risks filed against it, a live corpus that is schema v5 (branch is
v6), and an enrichment queue that is currently wedged in production. Landing the
branch today would ship known bugs and leave the live DB in a half-migrated state.

Wave B is the path from "branch works in isolation" to "main is on sqlite-vec, live
DB is healthy, retrieval quality is proven." It is not new architecture; it is the
remaining work to cut over safely.

## Scope

**In scope**

- Fix the three still-outstanding HIGH-severity merge-blockers (R-01, R-06, R-16) and audit the two already-landed fixes (R-22, R-23) for correctness against the risk register's scenarios
- Add a schema v5→v6 forward migration usable by existing deployments
- Re-embed the live corpus into the new `knowledge_vectors` (vec0) table
- Clean up pre-cutover queue state: orphan `processing` locks, failed-row retry policy
- Retrospective noise cleanup: purge historical session-lifecycle knowledge_nodes that the new `is_enrichment_eligible` filter would have blocked
- Drop the deprecated `relationships` table and its MCP endpoints in schema v7
- Baseline the eval harness on the live corpus pre- and post-cutover to prove no regression
- Rename branch `postgres` → `sqlite-vec-rollout` (cosmetic; local-only, no remote yet)

**Out of scope (post-merge follow-ups)**

- Connection pool for sqlite-vec-loaded connections (R-05)
- Move `ensure_vec_table` DDL to `_init_state` (R-04, R-13)
- Schema v7 summary column denormalization (R-07 mitigation)
- Redaction pattern expansion (`sk-*`, `glpat-*`, connection strings)
- Telemetry/spans on `retrieval.search` (R-10)
- Path-traversal semgrep warning triage in `storage.rs`
- Cross-encoder rerank, entity graph traversal, embedding fine-tuning

**Explicitly rejected**

- Postgres migration. The branch name was an early-exploration artifact; the data-store direction is sqlite-vec. The branch will be renamed before merge.

## Architecture

### B.1 — Merge-blockers

Three changes still to author; two to audit (already in main via #23 and #25).

**To author:**

| ID | Change | Files | Status |
|---|---|---|---|
| R-01 | Pipe filter kwargs through `mcp.ask()` to `rag_ask()` using the same `_parse_since_ms` + `Filters(...)` pattern already in `_retrieve_filtered` | `brain/src/hippo_brain/mcp.py:306-317` | **open** (TODO block confirmed present 2026-04-18) |
| R-06 | Embedding-model drift guard: compare stored `embed_model` on `knowledge_nodes` against live config at vec0 open; refuse writes on mismatch. Note: current code uses `_pad_or_truncate` which *hides* drift rather than surfacing it — remove the silent coercion too. | `brain/src/hippo_brain/embeddings.py`, `vector_store.py` | **open** |
| R-16 | Canonicalization is partially there (`name.lower().strip()` at `enrichment.py:99`) but doesn't collapse worktree-prefix variants, which is the primary observed fragmentation (`storage.rs` × 8 in live corpus). Extend the canonicalizer + backfill merge existing duplicates. | `brain/src/hippo_brain/enrichment.py`, `entity_resolver.py` (new), backfill script | **partial — extend + dedup** |

**To audit (already landed in main):**

| ID | Landed as | Audit task |
|---|---|---|
| R-22 | `450ee38 feat(brain): enrichment queue watchdog (reaper + preflight + claim cap) (#23)` | Read watchdog.py; confirm per-claim timeout, failure-reason tagging, concurrent-claim cap, and startup reconciliation all match the R-22 scenario. File gaps as separate tasks. |
| R-23 | `eccf617 fix(daemon): populate events.git_repo from cwd remote (#25)` | Read git_repo.rs; confirm the remote-based resolution handles worktrees correctly (both `hippo` and `hippo-postgres` should resolve to the same `git_repo`). Confirm a backfill for the 6,790 existing NULL rows exists or is filed. |

R-01 in particular needs a regression test asserting zero sources on a nonexistent-project filter. R-16 needs a test asserting that two variants of the same file collapse to one entity row.

### B.2 — Cutover

The live DB at `~/.local/share/hippo/hippo.db` is schema v5 (LanceDB-backed).
Branch is v6 (sqlite-vec + FTS5). Migration must be forward-only and idempotent.

**Migration script:** `brain/scripts/migrate-v5-to-v6.py` (new)

Phases inside the script:

1. **Preflight.** Read current `schema_version` PRAGMA. Abort unless `== 5`. Check
   `hippo doctor` says daemon and brain are stopped. Take a `.backup` of `hippo.db`
   to `hippo.db.v5-backup-<ts>` using SQLite `.backup` (not file copy — WAL-safe).
2. **Schema forward.** Apply the v6 DDL from `crates/hippo-core/src/schema.sql`
   (virtual tables + triggers). Bump `schema_version` to 6.
3. **Queue cleanup.** Release all `processing` locks older than 5 minutes (reset to
   `pending`). For rows in `failed` with `retry_count < 3` and
   `last_failure_ts > 24h ago`, reset to `pending`. Permanently skip older failures
   (keep in table for diagnostics; mark `giveup=1`).
4. **Noise cleanup.** For every existing knowledge_node, re-run `is_enrichment_eligible`
   against its source event. Hard-delete nodes where the check now returns false;
   their vector + FTS rows cascade via triggers.
5. **Re-embed.** Use the existing `migrate-vectors.py` (R-15: currently serial; acceptable
   for a one-shot against ~1.9K live nodes at ~200ms each = ~6 min). Parallelize only
   if that estimate busts. Write to the new `knowledge_vectors` vec0 table.
6. **Relationships drop (schema v7 preview).** Conditional on user approval in the plan:
   include a v6→v7 bump that DROPs the `relationships` table and removes its MCP tool
   endpoints. Confirmed deprecated (not un-wired) per 2026-04-17 amendment. Alternative:
   defer v7 to a separate PR.
7. **Verify.** Run `hippo doctor`; assert node count == vec0 row count == FTS row
   count; assert no `processing > 5min` left; assert new writes land in all three
   tables via a synthetic enrichment.

**Rollback:** restore the `.backup` snapshot. No in-place rollback after step 5
(re-embed is destructive of the LanceDB artifacts). Document this in the migration
script docstring and require `--i-have-a-backup` flag to proceed past step 4.

### B.3 — Validation

**Pre-cutover baseline.** Run `hippo-eval` on the live v5 corpus with `--mode semantic`
(main-branch compatibility mode from the merged `evaluation.py`). Archive the scorecard
as `docs/superpowers/specs/2026-04-18-eval-baseline-pre-cutover.md`.

**Post-cutover eval.** Run `hippo-eval` on the migrated v6 corpus with `--mode hybrid`
(full RRF+MMR). Archive as `2026-04-18-eval-baseline-post-cutover.md`.

**Gate.** Post-cutover Recall@10 must be ≥ pre-cutover Recall@10 on the shared subset of
the 40-Q set (not all 40 are comparable: some probe features that only exist post-cutover,
like `search_hybrid`). Regression on any HIGH-priority question is a merge blocker.

LanceDB side is not re-runnable on this branch (removed), so the "did we beat LanceDB?"
question is answered only if the user chooses to run pre-cutover eval against a
main-branch worktree — filed as optional.

## Success criteria

- [ ] R-01, R-06, R-16 fixed with regression tests; R-22 and R-23 audited against their original scenarios, any gaps filed
- [ ] Migration script runs end-to-end on a copy of the live DB with zero manual intervention
- [ ] Post-cutover `hippo doctor` passes; node/vec/FTS row counts match
- [ ] Post-cutover eval scorecard shows no Recall@10 regression vs. pre-cutover baseline
- [ ] Live corpus enrichment coverage returns to ≥88% within 24h of cutover (the level observed before LM Studio wedged)
- [ ] Branch renamed to `sqlite-vec-rollout`; PR opened against `main`; sign-off from reviewer
- [ ] `relationships` table and endpoints removed in schema v7 (if scoped in, else filed)
- [ ] Session handoff updated with post-merge state

## Risks & mitigations (Wave B itself)

- **Migration script bug corrupts live DB.** Mitigate with mandatory `.backup` snapshot + `--i-have-a-backup` flag. Practice the script on a full copy before touching real data.
- **LM Studio is still wedged at cutover time.** R-22 fix (watchdog) must land first, then manually kick LM Studio, then verify coverage recovers on v5 before migrating. Don't cut over onto a broken enrichment pipeline.
- **Re-embed takes longer than estimated** (6 min assumption could be 30 min if LM Studio is degraded). Run it as a background task, poll with `sqlite3` count checks; don't block user on the terminal.
- **Eval harness false-regression** due to different retrieval modes. Restrict the post/pre comparison to questions that are valid in `semantic` mode; document excluded questions.
- **Deprecated `relationships` drop breaks a consumer we missed.** Before v7 migration, grep the whole codebase + MCP tool list + agent skills for `relationships` references. If scope is uncertain, defer v7 to a follow-up PR (Wave B still passes without it).

## Team structure

Proposed team: `hippo-wave-b`. Four agents, two waves of execution.

- **B.1 parallel:** `mergeblocker-filters` (R-01), `mergeblocker-safety` (R-06), `mergeblocker-dedup` (R-16), `auditor` (R-22 + R-23 verification)
- **B.2 sequential:** `migration-author` (writes + dry-runs script), then `cutover-operator` (runs it on live DB with user-in-the-loop approval at each phase)
- **B.3 sequential:** `eval-runner` (pre/post scorecards), `reviewer` (final sign-off, updates session handoff)

Reviewer stays live throughout (same pattern as Wave A).

## Open questions

1. **Schema v7 scope:** drop `relationships` in the same PR as the cutover, or separate follow-up? Separate is safer (smaller blast radius) but leaves the vestige in main longer. **Recommend: separate PR after cutover lands and is stable for 48h.**
2. **Pre-cutover eval against main worktree:** do we care enough to prove sqlite-vec beats LanceDB numerically, or is forward-only comparison sufficient? **Recommend: forward-only. The qualitative wins (filter pushdown, hybrid, MMR) are the point.**
3. **Branch rename timing:** rename before PR or as part of PR? **Recommend: rename now (local-only, no remote tracking), PR will open from the renamed branch.**
4. **Approval gates in the migration script:** interactive prompts between phases, or one-shot with flags? **Recommend: phase-gated with `--yes-i-backed-up`, `--yes-drop-noise`, `--yes-reembed` flags so a dry-run can exit early and a real run is explicit.**
