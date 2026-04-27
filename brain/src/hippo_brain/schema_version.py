"""Single-sided view of the SQLite schema version the brain expects.

Kept in one module so the value appears in exactly one place inside the
Python tree — `_get_conn` guards against drift, and `/health` exposes it
to the daemon so a startup handshake can refuse to migrate the DB when
the two processes disagree.

Keep `EXPECTED_SCHEMA_VERSION` in sync with `EXPECTED_VERSION` in
`crates/hippo-core/src/storage.rs`. When the daemon runs its next
migration, bump both together.

v11→v12 adds `content_hash` and `last_enriched_content_hash` to
`claude_sessions`. As of T-A.5 the brain reads `content_hash` (in
`claim_pending_claude_segments`) and writes `last_enriched_content_hash`
on enrichment success. v11 stays in `ACCEPTED_READ_VERSIONS` for the
v11→v12 rollback window, but the brain's claim and write paths now
detect "no such column" errors and degrade gracefully when the columns
are absent (logging a warning and falling back to a column-less query
or skipping the write).
"""

from __future__ import annotations

EXPECTED_SCHEMA_VERSION: int = 12

# Versions brain can read without erroring, so the daemon can migrate
# forward and brain can still serve queries during the window where the
# new rows are settling in. Brain requires v5 as the minimum because the
# knowledge_nodes table and FTS5 index were added in that migration; v1–v4
# DBs must be migrated by the daemon before brain starts. v10 is kept for
# rollback compatibility during the v10→v11 window — the migration only adds
# columns (resolved_at, clean_ticks) to capture_alarms which brain never
# reads, so a v10-aware brain handles a v11 DB transparently. v11 is kept
# for rollback compatibility during the v11→v12 window — the brain reads
# the v12-added columns (content_hash, last_enriched_content_hash) in
# claude_sessions, but `claim_pending_claude_segments` and the enrichment
# writer detect missing columns at query time and fall back to a v11
# code path (logging a warning) so a brain attached to a rolled-back
# v11 DB continues to function with reduced features rather than
# silently halting the enrichment loop.
ACCEPTED_READ_VERSIONS: frozenset[int] = frozenset({EXPECTED_SCHEMA_VERSION, 11, 10, 9, 8, 7, 6, 5})
