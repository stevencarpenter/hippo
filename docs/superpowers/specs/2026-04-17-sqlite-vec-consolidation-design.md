# Design: sqlite-vec Consolidation + Retrieval Overhaul

**Status:** Draft for approval
**Author:** Claude (Opus 4.7) + Steven
**Date:** 2026-04-17
**Branch:** `postgres` (throwaway experimental branch)

## Motivation

Empirical test of the hippo MCP server (10 queries: 5 `ask`, 5 `search_knowledge`)
revealed three structural issues that prevent agents from using hippo as a
first-stop knowledge shop:

1. **Synthesis unreliability:** 3 of 5 `ask` calls failed with `Error: Synthesis
   failed: ` (empty exception). The retrieval layer worked; the LM Studio chat
   call threw, and `rag.py` traps it without surfacing anything actionable. An
   agent seeing this will stop trusting the tool.

2. **Storage fragmentation:** `knowledge_nodes` in SQLite stores `content` (JSON
   blob), `outcome`, `tags`; LanceDB stores a different schema with `summary`,
   `key_decisions`, `problems_encountered`, `cwd`, `git_branch`,
   `entities_json`, etc. Same node is written twice in two shapes. Drift already
   observed (vector count mismatches with knowledge-node count).

3. **Retrieval surface is too thin:** No filters (project/since/source/branch)
   on MCP tools. Scores are non-normalized (negative values, clustered). FTS5
   is unused — lexical fallback is `LIKE '%q%'` which misses obvious hits. No
   MMR/dedup, so near-duplicate session-start noise dominates some result sets.
   Citations don't expose UUIDs or event IDs, so agents can't follow up.

## Scope

**In scope**

- Migrate vector storage from LanceDB → sqlite-vec `vec0` virtual tables in
  the same SQLite DB (`~/.local/share/hippo/hippo.db`)
- Add FTS5 virtual tables on `knowledge_nodes` for BM25 lexical retrieval
- New retrieval module: hybrid (RRF of FTS5 + vec0) with MMR dedup and
  normalized cosine scores in [0, 1]
- Widen MCP tool surface: `project`, `since`, `source`, `branch`, `entity`
  filters on existing tools; new `search_hybrid`, `get_context`,
  `list_projects` tools; expose `uuid` and linked event IDs on citations
- Fix `ask` reliability: context-size budget, degraded mode (retrieval-only
  answer when synthesis fails), surface exception type/message/model, LM
  Studio preflight
- Filter session-lifecycle noise at enrichment time (don't enrich
  session-start/end events, very short sessions)
- Rewrite the ambiguous `except X, Y:` pattern in `rag.py` and
  `mcp_queries.py` to the explicit `except (X, Y):` tuple form (correction:
  empirical test in Python 3.14 shows this form IS parsed as a tuple-catch
  and both exceptions are caught — it is not a SyntaxError as previously
  believed. The rewrite is a clarity improvement, not a bug fix.)

**Out of scope (future passes)**

- Cross-encoder rerank (latency/compute)
- Fine-tuning embedding models on hippo corpora
- Entity graph traversal queries
- Removing LanceDB dep from `pyproject.toml` (leave in; dead code for now,
  remove in a follow-up PR)
- Postgres migration (explicitly rejected — see preceding conversation)

## Architecture

### Storage layer (schema v6)

Add to `crates/hippo-core/src/schema.sql`:

```sql
-- Virtual table backed by sqlite-vec extension
CREATE VIRTUAL TABLE knowledge_vectors USING vec0(
    knowledge_node_id INTEGER PRIMARY KEY,
    vec_knowledge FLOAT[768] distance_metric=cosine,
    vec_command  FLOAT[768] distance_metric=cosine
);

-- FTS5 full-text index over knowledge node content
CREATE VIRTUAL TABLE knowledge_fts USING fts5(
    summary,
    embed_text,
    content,
    tokenize = 'porter unicode61 remove_diacritics 2'
);

-- Triggers to keep FTS5 in sync with knowledge_nodes on insert/update/delete.
```

Schema migration: `user_version = 5 → 6`. Migration code in
`crates/hippo-core/src/storage.rs` creates the virtual tables and triggers.
Vectors are re-embedded on first boot after migration (nuke & re-embed policy
— throwaway branch, preserving LanceDB vectors is not a requirement).

### Connection bootstrap

Python `brain`: load `sqlite-vec` as a runtime-loadable SQLite extension on
every new connection:

```python
import sqlite_vec
def open_conn(path):
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn
```

Rust daemon does not need to load sqlite-vec — it only queries `knowledge_nodes`
(not vectors), and writes vectors go through the Python brain.

### Retrieval module (new)

