# Brain Consolidation Layer Design

> Status: draft. Linear push pending API access — see `docs/superpowers/plans/2026-07-21-brain-consolidation-phases.md` for the issue breakdown.

## Summary

Hippo's knowledge store is a flat, append-only pile of per-session observation nodes (19,160 rows as of 2026-07-21). This design adds a **consolidation layer**: a slow, batch-oriented loop in the brain that periodically distills raw observations into higher-order synthesis nodes (project digests, entity profiles, cross-session patterns), links the 39.8k existing entities into a relationship graph, scores node salience, and manages fact lifecycle (supersession). Everything runs on watermarks and cadence — never per event.

## Current state (verified 2026-07-21)

| Fact | Value | Implication |
|---|---|---|
| `knowledge_nodes` | 19,160 rows (`observation` 16.6k, `change_outcome` 2.5k) | Flat pile, one granularity level |
| `entities` | 39,809 rows | Rich but unlinked |
| `relationships` | **0 rows** | Dead schema — nothing writes to it (grep confirms; only `docs/archive/` mentions it) |
| `lessons` | 18 rows | Graduation works but is fed only by `capture_alarms` |
| Node lifecycle | none | No supersession, decay, or consolidation; stale facts compete with current ones forever |
| Query-time stack | hybrid vec+FTS5, rerank, evidence packets, `confidence_scoring`, `source_freshness` | Already strong — the weak end is the store |

## Concepts in plain terms

This section is the teaching layer. Each concept is stated as an engineering analogy, then tied to the concrete mechanism in this design.

### Episodic vs semantic memory

Human memory research separates *episodes* ("what happened Tuesday at 3pm") from *semantics* ("what I know about Postgres"). Hippo's enrichment pipeline produces only episodes: one `observation` node per session batch. When you ask "what's the state of the hippo project?", retrieval must reconstruct an answer from hundreds of raw episodes — noisy, slow, and lossy. Consolidation periodically reads recent episodes and writes **semantic nodes**: durable, distilled statements ("hippo's capture stack shipped P0–P3; current work is the consolidation layer"). Episodes stay as evidence; semantics become the first retrieval hit.

### Why a flat pile degrades

Three failure modes grow with node count:

1. **Near-duplicate crowding.** 16k observations of similar daily work means any query matches dozens of near-identical nodes, diluting ranking signal.
2. **Stale-fact competition.** An observation from March ("using LM Studio for inference") competes on equal footing with July's fact ("default backend is oMLX"). Nothing marks the March node as historical.
3. **No importance gradient.** A throwaway `ls` session and a sev1 debugging session have identical standing in the store. `confidence_scoring` patches this at query time, but the store itself carries no importance signal.

### Watermarks and incremental recomputation

A watermark is a high-water mark: "scope X is consolidated through node id N / timestamp T." Each consolidation run only reprocesses scopes with enough *new* activity past the mark. This is the same incremental-computation pattern as a materialized view refresh or a log compaction offset — and it is what makes the layer cheap enough to run on a cadence instead of per event. Trigger rule per scope: `new_nodes >= N` OR `now - last_run >= T`. Both tunable in config.

### Salience

Salience = a stored importance score per node, recomputed in batch. Inputs (deliberately simple, linear-weighted, tunable):

- outcome (`failure` > `partial` > `success` > `unknown`) — failures carry more future value
- decision content — nodes with non-empty `key_decisions`/`design_decisions`
- entity centrality — how connected the node's entities are in the relationship graph (this is why Phase 0 builds the graph first)
- recency decay — exponential half-life, same idea as `_recency_factor` in `confidence_scoring.py` but stored rather than computed per query
- evidence citations — how many synthesis nodes cite this node as support

Salience is a *ranking input* for retrieval, never a deletion criterion. Deleting history is a non-goal.

### Supersession

Facts have a lifecycle: `current` → `superseded`. When a new synthesis contradicts or replaces an older statement, we record `superseded_by = <node_id>` on the old node. Retrieval then prefers current nodes but can still surface history on demand (the `recent`/`known` modes in `agent_query` already hint at this routing). Supersede-never-delete preserves the audit trail and matches the immutable-revision discipline used by the auto-memory subsystem.

### Entity graphs

Today entities are tags on nodes. A graph adds typed edges between entities. The cheapest useful edge is **co-occurrence**: two entities linked to the same knowledge node are related, with weight = number of shared nodes. That is one `INSERT ... SELECT` over `knowledge_node_entities` — no LLM, no new capture. Once edges exist, retrieval can do **graph expansion**: query hits entity A → find A's strongest neighbors → pull their nodes too. This is the standard cure for vocabulary mismatch (you said "omlx", the relevant node only says "inference backend", but both connect to the `hippo` project entity).

## Design

### Process topology

A new `_consolidation_loop` coroutine in `BrainServer` (`brain/src/hippo_brain/server.py`), alongside `_enrichment_loop` / `_reaper_loop`. It lives in the brain — not a fifth LaunchAgent — because the brain already owns:

- the inference-server client and model preflight (`preflight_inference`)
- pause/resume for bench (`/control/pause`, `_paused`, `_resume_event`)
- query-priority arbitration (`_query_inflight` drain — consolidation must yield to queries exactly like enrichment does)

The loop is strictly decoupled from `_enrichment_loop`: enrichment keeps its per-batch contract untouched; consolidation reads only committed `knowledge_nodes` rows.

### Storage: reuse `knowledge_nodes`

