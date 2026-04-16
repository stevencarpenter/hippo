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

- `get_ci_status(repo, sha)` — structured CI outcome. Use for "did it pass."
- `get_lessons(repo?, path?, tool?)` — distilled past mistakes. Use pre-flight.
- `search_knowledge(query)` — semantic retrieval over knowledge nodes.
- `search_events(query)` — raw event timeline.
- `ask(question)` — synthesized prose answer. Use for human-shaped questions.
- `get_entities(...)` — graph exploration.

Prefer the structured tools over `ask` when you know what shape you
want — they are cheaper and machine-friendly. `ask` runs a full RAG
pipeline and returns prose.
