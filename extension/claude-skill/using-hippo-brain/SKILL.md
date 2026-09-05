---
name: using-hippo-brain
description: Use when working in a repo with hippo coverage — query past
  CI outcomes for in-flight pushes, retrieve lessons before editing in
  failure-prone areas, and answer retrospective questions about what
  was done. Do not invoke for routine acknowledgments or tiny exchanges.
---

# Using the Hippo Brain

You have access to a persistent local knowledge base via the `hippo` MCP
server. It captures shell activity, prior Claude sessions, browser
history, and CI outcomes from GitHub Actions. Use it as memory across
sessions.

## When to query (and when not to)

| Situation | Action |
|---|---|
| Starting substantive work in a repo for the first time this session | Optional: `get_lessons(repo=<repo>)` for high-frequency patterns |
| Just edited or about to edit a file with a known failure history | `get_lessons(path=<path>)` |
| `git push` happened earlier in this session | Track the SHA mentally; when the user next re-engages or pauses, call `get_ci_status(repo, sha)` once |
| User asks "did it pass" / "what failed" / "what did I do" | `get_ci_status` or `ask` as appropriate |
| User says "yes", "ok", "proceed", "go ahead" | Do nothing. These are flow control, not work boundaries. |
| Routine multi-turn implementation | Do nothing. Don't poll between every edit. |

## In-flight SHA mental model

After `git push origin <branch>`, that SHA is "in flight" until CI
reaches a terminal state (typically 3–10 min). You don't need to poll.
Check once when the user re-engages after a quiet pause, or when
starting a new task. If CI failed, surface the annotations and propose
a fix — don't bury it. If CI passed, no need to mention unless asked.

## Tool selection

Retrieval, cheapest/most-structured first:

- `search_hybrid(query, mode=hybrid|semantic|lexical|recent)` — ranked hits
  as structured dicts (uuid, score, summary, outcome, cwd, branch, …), no
  synthesis. The default general-purpose retriever.
- `search_knowledge(query, mode=semantic|lexical)` — enriched knowledge
  nodes only. Use when you specifically want distilled knowledge, not raw events.
- `search_events(query, source=shell|claude|browser|all)` — raw event
  timeline (shell commands, sessions, browser history).
- `get_context(query)` — hybrid retrieval rendered as a Markdown block ready
  to paste into a prompt. Use when you want context *for the model*, not a list.
- `ask(question)` — synthesized prose answer (full RAG pipeline). The most
  expensive option; use only for human-shaped questions where prose is wanted.

Targeted lookups:

- `get_ci_status(repo, sha=…|branch=…)` — structured CI outcome. Use for "did it pass."
- `get_lessons(repo?, path?, tool?)` — distilled past mistakes. Use pre-flight.
  Only patterns seen 2+ times graduate — a single failure won't appear.
- `get_entities(type?, query?)` — knowledge-graph entities (project, tool,
  file, domain, concept, service).
- `list_projects()` — distinct projects seen. Use for discovery before scoping.

Prefer the structured retrievers (`search_hybrid` / `search_knowledge`) over
`ask` when you know what shape you want — they are cheaper and machine-friendly.

## Scope every query

All retrieval tools (`search_hybrid`, `search_knowledge`, `search_events`,
`get_context`, `ask`) accept `project` and `since` — using them is the biggest
precision win:

- `project=<repo-or-cwd-substring>` — restrict to the repo you're in.
- `since="24h"` / `"7d"` / `"30m"` — bound the time window.

Most also accept, with two exceptions to watch:

- `source=` — origin filter. `search_hybrid` / `search_knowledge` /
  `get_context` / `ask` take `shell|claude|browser|workflow`; `search_events`
  takes `shell|claude|browser|all` (no `workflow`).
- `branch=<git-branch>` — exact-match the branch. Not available on `get_context`.

When working in a specific repo, default to scoping by `project` — an
unscoped query searches every project you've ever touched.
