# Session Handoff: Wave B Complete

**Purpose:** allow a fresh Claude Code session to pick up after Wave B lands on main.
**Last updated:** 2026-04-18 — team-lead
**Branch:** `sqlite-vec-rollout` (formerly `postgres`) on `/Users/carpenter/projects/hippo-postgres`
**Team name:** `hippo-wave-b`
**Supersedes:** `2026-04-17-session-handoff.md`

## What shipped

**Wave A** (documented in `2026-04-17-sqlite-vec-consolidation-design.md`): sqlite-vec + FTS5 storage engine, RRF+MMR hybrid retrieval, filter-aware MCP surface, `ask()` reliability overhaul, noise filtering at enrichment claim time.

**Wave B** (documented in `2026-04-18-wave-b-sqlite-vec-rollout-design.md`):
- R-01: `mcp.ask()` filter kwargs wired through (`aca474a`)
- R-06: embed-drift guard + strict dim check + `/health` + `hippo doctor` surface (`a233b29`)
- R-16: worktree-prefix canonicalization + dedup migration + config.toml integration with `HIPPO_PROJECT_ROOTS` precedence chain (`6eeaf0e` + `b2d6827`)
- R-22: watchdog (landed upstream via `450ee38`, audited `99f3a1b`)
- R-23: `events.git_repo` forward-path (upstream `eccf617`) + historical backfill (`4722ca5`)
- Migration script `migrate-v5-to-v6.py` with 8 phased gates (`6ef3e65` + `ab43e5a` + `f974213`)
- Pre/post-cutover eval baselines (`a9f4354`, `f84978f`)

**Live corpus post-cutover (2026-04-18):**
- Schema v6
- 2,193 knowledge_nodes / 2,193 vec0 rows / 2,193 FTS rows (100% coverage)
- 8,401 events; 7,570 `git_repo` backfilled (813 unresolvable non-git cwds remain NULL)
- 4,443 entities (413 merged by dedup)

**Retrieval quality post-cutover (hybrid mode, 40-Q set):**
- recall@10 mean: 0.308 / median: 0.250
- MRR mean: 0.308 / median: 0.156
- coverage_gap: 0.000
- latency p50/p95: 3.0s / 3.1s
- 2 of 3 absolute thresholds met; recall@10 miss is -0.04 and explained by 4 adversarial questions in the full-set denominator

## Filed follow-ups (tracked but not scheduled)

All task IDs are in `~/.claude/tasks/hippo-wave-b/`.

| # | Title | Priority |
|---|---|---|
| 6 | test-fixture: update `_insert_claude_segment` to pass eligibility filter | post-merge |
| 15 | R-06 nit: treat `stored=""` as "not initialized, record live model" | post-merge |
| — | Connection pool for sqlite-vec conns (R-05) | post-merge |
| — | Move `ensure_vec_table` to `_init_state` (R-04, R-13) | post-merge |
| — | Schema v7 summary denormalization (R-07) | post-merge |
| — | Drop deprecated `relationships` table in schema v7 | post-merge |
| — | Redaction pattern expansion (`sk-*`, `glpat-*`, conn strings) | post-merge |
| — | `retrieval.search` telemetry/spans (R-10) | post-merge |
| — | Path-traversal semgrep triage in storage.rs | post-merge |
| — | Parallelize `migrate-vectors.py` (R-15) | post-merge |
| — | ANN index on `knowledge_vectors` at 10× corpus (R-02) | scaling concern |

## Team state

**Team:** `hippo-wave-b` at `~/.claude/teams/hippo-wave-b/config.json`

**Agents (all shut down gracefully after their tasks):**
- `auditor` — R-22 + R-23 verification
- `mergeblocker-filters` — R-01
- `mergeblocker-safety` — R-06
- `mergeblocker-dedup` — R-16
- `mergeblocker-backfill` — R-23 backfill
- `phase1-reviewer` — Phase 1 retrospective code review
- `phase1-fixup` — MED-1, MED-2, LOW-1, LOW-2 from review
- `migration-author` — Phase 2.1 migration script
- `migration-reviewer` — adversarial 14-scenario safety audit
- `migration-fixup` — H1, H2, H3, M1, M2 from review
- `eval-runner` — Phase 3.1 pre-cutover baseline

Phase 2.3 (real cutover) executed by the user; Phase 3.2 post-cutover eval executed by team-lead inline.

## How to resume if cut off mid-turn

1. Check `git log --oneline main..sqlite-vec-rollout` to see what's on the branch
2. If PR is open: check `gh pr view` for status
3. If PR is merged: migrate this handoff doc to the main branch and update memory
4. Task #6 and #15 are the only remaining branch-scoped actions before the "post-merge follow-ups" list kicks in

## Branch rename history

Originally `postgres` (early-exploration name from when Postgres migration was still on the table). Renamed to `sqlite-vec-rollout` on 2026-04-18 before PR creation. No remote tracking, so rename was cosmetic.
