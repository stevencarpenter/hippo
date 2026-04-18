# Session Handoff: hippo-sqlite-vec Team

**Purpose:** allow a fresh Claude Code session (or a resumed session after context loss / network drop / token exhaustion) to pick up this project without re-inferring everything.

**Last updated:** 2026-04-17 — team-lead
**Branch:** `postgres` on `/Users/carpenter/projects/hippo-postgres`
**Team name:** `hippo-sqlite-vec`

## What this project is

Branch `postgres` is a throwaway experimental rewrite of hippo's retrieval layer:
- Consolidated LanceDB + SQLite → single sqlite-vec + FTS5 store
- Added RRF + MMR hybrid retrieval with filter pushdown
- Fixed `ask()` reliability (context budget, degraded mode, preflight)
- Widened MCP surface with filters + new tools
- Filtered session-lifecycle noise at enrichment claim time

The complete architecture is in
`docs/superpowers/specs/2026-04-17-sqlite-vec-consolidation-design.md`.

## Wave 1 complete (implementation)

12 commits on `postgres` since the spec:

| Phase | Commits | Outcome |
|---|---|---|
| Spec | 0235b4f, 3057b0d, 307d702 | Design + 3 corrections |
| storage | d93a9bb, a2a0afc, b0298af | Schema v6 + vector_store |
| retrieval | 1c3bb91, a3eeb9f, e964265, 601168d | RRF + MMR + filter pushdown + #8/#9 fixes |
| synthesis | 99b57d7 | rag.py reliability + filter plumbing |
| mcp-surface | c6ed1cd | Filter params + new tools |
| enrichment | 763e59d | Noise-eligibility filter |
| review | 48ee6b7 | Scorecard + benchmark + integration test |

**Team task list:** `~/.claude/tasks/hippo-sqlite-vec/` — builder tasks #1–#9
are completed, #7 (ruff py314) closed as not-a-bug.

**Shut-down agents:** storage, retrieval, synthesis, mcp-surface, enrichment
(all terminated gracefully 2026-04-17 ~10:56).

**Live agents:** reviewer (idle).

## Wave 2 in progress (corpus evaluation + prognostication)

Dispatching now:
- **corpus-analyst** (task #10): deep corpus health report.
  Focus points from baseline snapshot:
  - 435 `processing` rows in enrichment_queue (likely orphan locks)
  - 213 `failed` enrichment rows
  - 0 relationships (graph edges never populated)
  - 0 lessons (CI learning empty)
  - Only 1 redaction across 6,655 events (suspicious)
  - Entity type balance file-dominated (47%); project only 3%
  - Output: `docs/superpowers/specs/2026-04-17-corpus-health-report.md`
- **metrics-designer** (task #11): evaluation harness design + `hippo eval` CLI.
  - 30–50 labeled Q/A set
  - Quantitative metrics: coverage, graph density, queue health, throughput, freshness, entity balance
  - Qualitative metrics: Recall@K, MRR, NDCG, source diversity, near-duplicate density, synthesis groundedness (LLM-judge), summary coherence, embedding cohesion, coverage gaps
  - Output: spec + brain/src/hippo_brain/eval.py + brain/tests/test_eval.py
- **pitfall-auditor** (task #12): risk register for the new architecture.
  - Scaling failures at 10x / 100x corpus
  - Multi-user / OSS adoption pitfalls
  - Adversarial query resilience
  - Schema trigger semantics under load
  - MCP tool contract assumptions
  - Output: `docs/superpowers/specs/2026-04-17-risk-register.md`

## Live corpus baseline (as of 2026-04-17)

From `sqlite3 ~/.local/share/hippo/hippo.db` (schema v5 live; v6 is branch-only):

| Source | Raw | Enriched | Coverage |
|---|---:|---:|---:|
| Shell events | 6,655 | 5,862 | 88% |
| Claude sessions | 963 | 963 | 100% |
| Browser events | 16 | 10 | 62% |
| Workflow runs | 0 | 0 | — |

| Knowledge graph | Count |
|---|---:|
| Knowledge nodes | 1,878 (all `node_type='observation'`) |
| Entities | 4,438 (file 47%, concept 18%, tool 16%, service 16%, project 3%) |
| Relationships | 0 |
| Knowledge-node ↔ entity links | 18,413 (mean 9.8/node) |

| Queue health | Count |
|---|---:|
| Shell enrichment done | 5,862 |
| Shell enrichment failed | 213 |
| Shell enrichment pending | 145 |
| Shell enrichment processing (orphans) | 435 |

## Open items for a resumed session

1. **Retrieval quality is unmeasured on real corpus.** Cold-turkey branch means no A/B possible without backfill. metrics-designer (task #11) produces the harness.
2. **Corpus health concerns** (stale locks, missing relationships, weak redaction) — corpus-analyst investigates (#10).
3. **Post-migration pitfalls** — pitfall-auditor audits (#12).
4. **Follow-ups filed but not scheduled:**
   - Retrospective noise cleanup (purge historical noise knowledge_nodes pre-backfill)
   - Schema v7 summary denormalization
   - Connection pool for sqlite-vec-loaded connections in mcp.py
   - Triage of pre-existing storage.rs path-traversal semgrep warnings
5. **User has not yet decided** whether to merge `postgres` to main, run backfill, or keep experimenting.

## Corrections made in this session (worth remembering)

- `except A, B:` without parens is a NEW Python 3.14 language feature, not
  Python 2 syntax. Ruff at `target-version="py314"` correctly canonicalizes
  to this form. I flagged it as a bug three times based on wrong
  intuition; corrected only when ruff at py313 surfaced the authoritative
  release-note reference. See `reference_ruff_py314_regression.md` in memory.
- Reviewer's scorecard is 9 met / 3 partial / 0 unmet. Two high-severity
  findings (FTS5 punctuation crash, project filter semantic mismatch) were
  fixed by retrieval in commit 601168d.

## How to resume if cut off mid-turn

1. Read this file first.
2. Read the spec doc at `docs/superpowers/specs/2026-04-17-sqlite-vec-consolidation-design.md`.
3. Run `git log --oneline 0235b4f..HEAD` on branch `postgres` to see commits since spec.
4. Run `TaskList` to see current team tasks.
5. Check `~/.claude/teams/hippo-sqlite-vec/config.json` for live agent list.
6. Messages from agents arrive automatically as new turns; respond via `SendMessage`.
7. If an agent reports completion, verify their commit landed (`git log --oneline -5`) and route any flagged issues to the appropriate owner.
8. Branch `postgres` is throwaway; do not push to remote without explicit user approval.
