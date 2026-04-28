# RAG entity surfacing — fix for issue #108

**Status:** Design complete, ready for implementation plan.
**Issue:** [#108 — RAG synthesis misses identifiers present in retrieved embed_text (per-hit truncation in `_hit_lines`)](https://github.com/stevencarpenter/hippo/issues/108)
**Authors:** Steven Carpenter (with Claude as drafting partner)
**Date:** 2026-04-27

## Problem

`brain/src/hippo_brain/rag.py::_build_rag_prompt` proportionally truncates each retrieval hit's `embed_text`, `commands_raw`, and `design_decisions` to fit `DEFAULT_MAX_CONTEXT_CHARS` (8000). With 10 hits and only `embed_text` + `design_decisions` carrying real content (since `_result_to_hit` hardcodes `commands_raw=""` on the SQLite path), per-hit `embed_text` gets cut to a few hundred characters. Identifiers at the tail of the identifier-dense `embed_text` (PR #100) are dropped mid-token. The synthesis model (`qwen3.6-35b-a3b-ud-mlx`) follows the VERBATIM PRESERVATION rule correctly given what it sees and reports the identifier as missing.

Concrete repro from issue #108: `hippo ask "what env var does dedup-entities.py require?"` returns *"the context does not mention any specific environment variable…"* even though `HIPPO_PROJECT_ROOTS` is literally in the retrieved `embed_text` of a top hit — just past the truncation cap.

## Why this fix and not the alternatives

### Option A — bump `DEFAULT_MAX_CONTEXT_CHARS` only

A larger budget shifts the truncation point but doesn't eliminate it. With 10 hits and identifier-dense embed_text easily exceeding 1KB per hit, even 16000 chars total leaves embed_text under 1500 chars/hit after structural overhead — still tail-clipped on busy nodes. **Rejected as the sole fix.** A modest bump (8000 → 12000) does ride along with this design to absorb the new structural overhead from the Entities line; see "Budget bump" below.

### Option B — identifier-priority truncation inside `_truncate`

Regex-extract `UPPERCASE_UNDERSCORE`, semver, `pkg@ver`, paths, etc. and pin them to the head of the rendered `embed_text`. Strictly worse than C: it re-derives at the render layer what the enrichment LLM already structured into `entities.tools/files/services/projects` under the same VERBATIM PRESERVATION rule. If structured entities is missing identifiers, the right fix is to strengthen the enrichment prompt — not patch around it at render time. **Rejected.**

### Option C — surface structured entities as a dedicated context line

Read the existing canonicalized entities from the `entities` table (joined via `knowledge_node_entities`), render them as a comma-separated `Entities:` line above the truncatable `Detail:` line. Single source of truth (the LLM emitted them once, into a structured field). Mirrors the existing precedent of `design_decisions` plumbing. **Selected.**

### Out of scope but worth a follow-up: re-ranking by question-identifier overlap

A higher-leverage improvement is question-side identifier extraction (`UPPERCASE_UNDERSCORE`, semver, paths, backtick-quoted tokens) followed by re-ranking hits whose entities/embed_text contain those tokens. This is genuinely better than render-layer plumbing but doubles the blast radius of this PR (changes retrieval ordering, requires retest of all rag tests, interacts with MMR diversity). **Filed as a Phase 2 follow-up issue** referenced from the implementation PR.

## Design

### Data flow

```
hippo ask "<question>"
  └─ rag.ask()
      └─ retrieval.search(conn, …)
          └─ _fetch_details(conn, node_ids)
              ├─ SELECT … FROM knowledge_nodes               (existing)
              ├─ JOIN knowledge_node_events                  (existing)
              ├─ JOIN knowledge_node_claude_sessions         (existing)
              └─ ★ NEW: window-limited JOIN                  (entities + knowledge_node_entities)
          └─ _to_result(score, detail)
              └─ ★ NEW: SearchResult(entities=detail["entities"])
      └─ _build_rag_prompt(question, hits, max_chars=12000)  ← ★ default 8000 → 12000
          └─ _hit_lines(i, hit, embed_cap, cmd_cap, design_cap)
              ├─ "[i] (score, date)"                         (existing)
              ├─ "Summary: …"                                (existing)
              ├─ ★ NEW: "Entities: tool1, file1, …"          (capped 500 chars)
              ├─ "Detail: <embed_text truncated>"            (existing — proportional)
              ├─ "Design decisions: …"                       (existing — proportional)
              └─ "Commands:" / "CWD:" / "Branch:" / …        (existing)
```

### Two load-bearing invariants

1. **The `Entities:` line lives in the structural budget**, not the proportional payload allocator. The second pass of `_build_rag_prompt` (rag.py:244–250) measures structural overhead by re-rendering each hit with `embed_text`/`commands_raw`/`design_decisions` stripped to zero. The new line doesn't touch those three fields, so it's automatically counted as structural and never proportionally truncated. Implementation requires no changes to `_build_rag_prompt` beyond the constant bump.
2. **Entity source is the normalized `entities` table** (read via the existing `knowledge_node_entities` join), not the raw `content` JSON blob. We read the worktree-prefix-stripped `name` column (PR #105 specifically fixed worktree pollution at this layer). Reading from the JSON blob would re-introduce the bug for any older nodes.

### Single source of truth for the type list

The hardcoded type filter is a known landmine: `browser_enrichment.py` already adds a `domain` type to its own type map. Two future risks:
- Someone adds a new entity type to `SHELL_ENTITY_TYPE_MAP` or `BROWSER_ENTITY_TYPE_MAP` and the SQL filter silently drops it from retrieval.
- Someone adds a type without updating `_IDENTIFIER_TYPE_ORDER` in rag.py and it's silently dropped from rendering even if SQL returns it.

**Mitigation:** A single tuple `IDENTIFIER_ENTITY_TYPES` lives in `enrichment.py` (next to `SHELL_ENTITY_TYPE_MAP`), imported by both `retrieval.py` (for the SQL `WHERE` clause and Python filter) and `rag.py` (for render ordering). A new exclusion set `NON_IDENTIFIER_ENTITY_TYPES = ("concept",)` is also defined there — these are types deliberately excluded from the line (errors are prose, not bindable tokens).

A guard test in `test_enrichment.py` (or a new `test_entity_taxonomy.py`) asserts that **every value across every `*_ENTITY_TYPE_MAP` is either in `IDENTIFIER_ENTITY_TYPES` or in `NON_IDENTIFIER_ENTITY_TYPES`**. The next type addition either updates one of those sets or the test fails loudly.

### SQL change in `_fetch_details`

A fourth batched join, after the existing event and claude_session joins:

```sql
SELECT knowledge_node_id, type, name FROM (
  SELECT
    kne.knowledge_node_id,
    ent.type,
    substr(ent.name, 1, 200) AS name,
    ROW_NUMBER() OVER (
      PARTITION BY kne.knowledge_node_id
      ORDER BY ent.type, ent.name
    ) AS rn
  FROM knowledge_node_entities kne
  JOIN entities ent ON ent.id = kne.entity_id
  WHERE kne.knowledge_node_id IN (?, ?, …)
    AND ent.type IN ('tool', 'file', 'service', 'project')   -- from IDENTIFIER_ENTITY_TYPES
)
WHERE rn <= 20
```

Rationale:
- **`PARTITION BY ... ROW_NUMBER ... rn <= 20`** — bounds memory at the SQL layer. A pathological enrichment with 80 file entities on one node would otherwise pull 800 rows into Python before the render-layer 500-char cap clips them.
- **`substr(ent.name, 1, 200)`** — schema has no length cap on `entities.name`; a 10KB pathological name would still be fetched without this. Cheap defense.
- **No `COLLATE NOCASE`** in the ORDER BY — preserves the existing `idx_entities_type_name` (binary collation) for sorting.
- **SQL-layer type filter** — keeps half the join unfetched. Even without an index on `entities.type`, the per-id row count is small enough that round-trip cost dominates.
- **`IDENTIFIER_ENTITY_TYPES` is interpolated as `?` parameters**, not f-string concatenated, even though the constant is local — defensive habit.

The Python loop appends each `(knowledge_node_id, type, name)` into `details[node_id]["entities"][type] -> [name, ...]`, mirroring the events/sessions pattern.

### `SearchResult` dataclass change (retrieval.py:38)

Add:

```python
entities: dict[str, list[str]] = field(default_factory=dict)
```

Bucketed by type (`{"tool": [...], "file": [...], "service": [...], "project": [...]}`). Type-bucketed not flat so the renderer chooses ordering and so future callers can filter by type.

We deliberately do **not** use a typed `Entity(type, name, canonical)` dataclass: `canonical` is unused at the render layer (the `name` column is already worktree-stripped, which is what verbatim preservation needs). YAGNI until canonical actually matters somewhere.

### `_to_result` change (retrieval.py:632)

Pass `entities=dict(detail.get("entities") or {})` into the `SearchResult` constructor, mirroring the defensive copy used for `linked_event_ids` and `design_decisions`.

### `_result_to_hit` change (rag.py:366)

Add `"entities": dict(r.entities)` to the returned hit dict.

### `_render_entities_line` helper (new in rag.py)

```python
_ENTITIES_LINE_CAP = 500

def _render_entities_line(entities: dict | None) -> str | None:
    """Render structured entities as a flat comma-separated line.

    Returns None when there is nothing to surface — caller omits the line
    rather than emitting a bare "Entities:" prefix. Capped at
    _ENTITIES_LINE_CAP so identifier-rich hits don't crowd embed_text out
    of the structural budget.
    """
    if not isinstance(entities, dict):
        return None
    seen: set[str] = set()
    tokens: list[str] = []
    for etype in IDENTIFIER_ENTITY_TYPES:
        for name in entities.get(etype) or []:
            if not isinstance(name, str) or not name:
                continue
            if name in seen:
                continue
            seen.add(name)
            tokens.append(name)
    if not tokens:
        return None
    return f"Entities: {_truncate(', '.join(tokens), _ENTITIES_LINE_CAP)}"
```

Cross-type dedup is intentional: the same canonical token can appear under multiple type buckets (a tool that's also a project name). We render it once.

### `_hit_lines` change (rag.py:108)

Insert the new line between `Summary:` and `Detail:`:

```python
if hit.get("summary"):
    lines.append(f"Summary: {hit['summary']}")
entities_line = _render_entities_line(hit.get("entities"))
if entities_line:
    lines.append(entities_line)
if hit.get("embed_text"):
    lines.append(f"Detail: {_truncate(hit['embed_text'], embed_cap)}")
```

Position above `Detail:` is intentional and tested (see "Test plan" below) — visual proximity to `Summary:` reinforces "this is structured metadata about the hit," and ordering pins the structural-vs-payload distinction.

### Budget bump (rag.py:39)

`DEFAULT_MAX_CONTEXT_CHARS = 8000` → `12000`.

Justification: the new `Entities:` line adds up to 500 chars × 10 hits = 5000 chars of structural overhead worst-case (typical: 1500–2500). Without a bump, every char added to structural shrinks the proportional payload budget for `embed_text`. We bundle the bump with the feature deliberately — they're causally linked. Splitting them obscures cause-and-effect; if bisection ever needs them apart, file-level revert works.

The qwen3.6-35b-a3b-ud-mlx model has plenty of context window for 12000 chars + system prompt + question; synthesis latency impact is negligible.

## Naming choice: `Entities:` not `Identifiers:`

The line label uses `Entities:` to match storage vocabulary (`entities` table, `knowledge_node_entities`, `mcp__hippo__get_entities`). The synthesis system prompt's "identifiers" wording is a noun for what the *tokens* are (env vars, semver, paths); the line label names where they came from. Vocabulary consistency with the rest of the codebase wins.

## Test plan

### P0 — must land with the implementation

1. **`test_entities_line_appears_above_detail_line`** — position invariant in `_hit_lines` output. Asserts `Entities:` line index < `Detail:` line index.
2. **`test_entities_survive_proportional_truncation`** — force `_build_rag_prompt`'s second pass with `max_context_chars` low enough to trigger truncation; assert the `Entities:` line is preserved verbatim in the rendered prompt while `Detail:` shrinks. This is the design's load-bearing claim.
3. **`test_fetch_details_hydrates_entities`** — direct retrieval-layer test against the existing schema fixture in `test_retrieval.py`. Asserts `SearchResult.entities` is populated for all returned ids, type-bucketed correctly, with the SQL `substr(…, 1, 200)` cap and per-node `LIMIT 20` window honored. (We do not test for "single query"; that's a code-review concern, not a test concern.)
4. **`test_entities_dedup_across_types`** — same canonical token in `tool` and `project`; rendered exactly once.

### P1 — recommended

5. **`test_entities_cap_truncates_via_truncate_helper`** — feed 100 long identifiers; assert the rendered line ≤ 500 chars and ends with the existing `_truncate` ellipsis behavior. (We deliberately do *not* require token-boundary truncation — `_truncate` is char-based and consistent with the rest of the codebase.)
6. **`test_entities_omitted_when_all_types_empty`** — covers `entities={}`, `entities={"tool": []}`, and missing key. Asserts no stray `Entities:` line is rendered.
7. **`test_concept_entities_excluded_from_entities_line`** — a `concept`-typed entity is present in `entities` but not rendered.

### P2 — nice to have

8. **`test_budget_accounts_for_entities_in_structural_pass`** — with 10 hits × 500-char entities and `max_chars=8000`, the `_MIN_PER_HIT_FIELD_CHARS` floor still holds and `Detail:` renders something non-empty.

### Existing tests at risk

- `test_oversized_context_is_capped_before_chat` (test_rag.py:472): asserts `len(user_content) < 4500` with `max_context_chars=3000`. Adding ~500 chars of entities × 2 hits = +1000 may push past 4500. **Action: adjust the assertion or the input fixture to account for the new structural overhead.** The test still has signal — it's checking that truncation happens, not the exact byte count.
- `test_context_budget_truncates_oversized_payload` (test_rag.py:180): same risk pattern. **Action: same.**
- Any retrieval test asserting exact `SearchResult` field set via `dataclasses.fields()` or `asdict` equality — these need updates for the new `entities` field. (None observed in current `test_retrieval.py` but worth grepping for during implementation.)

### Taxonomy guard test

A standalone test asserts every value across `SHELL_ENTITY_TYPE_MAP`, `BROWSER_ENTITY_TYPE_MAP`, and any other `*_ENTITY_TYPE_MAP`s is in either `IDENTIFIER_ENTITY_TYPES` or `NON_IDENTIFIER_ENTITY_TYPES`. The next type addition fails this test if the author forgets to update either set.

## Acceptance criteria

- [ ] `_build_rag_prompt` output for a hit with a populated `entities` dict includes every token from `IDENTIFIER_ENTITY_TYPES` buckets in the rendered prompt regardless of `embed_text` truncation, up to the 500-char per-hit cap. (Asserted as test P0-2.)
- [ ] All P0 + P1 tests above pass.
- [ ] Existing `test_*.py` files in `brain/tests/` pass with at most the adjustments noted under "Existing tests at risk."
- [ ] Manual smoke test: querying for an identifier *known to be present in a top-retrieved hit's `entities`* (verified via `mcp__hippo__get_entities` or a direct SQL probe before running the RAG query) returns the token verbatim from `hippo ask`. Not a CI test — synthesis is non-deterministic and depends on entity hydration coverage.
- [ ] Taxonomy guard test passes.
- [ ] No regressions in `cargo test` or `uv run --project brain pytest brain/tests`.

## Known limitation: env vars and other unbucketed token kinds

This design surfaces tokens that the enrichment LLM placed into one of the `IDENTIFIER_ENTITY_TYPES` buckets (`tool`, `file`, `service`, `project`). The enrichment prompt does not have a dedicated bucket for environment variable names. Whether `HIPPO_PROJECT_ROOTS` (the canonical token from issue #108) reaches `entities` depends on whether the LLM happened to classify it as a `tool` or `service` for that node.

**Pre-implementation verification:**

Before declaring issue #108 closed, run a direct probe:

```sql
SELECT ent.type, ent.name
FROM knowledge_node_entities kne
JOIN entities ent ON ent.id = kne.entity_id
JOIN knowledge_nodes kn ON kn.id = kne.knowledge_node_id
WHERE kn.embed_text LIKE '%HIPPO_PROJECT_ROOTS%';
```

- **If the env var is bucketed in any of the four identifier types**, this design fixes the issue.
- **If it isn't**, this design is necessary but not sufficient for the canonical repro. The follow-up choices are: (a) extend the enrichment prompt to add an `env_vars` bucket (small change, requires re-enrichment of historical nodes), (b) extract env vars at render time via regex against `embed_text` (re-introduces some of Option B's downsides but with a tightly scoped pattern), or (c) implement question-side identifier extraction + re-rank as Phase 2 — that path naturally surfaces *any* token in question + embed_text overlap regardless of entity bucketing.

The implementer must run this probe and document the result in the implementation PR. If the result drives a (a)/(b)/(c) decision, that decision is made at PR-review time, not deferred.

## Out of scope

- Changing the synthesis model or its system prompt — those are correct as of PR #107.
- Changing what the enrichment model writes into `embed_text` or `entities` — PR #100 made those identifier-dense already.
- Question-side identifier extraction + re-rank (filed as Phase 2 follow-up).
- Migrating `entities.name` to a length-capped column at the schema layer — handled defensively at the SQL read layer here; schema change is independent and not required.

## References

- Issue: [#108](https://github.com/stevencarpenter/hippo/issues/108)
- Predecessor PRs: [#107](https://github.com/stevencarpenter/hippo/pull/107) (synthesis prompt), [#100](https://github.com/stevencarpenter/hippo/pull/100) (identifier-dense embed_text), [#105](https://github.com/stevencarpenter/hippo/pull/105) (worktree-prefix stripping in `entities.name`)
- Code touched: `brain/src/hippo_brain/rag.py`, `brain/src/hippo_brain/retrieval.py`, `brain/src/hippo_brain/enrichment.py`
- Tests touched: `brain/tests/test_rag.py`, `brain/tests/test_retrieval.py`, plus a new taxonomy guard test
