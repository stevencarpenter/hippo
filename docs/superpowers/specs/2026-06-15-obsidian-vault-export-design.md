# Obsidian Vault Export — Design Spec

**Date:** 2026-06-15
**Status:** Approved for planning
**Branch:** `feat/obsidian-vault-export`

## 1. Goal

Export the full hippo knowledge base into an **Obsidian-compatible markdown vault** in
which links between notes are first-class edges. The vault is a **one-way projection** of
the SQLite knowledge base: SQLite is the source of truth, the markdown is a derived
artifact, and edits made inside the vault are **discarded** on the next sync.

**Primary consumer:** AI coding agents (Claude Code, Codex) that read and traverse the
vault as raw markdown + frontmatter. **Secondary consumer:** a human browsing the vault in
Obsidian (graph view, backlinks pane, Dataview/Bases). Where the two conflict, the agent
wins, but we stay within native Obsidian conventions so the human path is not broken.

## 2. Scope

**In scope:**
- A one-shot full export (`hippo export vault`) and an incremental live sync
  (`com.hippo.vault-sync` launchd service), both implemented as a **full reconcile** of the
  vault against the live DB.
- A mixed vault: one note per knowledge node **and** one note per entity, with
  bidirectional wikilinks.
- Node→node "related" edges computed from **rarity-weighted shared entities** (bounded).
- Entity pages that list their member nodes (real hubs, not stubs).

**Out of scope (non-goals):**
- Reading edits back from the vault into SQLite (strictly one-way).
- Exporting raw event/session bodies verbatim — only enriched knowledge nodes + entities.
- A bespoke Obsidian plugin. The vault must be navigable with native Obsidian features +
  the generated index notes; Dataview/Bases are a bonus, never a requirement.

## 3. Why this design changed after adversarial review

A 7-lens adversarial review (verified against the **live** `hippo.db`, 13,579 nodes)
invalidated three of the four detailed decisions in the original sketch. The corrections
below are load-bearing; each was confirmed by direct query or code read, not assumed.

| Original assumption | Reality (verified) | Correction |
|---|---|---|
| Node `uuid` is stable; content changes but uuid doesn't | Agentic re-enrichment `DELETE`s the node and re-`INSERT`s a **new** `uuid4` (`claude_sessions.py:842,873`; `opencode_sessions.py:265`; `enrichment.py:465`). 4,656 dead ids already exist. | **Filenames derive from the stable source key, not the uuid.** |
| `related:` draws from the `relationships` table | `relationships` has **0 rows** live, and is entity→entity, not node→node. | Drop `relationships` as a node-edge source entirely. |
| Shared-entity co-occurrence gives node→node edges | **56.3M** co-occurrence pairs; `tool:git` links 5,699 nodes (42% of KB), `project:hippo` 5,671. Raw co-occurrence is noise. | **Rarity-weighted, hub-excluded, top-K** related edges. |
| `node_type` is `observation`, `outcome` is a 4-value enum, `content` is JSON | `node_type` also has `change_outcome` (1,618 nodes); `outcome` has 7 values incl. `cancelled`/`action_required`/`skipped`; **803 nodes (~6%) have non-JSON `content`**. | Handle the full vocab + a non-JSON fallback render. |
| `uv run --project brain` works from the installed binary | That path is repo-relative; every existing Rust→brain call goes over **HTTP**. | Export is a **brain HTTP endpoint**. |

## 4. Architecture

```
┌─────────────────┐  POST /vault/export        ┌──────────────────────────┐
│ hippo (Rust CLI)│ ─────────────────────────▶ │ hippo-brain server        │
│ export vault    │  {mode, out, ...}          │  vault_export.py          │
└─────────────────┘                            │   reads SQLite (1 snapshot)│
                                               │   renders markdown        │
┌─────────────────────────┐  same endpoint     │   reconciles vault dir    │
│ launchd                  │ ─────────────────▶ │   writes _vault_meta.json  │
│ com.hippo.vault-sync     │  on interval       └──────────────────────────┘
└─────────────────────────┘
```

- **`brain/src/hippo_brain/vault_export.py`** — the renderer + reconciler. Pure
  read-render-write over a single read transaction. All logic and tests live here.
- **Brain HTTP endpoint** — add `Route("/vault/export", self.vault_export, methods=["POST"])`
  to `server.py:get_routes()`. Reuses the brain's DB connection and the established
  Rust→brain HTTP transport (consistent with `/query`, `/ask`).
- **Rust CLI** — `hippo export vault [--out DIR] [--full]` sends one HTTP POST and renders
  the brain's progress/summary response. No subprocess, no `uv` dependency at the Rust layer.