`brain/src/hippo_brain/retrieval.py` exposes a single entry point:

```python
@dataclass
class Filters:
    project: str | None = None        # substring match across cwd + git_repo + project_dir
    since_ms: int | None = None       # epoch ms lower bound
    source: str | None = None         # "shell" | "claude" | "browser" | "workflow"
    branch: str | None = None
    entity: str | None = None         # canonical entity name

@dataclass
class SearchResult:
    uuid: str
    score: float                      # normalized [0, 1]
    summary: str
    embed_text: str
    outcome: str | None
    tags: list[str]
    cwd: str
    git_branch: str
    captured_at: int
    linked_event_ids: list[int]       # for follow-up queries

def search(conn, query, query_vec, filters, mode="hybrid", limit=10) -> list[SearchResult]:
    ...
```

**Modes:**

- `semantic`: vec0 KNN only
- `lexical`: FTS5 BM25 only
- `hybrid`: RRF merge of the two (k=60, standard)
- `recent`: date-ordered with query as loose filter

**Pipeline (hybrid mode):**

1. Apply filters as WHERE clauses at the vec0/FTS5 query level (pushed down)
2. Retrieve top-3K from vec0 and top-3K from FTS5 (over-fetch for RRF headroom)
3. RRF merge: `score = Σ 1/(k + rank_i)` for each hit across both lists
4. MMR diversification: λ=0.7 trade-off between RRF score and cosine distance
   to previously-selected hits
5. Return top-K with normalized scores

Score normalization: cosine distance is in [0, 2]; we L2-normalize vectors at
embed time, so cosine_distance ∈ [0, 2] and `score = 1 - cosine_distance / 2` ∈
[0, 1]. RRF scores are normalized to the top result.

### Synthesis reliability (rag.py)

Changes:

1. **Context budget.** Cap `_build_rag_prompt` output at 8000 chars.
   Truncate `embed_text` and `commands_raw` per-hit proportionally if needed.
2. **Degraded mode.** If synthesis fails or returns empty, return a shaped
   response with `answer = None` and `sources` populated; `format_rag_response`
   renders sources as markdown "Raw notes" section.
3. **Error surfacing.** Catch `httpx.HTTPError`, `httpx.TimeoutException`,
   generic `Exception` separately. Include exception type, model name,
   endpoint, and a human-readable stage tag in the error.
4. **LM Studio preflight.** Add `lm_client.health_check(model)` that probes
   the models endpoint; `ask()` runs it first and returns a degraded response
   with a clear "query model not loaded" error if the check fails.

### MCP surface (mcp.py, mcp_queries.py)

Changes to existing tools (`ask`, `search_knowledge`, `search_events`,
`get_entities`):

- Add optional `project`, `since`, `source`, `branch` filter parameters
- Source objects gain `uuid`, `linked_event_ids`

New tools:

- `search_hybrid(query, filters, mode, limit)` — direct access to the retrieval
  module without synthesis
- `get_context(query, filters, limit)` — returns a pre-formatted Markdown
  context block ready to paste into an agent's prompt
- `list_projects()` — returns distinct `git_repo` + `cwd` roots seen in events,
  sorted by last_seen

### Enrichment filtering (enrichment.py + claude_sessions.py)

New function `is_enrichment_eligible(event) -> bool` applied at claim time.
Filters out:

- Shell events with `command` matching session-start/cleanup patterns
  (`exec zsh`, empty commands, etc.) AND no stdout/stderr AND duration < 100ms
- Claude sessions with `message_count < 3` AND no tool_calls
- Browser events with `dwell_ms < 1000`

Events marked ineligible get `status = 'skipped'` in the enrichment queue
(existing state, just underused).

## Data policy

**Nuke & re-embed.** On first boot after schema migration:

1. Rust daemon runs migration (creates vec0/FTS5 virtual tables + triggers)
2. Python brain detects `knowledge_vectors` is empty but `knowledge_nodes` has
   rows → triggers re-embed of all existing knowledge nodes
