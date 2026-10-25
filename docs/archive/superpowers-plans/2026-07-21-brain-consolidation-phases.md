# Brain Consolidation Layer — Phase Plan

> Linear push pending API access. When pushed: one parent initiative ("Brain consolidation layer") with the four phases below as child issues, executed as independently testable vertical slices in order 0 → 1 → 2 → 3. (Precedent: `2026-06-28-claude-auto-memory-epic.md` used SNUG-xxx IDs; confirm team key + project at push time.)
>
> Design context: `docs/superpowers/specs/2026-07-21-brain-consolidation-layer-design.md` — read the "Concepts in plain terms" section first; each phase below lists what you'll learn by building it.
> Provenance baseline: `docs/research/2026-07-21-provenance-baseline-expansion.md` — decisions D2–D4 are absorbed into these phases (Phase 1 gains a `derivation_runs` table and run-linked `synthesis_sources`; Phase 2 upgrades evidence packets to claim-level char offsets; Phase 3 implements validity-window invalidation). D5 (hash chaining) and D8 (scoped agent access) are deliberately out of scope here — they are the multi-user gate.

## Constraints

- Never per-event: consolidation runs on watermarks + cadence + manual trigger only.
- Supersede, never delete: raw observations are immutable evidence.
- Synthesis nodes live in `knowledge_nodes`/`knowledge_vectors` — no parallel store, no retrieval rewrite.
- The consolidation loop yields to queries (`_query_inflight` drain) and honors `/control/pause`, exactly like `_enrichment_loop`.
- Existing inference, sqlite-vec, FTS5, source-health, watchdog, doctor, HTTP, CLI, and MCP paths are extended rather than duplicated.
- All timestamps epoch ms; WAL; `busy_timeout=5000`; schema versioned (current v23 → v24 for this work).
- Tests first, per house style.

## Phase 0 — Relationship graph backfill + salience scoring (no LLM)

**Goal:** turn 39.8k isolated entities into a traversable graph and give every node a stored importance score. Pure SQL/Python — zero inference cost, zero prompt risk.

**You'll learn:** what a co-occurrence graph is and why it's the cheapest useful edge type; how a stored salience score differs from query-time confidence scoring (`confidence_scoring.py`); why graph centrality is a proxy for "how load-bearing is this entity."

1. Add failing tests for the co-occurrence backfill: edge creation from shared `knowledge_node_entities` rows, idempotent re-run (`ON CONFLICT` weight update), no self-edges, no duplicate undirected pairs.
2. Implement `hippo_brain/relationships.py::backfill_co_occurrence` as a single transaction; expose via `hippo consolidate --backfill-relationships [--dry-run]`.
3. Add failing tests for salience: outcome weighting (failure > partial > success), decision-content boost, recency half-life decay, centrality boost from the new edges.
4. Implement `hippo_brain/salience.py::recompute_salience` (schema v24 `salience` column); weights from config, not constants.
5. Add a mise task (`consolidate:backfill`) and doctor check reporting edge count + salience distribution.
6. Verify: `mise run test`; backfill the live DB; confirm `relationships` row count > 0 and spot-check a known entity pair.

**Acceptance:** `relationships` populated and idempotently refreshable; every `knowledge_nodes` row has a non-default salience; no enrichment-loop code touched; bench retrieval scores unchanged or better (no regression gate).

## Phase 1 — Consolidation loop + watermarks + `project_digest`

**Goal:** the flagship slice. A `_consolidation_loop` in `BrainServer` that synthesizes rolling per-project digests, incrementally, with supersession.

**You'll learn:** watermark-driven incremental recomputation (the same pattern as materialized-view refresh and log-compaction offsets); episodic → semantic memory distillation; how to version LLM-derived artifacts so prompt iteration doesn't strand stale output (`synthesis_version`, mirroring `enrichment_version`).

