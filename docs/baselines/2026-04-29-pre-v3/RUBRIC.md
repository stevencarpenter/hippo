# Hippo v3 enrichment scorecard rubric

You are one of five experts on a panel scoring 100 re-enriched knowledge nodes
produced by `qwen3.6-35b-a3b-ud-mlx` against the v3 enrichment system prompt.

## Inputs
- `/tmp/hippo-eval-panel/dossier.jsonl` — one JSON per line, 100 lines.
  Each record has:
    node_id, uuid, stratum, source ("claude" | "shell" | "dual"),
    enrichment_model,
    content: {summary, intent, outcome, entities{projects,tools,files,services,errors,env_vars,domains?}, tags, key_decisions, problems_encountered, design_decisions},
    embed_text, content_len, embed_text_len,
    shell_events (if shell or dual): event rows with command/exit_code/cwd/stdout_truncated/stderr_truncated/shell,
    claude_segments_text (if claude or dual): joined summary_text,
    claude_session_meta (if claude or dual): per-segment metadata.

## v3 system-prompt rules the model was bound by
1. **Verbatim preservation** of identifiers, env vars (UPPERCASE_WITH_UNDERSCORES),
   versions (\d+\.\d+\.\d+), package@version, symbol names, CLI flags, file paths,
   command names. If unsure → omit, never guess.
2. **`embed_text` must be identifier-dense tag soup** — keyword retrieval, not prose.
3. **Specific, not generic** summaries (no "edited a Rust file").
4. **`design_decisions`** must be a list of {considered, chosen, reason} objects;
   empty list if no alternatives were weighed.
5. **Entity buckets**: projects/tools/files/services/errors/env_vars.
   Worktree prefixes (`.claude/worktrees/<X>/`) stripped from path-typed entity names.
6. **Outcome** ∈ {success, partial, failure, unknown}.

## Consumption sites (informs "suitability" scores)
- `hippo ask` RAG synthesis renders: `Summary:` (capped ~120 chars), `Entities:` flat
  identifier line (path-typed: tool/file/service/project/env_var only), `Detail:`
  (truncated `embed_text`), `design_decisions` verbatim. Truncation caps mean
  dense, front-loaded content wins.
- MCP tools: `mcp__hippo__{ask, search_knowledge, search_events, get_entities,
  search_hybrid}`. `search_knowledge` ranks by FTS over summary+embed_text+content
  blended with sqlite-vec cosine on the embed_text vector. `get_entities` reads
  the entity buckets directly.

## Per-node scoring (1=poor, 5=excellent)
Score every node on EVERY dimension. Use 1-5 integers only.

- **accuracy** — Does the enrichment faithfully represent the source? Any
  hallucinated identifiers, fabricated outcomes, or contradicted facts?
  Verbatim-preservation rule violations are accuracy hits, not succinctness.
- **succinctness** — Is `summary` informative without bloat? Is `embed_text`
  appropriately sized (not too sparse, not padded)? Are key_decisions /
  problems_encountered / design_decisions tight?
- **usefulness** — Does the enrichment capture the *substance* of the work
  (decisions, outcomes, what changed) vs surface-level "what tools were used"?
- **ask_suitability** — When the truncated render hits a user via `hippo ask`,
  does it actually help answer realistic questions? Score the post-truncation
  utility, not the raw fields.
- **mcp_suitability** — Will the four MCP tools surface this node well?
  Specifically: clean entity buckets for `get_entities`, FTS-friendly
  summary+embed_text for `search_knowledge`, identifier density for hybrid
  search ranking, design_decisions usefulness for `ask`.

## Output format

Write your output as JSONL to `/tmp/hippo-eval-panel/scores_<EXPERT_ID>.jsonl`,
one row per node:

    {"node_id": 1234, "uuid": "...", "accuracy": 4, "succinctness": 5,
     "usefulness": 4, "ask_suitability": 4, "mcp_suitability": 5,
     "notes": "<≤140 chars; concrete>"}

Then write a markdown summary to `/tmp/hippo-eval-panel/summary_<EXPERT_ID>.md`:

    # <Expert role> summary
    Mean scores: accuracy X.X / succinctness X.X / usefulness X.X / ask X.X / mcp X.X
    ## Top strengths (top 3, concrete)
    ## Top weaknesses (top 3, concrete)
    ## Worst 5 nodes
    | uuid | reason |
    ## Cross-cutting observation
    <1 paragraph: the single most important finding from your lens>

## Discipline
- Score independently. Don't try to triangulate with other experts.
- Be willing to score 1 or 5 — flat 3s across the board are useless.
- Cite specific node uuids in your strengths/weaknesses, never vague.
- If you find a structural bug (e.g., missing field, wrong shape), flag it in
  notes, but don't conflate it with quality.
