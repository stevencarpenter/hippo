# Implementation notes — RAG entity surfacing (issue #108)

## Gate 0 — env-var bucketing probe

Spec section: "Known limitation: env vars and other unbucketed token kinds"
(`docs/archive/feature-waves/2026-04-27-rag-entity-surfacing-design.md`).

### Command

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

### Output (full, abridged where row content is exact-duplicate)

The query returned a large set of rows. Among the `IDENTIFIER_ENTITY_TYPES`
buckets (`tool`, `file`, `service`, `project`), **the literal string
`HIPPO_PROJECT_ROOTS` is not present in any row**. Concrete distinct values
observed in those four buckets across all matching nodes:

- `tool`: `Claude Code`, `Haiku agent`, `SQLite`, `Sonnet agents`, `bash`,
  `gh`, `gh (GitHub CLI)`, `gh pr create`, `git`, `git log`, `grep`,
  `hippo alarms prune`, `hippo doctor`, `launchctl`, `launchd`, `mise`,
  `mise run install --clean`, `nohup`, `py_compile`, `pytest`, `python`,
  `python3`, `ruff`, `ruff check`, `ruff format`, `sed`, `semgrep`,
  `sqlite3`, `uv`, `uv run`, `uv run --project brain`
- `file`: paths only (e.g.,
  `/Users/carpenter/projects/hippo/brain/src/hippo_brain/enrichment.py`,
  `~/.config/hippo/config.toml`, `brain/scripts/dedup-entities.py`, etc.)
- `service`: `GitHub`, `SQLite`, `MCP (hippo)`, `sqlite-vec`,
  `mergeblocker-dedup`, `pytest test suite`, etc.
- `project`: `brain`, `hippo`, `hippo-postgres`

The only row that mentions `HIPPO_PROJECT_ROOTS` at all in the joined entity
set is in the `concept` bucket, with name `HIPPO_PROJECT_ROOTS not set` —
i.e., the LLM modeled the *condition* "the env var is not set" as a concept,
not the env var itself as a tool/service.

`concept` is in `NON_IDENTIFIER_ENTITY_TYPES`, so the `Entities:` line will
not surface it (per spec section "Single source of truth for the type list").

### Interpretation

**RESULT (initial probe): env var is NOT bucketed in IDENTIFIER_ENTITY_TYPES.**

This was the canonical repro for issue #108 — the `Entities:` line render fix
alone would not have closed it, because the LLM-emitted `entities` dict had no
slot for env vars to live in.

### Resolution: take Option (a) head-on (this PR)

Rather than ship a partial fix, this PR also implements the spec's option
(a): add a first-class `env_var` entity type. The full set of changes:

1. **Schema v12 → v13 migration** (`crates/hippo-core/src/storage.rs`,
   `schema.sql`) — extend the `entities.type` CHECK list with `'env_var'`.
   Migration recreates the table via SQLite's documented 12-step recipe
   (CREATE entities_new, copy rows, DROP, RENAME, recreate indexes) since
   ALTER TABLE cannot modify CHECK constraints. Idempotent: a partial-success
   crash is recoverable via `DROP TABLE IF EXISTS entities_new` at the top.
   Tested against both happy-path and crash-recovery scenarios.

2. **Enrichment prompt** (all three sources — shell, claude_sessions,
   browser) — adds an `env_vars` array to the `entities` JSON schema with
   guidance to surface UPPERCASE_UNDERSCORE env var names verbatim.

3. **Single-source-of-truth wiring** —
   `SHELL_ENTITY_TYPE_MAP["env_vars"] = "env_var"` and
   `IDENTIFIER_ENTITY_TYPES = (..., "env_var")`. The taxonomy guard test
   (added in the structural fix) auto-validates that every map value is
   classified.

4. **Validation** (`hippo_brain.models._ENTITY_KEYS`) — accepts the new
   bucket; non-string entries filtered like the others; absence defaults
   to `[]` so older LLM outputs still parse.

5. **Re-enrichment ratchet** — new constant
   `enrichment.CURRENT_ENRICHMENT_VERSION = 3`, used by all three write
   paths (`write_knowledge_node` × 3) and imported by the re-enrich
   script as its TARGET. Bumping this constant invalidates the entire
   corpus on the next re-enrich run — exactly what we want here, since
   v1/v2 nodes never had env_var extraction.

6. **schema_version.py** — `EXPECTED_SCHEMA_VERSION` bumped to 13; v12
   dropped from `ACCEPTED_READ_VERSIONS` (a v12 DB cannot accept env_var
   inserts and would crash mid-enrichment).

### Operational notes

- **Active re-enrichment was killed.** A run was at 232/6343 (~3.7%) when
  this work began. SIGTERM was safe — each node is its own transaction, no
  half-state. After this PR lands, re-enrichment must be restarted from
  scratch (`enrichment_version < 3` selects every node, including the 232
  that completed pre-bump).
- **Daemon must be restarted before brain.** The v12→v13 migration runs in
  `open_db`, owned by the daemon. Brain refuses v12 DBs at connect time.

### Acceptance for issue #108

After re-enrichment completes, the canonical repro
`hippo ask "what env var does dedup-entities.py require?"` should return
`HIPPO_PROJECT_ROOTS` verbatim — the script's enrichment node will have
`HIPPO_PROJECT_ROOTS` in its `env_var` bucket, that bucket will be hydrated
by `_fetch_details`, and the `Entities:` line will surface it above the
truncatable `Detail:` block.
