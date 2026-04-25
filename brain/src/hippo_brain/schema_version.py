"""Single-sided view of the SQLite schema version the brain expects.

Kept in one module so the value appears in exactly one place inside the
Python tree — `_get_conn` guards against drift, and `/health` exposes it
to the daemon so a startup handshake can refuse to migrate the DB when
the two processes disagree.

Keep `EXPECTED_SCHEMA_VERSION` in sync with `EXPECTED_VERSION` in
`crates/hippo-core/src/storage.rs`. When the daemon runs its next
migration, bump both together.
"""

from __future__ import annotations

EXPECTED_SCHEMA_VERSION: int = 9

# Versions brain can read without erroring, so the daemon can migrate
# forward and brain can still serve queries during the window where the
# new rows are settling in. Brain requires v5 as the minimum because the
# knowledge_nodes table and FTS5 index were added in that migration; v1–v4
# DBs must be migrated by the daemon before brain starts. Keep 8 for
# rollback compatibility during the v8→v9 window (capture_alarms table is
# additive and does not affect brain read paths).
ACCEPTED_READ_VERSIONS: frozenset[int] = frozenset({EXPECTED_SCHEMA_VERSION, 8, 7, 6, 5})
