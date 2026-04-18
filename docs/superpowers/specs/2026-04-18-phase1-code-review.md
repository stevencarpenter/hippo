# Phase 1 Retrospective Code Review

**Reviewer:** phase1-reviewer agent
**Date:** 2026-04-18
**Branch:** `sqlite-vec-rollout`
**Commits reviewed:** aca474a, a233b29, 6eeaf0e, 4722ca5
**Reference:** Wave B spec, risk register, implementation plan Task 1.6

---

## TL;DR Per-Commit Verdict

| Commit | Risk | Verdict | Summary |
|--------|------|---------|---------|
| `aca474a` | R-01 | ✅ clean | Filter wiring correct; tests valid; one trivially-true assertion (harmless) |
| `a233b29` | R-06 | ✅ clean | Drift guard sound; async tests confirmed running (`asyncio_mode=auto`); /health and doctor surface correctly |
| `6eeaf0e` | R-16 | ⚠️ concerns | Canonicalization logic correct, but fix is operationally inert without `HIPPO_PROJECT_ROOTS` set — which is set nowhere in runtime infrastructure |
| `4722ca5` | R-23 | ⚠️ concerns | Backfill logic and parity tests correct; missing `PRAGMA busy_timeout=5000` and `PRAGMA foreign_keys=ON` on the DB connection — convention violation that causes lockout under concurrent daemon writes |

---

## Overall Recommendation

**Phase 2 may proceed.** No HIGH blockers found. Two MED issues should be fixed in a follow-up commit on this branch before the PR is opened (both are small, targeted patches):

1. Add PRAGMA pragmas to backfill-git-repo.py
2. Either set `HIPPO_PROJECT_ROOTS` in the launchd plist or read project roots from `config.toml`

Neither blocks functional correctness for Phase 2 (migration script), but both will bite in the first real run of the backfill and will leave R-16 unmitigated in practice.

---

## Findings

### CRITICAL

_None._

### HIGH

_None._

---

### MED

#### MED-1 — `backfill-git-repo.py` missing required SQLite pragmas
**Commit:** `4722ca5` (R-23 completion)
**File:** `brain/scripts/backfill-git-repo.py:179`
**Confidence:** 95%

The script opens its connection with a bare `sqlite3.connect(str(db_path))` and sets only `conn.row_factory`. It never calls `PRAGMA busy_timeout=5000` or `PRAGMA foreign_keys=ON`, in direct violation of the project convention documented in CLAUDE.md:

> SQLite: WAL mode, PRAGMA foreign_keys=ON, PRAGMA busy_timeout=5000 on every connection

Without `busy_timeout`, if hippo-daemon is running and holds a write lock, the script immediately raises `sqlite3.OperationalError: database is locked` rather than waiting. The backfill is intended to run against the live DB on a real deployment, so the daemon will almost certainly be writing concurrently.

`dedup-entities.py` (same PR) correctly sets both pragmas — the backfill script just missed them.

**Remediation:**
```python
conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA foreign_keys=ON")
conn.execute("PRAGMA busy_timeout=5000")
```

---

#### MED-2 — R-16 canonicalization fix is a silent no-op in default deployments
**Commit:** `6eeaf0e` (R-16 worktree-prefix canonicalization)
**File:** `brain/src/hippo_brain/entity_resolver.py:26`
**Confidence:** 90%

`canonicalize()` strips worktree path prefixes only when `HIPPO_PROJECT_ROOTS` is set in the environment. The enrichment pipeline calls `canonicalize(entity_type, name)` without explicit `project_roots`, so it reads from the env var at runtime:

```python
env = os.environ.get("HIPPO_PROJECT_ROOTS", "")
if env:
    return [r.rstrip("/") for r in env.split(":") if r.strip()]
return []  # ← falls through; no prefix stripping
```

`HIPPO_PROJECT_ROOTS` is not set in any runtime-facing infrastructure in this repo (launchd plists, mise configs, daemon startup, `config.toml`). A grep of the repo confirms it appears only in:
- `brain/scripts/dedup-entities.py` docstring (documents it as required)
- `brain/src/hippo_brain/entity_resolver.py` implementation

