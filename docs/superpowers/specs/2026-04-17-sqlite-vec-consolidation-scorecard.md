# sqlite-vec Consolidation — Acceptance Scorecard

**Reviewer:** reviewer agent
**Date:** 2026-04-17
**Branch:** `postgres`
**Commits under review:** storage `a2a0afc`, retrieval `e964265`, synthesis / mcp-surface / enrichment (working-tree, uncommitted at review time).

## Validation pipeline (Phase A)

| Check | Result |
|---|---|
| `cargo fmt --check` | ✅ clean |
| `cargo clippy --all-targets -- -D warnings` | ✅ clean |
| `cargo test` | ✅ 100 + 49 + 12 + … all green, 0 failures |
| `ruff check brain/src brain/tests brain/scripts` | ✅ clean |
| `ruff format --check brain/src …` | ⚠️ `enrichment.py` needed reformat (reviewer formatted; upstream of task #7) |
| `pytest brain/tests -v` | ✅ 325 passed, 1 xfailed (pre-existing xfail in `test_server.py::test_query_returns_semantically_related_result`) |
| Semgrep on changed files | 11 findings, all pre-existing or false-positive patterns (see below) |
| Integration test (`test_integration_sqlite_vec.py`) | ✅ 4 passed after surfacing two real retrieval bugs (see tasks #8 and #9) |

### Semgrep triage

- **9× `python.sqlalchemy.security.sqlalchemy-execute-raw-query`** against `browser_enrichment.py`, `claude_sessions.py`, `enrichment.py`. All are pre-existing f-string SQL composition of static fragments + bind-param lists. No user input is interpolated. Rule is notoriously false-positive on non-sqlalchemy code; these files don't use SQLAlchemy at all. Keep as-is.
- **2× `rust.actix.path-traversal.tainted-path`** on `storage.rs:762` and `storage.rs:781` — pre-existing warnings flagged in the brief (item 4); out of scope for this pass.

## Acceptance criteria

| # | Criterion | Status | Notes |
|---|---|---|---|
| 1 | `ask` returns answer OR degraded response (no empty `Synthesis failed`) | ✅ **met** | `rag.py` now routes all exception sites through `_degraded_response`; integration test confirms degraded mode returns `sources` even when chat times out. |
| 2 | `search_knowledge` supports `project` / `since` / `source` filters | ⚠️ **partial** | Parameters wired through `mcp.py` and `mcp_queries._build_knowledge_filter_clause`. BUT the parallel `retrieval._apply_filters` path (used by `search_hybrid`, `get_context`, and `rag.ask` when any filter is set) uses inconsistent semantics — prefix LIKE only on cwd, no git_repo check. See task #9. |
| 3 | semantic mode returns scores in `[0, 1]` | ✅ **met** | `vector_store.knn_search` normalizes with `1 - dist/2.0` clamped; `retrieval._cosine_to_score` matches; integration test asserts invariant across all 4 modes. |
| 4 | Lexical mode finds "schema design" queries (FTS5 working) | ⚠️ **partial** | FTS5 table + triggers confirmed wired; lexical search returns results. BUT free-text queries containing FTS5 syntax chars (`?`, `:`, `-`, quotes, `*`) crash with `sqlite3.OperationalError: fts5: syntax error` — integration test reproduced with "what is the retrieval stack?". Task #8 filed. High severity because most natural questions end in "?". |
| 5 | `search_hybrid` exists and returns fused/deduplicated results | ✅ **met** | Tool registered in `mcp.py:651`; RRF fusion + MMR diversification in `retrieval._hybrid`; dedup verified in integration test. |
| 6 | `list_projects` returns active projects | ✅ **met** | `list_projects_impl` imported + registered in MCP surface. |
| 7 | `knowledge_vectors` (vec0) + `knowledge_fts` (FTS5) tables with triggers | ✅ **met** | `schema.sql` adds FTS5 + triggers at v6; vec0 created idempotently by `vector_store.open_conn`; integration test exercises both via real schema replay. |
| 8 | LanceDB write path gone from brain code | ✅ **met** | `grep -r "import lance"` returns only docstring/comment references; no runtime imports, no function calls. The comments should eventually be tidied but don't execute anything. |
| 9 | Enrichment queue shows `skipped` for noise events | ✅ **met** | `is_enrichment_eligible` wired at claim time in `enrichment.py:292`; queue row set to `status='skipped'` with reason in `error_message`. |
| 10 | Full test suite + lint/fmt/clippy clean | ✅ **met** | See Phase A table. `enrichment.py` reformat caught during review — attributable to upstream ruff regression (task #7), not a builder error. |
| 11 | Semgrep no findings (or all triaged) | ✅ **triaged** | See Semgrep triage above; all 11 findings are false-positive or pre-existing. |
| 12 | Benchmark: hybrid ≥ LanceDB on 10-Q eval set | ⚠️ **partial** | Eval set created (`brain/tests/eval_questions.json`); hybrid-mode scored (see `2026-04-17-retrieval-benchmark.md`). LanceDB side skipped: removed on this branch (scope decision in spec's "Open questions" — not dual-written), so A/B against the old path would require either a main-branch worktree with real past-indexed data, or a re-embedding run we don't have infra time for. Documented as limitation; hybrid alone is qualitatively acceptable. |

**Summary:** 9 met, 3 partial, 0 unmet.

## Follow-up tasks

- **#7 Fix ruff py314 target-version formatter regression** — already filed; reference memory `reference_ruff_py314_regression.md`. Workaround: pin `target-version = "py313"` until ruff upstream fixes.
- **#8 retrieval: sanitize FTS5 query for punctuation / operators** — high severity; `rag.ask` currently fails whenever a filter is active and the question contains `?` / `:` / `-` etc.
- **#9 retrieval: project filter is prefix LIKE + misses git_repo** — high severity; filter semantics diverge between `retrieval.py` and `mcp_queries.py`, causing empty results for many natural project values.
- **[NEW] Retrospective noise cleanup** — `is_enrichment_eligible` only gates NEW work. Historical noise knowledge nodes persist and will be re-embedded by `migrate-vectors.py`. Needs a purge/cleanup script.
- **[NEW] Schema v7 summary denormalization** — move `summary` to a dedicated column on `knowledge_nodes` instead of extracting from `content` JSON in triggers. Simplifies trigger and speeds SELECTs.
- **[NEW] Connection pool for sqlite-vec-loaded conns** — `_open_retrieval_conn` in MCP surface opens a fresh conn with extension-load per `search_hybrid` / `get_context` call. For hot paths, pool instead.
- **[NEW] Remove LanceDB docstring/comment references** — low priority cosmetic.

## Brief items requested by team-lead

1. **Ruff formatter claim verified empirically.** With `target-version = "py314"`, ruff 0.15.8 rewrites `except (A, B):` → `except A, B:` inside `brain/`. Standalone files outside the project (no target-version) are untouched. Task #7 tracks the workaround.
2. **test_server.py status:** 28 passed, 1 xfailed (`test_query_returns_semantically_related_result`) — storage's report matches. The xfail is pre-existing, not a regression.
3. **Storage's `except Exception` workaround in `embeddings.py._safe_json`:** formatter regression is real → the workaround stands. When task #7 lands (or `target-version` is pinned to py313), tighten to `except ValueError` (covers `JSONDecodeError`).
4. All four follow-up tasks from brief item #4 filed above.
5. Spec correction re: `except X, Y:` semantics acknowledged. Scorecard treats both forms as semantically equivalent; no criteria blocked.

## Verdict

**Sign-off with conditions.** The storage/retrieval/synthesis/mcp-surface/enrichment work substantively meets the spec. Two high-severity retrieval bugs (tasks #8 and #9) are real and affect the primary user-facing codepath (`rag.ask` + `search_hybrid` with filters). They are fixable in small patches and do not invalidate the architectural direction. Recommend blocking a main-branch merge on #8 and #9; tasks #7 and the other follow-ups can land post-merge.