- **launchd `com.hippo.vault-sync`** — `StartInterval`-driven (mirrors `com.hippo.cursor-session`),
  POSTs the export endpoint on the `[vault] poll_interval_secs` cadence. The brain must be up;
  if it isn't, the tick is a no-op logged to stderr (the brain daemon is already a managed service).

## 5. Vault layout

```
<vault>/
├── _vault_meta.json              # format version, hippo version, schema user_version, config hash
├── .gitignore                    # ignores the whole vault by default (see §10)
├── _index.md                     # small MOC: links to per-project + per-month index notes
├── indexes/
│   ├── project-<slug>.md         # one MOC per project entity, lists its nodes
│   └── month-YYYY-MM.md          # one MOC per month, lists nodes created that month
├── knowledge/
│   └── <shard>/<source-key-slug>.md   # sharded; see §6
└── entities/
    └── <type>/<canonical-slug>.md     # type ∈ project file tool service repo host person concept domain env_var
```

`knowledge/` is **sharded** (e.g. `knowledge/2026-06/<slug>.md` by node `created_at` month)
to avoid a single flat folder of ~13.5k files, which exceeds Obsidian's documented
degradation threshold. The shard is derived from immutable `created_at`, so a node never
migrates shards.

## 6. Node identity & filenames (stable source-key slug)

A knowledge node's `uuid` is **not** stable (§3). Filenames therefore derive from the
node's **stable source key**, resolved deterministically at export time:

**Source priority** (a node may link several source types; pick the highest-priority
present): `agentic` → `workflow` → `browser` → `shell-event` → `lesson`.

**Natural key within each source** (all are stable DB identities that survive re-enrichment,
because they are the *source* rows, not the node):
- agentic: `{harness}-{session_id}-{segment_index}` from the linked `agentic_sessions` row.
  When a node links **multiple** agentic sessions (1,548 nodes do), pick the **minimum**
  `(session_id, segment_index)` — deterministic and stable across re-mint, since the
  re-minted node re-links the same session set.
- workflow: `wf-{run_id}`; browser: `web-{browser_event_id}`; shell: `evt-{event_id}`;
  lesson: `lesson-{lesson_id}`.

**Collision disambiguation.** Two distinct live nodes can share a primary source key — e.g.
a `change_outcome` (CI) node co-links the same agentic session as an `observation` node.
The slug therefore prefixes the node_type discriminator for non-observation nodes
(`...-co` for `change_outcome`) and, for any residual exact collision, appends a
deterministic numeric suffix ordered by node `id`. Full reconcile (§8) makes any transient
collision self-healing.

**Fallback.** If a node has no source link at all (0 occur live, but defensively), slug =
`node-{uuid}`. The current `uuid` is always written into frontmatter regardless, for DB
cross-reference.

## 7. Note formats

### 7.1 Knowledge node note

Frontmatter is emitted through a **real YAML serializer** (not f-strings); every wikilink
value in a list is force-quoted (an unquoted leading `[` is a YAML flow-sequence indicator
and silently mangles or — on an unbalanced bracket — destroys the whole frontmatter block).

```markdown
---
uuid: 8f3a1c2b-...            # current (volatile) DB uuid, for cross-reference
source: claude-code/<session_id>#<segment_index>   # stable origin
type: knowledge
node_type: observation        # or change_outcome
outcome: success              # full vocab: success|partial|failure|unknown|cancelled|action_required|skipped
intent: debugging
created: 2026-06-15T10:30:00Z  # converted from epoch-ms via a correct ISO8601 formatter
updated: 2026-06-15T10:31:42Z
tags: [hippo, rust, storage]   # validated/slugified; the ONLY tag surface (no body #hashtags)
related:
  - "[[claude-code-7b2f9a-3|FTS trigger backfill]]"   # quoted; bare source-key slug + alias; bounded top-K (§9)
aliases:
  - "Fixed embed_text truncation in storage.rs"   # short DERIVED headline (§7.3), not the full summary
---

# Fixed embed_text truncation in storage.rs

**Outcome:** success · **Intent:** debugging · **Source:** claude-code session

## Summary
<content.summary>

## Key Decisions
- ...

## Problems Encountered
- ...

## Design Decisions
- **Considered** X — **Chose** Y — **Reason** Z   <!-- LIST, not a table: 84 nodes contain `|` -->

## Entities
- Projects: [[entities/project/hippo|hippo]]
- Files: [[entities/file/crates-hippo-core-src-storage-rs|storage.rs]]

## Related
- [[claude-code-7b2f9a-3|FTS trigger backfill]]   <!-- mirrors frontmatter related[]; bare slug resolves across shards; body links are reliably backlinked -->

## Sources
- agentic-session: claude-code/<session_id>#<segment_index>
```

