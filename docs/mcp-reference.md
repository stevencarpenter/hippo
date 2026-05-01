# MCP Tool Reference

Full reference for hippo's MCP server. Every tool that `hippo-mcp` exposes, with arguments, return shapes, examples, and selection guidance. The MCP server source is `brain/src/hippo_brain/mcp.py`; this doc is what to read instead.

For setup (adding hippo to your MCP config), see the [README's MCP Server section](../README.md#mcp-server). For the trust-boundary discussion (what granting MCP access actually exposes), see the [README's Privacy and Security section](../README.md#privacy-and-security).

## Tool selection guide

| You want… | Reach for | Why |
|---|---|---|
| A synthesized prose answer with cited sources | `ask` | Performs retrieval + LLM synthesis end-to-end. Slow (~1-3 s) but most useful for "what was I working on?" / "how did I fix that?" / "why did we choose X?" |
| A list of relevant knowledge nodes (no synthesis) | `search_knowledge` or `search_hybrid` | Retrieval only. Fastest path. Use `search_hybrid` when you want score-fused vec0 + FTS5 results; `search_knowledge` for the simpler "semantic with lexical fallback" path. |
| A Markdown context block ready to paste into another agent's prompt | `get_context` | Same retrieval as `search_hybrid`, rendered as a prompt-shaped block (numbered list + per-hit summary/outcome/cwd/uuid). |
| Raw shell commands / Claude tool calls / browser visits — not enriched summaries | `search_events` | Operates on the events tables, not knowledge nodes. Use for "what command did I run?" / "what URL was I on?" |
| The list of projects in the corpus | `list_projects` | Use for discovery before filtering other tools by `project`. |
| Extracted entities (project, file, tool, env_var, etc.) | `get_entities` | Knowledge graph view. Filter by `type` to scope. |
| CI status for a recent push | `get_ci_status` | Structured data; preferred over `ask` for known-shape queries. |
| Lessons (graduated recurring CI failures) | `get_lessons` | Pre-flight before editing in a known failure-prone area. Lessons require ≥ 2 occurrences to graduate. |

The ones to reach for first: **`ask`** for natural-language questions with answer synthesis; **`search_hybrid`** for raw retrieval; **`search_events`** for raw event lookup.

## Common arguments

Several tools share filter arguments. They all behave the same way:

| Argument | Type | Behavior |
|---|---|---|
| `since` | `str` | Time window. **Strict format only**: `^<digits><unit>$` where unit is `m`/`h`/`d`. Examples: `"30m"`, `"24h"`, `"7d"`. `parse_since` returns 0 (no filter) for inputs with spaces, words, bare numbers, mixed case, or anything else that doesn't match. Empty string disables. |
| `project` | `str` | Substring match on `cwd` or `git_repo` of the events/sessions linked to a knowledge node. Use `list_projects` first to find candidates. |
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

**Returns** — a single string (Markdown). Begins with the synthesized answer; ends with a `Sources:` block listing each cited node's score, summary, cwd, and timestamp. Rendered nicely by `glow`.

**Example**

```
ask({"question": "What dep bumps shipped in v0.13.0?", "since": "30d"})
```

```
v0.13.0 included two CVE-related upgrades:
- python-multipart 0.0.22 → 0.0.26
- pygments 2.19.2 → 2.20.0

Sources:
  1. [98%] Patched two transitive Python vulnerabilities (python-multipart and pygments)…
     /Users/carpenter/projects/hippo (feat/claude-tool-enrichment-policy) — 2026-04-22
  2. [94%] Pushed v0.13.0 release tag…
     /Users/carpenter/projects/hippo (main) — 2026-04-22
```

**Pitfalls**

- Returns an error string (not an exception) if `[models].query` is unset or LM Studio is unreachable.
- Probe events filtered out at the query layer (AP-6) — they will never appear in source citations.

---

### `search_knowledge`

Search enriched knowledge nodes; no synthesis. Defaults to semantic; falls back to lexical on embedding failure or when filters are applied.

**Arguments**

| Name | Type | Default | Notes |
|---|---|---|---|
| `query` | `str` | required | Search query text. |
| `mode` | `str` | `"semantic"` | `"semantic"` (vector similarity via LM Studio embedding model) or `"lexical"` (SQL `LIKE` over `knowledge_nodes.content` / `embed_text` — does NOT use the FTS5 index). |
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
  "linked_browser_event_ids": [9001]
}
```

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

**Returns** — same `SearchResult` shape as `search_knowledge`.

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
| `claude` (rows from `claude_sessions`) | `summary_text` | `"messages=<count> tools=<count>"` (tool count derived from `tool_calls_json`) | `cwd` / `git_branch` from the session |
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

The list is the union of distinct `(git_repo, cwd_root)` pairs from shell `events` and `claude_sessions` (browser events have no cwd, so they're skipped). `last_seen` is `MAX(timestamp)` / `MAX(start_time)` across both sources. There is no `event_count` field today.

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

## Common pitfalls

- **Probe events are filtered out of every tool.** They have `probe_tag IS NOT NULL`. Even `search_events` won't return them — that's intentional (AP-6). If you need them for diagnostics, query `events` directly via SQL.
- **`since` parsing is strict.** Only `^<digits><unit>$` (unit is `m`/`h`/`d`) is accepted — e.g. `"30m"`, `"24h"`, `"7d"`. Inputs with spaces (`"30 m"`), full words (`"30 minutes"`), suffix variants (`"30min"`), bare numbers, or anything else parse to 0 (no filter applied) — silently. There is no parser warning.
- **`source="claude"` does NOT include `claude-tool` events in `search_events`.** `_search_claude_events` queries `claude_sessions` only. Claude tool-call events live in the `events` table (with `source_kind='claude-tool'`) and are returned under `source="shell"` (or `source="all"`) — `_search_shell_events` does not filter by `source_kind`. For `search_hybrid` / `search_knowledge`, `source="claude"` filters knowledge nodes whose linked events/sessions match the claude-side data.
- **Filter combinations apply AND, not OR.** `project="hippo"` + `branch="main"` returns only nodes from hippo's main branch.
- **`ask` returns an error STRING for misconfiguration**, not an exception. Check the prefix `"Error:"`.

## See also

- [README MCP Server section](../README.md#mcp-server) — setup
- [`docs/lifecycle.md`](lifecycle.md) — what writes to the tables these tools query
- [`docs/schema.md`](schema.md) — the SQLite tables behind each return shape
- [`brain/src/hippo_brain/mcp.py`](../brain/src/hippo_brain/mcp.py) — authoritative tool definitions
- [`brain/src/hippo_brain/retrieval.py`](../brain/src/hippo_brain/retrieval.py) — the hybrid retrieval layer
