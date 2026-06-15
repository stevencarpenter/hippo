# Obsidian Vault Export

The hippo vault export feature projects your knowledge base into an Obsidian-compatible markdown vault. The vault is a **one-way projection**: SQLite is the source of truth, the markdown is a derived artifact, and edits made inside the vault are discarded on the next sync.

## Quick Start

### One-shot export

```bash
hippo export vault --out ~/.local/share/hippo/vault
```

This performs a full reconcile: fetches the current knowledge base, renders all knowledge nodes and entity pages, writes changed files, deletes orphaned nodes, and updates metadata.

### Continuous sync

Enable vault export in your config:

```toml
# ~/.config/hippo/config.toml
[vault]
enabled = true
out = "~/.local/share/hippo/vault"
poll_interval_secs = 300       # sync every 5 minutes
```

Then install or reinstall the daemon:

```bash
hippo daemon install --force
```

This creates a launchd service `com.hippo.vault-sync` that runs the export on your configured interval.

**Important:** The `poll_interval_secs` and `out` paths are templated into the launchd plist at install time. Changing either one requires `hippo daemon install --force` for the change to take effect — a config-file edit alone is inert for these keys.

## Configuration

All keys live in the `[vault]` section of `~/.config/hippo/config.toml`:

| Key | Default | Notes |
|-----|---------|-------|
| `enabled` | `false` | Opt-in. When `false`, vault export is disabled and the launchd service is not installed. |
| `out` | `~/.local/share/hippo/vault` | Vault output directory. Must be a dedicated hippo vault; the exporter refuses to run if the target contains a foreign `.obsidian/` directory. |
| `poll_interval_secs` | `300` | Sync cadence for the `com.hippo.vault-sync` launchd service (seconds). Requires `hippo daemon install --force` to change. |
| `related_top_k` | `8` | Maximum number of "related" node links per knowledge node (rarity-weighted, hub-excluded; see below). |
| `hub_degree_cap` | `200` | Entity degree threshold for hub exclusion. Entities linked to more than this many nodes (e.g., `tool:git` → 5,699 nodes) are excluded from the related-edge scoring, preventing generic hubs from dominating the graph. |
| `hub_node_list_cap` | `200` | Maximum nodes listed on an entity page. Mega-hub entities show a truncation note: "showing N of M nodes". |
| `shard_by` | `"month"` | Knowledge base sharding scheme. `"month"` shards by node `created_at` month (e.g., `knowledge/2026-06/`); `"all"` uses a flat `knowledge/` directory. Month sharding avoids Obsidian's documented degradation threshold for single folders with 10k+ files. |

## Vault Layout

```
vault/
├── _vault_meta.json              # Format version, hippo version, schema, config hash
├── .gitignore                    # Ignores whole vault by default
├── _index.md                     # Root index: links to per-project and per-month MOC notes
├── indexes/
│   ├── project-<slug>.md         # One index per project entity
│   └── month-YYYY-MM.md          # One index per month (if month sharding)
├── knowledge/
│   └── <shard>/<source-key-slug>.md   # Knowledge nodes, sharded by created_at
└── entities/
    └── <type>/<canonical-slug>.md     # Entity hub pages (project, file, tool, service, etc.)
```

## File Identity

Knowledge node filenames derive from a **stable source-key slug**, not the node's `uuid` (which is re-minted on re-enrichment). The slug is determined by source priority:

1. **Agentic sessions** (`claude-code`, `codex`, `cursor`, `opencode`): `{harness}-{session_id}-{segment_index}` (picking the minimum session/segment pair if a node links multiple sessions)
2. **Workflow runs**: `wf-{run_id}`
3. **Browser events**: `web-{browser_event_id}`
4. **Shell events**: `evt-{event_id}`
5. **Lessons**: `lesson-{lesson_id}`

If a node has no source links, the fallback is `node-{uuid}`. Collision disambiguation for `change_outcome` nodes appends `-co` to the slug.

## Features

### Knowledge node notes

Each knowledge node renders as a markdown file with:

- **Frontmatter:** uuid, node type, outcome, intent, created/updated timestamps, tags, and a bounded list of related-node links
- **Headline:** derived from the first sentence of the content summary (capped at ~80 chars)
- **Sections:** Summary, Key Decisions, Problems Encountered, Design Decisions, Entities, Related nodes, and Sources
- **Entity links:** bidirectional wikilinks to all entity pages (project, file, tool, etc.)
- **YAML round-trip:** frontmatter is valid YAML and preserves wikilink syntax correctly

### Entity hub pages

Entity pages list their member knowledge nodes (up to `hub_node_list_cap`) with an explicit truncation note for mega-hubs. This makes entities navigable in Obsidian's graph view and searchable via the index.

### Related edges

Related links are computed from **rarity-weighted shared entities**. Generic hubs (`git`, `hippo`, `Claude Code`) are excluded via `hub_degree_cap`; rare shared entities (a specific file, a specific error) dominate the scoring. The top `related_top_k` neighbors are kept per node.

### Index notes (MOCs)

- **`_index.md`**: root index listing all projects and months
- **Per-project indexes** (`indexes/project-*.md`): list all nodes tagged with that project
- **Per-month indexes** (`indexes/month-YYYY-MM.md`): list all nodes created in that month

## Trust Boundary & Safety

The vault is plaintext on disk, outside the database boundary. Several safeguards apply:

- **Foreign vault guard:** Export refuses to run if the target directory contains a `.obsidian/` folder without hippo metadata, preventing accidental clobbering of user Obsidian vaults.
- **`.gitignore`:** A `.gitignore` file is written to the vault root, configured to ignore the entire tree by default, so the plaintext knowledge base doesn't silently land in a committed git repo.
- **Export-time redaction:** All rendered markdown is passed through hippo's redaction patterns (a defense-in-depth pass), redacting secrets and environment variable values.
- **Probe filtering:** Knowledge nodes sourced only by synthetic probe events are excluded from export (AP-6 guarantee).
- **One-way marker:** Every generated file opens with a comment stating it is auto-generated and edits will be overwritten on next sync.

## Observability

Run `hippo doctor` to check vault export health:

```bash
hippo doctor
```

The doctor reports a single vault line:
- `[--]` when vault export is disabled
- `[WW]` when enabled but no vault exists yet at the configured `out` (run `hippo export vault`)
- `[OK]` with the time since the last sync (derived from the `_vault_meta.json` mtime)
- `[!!]` if the metadata file cannot be read

## Design Rationale

For background on design decisions (stable source-key filenames, rarity-weighted related edges, full reconcile vs. watermark-incremental, YAML frontmatter format), see the full design spec at [`docs/superpowers/specs/2026-06-15-obsidian-vault-export-design.md`](../superpowers/specs/2026-06-15-obsidian-vault-export-design.md).