1. Add schema v24: `consolidation_state`, `synthesis_sources`, `knowledge_nodes.superseded_by`, `salience` (lands here or with Phase 0, whichever ships first).
2. Add failing tests for watermark selection: scope discovery from active `git_repo`s, threshold trigger (N new nodes), age trigger (T elapsed), no-op when under both.
3. Add failing tests for the digest contract: structural validation of LLM JSON output (SourceSchema-style), provenance rows in `synthesis_sources`, supersession of the prior digest in one transaction.
4. Implement `hippo_brain/consolidation.py` (scope evaluation, digest synthesis, supersede+write transaction) and `_consolidation_loop` in `server.py` with query-drain, pause/resume, and `max_llm_calls_per_run` budget.
5. Embed synthesis nodes through the existing `_embed_node` path; confirm they surface via `search_knowledge` with `node_type='project_digest'`.
6. Add `hippo consolidate [--scope X] [--dry-run]` CLI → brain endpoint for manual runs.
7. Add `brain/scripts/re-consolidate.py` (modeled on `re-enrich-knowledge-nodes.py`) for prompt iteration.
8. Verify: focused pytest, then a live run against the real DB with `--dry-run`, then a budgeted real run; inspect one digest by hand for faithfulness against its `synthesis_sources`.

**Acceptance:** digests regenerate only when their scope crosses a trigger; each digest lists verifiable source nodes; old digests are superseded, not deleted; enrichment loop behavior unchanged; `mise run test` green.

## Phase 2 — `entity_profile` + `pattern` syntheses + generalized lesson graduation

**Goal:** extend the loop to the two remaining synthesis types, and generalize lessons beyond `capture_alarms`.

**You'll learn:** entity-centric vs time-centric knowledge organization; why recurring-pattern detection needs occurrence gating (the existing 2+ rule in `lessons.py`) to avoid graduating noise; how synthesis types compose (digests cite patterns, profiles cite digests).

1. Add failing tests for `entity_profile`: scope = high-centrality entities from the Phase 0 graph; cap at top-K per run; content faithfulness to linked nodes only.
2. Implement profile synthesis through the same contract/provenance/supersession machinery from Phase 1.
3. Add failing tests for `pattern`: time-window scope, minimum supporting-node count, cross-session requirement (a pattern must span ≥2 sessions).
4. Implement pattern synthesis; feed recurring-failure patterns into a generalized lesson-graduation path alongside `capture_alarm_lessons.py` (keep the alarm path intact).
5. Extend bench golden Q/A with items answerable only from profiles/patterns.
6. Verify: focused pytest, live budgeted run, hand-inspect one profile and one pattern for hallucination against sources.

**Acceptance:** all three synthesis types live; lessons graduate from ≥2 independent sources; new bench items pass; LLM budget per run still capped.

## Phase 3 — Contradiction/supersession across time + retrieval weighting

**Goal:** stop stale facts from competing with current ones; make synthesis and observation nodes cooperate in ranking.

**You'll learn:** contradiction detection as a lifecycle problem (not a search problem); why supersession beats deletion for auditability; how ranking signals compose (vector score × salience × node-type weight × supersession preference) and why each multiplier needs a bench gate.

1. Add failing tests for contradiction candidates: same-entity, different-value statements across time windows (start with `change_outcome` nodes, which already encode transitions).
2. Implement the contradiction pass in the consolidation loop: candidate detection (SQL) → LLM confirmation (budgeted) → `superseded_by` edge on the loser.
3. Add failing retrieval tests: superseded nodes deprioritized but reachable; `node_type` weighting routes "state of X" to syntheses and "what happened" to observations.
4. Implement ranking changes behind config flags; wire into `retrieval.py` / `agent_query` mode routing.
5. Bench: full golden Q/A run pre/post; retrieval regression gate must hold or improve.
6. Update `docs/schema.md`, `docs/mcp-reference.md` (any new filters), and AGENTS.md architecture section.

**Acceptance:** stale-fact bench items resolve to current facts; superseded history still queryable in `recent` mode; all ranking changes flag-gated and bench-verified; docs current.

## Suggested Linear metadata (at push time)

| Field | Value |
|---|---|
| Team | hippo (confirm team key — SNUG precedent exists) |
| Project | Brain consolidation layer (or `hippo` — confirm) |
| Labels | `brain`, `consolidation`, plus `no-llm` (P0) / `synthesis` (P1–P3) |
| Dependencies | P1 blocked by P0; P2 blocked by P1; P3 blocked by P1 |
| Estimates | P0: S · P1: L · P2: M · P3: M |

## Execution notes

- Phases are vertical slices: each ships usable value and passes `mise run test` independently.
- Phase 0 is the safe on-ramp — no inference, no prompts, immediate graph to inspect.
- Keep a running "what I learned" note per phase in this file as you execute; it becomes the teaching record.
