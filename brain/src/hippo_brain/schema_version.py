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

v12→v13 extends the entities.type CHECK list with 'env_var' so the
enrichment pipeline can bucket environment variable names as a first-
class identifier type. Brain now writes `env_var`-typed entity rows
(via `SHELL_ENTITY_TYPE_MAP["env_vars"]`), so a pre-v13 DB would fail
with a CHECK constraint error on the first env_var insert. v12 is
dropped from `ACCEPTED_READ_VERSIONS` for the same reason v11 was.
"""

from __future__ import annotations

EXPECTED_SCHEMA_VERSION: int = 13

# Versions brain can read without erroring. Brain requires v5 as the
# minimum because the knowledge_nodes table and FTS5 index were added in
# that migration; v1–v4 DBs must be migrated by the daemon before brain
# starts. v10 is kept for rollback compatibility during the v10→v11
# window — that migration only adds columns (resolved_at, clean_ticks)
# to capture_alarms which brain never reads, so a v10-aware brain
# handles a v11 DB transparently. v11 was intentionally dropped when
# v12 introduced content_hash; v12 is dropped now for the same reason
# (v13 introduces env_var entity inserts that would fail the v12 CHECK).
ACCEPTED_READ_VERSIONS: frozenset[int] = frozenset({EXPECTED_SCHEMA_VERSION, 10, 9, 8, 7, 6, 5})
