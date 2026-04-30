<!--
HUMAN-ONLY USAGE NOTES (Claude should ignore this HTML comment block during iterations).

To start the loop:

  /ralph-loop "$(cat docs/superpowers/plans/2026-04-27-rag-entity-surfacing-ralph.md | sed -n '/^---RALPH-PROMPT-BEGIN---$/,/^---RALPH-PROMPT-END---$/p' | sed '1d;$d')" --completion-promise "RAG_ENTITY_SURFACING_COMPLETE" --max-iterations 25

Or just paste the prompt below into a fresh session.

The prompt is intentionally idempotent: re-running it on a partially-implemented branch must advance, not regress. Every iteration re-reads the spec, re-runs all verification gates, identifies the first failing gate, and closes it. The promise tag fires only when every gate passes.

Branch: I recommend running this in a worktree off `main` named `fix/rag-entity-surfacing` (issue #108).
-->

# Ralph Loop prompt — issue #108 RAG entity surfacing

---RALPH-PROMPT-BEGIN---
You are implementing the design at `docs/superpowers/specs/2026-04-27-rag-entity-surfacing-design.md` to fix hippo issue #108. This prompt runs every iteration. Same prompt, different file state.

# Your job each iteration

1. **Re-read the spec** at `docs/superpowers/specs/2026-04-27-rag-entity-surfacing-design.md`. The spec is the source of truth — if this prompt and the spec ever conflict, the spec wins.
2. **Run all verification gates below** in order. Stop at the first failing gate.
3. **Make the smallest change** that closes that gate. Do not skip ahead. Do not refactor unrelated code.
4. **Re-run the gates.** If any still fails, do nothing else this iteration — the next iteration will continue.
5. **Only when every gate passes**, emit the literal text `<promise>RAG_ENTITY_SURFACING_COMPLETE</promise>` exactly once and stop. Do not emit the promise if any gate fails. Do not emit it speculatively.

# Verification gates (run in order, stop at first failure)

## Gate 0 — pre-implementation env-var probe (one-time)

The spec's "Known limitation" section requires verifying whether `HIPPO_PROJECT_ROOTS` is bucketed in any `IDENTIFIER_ENTITY_TYPES` slot for the canonical repro node. Run once at the start of implementation and write the result to `IMPL_NOTES.md` in the repo root.

If `IMPL_NOTES.md` does not exist, run:

```bash
sqlite3 ~/.local/share/hippo/hippo.db "
SELECT ent.type, ent.name
FROM knowledge_node_entities kne
JOIN entities ent ON ent.id = kne.entity_id
JOIN knowledge_nodes kn ON kn.id = kne.knowledge_node_id
WHERE kn.embed_text LIKE '%HIPPO_PROJECT_ROOTS%'
  AND ent.type IN ('tool', 'file', 'service', 'project', 'concept');
"
```

Write `IMPL_NOTES.md` with:
- The exact command run
- The full output (one row per line, or `(no rows)`)
- A one-line interpretation: either `RESULT: env var IS bucketed in IDENTIFIER_ENTITY_TYPES — design fully closes #108` or `RESULT: env var is NOT bucketed in IDENTIFIER_ENTITY_TYPES — design is necessary but not sufficient; PR description must call this out and reference the Phase 2 follow-up`.

This gate passes once `IMPL_NOTES.md` exists with non-empty content. Do not delete this file later — it ships with the PR.

## Gate 1 — type-list single source of truth

In `brain/src/hippo_brain/enrichment.py`, near the existing `SHELL_ENTITY_TYPE_MAP` (~line 85), there must be:

```python
IDENTIFIER_ENTITY_TYPES: tuple[str, ...] = ("tool", "file", "service", "project")
NON_IDENTIFIER_ENTITY_TYPES: tuple[str, ...] = ("concept",)
```

Verify with: `grep -n "^IDENTIFIER_ENTITY_TYPES\|^NON_IDENTIFIER_ENTITY_TYPES" brain/src/hippo_brain/enrichment.py` — must show both definitions.

## Gate 2 — taxonomy guard test

A test must exist asserting: every value across every `*_ENTITY_TYPE_MAP` in the codebase is a member of `IDENTIFIER_ENTITY_TYPES + NON_IDENTIFIER_ENTITY_TYPES`. The next type addition fails this test if neither set is updated.

Search for existing maps before writing: `grep -rn "_ENTITY_TYPE_MAP\s*=" brain/src/`. At minimum `SHELL_ENTITY_TYPE_MAP` and `BROWSER_ENTITY_TYPE_MAP` must be covered.

Place the test in `brain/tests/test_entity_taxonomy.py` (new file). Verify it runs and passes:

```bash
uv run --project brain pytest brain/tests/test_entity_taxonomy.py -v
```

## Gate 3 — `SearchResult` carries entities

In `brain/src/hippo_brain/retrieval.py`, `SearchResult` (~line 38) must include:

```python
entities: dict[str, list[str]] = field(default_factory=dict)
```

Verify: `grep -n "entities:.*dict\[str, list\[str\]\]" brain/src/hippo_brain/retrieval.py`.

## Gate 4 — `_fetch_details` hydrates entities via window-limited query

In `brain/src/hippo_brain/retrieval.py::_fetch_details` (~line 441), there must be a fourth batched join after the events and claude_sessions joins. The exact SQL shape is in the spec's "SQL change in `_fetch_details`" section. Required elements:

- `ROW_NUMBER() OVER (PARTITION BY kne.knowledge_node_id ORDER BY ent.type, ent.name)` (per-node cap of 20)
- `WHERE rn <= 20` in the outer query
- `substr(ent.name, 1, 200)` to bound row size
- `WHERE ent.type IN (...)` parameterized from `IDENTIFIER_ENTITY_TYPES` (NOT f-string interpolated)
- No `COLLATE NOCASE` (preserves the existing `idx_entities_type_name` binary collation)
- The Python loop appends each `(node_id, type, name)` into `details[node_id]["entities"][type] -> [name, ...]`
- The detail dict initializes `"entities": {}` alongside `"linked_event_ids": []`

Verify: `grep -n "ROW_NUMBER\|substr(ent.name" brain/src/hippo_brain/retrieval.py` — must show both in `_fetch_details`.

## Gate 5 — `_to_result` plumbs entities

In `brain/src/hippo_brain/retrieval.py::_to_result` (~line 632), the `SearchResult(...)` constructor call must pass `entities=dict(detail.get("entities") or {})`.

Verify: `grep -n "entities=dict(detail" brain/src/hippo_brain/retrieval.py`.

## Gate 6 — `_result_to_hit` plumbs entities

In `brain/src/hippo_brain/rag.py::_result_to_hit` (~line 366), the returned dict must include `"entities": dict(r.entities)`.

Verify: `grep -n '"entities":.*dict(r.entities)' brain/src/hippo_brain/rag.py`.

## Gate 7 — `_render_entities_line` helper exists in rag.py

In `brain/src/hippo_brain/rag.py`, there must be:

- A constant `_ENTITIES_LINE_CAP = 500`
- A helper function `_render_entities_line(entities)` matching the implementation in the spec's `_render_entities_line` section. Must:
  - Return `None` for empty/missing/non-dict input
  - Iterate `IDENTIFIER_ENTITY_TYPES` in order, dedup tokens across types, skip empty/non-string entries
  - Return `f"Entities: {_truncate(', '.join(tokens), _ENTITIES_LINE_CAP)}"` when any token survives
- Imports `IDENTIFIER_ENTITY_TYPES` from `hippo_brain.enrichment`

Verify: `grep -n "_render_entities_line\|_ENTITIES_LINE_CAP\|from hippo_brain.enrichment import.*IDENTIFIER_ENTITY_TYPES" brain/src/hippo_brain/rag.py`.

## Gate 8 — `_hit_lines` inserts the entities line

In `brain/src/hippo_brain/rag.py::_hit_lines` (~line 108), the new entities line must be inserted between the `Summary:` block and the `Detail:` block. Implementation:

```python
entities_line = _render_entities_line(hit.get("entities"))
if entities_line:
    lines.append(entities_line)
```

Position is load-bearing: must be AFTER summary handling and BEFORE the embed_text/Detail handling. The position invariant is asserted by test P0-1.

Verify: `grep -n "_render_entities_line(hit" brain/src/hippo_brain/rag.py`.

## Gate 9 — `DEFAULT_MAX_CONTEXT_CHARS` bumped to 12000

In `brain/src/hippo_brain/rag.py` (~line 39):

```python
DEFAULT_MAX_CONTEXT_CHARS = 12000
```

Verify: `grep -n "^DEFAULT_MAX_CONTEXT_CHARS = 12000" brain/src/hippo_brain/rag.py`.

## Gate 10 — P0 tests exist and pass

The four P0 tests from the spec's "Test plan" section:

1. `test_entities_line_appears_above_detail_line` (in `test_rag.py`)
2. `test_entities_survive_proportional_truncation` (in `test_rag.py`)
3. `test_fetch_details_hydrates_entities` (in `test_retrieval.py`)
4. `test_entities_dedup_across_types` (in `test_rag.py`)

Verify each test exists and passes:

```bash
uv run --project brain pytest brain/tests/test_rag.py -v -k "test_entities_line_appears_above_detail_line or test_entities_survive_proportional_truncation or test_entities_dedup_across_types"
uv run --project brain pytest brain/tests/test_retrieval.py -v -k "test_fetch_details_hydrates_entities"
```

Both commands must show 1+ tests collected and all passing.

## Gate 11 — P1 tests exist and pass

The three P1 tests:

5. `test_entities_cap_truncates_via_truncate_helper`
6. `test_entities_omitted_when_all_types_empty`
7. `test_concept_entities_excluded_from_entities_line`

```bash
uv run --project brain pytest brain/tests/test_rag.py -v -k "test_entities_cap_truncates or test_entities_omitted_when_all_types_empty or test_concept_entities_excluded"
```

## Gate 12 — existing tests still pass

```bash
uv run --project brain pytest brain/tests -v
```

The full suite must pass. Two existing tests likely need adjustments per the spec's "Existing tests at risk" section:

- `test_oversized_context_is_capped_before_chat` (test_rag.py:472)
- `test_context_budget_truncates_oversized_payload` (test_rag.py:180)

If they fail, adjust the assertions or input fixtures to account for the new structural overhead. Do NOT comment them out, skip them, or weaken them beyond what the budget math demands. If you cannot make them pass with a justified adjustment, leave them failing and document why in `IMPL_NOTES.md` for human review (the loop will not promise-out in this case).

## Gate 13 — lint and format clean

```bash
uv run --project brain ruff check brain/src brain/tests
uv run --project brain ruff format --check brain/src brain/tests
```

Both must exit 0.

## Gate 14 — Rust side untouched

This is a brain-only change. Verify `git status` shows no changes under `crates/`:

```bash
git status --short crates/
```

Must be empty.

# Discipline rules

- **Do not add features beyond the spec.** No question-side identifier extraction, no re-ranking — those are explicitly Phase 2.
- **Do not skip a gate to reach the promise faster.** A failing gate means the next iteration continues; don't paper over it.
- **Do not modify the spec to match incorrect code.** If you genuinely believe the spec is wrong, write your reasoning into `IMPL_NOTES.md` under a `## Disagreement with spec` heading and STOP without emitting the promise. A human will review.
- **`# nosemgrep` annotations may be required** on the new SQL in `_fetch_details` if Semgrep flags the f-string placeholder pattern; mirror the existing comments on the surrounding queries.
- **Python 3.14 syntax is fine.** Unparenthesized multi-except (`except A, B:`) is valid PEP 758. Do not "fix" it.
- **`commands_raw=""` is hardcoded** in `_result_to_hit` for retrieval-search hits — leave it alone, that's intentional.

# When all gates pass

Emit exactly:

`<promise>RAG_ENTITY_SURFACING_COMPLETE</promise>`

Then stop. Do not commit. Do not push. Do not open a PR. The human reviews `IMPL_NOTES.md` and the diff before any of that happens.
---RALPH-PROMPT-END---
