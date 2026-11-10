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

Explicit project filters resolve canonical IDs using the current configuration, not historical alias associations. Current aliases also contribute normalized spellings to inference and provenance matching. Unresolved filters that name no current alias retain substring matching across assigned and unassigned provenance. Unassigned historical nodes remain searchable through read-only provenance matching until a separately authorized backfill assigns their keys. When no explicit project filter is supplied, search and `/ask` scope a single known project mention before retrieval. Inference includes canonical alias targets immediately after a configuration edit, without enrichment or backfill. A longer named non-project entity, such as `Lumin Digital`, does not resolve to the short project alias `lumin`. Multiple project mentions do not imply a single project filter. Entity display names remain separate from project identity.

## Backfill

After a separately authorized database migration, an operator can run this idempotent command against an explicit existing database:

```sh
uv run --project brain python -m hippo_brain.projects --db /explicit/path/to/hippo.db
```

The backfill creates identities from captured repositories and agentic sessions. It attributes existing nodes through their source links, including `knowledge_node_agentic_sessions`. It does not change captured event, session or knowledge content fields. This source change does not authorize an installed-database backfill, deployment or live re-embedding.

## Concurrent projects

Several projects are usually active in the same minutes, so time overlap does not relate two captured items. Three paths apply project identity instead of time alone:

- Shell enrichment groups a claimed batch by captured `git_repo` before it splits on time (`_chunk_events` in `brain/src/hippo_brain/enrichment.py`). A node no longer mixes the commands of two repositories, and a terminal that alternates between two repositories yields two chunks. An event outside a repository stays with the repository of the events before it.
- Workflow-run enrichment links an agentic session to a run only when the session directory belongs to the run's repository. The ±15 minute window still applies.
- `get_lessons` drops a session lesson whose node directory belongs to another repository, and a node whose linked events span more than one project (`mixed_repository_nodes`).

`directory_in_project` in `brain/src/hippo_brain/projects.py` decides membership from stored metadata, with no filesystem lookup. `directory_repos` supplies the captured repositories for all directories in one read of `events`:

| Result | Condition |
|---|---|
| `True` | An event recorded in the directory carries the project's `git_repo`. This covers a worktree of any name and a subdirectory of the checkout. |
| `True` | No event was recorded in the directory, and the directory is named after the project. |
| `False` | Events recorded in the directory carry only other repositories. This covers a directory that another repository names after this project (`hippo/.claude/agents` is not the `agents` project). |
| `None` | Nothing is known: no event was recorded there and the name does not match. |

Enrichment links a session only on `True`, so a session in a directory with no captured event and an unrelated name (a new worktree, a Cursor empty window) is not linked.

The directory is where the session started. A session that starts in repository A and pushes repository B from there (for example with `git -C`) is placed in A: it is not linked to B's CI run, and the repair below removes its old link to that run.

Nodes written before this change keep their content. Workflow nodes written before it also keep links to sessions from other repositories. Count those links, then remove them, with this idempotent command against an explicit existing database:

```sh
uv run --project brain python -m hippo_brain.workflow_enrichment --db /explicit/path/to/hippo.db
uv run --project brain python -m hippo_brain.workflow_enrichment --db /explicit/path/to/hippo.db --apply
```

Without `--apply` the command opens the database read-only and prints the count. With `--apply` it first takes a backup checkpoint of the configured database (`checkpoint_before_mutation`, the same step the dedup and re-enrich scripts take) and stops if the checkpoint fails. It then deletes a link only on `False` (events place the session in another repository), keeps a link whose session repository is unknown, and re-attributes the affected nodes. This source change does not authorize running `--apply` on an installed database.

## Chosen acceptance approach

Keep `hippo_kb_project_fragmentation_ratio` as an informational metric with its existing formula and published meaning. It remains raw project basenames divided by dot/separator-normalized project basenames. Canonical aliases do not redefine its denominator. Do not rewrite captured metadata to make the ratio fall.

Verify canonical identities, aliases and project-versus-employer retrieval directly. `brain/tests/test_projects.py` exercises synthetic precedence, normalization, configured SNUG families, ingest/query parity, existing-node backfill, configuration edits and the `/ask` evidence boundary. It also verifies that canonicalization leaves source metadata and the fragmentation metric unchanged. Rust storage tests execute the additive migration and its reruns while preserving knowledge and HIPO-13 receipts. The full acceptance gate is `mise run test`.
