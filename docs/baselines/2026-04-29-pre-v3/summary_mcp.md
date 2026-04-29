# MCP-consumer summary

Mean scores: accuracy 4.78 / succinctness 4.53 / usefulness 4.33 / ask 4.50 / mcp 3.80

Nodes scored: 100

## Top strengths (top 3, concrete)

- **Identifier-dense embed_text wins for `search_hybrid`.** Mean per-tool score for hybrid input is 4.63/5. Best example: `25b32204-7111-4d8d-8f9b-e0a2d5e4610a` packs paths, env vars, and CLI flags into a tag-soup line that FTS5 will hit on dozens of plausible queries.
- **`design_decisions` is mostly well-shaped.** Across 100 nodes only a handful violate the {considered,chosen,reason} schema, so `mcp__hippo__ask` can splice them verbatim into answers without post-processing.
- **Path-typed entities are usable for `get_entities` filtering.** Best node: `e276ab79-158b-4e76-a35d-6fbc54d0706c` — mean per-tool entity-bucket score 4.87/5.

## Top weaknesses (top 3, concrete)

- **`get_entities` returns inconsistent surface forms across nodes.** See drift list below — same logical entity (e.g. `hippo-daemon` vs `crates/hippo-daemon`) shows up in multiple spellings, breaking exact-match filtering by the calling agent. Worst node: `c1260596-e479-4bc7-a9ff-6904f722fefd`.
- **Some `embed_text` lines drift toward prose, hurting `search_hybrid` FTS hits.** Worst node: `1b1ac570-0ec9-4404-93c3-e1852b1a3333` — reads like a paragraph instead of tag soup; cosine ranking will dominate and lexical hits will miss.
- **`ask` answers risk being hand-wavy when the first-120-char summary head lacks identifiers.** Worst-for-ask node: `1b1ac570-0ec9-4404-93c3-e1852b1a3333`. Truncation render in `format_rag_response` caps `Summary:` ~120 chars; if the head is generic, the synthesized answer inherits the vagueness.

## Worst 5 nodes

| uuid | reason |
| --- | --- |
| `fe5d66df-c6f0-43bd-ad00-f6f18bbd7f0f` | lower env_var; bloated summary >700ch; 4 bucket entities absent in embed |
| `b239a21e-24d2-434e-b4bf-ffe8ebe757c1` | very short summary; low embed identifier density (5); no decisions for ask synthesis |
| `da979c70-f512-4dc3-ba51-2a93ba3f6935` | very short summary; low embed identifier density (7); no decisions for ask synthesis |
| `b9a5e0cd-a994-4c78-8921-64c2a23dc757` | very short summary; low embed identifier density (7); no decisions for ask synthesis |
| `5bbbc30a-fe97-4ce4-a12a-b84eafb208c7` | very short summary; low embed identifier density (7); no decisions for ask synthesis |

## MCP-tool-specific findings

- **`mcp__hippo__get_entities`** — mean 4.87/5. Best uuid: `e276ab79-158b-4e76-a35d-6fbc54d0706c`. Worst uuid: `c1260596-e479-4bc7-a9ff-6904f722fefd`.
- **`mcp__hippo__search_knowledge`** — mean 4.82/5. Best uuid: `25b32204-7111-4d8d-8f9b-e0a2d5e4610a`. Worst uuid: `1b1ac570-0ec9-4404-93c3-e1852b1a3333`.
- **`mcp__hippo__search_hybrid`** — mean 4.63/5. Best uuid: `25b32204-7111-4d8d-8f9b-e0a2d5e4610a`. Worst uuid: `1b1ac570-0ec9-4404-93c3-e1852b1a3333`.
- **`mcp__hippo__ask`** — mean 4.5/5. Best uuid: `25b32204-7111-4d8d-8f9b-e0a2d5e4610a`. Worst uuid: `1b1ac570-0ec9-4404-93c3-e1852b1a3333`.
- **`mcp__hippo__search_events`** — mean 4.96/5. Best uuid: `25b32204-7111-4d8d-8f9b-e0a2d5e4610a`. Worst uuid: `b63c3f9b-d9b5-4510-ad13-afe41d5a3435`.

Common failure modes per tool:

