# SQLite Schema Reference

The state of `~/.local/share/hippo/hippo.db`: the live tables, the per-version migration history that produced them, and the recovery story when daemon and brain disagree on what version is loaded. Companion to [`lifecycle.md`](lifecycle.md) (which traces what writes to which table) and [`capture/operator-runbook.md`](capture/operator-runbook.md) (recipes for the most common schema-related failure: version mismatch).

## At a glance

| Fact | Value |
|---|---|
| Current version | **13** |
| Authoritative schema | [`crates/hippo-core/src/schema.sql`](../crates/hippo-core/src/schema.sql) |
| Version constant (Rust) | `crates/hippo-core/src/storage.rs::EXPECTED_VERSION` |
| Version constant (Python) | `brain/src/hippo_brain/schema_version.py::EXPECTED_SCHEMA_VERSION` |
| Daemon refuses to bind on mismatch | `crates/hippo-daemon/src/schema_handshake.rs` |
| Migration runner | `crates/hippo-core/src/storage.rs::open_db` |
| Live version (yours) | `sqlite3 ~/.local/share/hippo/hippo.db "PRAGMA user_version;"` |

Daemon and brain handshake on this constant at startup. If they disagree the daemon refuses to bind its socket, the brain refuses to enrich, and `hippo doctor` surfaces the mismatch with a remediation hint. See [Version mismatch recovery](#version-mismatch-recovery).

## Per-version changelog

The Rust migration runner at `storage.rs::open_db` walks every version from the loaded `PRAGMA user_version` up to `EXPECTED_VERSION`, applying the migration block for each step. Migration crash-safety is mixed:

- **Crash-safe (v8 and later):** every CREATE uses `IF NOT EXISTS`, every ALTER goes through `add_column_if_missing` (which pre-checks `PRAGMA table_info`), and every seed insert uses `INSERT OR IGNORE`. A daemon that crashes after adding a column but before bumping `user_version` retries the migration cleanly on next start.
- **Not crash-safe (v1→v2, v6→v7):** these blocks issue unguarded `ALTER TABLE … ADD COLUMN` inside the same `execute_batch` as `PRAGMA user_version = N`. A crash between the `ALTER` and the `PRAGMA` leaves the column added but the version unchanged; the next start retries the same `ALTER` and SQLite errors with "duplicate column name". Recovery is a manual `PRAGMA user_version = N` after confirming the column landed. The `add_column_if_missing` pattern was introduced at v8 and used consistently from there forward.

| Version | Summary | Tables/columns | Operational impact |
|---|---|---|---|
| **v1** | Initial schema. | `events` (without `envelope_id`), `sessions`. | Baseline. Every fresh install since v1 lands here first then migrates forward. |
| **v2** | Event dedup. | `events.envelope_id` column + unique index. | Browser visits and Claude tool events get a stable dedup key, enabling replay-safe ingest. |
| **v3** | Claude session ingest. | `claude_sessions`, `knowledge_node_claude_sessions`, `claude_enrichment_queue`. | Hippo's first non-shell capture path. The `(session_id, segment_index)` UNIQUE constraint introduced here remains the watcher's upsert conflict target through every later migration (v12 added `content_hash` columns alongside it but did not change the conflict key itself). |
| **v4** | Browser source. | `browser_events`, `browser_enrichment_queue`, `knowledge_node_browser_events` plus six indexes on `browser_events` and the queue (`idx_browser_events_timestamp`, `idx_browser_events_domain`, `idx_browser_events_envelope_id` [UNIQUE], `idx_browser_events_enriched`, `idx_browser_queue_pending`, `idx_browser_events_ts_domain`). | Firefox extension begins landing visits via Native Messaging. The unique-on-`envelope_id` index is what lets `make_envelope_id` dedup same-URL repeats within `dedup_window_minutes`. |
| **v5** | GitHub Actions ingest. | `workflow_runs`, `workflow_jobs`, `workflow_annotations`, `workflow_log_excerpts`, `workflow_enrichment_queue`, `sha_watchlist`, `lessons`, `lesson_pending`, `knowledge_node_workflow_runs`, `knowledge_node_lessons`. | Workflow-poller (`gh_poll.rs`) starts ingesting CI runs; `lessons` becomes the substrate for graduating recurring CI failures into named tips. |
| **v6** | Full-text search on knowledge nodes. | `knowledge_nodes` (created here for legacy v1 DBs that predate it), `knowledge_fts` (FTS5 virtual table over `summary`/`embed_text`/`content`), AI/AD/AU triggers to keep FTS in sync. Note: `knowledge_vectors` (vec0) is NOT created here — the Rust daemon doesn't load the sqlite-vec extension; the Python brain creates it lazily on first embed. | `hippo query --raw` now does FTS5 lexical search. The `MATCH` operator fast-paths over `embed_text` without round-tripping through Python. |
| **v7** | Multi-source events. | `events.source_kind` (default `'shell'`), `events.tool_name`, `idx_events_source_kind`. | First step toward the multi-source capture stack. `source_kind='claude-tool'` rows enter the events table for tool-call envelopes derived from Claude session ingest. |
| **v8** | Capture-reliability ground truth. | `source_health` table seeded with rows for `shell`, `claude-tool`, `claude-session`, `browser` from the latest existing event timestamps; `events.probe_tag`, `claude_sessions.probe_tag`, `browser_events.probe_tag`. | Enables [`capture/architecture.md`](capture/architecture.md)'s I-1..I-10 invariants. Probe events get a `probe_tag IS NOT NULL` marker that all user-facing queries filter on (AP-6). |
| **v9** | Watchdog alarm ledger. | `capture_alarms`, `idx_capture_alarms_invariant_active` (partial index over un-acked rows), `idx_claude_sessions_start_time`. | `hippo watchdog run` writes alarm rows; `hippo alarms list / ack` operates on this table. |
| **v10** | Watcher resume state. | `claude_session_offsets` (per-file byte offset + inode for the FS watcher), `claude_session_parity` (now-unused; kept so v9→v10 migration on existing DBs converges with fresh installs). | The FS watcher (T-5/PR #86) becomes durable across daemon restarts. The legacy tmux tailer was deleted in T-8/PR #89; `claude_session_parity` is the residue. |
| **v11** | Auto-resolve alarms. | `capture_alarms.resolved_at`, `capture_alarms.clean_ticks` (CHECK ≥ 0). The "active alarm" partial index is rebuilt to include `resolved_at IS NULL`. | Watchdog automatically resolves an alarm after 2 consecutive clean evaluations; resolved rows stop suppressing new alarms via rate-limiting. Cleared with `hippo alarms prune`. |
| **v12** | Claude segment dedup-by-content. | `claude_sessions.content_hash`, `claude_sessions.last_enriched_content_hash`. | Phase 1 fix for the AP-12 INSERT-OR-IGNORE bug. The watcher upserts segments with a content-hash; the brain compares against `last_enriched_content_hash` to gate re-enrichment. Pre-v12 rows have `content_hash IS NULL` until the next watcher pass re-hashes them. |
| **v13** | `env_var` entity type. | `entities.type` CHECK list extended with `'env_var'`. SQLite cannot ALTER a CHECK constraint, so the migration follows the 12-step table-recreate recipe (PRAGMA `foreign_keys=OFF` → BEGIN → DROP TABLE IF EXISTS entities_new → CREATE TABLE entities_new with the expanded CHECK → INSERT … SELECT → DROP TABLE entities → ALTER TABLE entities_new RENAME TO entities → recreate indexes → `foreign_key_check` → `PRAGMA user_version = 13` → COMMIT → `foreign_keys=ON`). The `user_version` bump is inside the same `execute_batch` as the COMMIT, so a crash after rename can't leave the DB at v12 with the new CHECK. | RAG synthesis surfaces env-var identifiers (`HIPPO_FORCE`, `HIPPO_PROJECT_ROOTS`, etc.) on the dedicated `Entities:` line that lives outside the truncatable `Detail:` block. Closed [#108](https://github.com/stevencarpenter/hippo/issues/108). |

## Reading the live schema

```bash
# Full schema dump (pipe through less for large outputs)
sqlite3 ~/.local/share/hippo/hippo.db .schema

# Single table
sqlite3 ~/.local/share/hippo/hippo.db ".schema events"

# Indexes only
sqlite3 ~/.local/share/hippo/hippo.db ".indexes events"

# Confirm version
sqlite3 ~/.local/share/hippo/hippo.db "PRAGMA user_version;"
```

### Top-level table map

| Table | What it holds | Primary writer |
|---|---|---|
| `events` | Shell commands and Claude tool-use events. `source_kind` distinguishes; `probe_tag` marks synthetic. | `storage.rs::insert_event_at` |
| `sessions` | One row per zsh session (start time, hostname, shell, user). | Daemon at session start |
| `claude_sessions` | One row per `(session_id, segment_index)`. Holds segment-derived summary, tool calls JSON, message count, content hashes. | `claude_session.rs::insert_segments` |
| `claude_session_offsets` | Per-file FS-watcher resume state (byte_offset, inode, device). | `watch_claude_sessions.rs::process_file` |
| `browser_events` | Firefox-extension visits with Readability-extracted main text, dwell, scroll depth. | `storage.rs::insert_browser_event` |
| `workflow_runs` / `_jobs` / `_annotations` / `_log_excerpts` | GitHub Actions ingest. | `gh_poll.rs::run_once` |
| `sha_watchlist` | Per-(repo, sha) follow flag for in-flight CI runs. Drives the gh-poller's "wait for this SHA's runs to settle" loop. | `gh_poll.rs` |
| `lessons` / `lesson_pending` | Graduated recurring CI tips. | Brain enrichment via `_enrich_workflow_runs` |
| `env_snapshots` | Hashed environment-variable snapshots referenced by `events.env_snapshot_id`. Lets multiple events share one snapshot rather than embedding env-var sets in every row. | Daemon at session start |
| `knowledge_nodes` | The synthesized output of enrichment. The `content` column is a JSON blob (with `summary` / `intent` / `entities` / `tool_calls` / etc. as inner fields); `embed_text` and `node_type`/`outcome`/`tags` are real columns. | `enrichment.py::write_knowledge_node`, `claude_sessions.py::write_claude_knowledge_node` |
| `knowledge_node_events` / `_claude_sessions` / `_browser_events` / `_workflow_runs` / `_lessons` | Link tables tying knowledge nodes back to their source events. | Same writers as `knowledge_nodes` |
| `entities` | Extracted identifiers (project, file, tool, service, repo, host, person, concept, domain, env_var). UNIQUE `(type, canonical)`. | `enrichment.py::upsert_entities` |
| `event_entities` / `knowledge_node_entities` | Many-to-many links from rows to extracted entities. | Same |
| `relationships` | Directed `(source_entity, predicate, target_entity)` graph edges. | Brain enrichment |
| `enrichment_queue` / `claude_enrichment_queue` / `browser_enrichment_queue` / `workflow_enrichment_queue` | Per-source queue tables. Each row is a claim ticket with `status`, `priority`, `retry_count`, `locked_at`, `locked_by`. | Daemon on insert; brain on claim/complete; watchdog reaper on timeout |
| `source_health` | Per-source last_event_ts, consecutive_failures, probe_ok, probe_lag_ms. The watchdog's source of truth. | Daemon (capture path), watchdog (probe results) |
| `capture_alarms` | Watchdog invariant violations. Append-only ledger. | `hippo watchdog run` |
| `claude_session_parity` | Legacy parity-check ledger from the tmux-tailer / FS-watcher transition (T-5..T-8). Retained so v9→v10 migrations on existing databases converge with the same shape as fresh installs; not written by any current code path. | (no live writer) |
| `knowledge_fts` | FTS5 virtual table over `knowledge_nodes.summary` / `embed_text` / `content`. | Triggers (auto-synced with `knowledge_nodes`) |
| `knowledge_vectors` | sqlite-vec virtual table holding 768-dim embedding vectors. | `embeddings.py::embed_knowledge_node` (Python brain — Rust daemon doesn't load vec0) |
| `embed_model_meta` | Single-row tracking table for the model that produced the corpus's vectors. | Same |

### Foreign-key relationships (high level)

```
sessions ──< events
             ├── source_kind in {'shell', 'claude-tool', ...}
             └── probe_tag NULL except for synthetic probes

claude_sessions ──< knowledge_node_claude_sessions >── knowledge_nodes
events           ──< knowledge_node_events           >── knowledge_nodes
browser_events   ──< knowledge_node_browser_events   >── knowledge_nodes
workflow_runs    ──< knowledge_node_workflow_runs    >── knowledge_nodes
lessons          ──< knowledge_node_lessons          >── knowledge_nodes

knowledge_nodes ──< knowledge_node_entities >── entities
                ──< knowledge_fts (FTS5 mirror, trigger-synced)
                ──< knowledge_vectors (sqlite-vec mirror, brain-managed)

source_health      (no FKs; one row per logical source)
capture_alarms     (no FKs; references invariant_id by string)
enrichment_queue   ──> events
claude_enrichment_queue ──> claude_sessions
browser_enrichment_queue ──> browser_events
workflow_enrichment_queue ──> workflow_runs
```

`PRAGMA foreign_keys` is ON for every connection (`storage.rs::open_db`, `vector_store.py::open_conn`).

## Migration guarantees

- **Single-shot per version.** Each migration block in `storage.rs::open_db` runs at most once per database lifetime: the version range guard (`if (1..=N).contains(&version)`) becomes false after `PRAGMA user_version = N+1` lands.
- **Idempotent on partial-success crash.** Every CREATE uses `IF NOT EXISTS`; every ALTER goes through `add_column_if_missing` which pre-checks `PRAGMA table_info`; every seed insert uses `INSERT OR IGNORE`. A daemon that crashes after adding a column but before bumping `user_version` will retry the migration cleanly on next start.
- **Atomic version bumps for the table-recreate path.** Only **v13** is a true table-recreate, and its migration bundles `PRAGMA user_version = 13` into the same `execute_batch` as the `DROP TABLE` / `RENAME` / `COMMIT` sequence — a crash after the rename can't leave the DB at v12 with the v13 CHECK constraint live. Earlier versions (including v8, which is a CREATE-IF-NOT-EXISTS plus `add_column_if_missing` loop) issue `PRAGMA user_version = N` in a separate `execute_batch` after the migration body completes, so re-run safety on those steps comes from the idempotency of each individual statement, not from atomicity with the version bump.
- **PRAGMA `foreign_keys` discipline.** Migrations that require dropping a table (v13) explicitly turn FKs off, run inside a transaction, and turn them back on at the end. Other migrations rely on the default `foreign_keys=ON` set by `open_db`.

## Version mismatch recovery

Symptom: daemon refuses to bind its socket; `hippo doctor` reports a schema-version mismatch.

```bash
# Run the unified handshake check (compares all three at once).
hippo doctor --explain | grep -A 4 "schema"

# Or check each side individually:

# 1. What does the live DB say?
sqlite3 ~/.local/share/hippo/hippo.db "PRAGMA user_version;"

# 2. What version does the daemon binary expect? (compiled-in constant)
grep -E "^pub const EXPECTED_VERSION" \
  ~/projects/hippo/crates/hippo-core/src/storage.rs

# 3. What version does the brain expect?
uv run --project brain python -c \
  "from hippo_brain.schema_version import EXPECTED_SCHEMA_VERSION; print(EXPECTED_SCHEMA_VERSION)"
```

All three numbers must match. Common causes and fixes:

| Cause | Fix |
|---|---|
| Daemon updated, brain not yet — `mise run install` ran but the brain venv didn't refresh. | `mise run install --clean` rebuilds and re-syncs the brain. |
| Brain updated, daemon not yet — happens when only `brain/` changed in a release. | Same — `mise run install --clean`. |
| DB at higher version than binaries — you upgraded then downgraded. | Migrations are forward-only. Either upgrade the binaries again, or restore a backup taken before the higher version was applied. |
| DB at lower version than binaries — fresh checkout against an old DB that hasn't been touched. | Just start the daemon (`mise run start`); migrations will run forward. |
| DB at v0 — a fresh DB. | Same — daemon will create from scratch via `schema.sql`. |

**Don't manually `PRAGMA user_version = N`.** The version bump is a marker for "the migration body has run." Setting it manually skips the migration body, leaving the DB structurally inconsistent with what the binary expects.

**Backup before manual surgery.** `cp ~/.local/share/hippo/hippo.db{,.bak.$(date +%Y%m%d-%H%M)}`.

## Adding a new migration

When you bump `EXPECTED_VERSION` from N to N+1:

1. Write the migration block in `storage.rs::open_db` guarded by `if (1..=N).contains(&version)`.
2. Update `crates/hippo-core/src/schema.sql` so fresh installs match.
3. Bump `EXPECTED_VERSION` in `storage.rs` AND `EXPECTED_SCHEMA_VERSION` in `brain/src/hippo_brain/schema_version.py` in the same PR (they must agree).
4. Bump `[workspace.package].version` in `Cargo.toml`, `[project].version` in `brain/pyproject.toml`, and `hippo-gui/VERSION` for the lockstep release (see [`docs/RELEASE.md`](RELEASE.md)).
5. Add a row to the changelog table above.
6. If your migration touches `entities.type` or other CHECK constraints, follow the 12-step table-recreate recipe from v13. Test the partial-success-crash case: kill the migration mid-way and confirm re-run lands cleanly.

## See also

- [`lifecycle.md`](lifecycle.md) — what writes to which table.
- [`capture/architecture.md`](capture/architecture.md) — `source_health`, `capture_alarms`, and the watchdog's view of the schema.
- [`capture/operator-runbook.md`](capture/operator-runbook.md) — recipes for the most common schema-related failure (version mismatch).
- [`RELEASE.md`](RELEASE.md) — lockstep version contract and release workflow.
- [`crates/hippo-core/src/schema.sql`](../crates/hippo-core/src/schema.sql) — authoritative SQL for fresh installs.