Synthesis output is stored as rows in `knowledge_nodes` + `knowledge_vectors` with new `node_type` values. This is the central architectural decision: it gives synthesis nodes hybrid retrieval, FTS5, MCP tool access, evidence packets, and confidence scoring **with zero retrieval-code changes**. New node types:

| `node_type` | Scope | Content |
|---|---|---|
| `project_digest` | git_repo × time window | Rolling state-of-project: in-flight work, shipped changes, open decisions |
| `entity_profile` | high-centrality entity | Distilled "what is known about X" across all linked nodes |
| `pattern` | cross-session theme | Recurring arcs: debugging sagas, migrations, decision threads |

### Schema changes (v24)

```sql
-- Watermarks: one row per consolidation scope.
CREATE TABLE consolidation_state (
    scope_key        TEXT NOT NULL,          -- e.g. 'project:hippo', 'entity:omlx'
    synthesis_type   TEXT NOT NULL,          -- 'project_digest' | 'entity_profile' | 'pattern'
    watermark_node_id INTEGER NOT NULL,      -- consolidated through this knowledge_nodes.id
    last_run_ts      INTEGER NOT NULL,
    synthesis_version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (scope_key, synthesis_type)
);

-- Provenance: which observations support which synthesis (evidence trail).
CREATE TABLE synthesis_sources (
    synthesis_node_id INTEGER NOT NULL REFERENCES knowledge_nodes(id),
    source_node_id    INTEGER NOT NULL REFERENCES knowledge_nodes(id),
    PRIMARY KEY (synthesis_node_id, source_node_id)
);

-- Lifecycle + importance on every node.
ALTER TABLE knowledge_nodes ADD COLUMN superseded_by INTEGER REFERENCES knowledge_nodes(id);
ALTER TABLE knowledge_nodes ADD COLUMN salience REAL NOT NULL DEFAULT 0.0;

-- relationships table already exists (schema v5) — Phase 0 only backfills it.
```

All timestamps epoch ms (i64), WAL, `busy_timeout=5000` — per repo convention.

### Relationships backfill (Phase 0, pure SQL)

```sql
INSERT INTO relationships (from_entity_id, to_entity_id, relationship, weight, ...)
SELECT a.entity_id, b.entity_id, 'co_occurs', COUNT(*), ...
FROM knowledge_node_entities a
JOIN knowledge_node_entities b ON a.knowledge_node_id = b.knowledge_node_id
WHERE a.entity_id < b.entity_id
GROUP BY 1, 2;
```

(Exact column mapping per existing `relationships` schema; co-occurrence is the seed edge type. Typed edges like `caused_by` from `change_outcome` content come later, only if co-occurrence proves useful in retrieval.)

### Synthesis contracts

Each synthesis type gets a system prompt + JSON output contract, mirroring the existing enrichment contract (`SYSTEM_PROMPT` in `enrichment.py`, validated by a `SourceSchema`-style structural check). Output per synthesis run: one synthesis node (summary, key facts, open questions, confidence) + the list of supporting node IDs → `synthesis_sources`. The previous synthesis for the scope is marked `superseded_by` the new one in the same transaction.

### Triggers and budget

Three triggers, all funneling into the same watermark check:

1. **Watermark threshold** (primary): scope re-synthesizes when `new_nodes >= consolidation.new_node_threshold` (default ~50) or `age >= consolidation.max_age_secs` (default 24h).
2. **Cadence** (backstop): loop wakes every `consolidation.poll_interval_secs` (default 1h), evaluates all active scopes.
3. **Manual**: `hippo consolidate [--scope X] [--dry-run]` CLI → brain HTTP endpoint, for learning and debugging.

Budget safety: `consolidation.max_llm_calls_per_run` (default ~10), loop sleeps when the inference server is busy with queries, and the whole loop honors `_paused`. Supersede-not-delete everywhere.

### Versioning

`synthesis_version` in `consolidation_state` mirrors `enrichment_version`: bump the constant when prompts or models materially change; stale scopes re-synthesize on their next trigger. A `brain/scripts/re-consolidate.py` (modeled on `re-enrich-knowledge-nodes.py`) forces full re-synthesis for prompt iteration during development.

### Retrieval integration

Minimal, deliberate changes only:

- Ranking gains a small `salience` multiplier and a `superseded_by IS NULL` preference (not filter — history stays reachable).
- `node_type` becomes a retrieval weight: synthesis types boosted for "state of X" queries, raw observations for "what exactly happened" queries. `agent_query`'s existing modes (`known`/`recent`/`decisions`) are the natural routing surface.

### Measuring value

The bench harness (`brain/src/hippo_brain/bench/`) gains a small golden Q/A set **answerable only from synthesized knowledge** (e.g., "what is the current default inference backend and when did it change?"). If consolidation works, those score well; if the syntheses are hallucinating or stale, the gate fails. This keeps the layer honest the same way Tier-0 gates keep enrichment honest.

## Non-goals

- No per-event consolidation, ever.
- No deletion of observations (supersede only).
- No new LaunchAgent or process.
- No cross-machine sync, no cloud inference — local oMLX/LM Studio only.
- No fine-tuning / training in this layer (`training.py` exists separately).

## Open questions

1. Salience weight values — start equal-ish, tune against bench retrieval scores rather than intuition.
2. `pattern` scope discovery: entity-cluster-driven vs time-window-driven. Start time-window (simpler), revisit.
3. Should `entity_profile` syntheses feed `get_entities` MCP output directly, or stay retrieval-only? Lean retrieval-only initially.
