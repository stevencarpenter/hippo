# Canonical project identity (HIPO-10)

Schema v27 adds `projects`, `project_aliases`, and `knowledge_nodes.project_id`. Schema v24 already belongs to Pi capture. This migration adds storage only. It does not rewrite event or session metadata or run a data backfill.

Enrichment attributes knowledge nodes from captured provenance, not model-generated project entities. Captured remote-derived `owner/repo` or remote URLs take precedence over local repository names. Repository names take precedence over session directory basenames. Capture already uses Rust `derive_git_repo` to prefer the remote URL over the local repository name. Backfill uses stored metadata only, with no filesystem Git lookup. A node with multiple projects at the winning precedence keeps a NULL key rather than choosing an arbitrary project. Encoded session directories use captured `cwd` before basename normalization. The separate entity resolver retains its existing exact-root and subpath protections.

## Editable aliases

Use alias-to-canonical-name entries in the existing Hippo configuration:

```toml
[projects.aliases]
"SNUG" = "snugmarina"
"SNUG project" = "snugmarina"
"SNUG-23" = "snugmarina"
"snug-31" = "snugmarina"
"snugmarina-base" = "snugmarina"
```

Owner prefixes, `.git` suffixes, leading dots, case and whitespace normalize without explicit aliases. Other families require configuration. Both enrichment and query resolution read the map. Queries resolve newly configured aliases against existing project keys without changing the database. The backfill can update existing keys after a configuration edit.

Explicit project filters resolve canonical IDs using the current configuration, not historical alias associations. Current aliases also contribute normalized spellings to inference and provenance matching. Unresolved filters that name no current alias retain substring matching across assigned and unassigned provenance. Unassigned historical nodes remain searchable through read-only provenance matching until a separately authorized backfill assigns their keys. Search and `/ask` also scope a single known project mention before retrieval. A longer named non-project entity, such as `Lumin Digital`, does not resolve to the short project alias `lumin`. Multiple project mentions do not imply a single project filter. Entity display names remain separate from project identity.

## Backfill

After a separately authorized database migration, an operator can run this idempotent command against an explicit existing database:

```sh
uv run --project brain python -m hippo_brain.projects --db /explicit/path/to/hippo.db
```

The backfill creates identities from captured repositories and agentic sessions. It attributes existing nodes through their source links, including `knowledge_node_agentic_sessions`. It does not change captured event, session or knowledge content fields. This source change does not authorize an installed-database backfill, deployment or live re-embedding.

## Chosen acceptance approach

Keep `hippo_kb_project_fragmentation_ratio` as an informational metric with its existing formula and published meaning. It remains raw project basenames divided by dot/separator-normalized project basenames. Canonical aliases do not redefine its denominator. Do not rewrite captured metadata to make the ratio fall.

Verify canonical identities, aliases and project-versus-employer retrieval directly. `brain/tests/test_projects.py` exercises synthetic precedence, normalization, configured SNUG families, ingest/query parity, existing-node backfill, configuration edits and the `/ask` evidence boundary. It also verifies that canonicalization leaves source metadata and the fragmentation metric unchanged. Rust storage tests execute the additive migration and its reruns while preserving knowledge and HIPO-13 receipts. The full acceptance gate is `mise run test`.
