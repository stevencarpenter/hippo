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
"""

from __future__ import annotations

EXPECTED_SCHEMA_VERSION: int = 14

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
