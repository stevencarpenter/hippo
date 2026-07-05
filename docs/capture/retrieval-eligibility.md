# Retrieval eligibility (SNUG-121)

User-facing agent retrieval (RAG, MCP `ask` / `search_knowledge`, brain `/ask`) applies a single eligibility policy so operational metadata never masquerades as user-meaningful evidence.

Implementation: `brain/src/hippo_brain/retrieval_eligibility.py`. Wired through `retrieval.Filters.include_excluded` and env `HIPPO_RETRIEVAL_INCLUDE_EXCLUDED=1`.

## Default policy by source family

| Family | Included when | Excluded by default |
|--------|---------------|---------------------|
| **shell** | `events.probe_tag IS NULL` | Synthetic probe commands (`probe_tag` set) |
| **browser** | `browser_events.probe_tag IS NULL` | Probe canary page views |
| **agentic** (claude/codex/cursor/opencode) | Non-probe, non-journal, non-empty, settled segment | `probe_tag` set; `journal.jsonl` under `subagents/.../workflows/`; `message_count=0` with blank `summary_text`; `end_time` within 90s settle window of now (in-flight) |
| **workflow** (GitHub Actions) | `conclusion IS NOT NULL` or `status = completed` | In-progress status journal rows |
| **claude-auto-memory** | Active document revision (`memory_documents.state = 'active'`) | Superseded / inactive revisions |

Enrichment-time filters (`is_enrichment_eligible`) prevent most noise from ever becoming knowledge nodes. Retrieval eligibility is defense-in-depth for rows that slip through or for hydration of linked sources.

## Operator / debug mode

Set any of:

- `Filters(include_excluded=True)` on programmatic retrieval
- MCP tool param `include_excluded=true` on `search_knowledge` and `ask`
- Environment `HIPPO_RETRIEVAL_INCLUDE_EXCLUDED=1` for the brain/MCP process

Operator mode disables the eligibility gate so capture-health investigations can see probe and diagnostic rows. **Never enable in production agent defaults.**

## Adding a new source

1. Add enrichment eligibility in `enrichment.is_enrichment_eligible` (capture path).
2. Add retrieval SQL helpers in `retrieval_eligibility.py`.
3. Extend `knowledge_node_eligible_exists_sql` with an `EXISTS` branch.
4. Add a regression test in `brain/tests/test_retrieval_eligibility.py`.

See also AP-6 in [`anti-patterns.md`](anti-patterns.md).
