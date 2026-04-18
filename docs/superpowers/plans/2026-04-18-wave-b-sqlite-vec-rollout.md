# Wave B Implementation Plan: sqlite-vec Rollout

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge the `sqlite-vec-rollout` branch (formerly `postgres`) to `main` with a healthy live corpus on schema v6, no retrieval regression, and the three still-outstanding merge-blockers fixed (R-22 + R-23 already landed via main PRs #23 and #25 — to be audited not re-implemented).

**Architecture:** See `docs/superpowers/specs/2026-04-18-wave-b-sqlite-vec-rollout-design.md`.

**Tech Stack:** Rust 2024 (hippo-core, hippo-daemon), Python 3.14 (brain, uv), rusqlite, sqlite-vec, FTS5, pytest, ruff.

**Pre-flight:**
- Branch rename runs before Phase 1 (Task 0.1).
- Every cargo/python command runs from repo root.
- Validation: each merge-blocker fix gets a regression test before marking complete.
- Reviewer agent runs continuously, files follow-up tasks on drift.

---

## Phase 0: Rename branch and open scaffolding

### Task 0.1: Rename local branch `postgres` → `sqlite-vec-rollout`

- [ ] Verify no remote tracking: `git branch -vv | grep postgres` should not show `[origin/...]`
- [ ] Rename: `git branch -m postgres sqlite-vec-rollout`
- [ ] Verify: `git status` on new name; `git log --oneline -3` unchanged
- [ ] Update `MEMORY.md` pointer in `~/.claude/projects/-Users-carpenter-projects-hippo/memory/project_sqlite_vec_branch.md` to reference new name (the file path stays — repo is still at `hippo-postgres/`)

### Task 0.2: File follow-up post-merge tasks

- [ ] Open GitHub issues (or the repo's issue tracker equivalent) for every post-merge item from the Wave B spec "Out of scope" list, so they don't disappear after this PR lands:
  - Connection pool for sqlite-vec conns (R-05)
  - Move `ensure_vec_table` to `_init_state` (R-04, R-13)
  - Schema v7 summary denormalization (R-07)
  - Redaction pattern expansion
  - `retrieval.search` telemetry (R-10)
  - Path-traversal semgrep triage

---

## Phase 1: Merge-blockers (parallel)

### Task 1.1: R-01 — Wire filters through `mcp.ask()`

**Files:** `brain/src/hippo_brain/mcp.py`, `brain/tests/test_mcp_queries.py`

- [ ] Replace the TODO block at `mcp.py:306-308` with:
  - parse `since` via existing `_parse_since_ms`
  - build `Filters(project=project, source=source, branch=branch, since_ms=...)`
  - pass into `rag_ask(...)` alongside the question
- [ ] Remove the "filters are forwarded … once retrieval lands" claim from the tool docstring
- [ ] Add regression test: `test_ask_respects_nonexistent_project_filter` — asserts zero sources returned
- [ ] Add regression test: `test_ask_respects_since_filter` — asserts sources are within the window
- [ ] Run `uv run --project brain pytest brain/tests -v -k mcp` green
- [ ] Run `uv run --project brain ruff check brain/ && uv run --project brain ruff format --check brain/` clean

### Task 1.2: R-06 — Embedding model drift guard

**Files:** `brain/src/hippo_brain/embeddings.py`, `brain/src/hippo_brain/vector_store.py`, tests

- [ ] At vector-store open time, read `config.embeddings.model` and compare against the `embed_model` column of the most recent `knowledge_nodes` row
- [ ] If mismatch and not empty: log ERROR with both model names, refuse writes (reads OK), surface via `hippo doctor`
- [ ] If mismatch is expected (new deployment, explicit `--allow-embed-switch`), allow and log WARN
- [ ] Add unit test: `test_embed_drift_blocks_writes` using a fake stored model name
- [ ] Run tests + lint green

### Task 1.3: R-16 — Entity canonicalization (extend existing partial fix)

**Files:** `brain/src/hippo_brain/enrichment.py`, `brain/src/hippo_brain/entity_resolver.py` (new), `brain/scripts/dedup-entities.py` (new), tests

Current state: `enrichment.py:99` does `canonical = name.lower().strip()` — correct case folding but doesn't handle worktree-prefix variants (the primary live-corpus fragmentation). Existing `UNIQUE(type, canonical)` schema stays.

- [ ] Write `entity_resolver.canonicalize(type, value) -> str` — the extended version:
  - lowercase + strip (already done, consolidate here)
  - strip trailing slashes
  - strip leading absolute-path prefixes and resolve to repo-relative: `/Users/carpenter/projects/hippo-postgres/src/foo.rs` → `src/foo.rs`
  - collapse internal whitespace
- [ ] Replace the inline canonicalization at `enrichment.py:99` with the resolver call
- [ ] Write `scripts/dedup-entities.py`: find groups where old `canonical` diverges but new canonical matches; merge (keep oldest id; relink `knowledge_node_entity` rows; delete duplicates). Run as a one-shot during migration.
- [ ] Unit tests: path-prefix collapse, trailing slash, case, whitespace
- [ ] Integration test: insert `/Users/carpenter/projects/hippo/src/storage.rs` and `/Users/carpenter/projects/hippo-postgres/src/storage.rs`, run dedup, assert single row
- [ ] Run python tests + ruff clean

### Task 1.4: R-22 audit — enrichment watchdog (landed in main via #23)

**Files:** `brain/src/hippo_brain/watchdog.py`, `brain/src/hippo_brain/enrichment.py`, `brain/tests/test_watchdog.py`

- [ ] Read `watchdog.py` + `test_watchdog.py` end-to-end
- [ ] Verify against R-22 scenarios from the risk register:
  - per-claim timeout with configurable value
  - failure rows tagged with a watchdog-specific reason (distinct from LLM-400 failures)
  - concurrent-claim cap enforced at enqueue time
  - startup reconciliation of `processing` rows from a prior crash
- [ ] For each scenario not fully covered, open a follow-up issue labeled `wave-b-gap` — do NOT block the plan; we'll decide if gaps are merge-blockers or post-merge fixes after seeing them
- [ ] Mark R-22 as **mitigated** in the risk register with commit SHA `450ee38`

### Task 1.5: R-23 audit — events.git_repo (landed in main via #25)

**Files:** `crates/hippo-daemon/src/git_repo.rs`, `crates/hippo-daemon/src/commands.rs`, backfill script if present

- [ ] Read `git_repo.rs` end-to-end
- [ ] Verify worktree handling: running in `hippo-postgres/` resolves to the same `git_repo` as `hippo/` (uses remote URL, not worktree path)
- [ ] Confirm NEW events are tagged with `git_repo` — check the insert path in `commands.rs`
- [ ] Confirm a BACKFILL path exists for the 6,790 pre-existing NULL rows. If absent, this is a merge-blocker — file as a new task under Phase 1
- [ ] Mark R-23 as **mitigated** in the risk register with commit SHA `eccf617`

### Task 1.6: Reviewer pass on Phase 1

- [ ] Reviewer agent runs full `cargo clippy -- -D warnings`, `cargo fmt --check`, `cargo test`, `uv run --project brain pytest`, `ruff check`, `ruff format --check`
- [ ] Reviewer confirms each outstanding merge-blocker (R-01, R-06, R-16) has a regression test
- [ ] Reviewer updates `docs/superpowers/specs/2026-04-17-risk-register.md` marking R-01, R-06, R-16 as **mitigated** with their new commit SHAs, and R-22, R-23 as **mitigated** with SHAs `450ee38` and `eccf617`

---

## Phase 2: Cutover

### Task 2.1: Author migration script

**Files:** `brain/scripts/migrate-v5-to-v6.py` (new), `brain/tests/test_migration_v5_v6.py` (new)

- [ ] Implement phased script with flags (`--yes-i-backed-up`, `--yes-drop-noise`, `--yes-reembed`, `--dry-run`)
- [ ] Phase 1: preflight (schema_version check, daemon/brain stopped check, `.backup` snapshot)
- [ ] Phase 2: apply v6 DDL (virtual tables + triggers), bump schema_version
- [ ] Phase 3: queue cleanup (orphan releases, failed-row retry policy)
- [ ] Phase 4: retrospective noise cleanup (delete nodes where `is_enrichment_eligible` now returns false)
- [ ] Phase 5: re-embed via existing `migrate-vectors.py` into vec0
- [ ] Phase 6: verify (row count parity, synthetic enrichment round-trip)
- [ ] Every phase logs structured output to `logs/migration-<ts>.log`
- [ ] Write test: synthesize a v5 DB in tmpdir, run script, assert v6 state
- [ ] Write test: abort path — simulate failure at each phase, assert no partial state

### Task 2.2: Dry-run on a production DB copy

- [ ] `cp ~/.local/share/hippo/hippo.db /tmp/hippo-dryrun.db`
- [ ] Run `migrate-v5-to-v6.py /tmp/hippo-dryrun.db --dry-run`
- [ ] Inspect log for unexpected row counts (e.g., noise cleanup deleting >20% of nodes would be suspicious)
- [ ] If surprises: stop, re-diagnose, adjust the script. Do not proceed to 2.3 until a dry-run completes with expected counts.

### Task 2.3: Real cutover (user-in-the-loop)

- [ ] `mise run stop` — daemon + brain down
- [ ] Verify LM Studio is responsive (R-22 watchdog is no excuse for cutting over onto a wedged LLM): `curl http://localhost:1234/v1/models`
- [ ] Run migration script with all phase flags; inspect each phase's summary before proceeding (interactive OR use `--yes-*` flags after user approval)
- [ ] After phase 5 (re-embed): spot-check via `sqlite3 hippo.db "SELECT COUNT(*) FROM knowledge_nodes; SELECT COUNT(*) FROM knowledge_vectors;"` — numbers match
- [ ] `mise run start`
- [ ] `hippo doctor` passes

### Task 2.4: 24-hour soak

- [ ] Monitor `hippo doctor` output daily
- [ ] Check enrichment coverage climbs toward 88% (pre-wedge baseline)
- [ ] Check `retrieval.search` latency via logs/metrics; regression on typical queries blocks merge
- [ ] File any anomalies as follow-up tasks, not blockers, unless data loss is suspected

---

## Phase 3: Validation + merge

### Task 3.1: Pre-cutover eval scorecard (run BEFORE Phase 2)

- [ ] From a main-branch worktree OR using main's `evaluation.py --mode semantic` compatibility path, run `hippo-eval` against the v5 live corpus
- [ ] Archive output to `docs/superpowers/specs/2026-04-18-eval-baseline-pre-cutover.md`
- [ ] Note which of the 40 questions are `semantic`-mode-compatible (the rest only work post-cutover)

### Task 3.2: Post-cutover eval scorecard (after 2.4)

- [ ] Run `hippo-eval --mode hybrid` against the migrated v6 corpus
- [ ] Archive output to `docs/superpowers/specs/2026-04-18-eval-baseline-post-cutover.md`
- [ ] Compute Recall@10 delta on the shared subset
- [ ] If delta < 0 on any HIGH-priority question: stop. Do not merge. File as Phase 1 regression work.

### Task 3.3: PR and merge

- [ ] Branch is named `sqlite-vec-rollout` (rename from Task 0.1)
- [ ] Open PR against `main`; title: `feat(retrieval): consolidate on sqlite-vec + FTS5 (Wave A+B)`
- [ ] PR body summarizes Wave A architecture, Wave B merge-blockers mitigated (link to risk register), cutover runbook (the migration script), and eval baselines
- [ ] Reviewer agent (or a fresh code-reviewer spawn) runs `/review` on the PR
- [ ] Squash vs. merge-commit: **merge-commit** (preserves Wave A and Wave B history; 25+ commits with coherent messages)
- [ ] After merge: push to remote

### Task 3.4: Post-merge cleanup

- [ ] Update `docs/superpowers/specs/2026-04-17-session-handoff.md` with post-merge state: Wave A and Wave B shipped, branch merged, follow-ups tracked
- [ ] Update memory file `~/.claude/projects/-Users-carpenter-projects-hippo/memory/project_sqlite_vec_branch.md` to reflect merged state (or delete if fully superseded by a `project_retrieval_architecture.md`)
- [ ] Optional: schema v7 PR to drop `relationships` table (separate, 48h after cutover is stable)

---

## Exit criteria (copied from spec, track here)

- [ ] R-01, R-06, R-16 mitigated with regression tests; R-22 and R-23 audits complete
- [ ] Migration script runs end-to-end clean on a DB copy
- [ ] Post-cutover `hippo doctor` passes
- [ ] Post-cutover eval shows no Recall@10 regression
- [ ] Live coverage returns to ≥88% within 24h
- [ ] Branch renamed, PR opened, merged
- [ ] Session handoff updated
