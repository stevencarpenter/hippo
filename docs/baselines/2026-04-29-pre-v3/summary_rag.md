# RAG / `hippo ask` expert summary
Mean scores: accuracy 4.36 / succinctness 3.73 / usefulness 3.26 / ask 3.44 / mcp 4.02

Corpus stats (post-truncation render):
- 10/100 nodes start summary with filler openings ("The user…", "Conducted a…") that burn the 120-char budget
- 18/100 nodes start summary with a strong action verb tied to a concrete artefact
- 39/100 nodes have populated `design_decisions`; 6 clearly weighed alternatives in source but emitted an empty list
- **66/100 nodes have populated `key_decisions` or `problems_encountered` content (RAG render NEVER surfaces these fields).** 5/100 cross the high bar of ≥600 chars + ≥5 unique identifiers not visible in summary/embed_text — these are pure render losses with no compensation elsewhere.
- 24/100 nodes hit the 500-char Entities cap; 11 of those lose env_vars to truncation

## Top strengths (top 3, concrete)
- Strong-open summaries land in the survivable 120-char window: e.g. `8479a5a0-c0ab-477a-ba84-6ed6bc5caa05` and `0b7eab41-17b7-42f0-b993-c6e06013c7b2` lead with a concrete verb + artefact, so the synthesis LLM has something to anchor on before the truncate cliff.
- Where `design_decisions` is populated (39 nodes), the verbatim `considered/chose/reason` triples are exactly the synthesis substrate the LLM needs to answer "why did I pick X?" questions — see `842bfe93-ec2e-4b4b-af2d-769485a8f887` and `94aaf8be-4f3d-41e8-a630-526a61bf3c1a`.
- Identifier-dense embed_text front-loads paths and symbols, so the 800-char Detail truncation in 10-hit RAG context still preserves the high-value tokens (e.g. `65bb10db-7a8b-4466-82cd-d075169a6d63`).

## Top weaknesses (top 3, concrete)
- **Filler summary openings burn the 120-char Summary budget**: 10/100 nodes start with phrases like "The user requested…" or "Conducted a comprehensive…". After truncation the synthesizer sees no concrete artefact. Worst offenders: `67368c74-9405-4f53-8341-1da7bf98e634`, `0111018e-2904-4f3b-b7a9-bf72584af367`.
- **Substantive content trapped in non-rendered fields**: 5/100 nodes have ≥600 chars of unique-identifier-bearing content in `key_decisions`/`problems_encountered` that RAG render skips entirely (see Render gaps below). The synthesizing LLM literally never sees this content — e.g. `25b32204-7111-4d8d-8f9b-e0a2d5e4610a` (1906 chars orphaned, 8 unique identifiers not visible elsewhere).
- **`design_decisions` empty when source clearly weighed alternatives**: 6/100 nodes have alternative-weighing language ("instead of", "considered", "rather than") in source but emit `design_decisions: []`. "Why did I pick X?" questions return retrieval-blind. e.g. `25b32204-7111-4d8d-8f9b-e0a2d5e4610a`.

## Worst 5 nodes
| uuid | reason |
|---|---|
| 67368c74-9405-4f53-8341-1da7bf98e634 | ask=1 use=3 mcp=5 | filler-open, orphan(1548c), env-lost |
| 0111018e-2904-4f3b-b7a9-bf72584af367 | ask=1 use=2 mcp=3 | dd-warranted-missing |
| 25b32204-7111-4d8d-8f9b-e0a2d5e4610a | ask=1 use=3 mcp=5 | orphan(1906c), env-lost |
| 6dd039b8-5525-4539-87ed-782f59ed6ad5 | ask=1 use=3 mcp=5 | orphan(1906c), env-lost |
| b239a21e-24d2-434e-b4bf-ffe8ebe757c1 | ask=1 use=3 mcp=3 |

## Render gaps (RAG lens — actionable)
Fields the model populates well but the RAG synthesis prompt builder NEVER renders. These are the biggest wins available without re-enriching: add render branches in `brain/src/hippo_brain/rag.py::_hit_lines`.

1. **`key_decisions` (most damaging)** — populated on a large fraction of nodes with terse, identifier-rich bullet summaries ("Decided to use X instead of Y because Z"). This is exactly synthesis-prompt-shaped content and is dropped on the floor. Examples with the most orphaned content: `25b32204-7111-4d8d-8f9b-e0a2d5e4610a` (1906 chars), `6dd039b8-5525-4539-87ed-782f59ed6ad5` (1906 chars), `6dbadd52-7c4e-4b92-a770-f0e330015656` (1646 chars).
2. **`problems_encountered`** — when populated, often carries the *error message* and *recovery action* the user will absolutely query for ("what went wrong with X?"). Render currently skips it. Notable cases: `87976564-3ace-4903-b8a4-0e666720d4f1`, `3a3ccf2f-2ef2-4a2d-8f5d-20e8ba2c1db3`.
3. **`outcome` is rendered as a one-token line** but the *meaning* of "partial" or "failure" is in `problems_encountered`. Currently the synthesizer sees `Outcome: partial` with no context for why, even though the model wrote it down. Same orphans as #2.

## Cross-cutting observation
The single biggest lever for `hippo ask` quality is NOT re-enrichment — it's plumbing. 66/100 nodes have populated content in `key_decisions` and/or `problems_encountered` that the RAG render in `brain/src/hippo_brain/rag.py::_hit_lines` silently drops. The synthesis LLM never sees any of it, regardless of how good the enrichment is. Add two render branches ("Decisions:" and "Problems:" lines under the same proportional truncation budgeting already applied to embed_text/commands/design_decisions) and the post-truncation context for the questions users actually type ("why did I do X?", "what error did I hit when Y?") would jump materially without touching the model. Secondary lever: a 10-node tail of filler-opening summaries ("The user requested…") burns the 120-char Summary budget. That's a prompt fix in the enricher ("Lead the summary with a strong verb + artefact, never with subject-first prose"), not a structural change.
