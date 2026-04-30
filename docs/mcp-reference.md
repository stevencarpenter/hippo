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
| `since` | `str` | Time window: `"30m"`, `"24h"`, `"7d"`, `"30d"`. Parses to epoch-ms, filters events/sessions newer than that. Empty string disables the filter. |
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
| `mode` | `str` | `"semantic"` | `"semantic"` (vector similarity via LM Studio embedding model) or `"lexical"` (FTS5 / LIKE fallback). |
| `limit` | `int` | `10` | |
| `project` / `since` / `source` / `branch` | `str` | `""` | See [Common arguments](#common-arguments). |

When any filter is applied, the implementation forces lexical mode (filter pushdown isn't supported in the semantic path).

**Returns** — list of `SearchResult`-shaped dicts:

```json
{
  "uuid": "node-uuid-...",
  "score": 0.87,
  "summary": "...",
  "embed_text": "identifier-dense tag soup",
  "outcome": "success" | "partial" | "failure" | "unknown",
  "tags": ["tag1", "tag2"],
  "cwd": "/Users/.../projects/hippo",
  "git_branch": "main",
  "captured_at": 1730000000000,
  "design_decisions": [{"considered": "...", "chosen": "...", "reason": "..."}, ...],
  "linked_event_ids": [12345, 12346, ...]
}
```

`design_decisions` is empty for nodes enriched before PR #100. `linked_event_ids` is empty for browser-only or workflow-only nodes.

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

**Returns** — list of event dicts. Shape depends on `source`:

- Shell: `{id, timestamp, command, exit_code, duration_ms, cwd, git_branch, source_kind, tool_name}` — no stdout/stderr in the response (those are stored but not returned by default to keep payloads bounded).
- Claude session: `{id, session_id, segment_index, summary_text, message_count, start_time, end_time}`.
- Browser: `{id, timestamp, url, title, domain, dwell_ms, scroll_depth}`.

When `source="all"`, results are interleaved by timestamp.

---

### `get_entities`

Browse the entities knowledge graph.

**Arguments**

| Name | Type | Default | Notes |
|---|---|---|---|
| `type` | `str` | `""` | One of `"project"`, `"tool"`, `"file"`, `"directory"`, `"path"`, `"service"`, `"repo"`, `"host"`, `"person"`, `"concept"`, `"domain"`, `"env_var"` (added in schema v13). Empty = all types. |
| `query` | `str` | `""` | Substring match on entity name. |
| `limit` | `int` | `50` | |
| `project` | `str` | `""` | Substring match on cwd/git_repo of co-occurring nodes. |
| `since` | `str` | `""` | Window applied to `entities.last_seen`. |

**Returns** — list of entity dicts:

```json
{
  "id": 1234,
  "type": "file",
  "name": "/Users/.../brain/src/hippo_brain/enrichment.py",
  "canonical": "brain/src/hippo_brain/enrichment.py",
  "first_seen": 1730000000000,
  "last_seen": 1730900000000,
  "occurrences": 47
}
```

`canonical` is the dedup key (worktree-stripped, project-root-relative); `name` is the display value the LLM emitted (worktree-stripped at write time per #105).

---

### `list_projects`

Distinct projects in the corpus, ordered by most-recent activity first.

**Arguments**

| Name | Type | Default | Notes |
|---|---|---|---|
| `limit` | `int` | `50` | |

**Returns** — list of dicts:

```json
{
  "git_repo": "stevencarpenter/hippo",
  "cwd_root": "/Users/carpenter/projects/hippo",
  "last_activity": 1730900000000,
  "event_count": 3247
}
```

Use this for discovery before filtering other tools by `project`.

---

### `get_context`

Hybrid retrieval rendered as a Markdown context block, ready to paste into another agent's prompt.

**Arguments** — same as `search_hybrid` minus `mode` and `branch`. Always uses `mode="hybrid"`.

**Returns** — single Markdown string. Shape:

```markdown
## Context for: <query>

### 1. <summary> [score]
- cwd: <cwd> · branch: <branch> · captured: <date>
- uuid: <uuid>
- outcome: <success/partial/failure>

<truncated embed_text>

### 2. ...
```

Embed text per hit is truncated to keep the block prompt-budget-friendly.

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

**Returns** — list of lesson dicts: `{repo, tool, rule_id, path_prefix, summary, fix_hint, occurrences, first_seen_at, last_seen_at}`.

Lessons graduate only after 2+ occurrences (single failures stay in `lesson_pending` and don't surface here).

## Common pitfalls

- **Probe events are filtered out of every tool.** They have `probe_tag IS NOT NULL`. Even `search_events` won't return them — that's intentional (AP-6). If you need them for diagnostics, query `events` directly via SQL.
- **`since` parsing is liberal.** `"30m"`, `"30 m"`, `"30 minutes"`, `"30min"` all work. Bare numbers are interpreted as minutes. Empty string disables.
- **`source="claude"` covers both `claude-tool` events and `claude_sessions`** for `search_events`. For `search_hybrid` / `search_knowledge`, it filters knowledge nodes that link back to either.
- **Filter combinations apply AND, not OR.** `project="hippo"` + `branch="main"` returns only nodes from hippo's main branch.
- **`ask` returns an error STRING for misconfiguration**, not an exception. Check the prefix `"Error:"`.

## See also

- [README MCP Server section](../README.md#mcp-server) — setup
- [`docs/lifecycle.md`](lifecycle.md) — what writes to the tables these tools query
- [`docs/schema.md`](schema.md) — the SQLite tables behind each return shape
- [`brain/src/hippo_brain/mcp.py`](../brain/src/hippo_brain/mcp.py) — authoritative tool definitions
- [`brain/src/hippo_brain/retrieval.py`](../brain/src/hippo_brain/retrieval.py) — the hybrid retrieval layer