- `get_entities`: worktree-prefixed paths leaking through, lowercased env vars, mis-bucketed paths landing in `tools[]`.
- `search_knowledge`: summaries occasionally too short to disambiguate results (<60 chars) or too long (>1200) to fit a tool result list.
- `search_hybrid`: prose-y embed_text reduces FTS5 token diversity; low identifier-uniqueness lowers BM25 ranking.
- `ask`: malformed `design_decisions` (non-dict items) and missing outcome strings make synthesized answers hedge.
- `search_events`: not directly scored from enrichments — but a node with no entity files/tools provides no anchor to correlate a returned event back to enriched context.

## Cross-node consistency check

Drift in surface forms across the 100 nodes:

  - **file** `/users/carpenter/projects/hippo/brain/src/hippo_brain/claude_sessions.py`: 8 occurrences, forms: ['/Users/carpenter/projects/hippo/.claude/worktrees/youthful-kirch-7d3b27/brain/src/hippo_brain/claude_sessions.py', '/Users/carpenter/projects/hippo/brain/src/hippo_brain/claude_sessions.py']
  - **file** `/users/carpenter/projects/hippo/brain/src/hippo_brain/enrichment.py`: 3 occurrences, forms: ['/Users/carpenter/projects/hippo/.claude/worktrees/feat-p2.2-synthetic-probes/brain/src/hippo_brain/enrichment.py', '/Users/carpenter/projects/hippo/brain/src/hippo_brain/enrichment.py']
  - **file** `/users/carpenter/projects/hippo/brain/src/hippo_brain/schema_version.py`: 5 occurrences, forms: ['/Users/carpenter/projects/hippo/.claude/worktrees/youthful-kirch-7d3b27/brain/src/hippo_brain/schema_version.py', '/Users/carpenter/projects/hippo/brain/src/hippo_brain/schema_version.py']
  - **file** `/users/carpenter/projects/hippo/brain/src/hippo_brain/server.py`: 8 occurrences, forms: ['/Users/carpenter/projects/hippo/.claude/worktrees/feat-p2.2-synthetic-probes/brain/src/hippo_brain/server.py', '/Users/carpenter/projects/hippo/brain/src/hippo_brain/server.py']
  - **file** `/users/carpenter/projects/hippo/brain/tests/test_enrichment.py`: 5 occurrences, forms: ['/Users/carpenter/projects/hippo/.claude/worktrees/gracious-williamson-8c3e1f/brain/tests/test_enrichment.py', '/Users/carpenter/projects/hippo/brain/tests/test_enrichment.py']
  - **file** `/users/carpenter/projects/hippo/crates/hippo-core/src/config.rs`: 12 occurrences, forms: ['/Users/carpenter/projects/hippo/.claude/worktrees/youthful-kirch-7d3b27/crates/hippo-core/src/config.rs', '/Users/carpenter/projects/hippo/crates/hippo-core/src/config.rs']
  - **file** `/users/carpenter/projects/hippo/crates/hippo-core/src/schema.sql`: 8 occurrences, forms: ['/Users/carpenter/projects/hippo/.claude/worktrees/youthful-kirch-7d3b27/crates/hippo-core/src/schema.sql', '/Users/carpenter/projects/hippo/crates/hippo-core/src/schema.sql']
  - **file** `/users/carpenter/projects/hippo/crates/hippo-core/src/storage.rs`: 13 occurrences, forms: ['/Users/carpenter/projects/hippo/.claude/worktrees/feat-p2.2-synthetic-probes/crates/hippo-core/src/storage.rs', '/Users/carpenter/projects/hippo/crates/hippo-core/src/storage.rs']
  - **file** `/users/carpenter/projects/hippo/crates/hippo-daemon/src/claude_session.rs`: 14 occurrences, forms: ['/Users/carpenter/projects/hippo/.claude/worktrees/feat-p2.2-synthetic-probes/crates/hippo-daemon/src/claude_session.rs', '/Users/carpenter/projects/hippo/.claude/worktrees/youthful-kirch-7d3b27/crates/hippo-daemon/src/claude_session.rs', '/Users/carpenter/projects/hippo/crates/hippo-daemon/src/claude_session.rs']
  - **file** `/users/carpenter/projects/hippo/crates/hippo-daemon/src/commands.rs`: 10 occurrences, forms: ['/Users/carpenter/projects/hippo/.claude/worktrees/feat-p2.2-synthetic-probes/crates/hippo-daemon/src/commands.rs', '/Users/carpenter/projects/hippo/crates/hippo-daemon/src/commands.rs']
  - **file** `/users/carpenter/projects/hippo/crates/hippo-daemon/src/daemon.rs`: 12 occurrences, forms: ['/Users/carpenter/projects/hippo/.claude/worktrees/feat-p2.2-synthetic-probes/crates/hippo-daemon/src/daemon.rs', '/Users/carpenter/projects/hippo/crates/hippo-daemon/src/daemon.rs']
  - **file** `/users/carpenter/projects/hippo/crates/hippo-daemon/src/lib.rs`: 6 occurrences, forms: ['/Users/carpenter/projects/hippo/.claude/worktrees/feat-p2.2-synthetic-probes/crates/hippo-daemon/src/lib.rs', '/Users/carpenter/projects/hippo/crates/hippo-daemon/src/lib.rs']
  - **file** `/users/carpenter/projects/hippo/crates/hippo-daemon/src/main.rs`: 8 occurrences, forms: ['/Users/carpenter/projects/hippo/.claude/worktrees/feat-p2.2-synthetic-probes/crates/hippo-daemon/src/main.rs', '/Users/carpenter/projects/hippo/crates/hippo-daemon/src/main.rs']
  - **file** `/users/carpenter/projects/hippo/crates/hippo-daemon/src/native_messaging.rs`: 3 occurrences, forms: ['/Users/carpenter/projects/hippo/.claude/worktrees/feat-p2.2-synthetic-probes/crates/hippo-daemon/src/native_messaging.rs', '/Users/carpenter/projects/hippo/crates/hippo-daemon/src/native_messaging.rs']
  - **file** `/users/carpenter/projects/hippo/crates/hippo-daemon/src/probe.rs`: 2 occurrences, forms: ['/Users/carpenter/projects/hippo/.claude/worktrees/feat-p2.2-synthetic-probes/crates/hippo-daemon/src/probe.rs', '/Users/carpenter/projects/hippo/crates/hippo-daemon/src/probe.rs']
  - **file** `/users/carpenter/projects/hippo/crates/hippo-daemon/src/watch_claude_sessions.rs`: 7 occurrences, forms: ['/Users/carpenter/projects/hippo/.claude/worktrees/agent-a7a4d92ee3f65fce3/crates/hippo-daemon/src/watch_claude_sessions.rs', '/Users/carpenter/projects/hippo/.claude/worktrees/youthful-kirch-7d3b27/crates/hippo-daemon/src/watch_claude_sessions.rs', '/Users/carpenter/projects/hippo/crates/hippo-daemon/src/watch_claude_sessions.rs']
  - **file** `/users/carpenter/projects/hippo/crates/hippo-daemon/tests/source_audit/claude_tool_events.rs`: 2 occurrences, forms: ['/Users/carpenter/projects/hippo/.claude/worktrees/feat-p2.2-synthetic-probes/crates/hippo-daemon/tests/source_audit/claude_tool_events.rs', '/Users/carpenter/projects/hippo/crates/hippo-daemon/tests/source_audit/claude_tool_events.rs']

Cross-bucket drift (same canonical name in multiple buckets): 17 cases — e.g. ['sqlite3', 'chezmoi', 'hippo doctor', 'superpowers', 'grafana'].

## Cross-cutting observation

From the consuming-agent perspective, the v3 enrichments are mostly high-quality fuel for `ask` and `search_hybrid`: summaries are specific, `embed_text` is tag-soupy, and entity buckets carry real identifiers. Two consumer-facing taxes dominate. **(1) Worktree-path leakage — 5/100 nodes still ship `.claude/worktrees/<X>/...` prefixed paths in `entities.files`** despite v3 rule 5 explicitly stripping them. This breaks `get_entities(type='file')` exact-match filtering and creates hybrid-search drift between worktree clones and main-tree paths for the same file. **(2) Cross-bucket canonicalization drift** — the same component shows up as `hippo-daemon` (tag), `hippo daemon` (tool), and `/Users/carpenter/projects/hippo/crates/hippo-daemon/...` (file) without a canonical mapping. An agent that does `get_entities(type='file')` then `search_hybrid(entity=name)` will pick one spelling and miss the others. The `entities.canonical` column already exists in the schema but is largely unused; populating it (and stripping worktree prefixes at enrichment time) would close the gap without touching the LLM prompt.
