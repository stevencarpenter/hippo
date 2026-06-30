"""Single-sided view of the SQLite schema version the brain expects.

Kept in one module so the value appears in exactly one place inside the
Python tree — `_get_conn` guards against drift, and `/health` exposes it
to the daemon so a startup handshake can refuse to migrate the DB when
the two processes disagree.

Keep `EXPECTED_SCHEMA_VERSION` in sync with `EXPECTED_VERSION` in
`crates/hippo-core/src/storage.rs`. When the daemon runs its next
migration, bump both together.

v11→v12 adds `content_hash` and `last_enriched_content_hash` to
`claude_sessions`. Brain now both reads `content_hash`
(`claim_pending_claude_segments`) and writes `last_enriched_content_hash`
(`write_claude_knowledge_node`), so v12 is the minimum readable version.
The daemon-side handshake (`schema_handshake.rs`) already enforces strict
equality, and dropping v11 from `ACCEPTED_READ_VERSIONS` brings brain's
DB-attach guard in line: pre-v12 DBs are rejected at connect time rather
than crashing later inside the enrichment loop on `no such column:
content_hash`.

v12→v13 extends the entities.type CHECK list with 'env_var' so the enrichment pipeline can bucket environment variable names as a first-class identifier type.
v13→v14 adds `agentic_sessions`, `agentic_enrichment_queue`,
and `agentic_cursor` tables. Brain now claims pending opencode
segments from `agentic_sessions` in parallel with Claude sessions.
v14→v15 seeds the `agentic-session-codex` row in `source_health`
so the Codex poller's health UPDATE is not a silent no-op.
v15→v16 seeds the `agentic-session-cursor` row in `source_health`
so the Cursor poller's health UPDATE is not a silent no-op.
v16→v17 rebuilds `agentic_sessions` to be segment-capable: adds
`segment_index`, `git_branch`, `is_subagent`, `tool_calls_json`,
`user_prompts_json`, `content_hash`, `last_enriched_content_hash`,
widens the `harness` CHECK to include 'cursor', and swaps the unique
constraint to `(session_id, harness, segment_index)`. At this step only
the opencode poller wrote here (every row at `segment_index = 0`); the
v17→v18 step migrated the other three harnesses onto this shape.
v17→v18 repoints all agentic writers and freezes the legacy Claude
family. The daemon writers (`claude_session.rs`, `codex_session.rs`,
`cursor_session.rs`) and the brain claim/write path now write the
`agentic_*` family exclusively (segmented — `segment_index` is no
longer pinned to 0 the way opencode's rows were), and the migration
idempotently backfills historical `claude_sessions`
/ `knowledge_node_claude_sessions` / `claude_enrichment_queue` rows into
the `agentic_*` tables (harness derived from `source_file`). The legacy
`claude_*` tables are now frozen — still created by `schema.sql`, no
longer written, dropped in a later unification step.
v18→v19 adds the read-only Claude auto-memory tables: document,
revision, chunk, enrichment queue, and knowledge-node link.
"""

from __future__ import annotations

EXPECTED_SCHEMA_VERSION: int = 19

# Versions brain can read without erroring.
#
# Historically this set carried v5–v10 for "rollback compatibility" on
# the assumption that older migrations only touched columns brain didn't
# read. v12→v13 broke that assumption: it changes the entities.type
# CHECK list, and the enrichment write path now emits 'env_var'-typed
# rows on every node. Any DB at v5–v12 still has the pre-env_var CHECK
# and would fail mid-enrichment with a CHECK constraint error. Reject
# at connect time instead — the brain/daemon handshake already enforces
# strict equality (`schema_handshake.rs`), and on this single-host
# deployment the daemon always migrates the DB to EXPECTED_SCHEMA_VERSION
# before brain attaches.
ACCEPTED_READ_VERSIONS: frozenset[int] = frozenset({EXPECTED_SCHEMA_VERSION})