**Entity wikilinks come from the `knowledge_node_entities` JOIN** (authoritative, typed),
**not** from the content-JSON `entities` buckets (whose bucket names — `errors`, `domains`,
etc. — do not map 1:1 to entity-table types and would mislabel links).

The `## Related` body section mirrors `related:` because frontmatter property links and body
links are presented differently in Obsidian's backlinks pane; the body form is the
universally-indexed one. Frontmatter `related:` is kept for Dataview/Bases querying.

### 7.2 Entity page (a real hub, not a stub)

```markdown
---
type: entity
entity_type: project
canonical: hippo
first_seen: 2026-01-01T00:00:00Z
aliases: ["Project: hippo"]
# NOTE: last_seen is intentionally OMITTED from frontmatter — it changes on every
# (re-)enrichment and would churn the file's content/mtime on every sync (§8).
---

# hippo

**Type:** project

## Nodes
- [[storage-fts-fix|Fixed embed_text truncation]]
- [[...]]                       <!-- bounded; for hub entities (>N nodes) paginate + log truncation -->
```

Entity pages **list their member nodes as outbound links** so the node↔entity edge is
bidirectional and visible to an agent that has no backlinks pane. For mega-hub entities
(e.g. `tool:git` → 5,699 nodes) the list is capped at a configured maximum with an explicit
"showing N of M" line (no silent truncation — AP per project conventions).

### 7.3 Derived headline

hippo has **no title field**. The only human text is `content.summary` (live: avg 378
chars, max 1,599). The note's H1 and `aliases:` entry use a **derived headline**: the
summary truncated at the first sentence boundary, capped at ~80 chars. The full summary
lives under `## Summary`. (A 378-char alias is useless in autocomplete and the graph.)

## 8. Sync model (fast full reconcile)

Both the one-shot and the launchd service run the **same reconcile**, over a single
read-transaction snapshot (WAL, `busy_timeout=5000`) so the export never sees a node
mid-replacement:

1. **Compute desired set** — query all live knowledge nodes (probe-filtered, §10) + all
   entities; derive each one's target path (§6) and rendered content.
2. **Write changed** — write a file only if its content differs from what's on disk
   (content-hash compare), so unchanged notes don't churn mtime / trigger Obsidian re-index
   storms. Each write is atomic (temp file + rename).
3. **Reconcile orphans** — delete any `knowledge/` or `entities/` file not in the desired
   set. This is what handles deletions and re-mint orphans (a `updated_at > watermark`
   query is structurally blind to both). The reconcile is scoped strictly to hippo-owned
   subtrees (§10).
4. **Write `_vault_meta.json`** — record `vault_format_version`, hippo version, schema
   `user_version`, and a hash of the relevant `[vault]` config.

At ~13.5k nodes with bounded `related:` (§9), a full reconcile is seconds, so we do **not**
implement watermark-incremental in v1. "Incremental" = "fast full reconcile on an interval."

