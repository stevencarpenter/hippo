# Legacy `claude_*` Table Cutover Runbook (SNUG-115)

Operator-facing checklist for the destructive v22 migration that drops frozen legacy agentic tables after the v17→v18 backfill to `agentic_sessions`. **Do not run the migration until this runbook's grep audit is clean and a HITL backup exists.**

Parent issue: [SNUG-115](https://linear.app/snugmarina/issue/SNUG-115).

## Current state (schema v22 after Phase A)

| Table | Status | Action in Phase B (v23) |
|-------|--------|---------------|
| `agentic_sessions` | **Live** — all harness writers | Keep |
| `agentic_enrichment_queue` | **Live** | Keep |
| `knowledge_node_agentic_sessions` | **Live** | Keep |
| `claude_sessions` | **Frozen** — no writers since unification | Drop after backfill verified |
| `claude_enrichment_queue` | **Frozen** | Drop |
| `knowledge_node_claude_sessions` | **Frozen** | Drop |
| `claude_session_parity` | **Frozen** — tmux tailer residue | Drop |
| `claude_session_offsets` | **Live on v21** — FS watcher resume state | **Renamed to `agentic_session_offsets` in v22 (Phase A)** |

The v17→v18 migration already copied historical rows into `agentic_sessions` / `agentic_enrichment_queue` / `knowledge_node_agentic_sessions` with `harness` derived from `source_file`. v22 is schema cleanup + reference purge, not another data migration.

## HITL prerequisites (mandatory)

1. **Stop writers briefly** (optional but safer): `mise run stop`
2. **Backup the database**:
   ```bash
   cp ~/.local/share/hippo/hippo.db ~/.local/share/hippo/hippo.db.pre-v22.$(date +%Y%m%d%H%M)
   sqlite3 ~/.local/share/hippo/hippo.db ".backup ~/.local/share/hippo/hippo.db.pre-v22-backup"
   ```
3. **Verify backfill completeness** (row counts should be stable; agentic ≥ legacy):
   ```bash
   sqlite3 ~/.local/share/hippo/hippo.db "
     SELECT 'claude_sessions' AS t, COUNT(*) FROM claude_sessions
     UNION ALL
     SELECT 'agentic_sessions', COUNT(*) FROM agentic_sessions
       WHERE harness IN ('claude-code','codex','cursor','opencode');
   "
   ```
4. **Record schema version**: `sqlite3 ~/.local/share/hippo/hippo.db 'PRAGMA user_version;'`

## Phase A — rename `claude_session_offsets` (**shipped in v22**)

The Claude FS watcher (`watch_claude_sessions.rs`) and backfill CLI now read/write `agentic_session_offsets`. Legacy `claude_session_offsets` remains on DBs upgraded from v21 (frozen, not written).

Phase A checklist (complete):

1. ~~Add `agentic_session_offsets` table~~
2. ~~v21→v22 migration: copy rows from `claude_session_offsets`~~
3. ~~Switch watcher + backfill to new table name~~
4. Legacy `claude_session_offsets` kept frozen until Phase B drop

## Phase B — drop legacy `claude_*` tables (destructive, not started)

Only after Phase A is merged and deployed:

1. Bump `EXPECTED_VERSION` / `EXPECTED_SCHEMA_VERSION` to **23** (Rust + Python together).
2. Migration SQL (sketch):
   ```sql
   DROP TABLE IF EXISTS knowledge_node_claude_sessions;
   DROP TABLE IF EXISTS claude_enrichment_queue;
   DROP TABLE IF EXISTS claude_sessions;
   DROP TABLE IF EXISTS claude_session_parity;
   DROP TABLE IF EXISTS claude_session_offsets;
   ```
3. Remove `CREATE TABLE` blocks for dropped tables from `crates/hippo-core/src/schema.sql`.
4. Purge references (see grep audit below).
5. Full test pass: `mise run test`

## Grep audit checklist

Run from repo root before opening the v22 PR. Goal: **zero runtime references** to dropped tables (docs/archive and historical migration fixtures may remain).

```bash
# Primary symbol — expect hits only in schema.sql, migration tests, bench frozen DDL, and this runbook until purge PR lands
rg -n 'claude_sessions' \
  --glob '!docs/archive/**' \
  --glob '!.understand-anything/**' \
  --glob '!**/schema_v*.sql' \
  crates/ brain/ scripts/ docs/capture/ config/

# Legacy queue + link tables
rg -n 'claude_enrichment_queue|knowledge_node_claude_sessions|claude_session_parity' \
  --glob '!docs/archive/**' \
  --glob '!.understand-anything/**' \
  crates/ brain/ scripts/

# Offsets table — runtime code should use agentic_session_offsets (v22+)
rg -n 'claude_session_offsets' crates/ brain/ docs/capture/

# Frozen bench corpus DDL (expected until bench fixture bump)
rg -n 'claude_sessions' brain/src/hippo_brain/bench/ brain/tests/test_bench_*.py
```

### Reference categories to purge in v22 PR

| Area | Typical files | Notes |
|------|---------------|-------|
| Rust writers/readers | `claude_session.rs`, `commands.rs`, `storage.rs` | Most paths already use `agentic_sessions` |
| Brain enrichment | `claude_sessions.py` | Module may shrink to harness helpers or be removed |
| Bench frozen DDL | `bench/corpus.py`, `test_bench_corpus.py` | Update shadow schema + regenerate corpus fixture |
| Doctor / source audit | `commands.rs`, `tests/source_audit/` | Point checks at `agentic_sessions` |
| Docs (non-archive) | `docs/schema.md`, `docs/lifecycle.md`, `CLAUDE.md` | Update table inventory |
| Semgrep / config | `.semgrep.yml`, `config.default.toml` | Remove stale table names |

As of 2026-07-05, `rg 'claude_sessions'` hits **~90 files** in `*.{rs,py,sql,md,toml}` (excluding `.understand-anything/`); the v22 PR must drive runtime references to zero.

## Post-migrate verification

```bash
mise run install          # or restart services
hippo doctor
cargo test -p hippo-core -p hippo-daemon
uv run --project brain pytest brain/tests -q
sqlite3 ~/.local/share/hippo/hippo.db "
  SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'claude_%';
"
```

Expected: no `claude_sessions` / `claude_enrichment_queue` / `knowledge_node_claude_sessions` / `claude_session_parity`. Offsets table name depends on Phase A completion.

## Rollback

1. `mise run stop`
2. Restore backup: `cp ~/.local/share/hippo/hippo.db.pre-v22.* ~/.local/share/hippo/hippo.db`
3. Deploy previous hippo binary/brain build (schema v21)
4. `mise run start && hippo doctor`

## Related docs

- [`docs/schema.md`](../schema.md) — migration history
- [`docs/capture/sources.md`](sources.md) — agentic source inventory
- [`docs/capture/operator-runbook.md`](operator-runbook.md) — day-to-day capture ops
