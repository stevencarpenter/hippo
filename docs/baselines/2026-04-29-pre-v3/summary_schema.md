# Schema / Python-data expert summary

Mean scores: accuracy 4.82 / succinctness 4.51 / usefulness 4.38 / ask 4.94 / mcp 4.43

100 nodes scored, written to `scores_schema.jsonl`.

## Structural fault tally

| Fault type | Count | Example uuid |
|---|---|---|
| invalid_json | 0 | — |
| missing required field | 0 | — |
| null where list expected | 0 | — |
| invalid outcome | 0 | — |
| entities not dict / missing buckets / wrong-type buckets | 0 | — |
| design_decisions wrong shape | 0 | — |
| worktree prefix not stripped (path-typed) | 6 | c1260596-e479-4bc7-a9ff-6904f722fefd |
| env_var bad case / not env-shaped | 3 | fe5d66df-c6f0-43bd-ad00-f6f18bbd7f0f |
| tool entity carries args ("cargo clippy") | 19 | 842bfe93-ec2e-4b4b-af2d-769485a8f887 |
| sparse tags (≤2) | 1 | d3368650-f2ee-474a-a51b-ca034c396465 |
| generic-only tag set / within-node duplicates | 0 | — |

`design_decisions` distribution: 0 nulls, 61 empty `[]`, 39 populated — every populated entry is a well-formed `{considered, chosen, reason}` dict. Outcomes: success 81 / partial 14 / failure 3 / unknown 2 (all valid).

## Worst 5 nodes

- `25b32204-…` and `6dd039b8-…` — files contain `gracious-williamson-8c3e1f` worktree prefix; tools include `uv run --project ...` composite.
- `c1260596-…` — `files` entity is a bare worktree dir.
- `266a9199-…` — `env_vars` include `$PPID`/`$$`; tools="cargo clippy"/"cargo test".
- `b63c3f9b-…` — `projects` entity is a worktree path (creates phantom project row).

## Cross-cutting observation

Hard schema is 100% clean: JSON validity, required keys, outcome enum, entities/dd shapes — zero violations across all 100 nodes. All remaining drift is in two semantic-taxonomy areas the validator does not enforce:

1. **Worktree prefixes leak into 6/100 path-typed entity names**, and `upsert_entities`'s fix-on-conflict-only rule leaves the pollution baked into first-writer rows.
2. **The `tools` bucket gets filled with shell-invocation phrases** (`cargo clippy`, `git log`, `uv run --project`) instead of bare command names in 19/100 nodes, defeating cross-node dedup and inflating `get_entities` cardinality.

Both are higher-leverage to fix in `upsert_entities` (add a tool-name normalize pass; tighten the worktree-strip to apply unconditionally on path types) than to chase via more prompt rules — those changes also retroactively clean already-written rows.

Authoritative refs consulted:
- `brain/src/hippo_brain/enrichment.py` (taxonomy maps + SYSTEM_PROMPT)
- `brain/src/hippo_brain/models.py` (`validate_enrichment_data`, `_VALID_OUTCOMES`, `_ENTITY_KEYS`)
- `brain/src/hippo_brain/entity_resolver.py` (`canonicalize`, `is_path_type`, `strip_worktree_prefix`)

Checker script: `check_schema.py`. Per-node detail dump: `_schema_fault_tally.json`.