3. LanceDB directory at `~/.local/share/hippo/vectors/` is left alone (dead
   data, can be manually `rm -rf`'d later)
4. Users on the main branch are unaffected (this is a branch-specific
   migration)

## Agent team composition

Six agents. Names are tmux window names. All work on branch `postgres` in the
same checkout; file ownership is enforced by mission briefs below.

| Name | Files owned | Depends on |
|---|---|---|
| **storage** | schema.sql, storage.rs, embeddings.py (rewrite), vector_store.py (new), migrations.py (new), pyproject.toml (add sqlite-vec), scripts/migrate-vectors.py (new) | — |
| **retrieval** | retrieval.py (new), tests/test_retrieval.py (new) | storage (interface only) |
| **synthesis** | rag.py, client.py, tests/test_rag.py (new) | — |
| **mcp-surface** | mcp.py, mcp_queries.py, models.py, tests/test_mcp_queries.py | retrieval (interface only) |
| **enrichment** | enrichment.py, claude_sessions.py, browser_enrichment.py, workflow_enrichment.py, tests/test_enrichment.py (modifications) | — |
| **reviewer** | docs/superpowers/specs/, integration tests, benchmarks | all others report done |

### Waves

- **Wave 1 (parallel):** storage, synthesis, enrichment — no cross-file
  dependencies
- **Wave 2 (parallel after Wave 1):** retrieval (needs storage schema),
  mcp-surface (needs retrieval interface)
- **Wave 3:** reviewer — runs full `ruff check` + `ruff format --check` +
  `cargo clippy --all-targets -- -D warnings` + `cargo fmt --check` +
  `pytest brain/tests` + `cargo test` + semgrep_scan on new code + benchmark
  against fixed question set

### Coordination

- Team lead (main session) creates the team, dispatches agents, monitors via
  tmux windows
- Agents communicate via `SendMessage` (by name) for interface negotiation
  (e.g., retrieval asks storage for the exact vec0 column names)
- Each agent marks their assigned task completed via `TaskUpdate` when done
- Reviewer runs in Wave 3 and files follow-up tasks if issues found

## Acceptance criteria

1. All `ask` queries return a response — either a synthesized answer OR a
   degraded "Raw notes" response. No more `Error: Synthesis failed: ` with
   empty message.
2. `search_knowledge` supports `project`, `since`, `source` filters
3. `search_knowledge` semantic mode returns scores in [0, 1] (no more negatives)
4. Lexical mode finds "schema design" queries (FTS5 working)
5. `search_hybrid` exists and returns fused/deduplicated results
6. `list_projects` returns the user's active projects
7. `knowledge_vectors` (vec0) and `knowledge_fts` (FTS5) tables exist with
   triggers keeping them in sync
8. LanceDB write path is gone from brain code (import + function calls)
9. Enrichment queue shows `skipped` status for noise events
10. Full test suite passes; lint/fmt/clippy clean
11. Semgrep scan on new code shows no findings (or all triaged)
12. Benchmark: hybrid mode retrieval quality meets or exceeds LanceDB-only
    mode on a fixed 10-question evaluation set (reviewer creates this set)

## Verification plan

- Unit tests per agent (in their test files)
- Integration test (reviewer owns): spin up brain, insert a fake knowledge
  node, query via MCP, assert shape and presence of new fields
- Manual smoke test (reviewer owns): run `hippo ask` on 10 real questions from
  the evaluation set, confirm no synthesis failures and structurally sound
  responses
- Semgrep scan (global CLAUDE.md requirement) on all changed code before
  reviewer signs off

## Open questions answered (defaults)

- **Data preservation?** No. Nuke & re-embed.
- **Dual-write during migration?** No. Cold-turkey switch (throwaway branch).
- **Remove LanceDB dep?** Not in this pass; follow-up.
- **Cross-encoder rerank?** Not in v1; follow-up if benchmarks warrant.
- **Worktrees per agent?** No. Shared checkout with strict file ownership,
  since the hippo-postgres directory is already an isolated branch.

## Out-of-scope issues flagged

- The `except X, Y:` pattern in `rag.py:102` and `mcp_queries.py:34, 98, 201`
  was originally flagged as a Python-2 SyntaxError. Empirical test in
  Python 3.14 shows it parses as an implicit tuple — both exceptions are
  caught. My initial reading was wrong; no import was broken, which is
  why the running brain worked fine.
- **Ruff py314 formatter regression (confirmed by storage with a test matrix):**
  `ruff 0.15.8` with `target-version = "py314"` in brain/pyproject.toml
  strips the parens from `except (A, B):` and rewrites to `except A, B:`
  on every format run. py311/py312/py313 targets preserve parens. So
  every agent who "fixed" the tuple-except form watched their fix silently
  reverted by the next `ruff format` pass. The committed code ends up
  unparenthesized. Python 3.14 still accepts this syntactically (tuple
  interpretation), so nothing breaks functionally — but it's cosmetically
  wrong and hostile to other Python versions.
  Recommended short-term fix: pin `target-version = "py313"` in
  brain/pyproject.toml and re-run `ruff format`. File upstream at
  astral-sh/ruff as a py314 regression. See the follow-up task.
- LanceDB vector-count-vs-knowledge-node-count drift has happened before
  (observed in prior sessions). The new scheme with triggers + single-DB
  transactional writes eliminates this class of bug.