**Format-version guard:** on start, if `_vault_meta.json` shows a different
`vault_format_version` (or the target dir's layout predates it), refuse incremental and
require a clean full re-export, so a future format change can't leave a half-migrated vault.

## 9. Node→node edges (bounded, rarity-weighted)

Raw shared-entity co-occurrence is unusable (56.3M pairs, hub-dominated). The `related:`
relation is computed as:

1. For each entity, compute its node-degree `d`. **Exclude** entities with `d > hub_degree_cap`
   (default 200) — generic hubs like `git`, `hippo`, `Claude Code` carry no relatedness signal.
2. For a node, score each candidate neighbor by the sum over shared (non-hub) entities of an
   **inverse-degree weight** (`1/log(1+d)` or similar) — rare shared entities (a specific
   file, a specific error string) dominate, as they should.
3. Keep the **top-K** neighbors (default `related_top_k = 8`).

This collapses the pair explosion (the excluded hubs are exactly what dominated the 56M),
yields bounded, meaningful edges, and is cheap enough to recompute fully each reconcile.

Entity-mediated links (every node → its entity pages, every entity page → its nodes) remain
the other first-class edge and require no thresholding.

## 10. Security, redaction & trust boundary

The vault is **plaintext on disk, outside hippo's DB boundary**. Therefore:

- **Default location:** a dedicated hippo-owned dir under XDG data
  (`~/.local/share/hippo/vault/`), never a user's existing Obsidian vault. The reconcile
  **refuses to run** if `--out` points at a directory containing a foreign `.obsidian/` or
  non-hippo markdown, to avoid clobbering user notes / config.
- **`.gitignore`:** the exporter writes a `.gitignore` at the vault root ignoring the whole
  tree by default, so a plaintext second brain doesn't silently land in a committed repo.
- **Export-time redaction pass:** rendered text is run through hippo's redaction patterns
  again as defense-in-depth — capture-time redaction has historical misses, and the vault
  turns any miss into a greppable plaintext file. `env_var` entities emit **names only**.
- **Probe filtering (AP-6):** `knowledge_nodes` has no `probe_tag` column, so probe
  contamination, if any reaches a node, is invisible at the node level. The export query
  **excludes nodes whose only source links are probe-tagged** source rows (join to
  `events`/`agentic_sessions`/`browser_events` and require a non-probe source), restoring
  the AP-6 guarantee at this new user-facing surface.
- **One-way banner:** every generated file opens with
  `<!-- GENERATED BY hippo export vault — edits are overwritten on next sync -->`.

## 11. Config (`[vault]` section)

```toml
[vault]
enabled = false                 # opt-in
out = "~/.local/share/hippo/vault"
poll_interval_secs = 300        # launchd cadence; templated into the plist at install time
related_top_k = 8
hub_degree_cap = 200            # entities above this are excluded from related[] scoring
hub_node_list_cap = 200         # max member nodes listed on an entity page
shard_by = "month"              # knowledge/ sharding scheme
```

Because launchd templates the interval/paths into the plist at **install** time
(`install.rs` `__..._SECS__` placeholders), changing `poll_interval_secs` or `out` requires
`hippo daemon install --force` to take effect — documented as such; a config edit alone is
inert for those two keys.

## 12. Observability

- **`hippo doctor`** gains a vault check: last successful export time, vault file count,
  orphan-reconcile count last run, and `_vault_meta.json` format-version drift. (A
  `source_health` row is **not** added — that table is scoped to capture/ingestion sources,
  and this is an export sink, not a capture path.)
- The feature is documented in `docs/` (a vault-export page) and referenced from the README;
  the new `com.hippo.vault-sync` service is listed in the operator-facing service inventory.

## 13. Testing strategy

- **Golden-file tests** in `brain/tests/` for the renderer: fixed node/entity rows →
  expected markdown, including the hard cases proven to exist live: non-JSON `content`,
  `node_type=change_outcome`, the full `outcome` vocab, `design_decisions` cells containing
  `|` and newlines, `concept` entity canonicals containing `[`/`]`/`:`/`/`/`"`, NULL
  `canonical`, and `name != canonical`.
- **YAML round-trip test:** every emitted frontmatter block re-parses under a YAML loader
  **and** an Obsidian-style `[[...]]` extractor recovers the intended links.
- **Reconcile test:** orphan files (simulating deleted / re-minted nodes) are removed;
  unchanged files are not rewritten (mtime stable); foreign `.obsidian/` dir aborts the run.
- **Slug test:** multi-agentic-session nodes and observation/change_outcome collisions on a
  shared session key produce distinct, deterministic, stable filenames.
- **Bounded-related test:** hub entities above the cap are excluded; `related[]` length ≤ K.

## 14. Findings traceability

This spec absorbs the 57 verified findings + 8 completeness gaps from the
2026-06-15 adversarial review. The highest-severity items and where they are addressed:

| Finding | Section |
|---|---|
| Re-enrichment mints new uuid → uuid filenames orphan | §3, §6 |
| `relationships` empty + entity-scoped | §3, §9 |
| Co-occurrence 56M-pair / hub blowup | §3, §9 |
| Concept canonicals / NULL canonical break slugs & wikilinks | §6, §13 |
| node_type / outcome vocab mismatch; 803 non-JSON nodes | §3, §7.1, §13 |
| Watermark blind to deletions; re-mint orphans | §8 |
| Flat 13.5k-file folder cliff; unbounded _index.md | §5 |
| `uv run --project brain` won't resolve installed | §3, §4 |
| YAML quoting / frontmatter-vs-body backlink asymmetry | §7.1 |
| Thin entity hubs are agent dead-ends | §7.2 |
| Redaction leakage / default path / trust boundary | §10 |
| One-way contract unstated; agent edits lost | §1, §10 |
| Format versioning; existing-vault clobber; doctor/docs gaps | §8, §10, §12 |

## 15. Open implementation details (deferred to the plan)

- Exact slug collision-suffix format and the inverse-degree weight function.
- Whether `## Sources` should resolve agentic session ids to file paths an agent can open.
- Pagination format for mega-hub entity node lists.
- Whether to also emit a machine-readable `graph.json` sidecar (an agent-ergonomics
  finding) — deferred; the per-file frontmatter is sufficient for v1.
