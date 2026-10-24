# Claude Auto-Memory Research Spike Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a reproducible, evidence-backed adopt/adapt/reject decision for `sqlite-memory` and freeze Hippo's initial auto-memory document/chunk contract.

**Architecture:** Keep all experimental binaries and databases outside the repository. Add a sanitized fixture corpus and a deterministic Python comparison harness under `brain/` that evaluates whole-file, Markdown-heading, and token-window chunking with lexical retrieval, mutation operations, and structural assertions. Preserve only the fixture, harness, generated Markdown report, and decision record.

**Tech Stack:** Python 3.14, stdlib `sqlite3`/FTS5, pytest, uv, mise, sqlite-memory/sqlite-vector scratch builds.

---

### Task 1: Establish the sanitized evaluation corpus

**Files:**
- Create: `brain/src/hippo_brain/_fixtures/auto_memory_spike/manifest.json`
- Create: `brain/src/hippo_brain/_fixtures/auto_memory_spike/source/*.md`
- Create: `brain/tests/test_auto_memory_spike.py`

- [ ] Write a failing manifest-validation test covering indexes, topic files, headings, lists, links, duplicate content, long content, and update/delete fixtures.
- [ ] Run `uv run --project brain pytest brain/tests/test_auto_memory_spike.py -v` and confirm the missing fixture failure.
- [ ] Add fully synthetic Markdown fixtures and expected query-to-document relevance labels.
- [ ] Run the targeted test and confirm it passes.

### Task 2: Implement deterministic candidate chunkers

**Files:**
- Create: `brain/src/hippo_brain/bench/auto_memory_spike.py`
- Modify: `brain/tests/test_auto_memory_spike.py`

- [ ] Add failing tests for whole-file, Markdown-heading, and token-window-with-overlap chunk boundaries.
- [ ] Run the targeted tests and confirm the chunker assertions fail.
- [ ] Implement the three pure chunking strategies with stable document/chunk identities.
- [ ] Run targeted tests and confirm they pass.

### Task 3: Add retrieval and mutation comparison

**Files:**
- Modify: `brain/src/hippo_brain/bench/auto_memory_spike.py`
- Modify: `brain/tests/test_auto_memory_spike.py`
- Modify: `mise.toml`

- [ ] Add failing tests for FTS5 retrieval scoring plus update, delete, rename, duplicate-path, rollback, deferred-indexing, and failure scenarios.
- [ ] Implement a scratch SQLite evaluator that atomically replaces each strategy's active chunks and verifies no stale rows remain.
- [ ] Add `mise run bench:auto-memory-spike` to regenerate the report from the sanitized corpus.
- [ ] Run the targeted tests and the mise task.

### Task 4: Build and inspect sqlite-memory outside the repository

**Files:**
- Create: `docs/research/2026-06-27-sqlite-memory-compatibility.md`

- [ ] Clone the pinned upstream release into a temporary directory and record commit, release, license, build dependencies, binary artifacts, and tests.
- [ ] Attempt a local build and load test without changing Hippo dependencies.
- [ ] Exercise add/update/delete/rename/duplicate-path/transaction/deferred-embedding behavior against a scratch database where supported.
- [ ] Compare sqlite-vector, embedding providers/dimensions, schema ownership, FTS, MCP, packaging, migrations, and rollback against current Hippo contracts.

### Task 5: Generate the decision report and freeze the contract

**Files:**
- Create: `docs/research/2026-06-27-auto-memory-spike-report.md`
- Modify: `docs/research/2026-06-27-sqlite-memory-compatibility.md`

- [ ] Run the comparison task and record per-strategy Hit@1, MRR, indexed chunk count, mutation correctness, and failure behavior.
- [ ] State an adopt/adapt/reject decision for sqlite-memory with concrete evidence.
- [ ] Freeze the minimal document/chunk/revision/projection contract required by SNUG-132.
- [ ] Scan both reports for placeholders, contradictions, unverified claims, and accidental source-memory content.

### Task 6: Verify and update Linear

**Files:**
- Verify only: all files above

- [ ] Run `mise run fmt:check`, targeted pytest, and `mise run bench:auto-memory-spike`.
- [ ] Run `git diff --check` and inspect the exact diff for private-memory leakage.
- [ ] Post the measurements and recommendation to SNUG-131.
- [ ] Leave SNUG-131 In Progress for maintainer review because it is explicitly HITL.
