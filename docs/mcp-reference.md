# MCP Tool Reference

Full reference for hippo's MCP server. Every tool that `hippo-mcp` exposes, with arguments, return shapes, examples, and selection guidance. The MCP server source is `brain/src/hippo_brain/mcp.py`; this doc is what to read instead.

**Before citing evidence:** read [`docs/capture/source-trust-contracts.md`](capture/source-trust-contracts.md) for per-source identity, timestamp, freshness, and safe-citation semantics. Eligibility filters (what retrieval excludes by default) are in [`docs/capture/retrieval-eligibility.md`](capture/retrieval-eligibility.md).

For setup (adding hippo to your MCP config), see the [README's MCP Server section](../README.md#mcp-server).

**Which copy runs.** Run the server from the installed brain, `uv run --no-sync --project ~/.local/share/hippo-brain hippo-mcp` (use the absolute home path in config files). `scripts/install.sh` syncs that directory with `uv sync --locked`, which installs the `hippo-mcp` console script (`brain/pyproject.toml`, `[project.scripts]`) together with the brain service, so both run the same version. `hippo doctor` prints `MCP server version`: `[OK]` when the installed version matches the running brain, `[WW]` when it differs (restart the brain service, or reinstall), `[!!]` when `.venv/bin/hippo-mcp` is missing. A running MCP session keeps its old code until the agent restarts it. For the trust-boundary discussion (what granting MCP access actually exposes), see the [README's Privacy and Security section](../README.md#privacy-and-security).

## Tool selection guide

| You want… | Reach for | Why |
|---|---|---|
| A synthesized prose answer with cited sources | `ask` | Performs retrieval + LLM synthesis end-to-end. Slow (~1-3 s) but most useful for "what was I working on?" / "how did I fix that?" / "why did we choose X?" |
| A compact answer plus evidence packets in one call | `agent_query` | Modes: `known`, `evidence`, `recent`, `decisions`. Returns bounded answer, hits with `evidence` (inline `freshness` + `confidence`), aggregated `freshness`, and `conflicts` when stale or contradictory evidence is detected. |
| A list of relevant knowledge nodes (no synthesis) | `search_knowledge` or `search_hybrid` | Retrieval only. Fastest path. Use `search_hybrid` when you want score-fused vec0 + FTS5 results; `search_knowledge` for the simpler "semantic with lexical fallback" path. Each hit's `evidence` packets include inline capture `freshness` (SNUG-125) and an explainable `confidence` rating (SNUG-126). |
| A Markdown context block ready to paste into another agent's prompt | `get_context` | Same retrieval as `search_hybrid`, rendered as a prompt-shaped block (numbered list + per-hit summary/outcome/cwd/uuid). |
| Raw shell commands / Claude tool calls / browser visits — not enriched summaries | `search_events` | Operates on the events tables, not knowledge nodes. Use for "what command did I run?" / "what URL was I on?" |
| The list of projects in the corpus | `list_projects` | Use for discovery before filtering other tools by `project`. |
| Extracted entities (project, file, tool, env_var, etc.) | `get_entities` | Knowledge graph view. Filter by `type` to scope. |
| CI status for a recent push | `get_ci_status` | Structured data; preferred over `ask` for known-shape queries. |
| Lessons (graduated recurring CI failures) | `get_lessons` | Pre-flight before editing in a known failure-prone area. Lessons require ≥ 2 occurrences to graduate. |
| How an agent performs (usage, review findings, verdicts, benchmarks) | `get_agent_scorecard` | Aggregates only; never returns finding or note text. |

`agent_query` analyzes design decisions in every query mode before omitting them from non-`decisions` hit payloads. Its decision-conflict heuristic compares distinct nodes with matching cwd/branch and opposed explicit alternatives. A warning requires chronology review; no warning does not establish consistency. See the [confidence reference](capture/confidence-scoring.md) for confidence caps.

The ones to reach for first: **`ask`** for natural-language questions with answer synthesis; **`search_hybrid`** for raw retrieval; **`search_events`** for raw event lookup.

## Common arguments

Several tools share filter arguments. Project matching differs between knowledge queries and raw event or entity lookups:

| Argument | Type | Behavior |
|---|---|---|
| `since` | `str` | Time window. **Strict format only**: `^<digits><unit>$` where unit is `m`/`h`/`d`. Examples: `"30m"`, `"24h"`, `"7d"`. `parse_since` returns 0 (no filter) for inputs with spaces, words, bare numbers, mixed case, or anything else that doesn't match. Empty string disables. |
| `project` | `str` | Knowledge queries use [canonical project scope](canonical-project-identity.md#editable-aliases). Raw event and entity lookups retain substring matching. Use `list_projects` first to find captured names. |
| `source` | `str` | One of `"shell"`, `"claude"`, `"browser"`, `"workflow"`. Empty string means all sources. |
| `branch` | `str` | Exact-match `git_branch` filter. Ignored for browser events. |
| `limit` | `int` | Max results. Clamped to a sane upper bound by `_clamp_limit`; values above that are silently capped. |

## Tools

### `ask`

Natural-language question → synthesized answer with cited sources.

**Arguments**

| Name | Type | Default | Notes |
|---|---|---|---|
| `question` | `str` | required | The natural-language question. |
| `limit` | `int` | `10` | Number of knowledge nodes to retrieve as context. |
| `project` / `since` / `source` / `branch` | `str` | `""` | See [Common arguments](#common-arguments). |

**Returns**: a single Markdown string containing the answer and a `Sources:` block with score semantics, summary, location, timestamp, node UUID, and confidence explanation where available. See the [retrieval score contract](eval-harness-design.md#metrics) and [confidence reference](capture/confidence-scoring.md) for interpretation.

**Example**

```
ask({"question": "What dep bumps shipped in v0.13.0?", "since": "30d"})
```

**Pitfalls**

- Returns an error string (not an exception) if `[models].query` is unset or the inference server is unreachable.
- Probe events filtered out at the query layer (AP-6) — they will never appear in source citations.

---

### `search_knowledge`

Search enriched knowledge nodes; no synthesis. Defaults to semantic; falls back to lexical on embedding failure or when filters are applied.

**Arguments**

| Name | Type | Default | Notes |
|---|---|---|---|
| `query` | `str` | required | Search query text. |
| `mode` | `str` | `"semantic"` | `"semantic"` (vector similarity via the inference server's embedding model) or `"lexical"` (SQL `LIKE` over `knowledge_nodes.content` / `embed_text` — does NOT use the FTS5 index). |
| `limit` | `int` | `10` | |
| `project` / `since` / `source` / `branch` | `str` | `""` | See [Common arguments](#common-arguments). |

When any filter is applied, the implementation forces lexical mode (filter pushdown isn't supported in the semantic path).

**Returns** — list of `SearchResult`-shaped dicts (from `shape_semantic_results` / `search_knowledge_lexical` in `brain/src/hippo_brain/mcp_queries.py`):

```json
{
  "uuid": "node-uuid-...",
  "score": 0.87,
  "summary": "...",
  "intent": "",
  "outcome": "success" | "partial" | "failure" | "unknown",
  "tags": ["tag1", "tag2"],
  "embed_text": "identifier-dense tag soup",
  "cwd": "/Users/.../projects/hippo",
  "git_branch": "main",
  "captured_at": 1730000000000,
  "linked_event_ids": [12345, 12346],
  "linked_claude_session_ids": [501, 502],
  "linked_browser_event_ids": [9001],
  "evidence": [
    {
      "ref": "shell-12345",
      "source_kind": "shell",
      "table": "events",
      "row_id": 12345,
      "timestamp_ms": 1730000000000,
      "excerpt": "cargo build --release",
      "rank": 0,
      "retrieval_score": 0.87
    }
  ]
}
```

`search_hybrid` and semantic `search_knowledge` (via `_retrieve_filtered`) include an `evidence` array on each hit — inspectable citations per [`source-trust-contracts.md`](capture/source-trust-contracts.md). Operator debug: `hippo-evidence-inspect shell-12345`.

The `linked_*_ids` arrays are empty when a node has no links to that source (e.g., a browser-only node returns `[]` for `linked_event_ids`).

---

### `search_hybrid`

Hybrid retrieval (sqlite-vec + FTS5 score fusion) over knowledge nodes. No synthesis; same return shape as `search_knowledge`.

**Arguments**

Same as `search_knowledge`, plus:

| Name | Type | Default | Notes |
|---|---|---|---|
| `mode` | `str` | `"hybrid"` | `"hybrid"` (default — RRF score fusion), `"semantic"`, `"lexical"`, or `"recent"`. |
| `entity` | `str` | `""` | Require a specific canonical entity name to appear among the node's linked entities. |

**Returns**: the `SearchResult` shape above, including `score_semantics` and `confidence`. See the [retrieval score contract](eval-harness-design.md#metrics) for interpretation.

**When to prefer over `search_knowledge`**

`search_hybrid` is the structured retrieval path used by `ask`/`get_context` internally; it supports filter pushdown and the `entity` argument. Reach for `search_knowledge` only when you want the legacy "semantic with lexical fallback" behavior.

---

### `search_events`

Search raw events — shell commands, Claude tool calls, browser visits — not enriched summaries.

**Arguments**

| Name | Type | Default | Notes |
|---|---|---|---|
| `query` | `str` | `""` | Text to search for. Substring match on commands / Claude session summary_text / browser titles. |
| `source` | `str` | `"all"` | `"shell"`, `"claude"`, `"browser"`, `"all"`. |
| `since` / `project` / `branch` | `str` | `""` | See [Common arguments](#common-arguments). |
| `limit` | `int` | `20` | |

**Returns** — list of normalized event dicts. The shape is the same across all three sources (the per-source helpers in `mcp_queries.py` project each row into this canonical envelope):

```json
{
  "id": 12345,
  "source": "shell" | "claude" | "browser",
  "timestamp": 1730000000000,
  "summary": "...",
  "cwd": "/Users/.../projects/hippo",
  "detail": "...",
  "git_branch": "main"
}
```

What lands in `summary` and `detail` is source-specific:

| Source | `summary` | `detail` | `cwd` / `git_branch` |
|---|---|---|---|
| `shell` (rows from `events`) | the command text | `"exit=<code> duration=<ms>ms"` | `cwd` and `git_branch` from the event |
| `claude` (rows from `agentic_sessions` where `harness IN ('claude-code', 'opencode', 'codex', 'cursor')`) | `summary_text` | `"messages=<count> tools=<count>"` (tool count derived from `tool_calls_json`) | `cwd` / `git_branch` from the session |
| `browser` (rows from `browser_events`) | `"<domain> — <title or url>"` | `"dwell=<ms>ms scroll=<pct>%"` | empty strings (browser events have no cwd/branch) |

When `source="all"`, results are interleaved by `timestamp` desc and capped at `limit`.

The original per-table fields (`command`, `exit_code`, `url`, `tool_calls_json`, etc.) are **not** returned — they're projected into `summary`/`detail` and the underlying row stays in SQLite. Use `hippo events` (CLI) or query SQLite directly when you need the raw columns.

---

### `get_entities`

Browse the entities knowledge graph.

**Arguments**

| Name | Type | Default | Notes |
|---|---|---|---|
| `type` | `str` | `""` | One of the schema's `entities.type` CHECK values: `"project"`, `"file"`, `"tool"`, `"service"`, `"repo"`, `"host"`, `"person"`, `"concept"`, `"domain"`, `"env_var"` (added in schema v13). Empty = all types. The brain doesn't emit every category in every corpus — query `get_entities` with no filter to see what your DB actually contains. |
| `query` | `str` | `""` | Substring match on entity name. |
| `limit` | `int` | `50` | |
| `project` | `str` | `""` | Substring match on cwd/git_repo of co-occurring nodes. |
| `since` | `str` | `""` | Window applied to `entities.last_seen`. |

**Returns** — list of entity dicts (`get_entities_impl` in `brain/src/hippo_brain/mcp_queries.py`):

```json
{
  "type": "file",
  "name": "/Users/.../brain/src/hippo_brain/enrichment.py",
  "canonical": "brain/src/hippo_brain/enrichment.py",
  "first_seen": 1730000000000,
  "last_seen": 1730900000000
}
```

`canonical` is the dedup key (worktree-stripped, project-root-relative); `name` is the display value the LLM emitted (worktree-stripped at write time per #105). The internal `entities.id` and any aggregate occurrence count are not exposed today.

---

### `list_projects`

Distinct projects in the corpus, ordered by most-recent activity first.

**Arguments**

| Name | Type | Default | Notes |
|---|---|---|---|
| `limit` | `int` | `50` | |

**Returns** — list of dicts (`list_projects_impl` in `brain/src/hippo_brain/mcp_queries.py`):

```json
{
  "git_repo": "stevencarpenter/hippo",
  "cwd_root": "/Users/carpenter/projects/hippo",
  "last_seen": 1730900000000
}
```

The list is the union of distinct `(git_repo, cwd_root)` pairs from shell `events` and `agentic_sessions` (`project_dir`, restricted to `harness IN ('claude-code', 'codex', 'cursor', 'opencode')`; browser events have no cwd, so they're skipped). `last_seen` is `MAX(timestamp)` / `MAX(start_time)` across both sources. There is no `event_count` field today.

---

### `get_context`

Hybrid retrieval rendered as a Markdown context block, ready to paste into another agent's prompt.

**Arguments** — `query`, `limit`, `project`, `since`, `source` (same semantics as `search_hybrid`). Does NOT accept `mode`, `branch`, or `entity`. Always uses `mode="hybrid"` internally.

**Returns** — single Markdown string rendered by `format_context_block` in `brain/src/hippo_brain/mcp_queries.py`. Shape:

```markdown
# Hippo context for: <query>

## [1] <summary> (score: 0.87)
- **Outcome:** <success/partial/failure>
- **CWD:** `<cwd>`
- **Branch:** `<git_branch>`
- **When:** <ISO timestamp>
- **uuid:** `<uuid>`

<truncated embed_text — up to 600 chars then `…`>

## [2] ...
```

When no results match, the block is `# Hippo context for: <query>\n\n_No relevant knowledge found._`. Embed text per hit is truncated to 600 characters (with a trailing `…`) to keep the block prompt-budget-friendly.

---

### `get_ci_status`

Structured CI status from a recent `git push`.

**Arguments**

| Name | Type | Default | Notes |
|---|---|---|---|
| `repo` | `str` | required | `"owner/repo"` format. |
| `sha` | `str \| None` | `None` | Git commit SHA. |
| `branch` | `str \| None` | `None` | Branch name (used when `sha` is not provided). |

**Returns** — dict with the most recent run's structured data (status, conclusion, jobs with annotations, started_at, completed_at, html_url). Empty dict `{}` if no matching run is found.

**When to prefer over `ask`**

`get_ci_status` is the right tool for "did my push pass CI?" — it returns structured data your script can act on without parsing prose.

---

### `get_lessons`

Distilled past-mistake lessons. Pre-flight before editing in a known failure-prone area.

**Arguments**

| Name | Type | Default | Notes |
|---|---|---|---|
| `repo` | `str \| None` | `None` | `"owner/repo"` format. |
| `path` | `str \| None` | `None` | Returns lessons whose stored `path_prefix` matches as a prefix of this path. |
| `tool` | `str \| None` | `None` | Filter by tool name (`"ruff"`, `"clippy"`, etc.). |
| `limit` | `int` | `10` | |

**Returns** — list of lesson dicts (from `dataclasses.asdict(Lesson)`): `{id, repo, tool, rule_id, path_prefix, summary, fix_hint, occurrences, first_seen_at, last_seen_at}`. `id` is the `lessons.id` primary key.

Lessons graduate only after 2+ occurrences (single failures stay in `lesson_pending` and don't surface here).

---

### `get_agent_scorecard`

Per-agent scorecards: session usage, review findings, verdict rates, escapes, and benchmark trends. Read-only. The result holds counts, sums, rates, and record ids only: no finding `claim`, source `line_text`, or verdict or escape `note` leaves through this tool (AP-14, [design §5.2](superpowers/specs/2026-10-25-agent-eval-schema-design.md)). The brain serves the same JSON at `GET /agents/scorecard`, and `hippo agents report` prints it as one table per agent.

**Which `agent` values join.** The scorecard joins on exact equality, so both sources must carry the registry agent name: `agentic_sessions.agent` (for Claude Code, the subagent type from the subagent's `meta.json`; empty for main sessions) and `eval_review_runs.agent` (the reviewing agent, for example `blind-reviewer`). A producer that writes a different name for the same agent produces a second, split scorecard. Benchmarks have no agent column. `BENCHES_BY_AGENT` in `brain/src/hippo_brain/scorecard_queries.py` lists the benches an agent owns by exact name: `blind-reviewer` owns `blind-review`, `blind-review-mechanical`, and `blind-review-private`. Any other agent owns only the bench named exactly like the agent, so agent `code` never claims bench `code-reviewer`.

**Arguments**

| Name | Type | Default | Notes |
|---|---|---|---|
| `agent` | `str` | `""` | Exact registry agent name. Empty returns one scorecard per agent that has a non-probe session or a review run under the other filters, or, when `project` is empty, a benchmark that matches `model` (a bench maps to its owning agent, else to an agent of the same name). Sorted by name. |
| `since` | `str` | `""` | See [Common arguments](#common-arguments). Applies to an invocation's first segment `start_time`, a run's `started_at`, and an escape's `recorded_at`. It does not apply to benchmarks. `filters.since_ms` in the result shows the threshold applied (0 = all time). The HTTP endpoint rejects a malformed window with 400. |
| `project` | `str` | `""` | Substring of a session's `cwd` or `project_dir`, or of a run's `repo_identity` or `repo`. Does not apply to benchmarks. |
| `model` | `str` | `""` | Exact match on a session's `model` or `requested_model`, a run's `requested_model` or one id in its comma-joined `model_id`, and a benchmark's `model` or one id in its `model_ids_json`. |

**Schema**: the tool needs schema v29 (`eval_*` tables) and v28 (session facts). On an older database it raises an error naming both versions and asking for `mise run install`; `GET /agents/scorecard` returns that message as `{"error": ...}` with status 503, and `hippo agents report` prints it.

**Returns**: `{filters, low_sample_min, scorecards}`. `filters` echoes `agent`, `since_ms`, `project`, `model`. `low_sample_min` is 5. Each entry of `scorecards`:

| Field | Definition | Denominator |
|---|---|---|
| `agent` | The agent name. | |
| `sessions.invocations` | Distinct `(session_id, harness)` pairs in `agentic_sessions` with this `agent`, `probe_tag IS NULL`, matching `project` and `model`, whose first matching segment starts at or after `since`. A subagent transcript is its own `session_id`, so one invocation is one subagent run. | |
| `sessions.subagent_invocations` | Invocations with `is_subagent = 1` as recorded (includes workflow subagents repaired by the v28 backfill). | `invocations` |
| `sessions.distinct_sessions` | Distinct parent sessions: `parent_session_id`, or the invocation's own `session_id` when it has no parent. | |
| `sessions.distinct_projects` | Distinct `project_dir` values. | |
| `sessions.duration_ms.median`, `.p90` | Per invocation, `agentic_session_facts.duration_ms` when present, else the last segment `end_time` minus the first `start_time`. Median of those values (integer ms); p90 is the nearest-rank 90th percentile. `null` with no invocations. | `duration_ms.denominator` (= `invocations`); `from_facts` counts values taken from facts |
| `sessions.tokens` | Sums over matching segments of `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, and `token_count`. `token_count` is the segment total as each writer defines it ([`docs/schema.md`](schema.md#agent-evaluation-columns-v28), `token_count` row): for Claude Code via the daemon it is `input_tokens + output_tokens` without cache tokens, and for opencode it can include cache tokens. It is not the sum of the four other columns. | |
| `sessions.cost_usd.total` | Sum of `agentic_session_facts.cost_usd`; `null` when no invocation has a cost. Claude Code writes the cost from `cost-state` records, which subagent transcripts usually do not carry, so a subagent's cost is usually n/a. | `cost_usd.denominator`: invocations with a non-NULL cost |
| `review_runs.runs`, `.failed_runs` | `eval_review_runs` rows for the agent under `since`, `project`, `model`; failed means `units_failed > 0`. | |
| `review_runs.units_total`, `.units_failed`, `.tokens` | Sums over those runs; `tokens` holds the four reviewer token columns. | |
| `findings.total` | `eval_findings` rows of those runs. | |
| `findings.judged`, `.open` | Findings with and without an effective verdict. | `total` |
| `findings.by_severity`, `.by_category` | Finding counts per `severity` and per `category`. | `total` |
| `findings.by_verdict` | Judged findings per effective verdict (includes `duplicate`). | `judged` |
| `findings.verdict_rates.{accepted,false-positive,not-useful,likely-addressed}` | `{count, denominator, rate, low_sample}`: `count` findings whose effective verdict has that value, `rate = count / denominator` (4 decimals, `null` when the denominator is 0), `low_sample` true when the denominator is below 5. | `judged` |
| `findings.likely_addressed_inferred` | Effective `likely-addressed` verdicts whose fix commit was inferred by the linkage job (note starts with `inferred:`), not observed. Only the boolean leaves; the note does not. | `by_verdict["likely-addressed"]` |
| `findings.likely_addressed_provenance_unknown` | Effective automatic `likely-addressed` verdicts whose `note` payload GC has cleared (`payload_expiry` passed, finding not pinned). The `inferred:` marker lived in that note, so these may be observed or inferred; they are not counted in `likely_addressed_inferred`. | `by_verdict["likely-addressed"]` |
| `escapes` | `{count, attribution}`. `attribution` is `"latest-run-in-repo"`: each `eval_escapes` row goes to one run, the latest run of any agent in the same repository that started before the escape's `recorded_at`. The repository match requires the same `repo` (local path) and the same `repo_identity`, so two repositories with the same path or the same name do not collide. `count` is the escapes (`recorded_at` at or after `since`) whose run belongs to this agent and passes the `since`, `project`, and `model` filters. Runs do not record the paths they reviewed, so attribution is per repository, not per path. | |
| `benchmarks[]` | One entry per `(bench, model)` pair for the benches the agent owns, filtered by `model`, sorted by bench and then model: the latest run (`bench_run_id`, `recorded_at`, `model`, `repo_commit`, `reps`, `cases`, `cases_sha256`, `failed_reviews`, `tokens`) and its `scores` (number and boolean values from `scores_json`; other values are dropped). `previous` is the latest earlier run with the same `bench`, `model`, and `cases_sha256` (`bench_run_id`, `recorded_at`, `scores`), or `null`, so a trend never compares two models or two case sets. `trend` maps each numeric score present in both to `latest - previous`. | |

The effective verdict follows [design §3.4](superpowers/specs/2026-10-25-agent-eval-schema-design.md): the latest human verdict (`auto = 0`) wins, else the latest automatic one, ordered by `decided_at` and then `verdict_id`. A finding with no verdict is open.

**Example**

```
get_agent_scorecard({"agent": "blind-reviewer", "since": "30d"})
```

## Common pitfalls

- **Probe events are filtered out of every tool.** They have `probe_tag IS NOT NULL`. Even `search_events` won't return them — that's intentional (AP-6). If you need them for diagnostics, query `events` directly via SQL.
- **`since` parsing is strict.** Only `^<digits><unit>$` (unit is `m`/`h`/`d`) is accepted — e.g. `"30m"`, `"24h"`, `"7d"`. Inputs with spaces (`"30 m"`), full words (`"30 minutes"`), suffix variants (`"30min"`), bare numbers, or anything else parse to 0 (no filter applied) — silently. There is no parser warning.
- **`source="claude"` covers all agentic harnesses, but does NOT include `claude-tool` events in `search_events`.** `_search_claude_events` queries `agentic_sessions` (where `harness IN ('claude-code', 'opencode', 'codex', 'cursor')`), so `source="claude"` surfaces Codex, Cursor, and opencode sessions alongside Claude Code ones. Claude tool-call events are different: they live in the `events` table (with `source_kind='claude-tool'`) and are returned under `source="shell"` (or `source="all"`) — `_search_shell_events` does not filter by `source_kind`. For `search_hybrid` / `search_knowledge`, `source="claude"` filters knowledge nodes whose linked events/sessions match the agentic-side data.
- **Only `get_agent_scorecard` reads agent-evaluation records, and only as aggregates.** The `eval_*` tables (review findings, verdicts, escapes, bench runs; schema v29) never become knowledge nodes, and no tool result carries a finding `claim`, `line_text`, or verdict or escape `note` (AP-14). Read the text locally with SQL.
- **Filter combinations apply AND, not OR.** `project="hippo"` + `branch="main"` returns only nodes from hippo's main branch.
- **`ask` returns an error STRING for misconfiguration**, not an exception. Check the prefix `"Error:"`.

## See also

- [Source trust contracts](capture/source-trust-contracts.md) — per-source semantics for agents (SNUG-122)
- [Retrieval eligibility](capture/retrieval-eligibility.md) — default noise-control policy (SNUG-121)
- [README MCP Server section](../README.md#mcp-server) — setup
- [`docs/lifecycle.md`](lifecycle.md) — what writes to the tables these tools query
- [`docs/schema.md`](schema.md) — the SQLite tables behind each return shape
- [`brain/src/hippo_brain/mcp.py`](../brain/src/hippo_brain/mcp.py) — authoritative tool definitions
- [`brain/src/hippo_brain/retrieval.py`](../brain/src/hippo_brain/retrieval.py) — the hybrid retrieval layer
