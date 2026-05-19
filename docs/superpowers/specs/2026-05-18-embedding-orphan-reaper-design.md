# Embedding Orphan-Reaper + Watchdog Invariant

**Date:** 2026-05-18
**Status:** Approved design
**Branch:** `feat/embedding-orphan-reaper`

## Context

hippo enriches captured activity into `knowledge_nodes` rows and embeds each into a
sqlite-vec `vec0` virtual table (`knowledge_vectors`) for semantic search. Embedding is
**opt-in per enrichment source**: each `_enrich_*` method in
`brain/src/hippo_brain/server.py` must explicitly schedule `_embed_node`. There is no
central embed step and no safety net.

That fragility shipped a bug: workflow/CI enrichment never embedded its nodes (fixed in
PR #167). At the time of writing, ~344 `knowledge_nodes` (~3.3% of the corpus) have no
vector row and are invisible to semantic search. ~288 are CI-run `change_outcome` nodes;
the rest are transient embed failures that, with no retry path, stayed orphaned
permanently.

PR #167 stops the bleed for new workflow nodes but does not (a) backfill the 344 existing
orphans or (b) prevent recurrence — a sixth source that forgets to embed, or a transient
embed failure, would silently orphan nodes again.

## Goals

1. Heal orphaned `knowledge_nodes` automatically, regardless of which source created them.
2. Backfill the existing ~344 orphans.
3. Provide a retry path for transient embed failures.
4. Alarm when orphans accumulate beyond a tolerated steady state — a backstop for the
   reaper itself failing.

## Non-goals

- **An embedding queue.** A queue that sources must enqueue into reproduces the
  opt-in-per-source fragility this work exists to remove. Rejected in favour of an
  anti-join.
- **Per-node retry caps / dead-lettering.** Poison nodes (input the embedder rejects) are
  rare; the reaper retries them indefinitely at negligible cost, and the alarm threshold
  tolerates a small steady state. Rejected as YAGNI.
- **Improving workflow nodes' `embed_text` quality** — a separate, pre-existing concern.

## Architecture

Two cooperating components that never call each other; they share only the `config.toml`
`[reaper]` section and the definition of "orphan".

### A. Embedding orphan-reaper — Python, `hippo_brain`

A new in-process loop `_embed_reaper_loop` on `BrainServer`, started in
`start_enrichment()` alongside `_enrichment_loop` and `_reaper_loop`, and cancelled in
`stop_enrichment()`.

Each tick:

1. `sleep(interval_secs)`.
2. Open a DB connection via `_get_conn()`.
3. Anti-join query for orphans:
   ```sql
   SELECT id, embed_text FROM knowledge_nodes
   WHERE created_at < :now_ms - :stale_ms
     AND id NOT IN (SELECT rowid FROM knowledge_vectors_rowids)
   ORDER BY created_at
   LIMIT :batch_size
   ```
4. For each orphan:
   `await self._embed_node(id, {"id": id, "embed_text": embed_text, "commands_raw": ""}, "reaper")`.
5. Log a one-line summary if any orphans were processed.

The reaper uses a plain connection (`_get_conn()`) and queries the
`knowledge_vectors_rowids` **shadow table** — whose `rowid` column is the
`knowledge_node_id` — rather than the `knowledge_vectors` virtual table, so it needs no
`vec0` module loaded. A missing shadow table (fresh install, no embeddings yet) is
caught and treated as "no orphans", the same `sqlite3.OperationalError` pattern
`_collect_queue_depths` already uses. Embedding itself still goes through the existing
`_embed_node` → `self._vector_table` vec0 handle. The loop is stateless: no new table, no
migration. A failed orphan stays an orphan and is retried next tick. The first ticks
drain the ~344-node backlog (~7 ticks at the default batch size).

### B. Watchdog invariant — Rust, `crates/hippo-daemon/src/watchdog.rs`

A new invariant — **I-14** (I-1..I-13 are already in use) — run by the existing
`com.hippo.watchdog` launchd job every 60s.

It asserts that:
```sql
SELECT count(*) FROM knowledge_nodes
WHERE created_at < :now_ms - :stale_ms
  AND id NOT IN (SELECT rowid FROM knowledge_vectors_rowids)
```
is `<= alarm_threshold`. On violation it writes a rate-limited `capture_alarms` row,
consistent with the existing invariants.

The Rust daemon does not load the `vec0` module, so the invariant queries the
`knowledge_vectors_rowids` **shadow table**. Its `rowid` column is the
`knowledge_node_id`, because the vec0 table's primary key is declared
`knowledge_node_id INTEGER PRIMARY KEY` and sqlite-vec aliases such a key to the shadow
table's `rowid`. This is an accepted coupling to a sqlite-vec internal; the invariant's
test in `capture_invariants.rs` guards against a silent break.

## Configuration

A new `[reaper]` section in `config.toml`, read by both the brain and the watchdog:

| key | default | unit | consumer |
|---|---|---|---|
| `interval_secs` | 300 | seconds | brain reaper loop cadence |
| `batch_size` | 50 | rows | orphans re-embedded per tick |
| `orphan_stale_secs` | 900 | seconds | minimum node age to count as an orphan |
| `alarm_threshold` | 25 | rows | watchdog invariant alarm cutoff |

`orphan_stale_secs` is shared: the same 15-minute cutoff defines "orphan" for both the
reaper and the invariant.

## Parameter rationale

- **`orphan_stale_secs` = 900 (15 min):** a node younger than this is normal
  inline-embed lag, not an orphan. It is also the **race guard** — 15 minutes far
  exceeds any in-flight inline embed task, so the reaper can never select a node whose
  inline embed is still running, avoiding a duplicate `vec0` insert.
- **`interval_secs` = 300:** the existing `_reaper_loop` runs on `poll_interval_secs`
  (~5s) — too hot for a loop issuing inference calls. The reaper gets its own slower
  cadence.
- **`batch_size` = 50:** drains the ~344-node backlog in ~7 ticks (~35 min);
  steady-state ticks process ~0.
- **`alarm_threshold` = 25:** node creation runs ~23/hour, so 25 orphans ≈ "reaper down
  ~1 hour" — fires on a real outage, ignores a stray poison node.

## Error handling

- **Single orphan embed failure:** `_embed_node` already catches, logs, and swallows the
  exception (and `embed_knowledge_node` increments the `_embed_failures` counter). The
  loop continues to the next orphan; the failed node stays an orphan and is retried next
  tick.
- **Whole-tick failure** (e.g. DB error): the tick body is wrapped in `try/except` with a
  logged warning, exactly as `_reaper_loop` does. The loop survives.
- **Embed-model drift:** `embed_knowledge_node` raises `EmbedDriftError` when the
  configured model differs from the corpus model. During a model migration the reaper
  logs and retries; it drains once config and corpus agree. No special handling.
- **Reaper vs. inline embedding race:** prevented by `orphan_stale_secs` — see parameter
  rationale.

## Testing

- **Reaper (Python, `brain/tests/`):**
  - Seed `knowledge_nodes` with an old un-embedded node, a recent un-embedded node, and an
    old already-embedded node. Run one reaper tick with `_embed_node` stubbed by a
    recorder. Assert only the old un-embedded node is embedded.
  - Assert a failing `_embed_node` does not abort the tick — remaining orphans are still
    processed.
- **Invariant (Rust, `crates/hippo-daemon/tests/capture_invariants.rs`):**
  - Seed orphan nodes at or below the threshold → invariant passes, no `capture_alarms`
    row written.
  - Seed above the threshold → invariant fails, one `capture_alarms` row written.
- TDD applies during implementation: a failing test precedes each unit.

## Scope

One PR (anticipated #168) spanning the Python brain and the Rust watchdog — a single
cohesive change. No schema migration. Documentation updated in the same PR:
`docs/capture/architecture.md` (the new invariant and the reaper loop) and a `[reaper]`
entry wherever config keys are documented.