In a standard deployment, `os.path.expanduser("/users/carpenter/projects/hippo-postgres/src/storage.rs")` will lowercase to a full absolute path — identical to pre-fix behavior. The primary R-16 scenario (`storage.rs × 8`) remains unfixed unless the user explicitly configures the env var.

No warning is emitted when the env var is absent and path-type canonicalization silently falls back.

**Remediation options (pick one):**

1. **Preferred:** Read project roots from `config.toml` (e.g., `[entities] project_roots = [...]`) and pass through `enrichment.py` → `canonicalize()`. Keeps config co-located with other brain config; no silent env-var dependency.

2. **Quick fix:** Set `HIPPO_PROJECT_ROOTS` in the launchd brain plist via chezmoi template using `{{ .chezmoi.homeDir }}/projects/hippo:{{ .chezmoi.homeDir }}/projects/hippo-postgres` (or equivalent). Documents the expectation; users see it at service definition level.

3. **Minimal guard:** Add a WARNING log in `_resolve_project_roots` when both `override` is None and the env var is empty — so at least the first enrichment run tells the user the guard is inactive.

---

### LOW

#### LOW-1 — Trivially-true assertion in `test_ask_respects_since_filter`
**Commit:** `aca474a` (R-01)
**File:** `brain/tests/test_mcp_server.py:629`

```python
# The source's captured_at is within the window
assert filters.since_ms <= recent_ts
```

`filters.since_ms` is ~1 hour ago; `recent_ts` is now. This is always true by construction and asserts nothing. The preceding bounds check (`expected_floor - 5000 <= filters.since_ms <= after`) is the meaningful assertion — it stays. The trailing assertion can be removed to avoid misleading readers into thinking it validates filtering.

Not a functional bug; tests pass correctly.

---

#### LOW-2 — Exact project-root match produces canonical `""`
**Commit:** `6eeaf0e` (R-16)
**File:** `brain/src/hippo_brain/entity_resolver.py:52-54`

```python
if v == normalized:
    v = ""
    break
```

An entity whose `name` is exactly a project root (e.g., the project directory itself listed as a `file` or `directory` entity) canonicalizes to `""`. If two such entities with the same `type` land in the DB, the second INSERT would violate `UNIQUE(type, canonical)`. Unlikely in practice (the enrichment LLM rarely emits a bare project root as a file entity), but worth a guard:

```python
if v == normalized:
    v = Path(normalized).name  # e.g. "hippo-postgres" instead of ""
    break
```

---

## Acceptance-Criteria Check (Task 1.6)

| Criterion | Status |
|-----------|--------|
| R-01 regression test: nonexistent project → zero sources | ✅ `test_ask_respects_nonexistent_project_filter` — patches `retrieval_search`, asserts `filters.project` set and `"No relevant knowledge"` in response |
| R-01 regression test: since-window forwarded | ✅ `test_ask_respects_since_filter` — asserts `since_ms` in correct epoch range |
| R-06 regression test: writes blocked on drift | ✅ `test_embed_drift_blocks_writes` + `test_embed_drift_blocks_writes_async` |
| R-06 regression test: empty corpus no-op | ✅ `test_embed_drift_empty_corpus_no_op` |
| R-06 regression test: allow_switch override | ✅ `test_embed_drift_allows_switch_flag` |
| R-16 regression test: worktree variants collapse | ✅ `test_both_worktree_variants_resolve_same` + `test_dedup_merges_worktree_fragments` |
| R-23 backfill: parse_owner_repo parity with Rust | ✅ 12 parity tests in `test_backfill_git_repo.py` |
| Async test execution | ✅ `asyncio_mode = "auto"` confirmed in `brain/pyproject.toml:49` |
| ruff/clippy clean | Not re-run by reviewer; prior CI run should cover; no new f-strings or obvious lint targets observed in diff |
| R-22 / R-23 risk register marking | Confirmed present in risk-register `## Status` sections |

---

## Scope and Drift Check

All four commits are tightly focused on their respective risks. No scope creep observed. The R-16 commit's semgrep-driven f-string SQL cleanup in `enrichment.py` (`executemany` replacement) is a positive side-effect, not drift. No TODO/FIXME left in hot paths.
