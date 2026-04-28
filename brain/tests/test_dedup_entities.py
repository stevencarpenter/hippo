"""Tests for brain/scripts/dedup-entities.py.

The script's filename uses a hyphen, so it can't be imported via the standard
`import` statement — we load it through `importlib.util` to exercise `run()`.
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "dedup-entities.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("dedup_entities", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def dedup(monkeypatch):
    """Load the dedup script with a deterministic project root pinned."""
    monkeypatch.setenv("HIPPO_PROJECT_ROOTS", "/Users/test/projects/hippo")
    from hippo_brain.entity_resolver import _cached_fallback_roots

    _cached_fallback_roots.cache_clear()
    return _load_script_module()


def _seed_entity(conn, etype, name, canonical, *, created_at: int | None = None):
    """Insert an entity row.

    When ordering between rows matters (e.g. merge-survivor selection picks
    the oldest `created_at`), pass an explicit `created_at` value. Relying on
    `time.sleep` between calls is unreliable: we round to integer ms and two
    consecutive calls within the same ms tick yield equal timestamps,
    making survivor selection nondeterministic and the test flaky.
    """
    if created_at is None:
        created_at = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO entities (type, name, canonical, first_seen, last_seen, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (etype, name, canonical, created_at, created_at, created_at),
    )
    conn.commit()


def test_lone_polluted_row_gets_rebranded(tmp_db, dedup):
    """A path entity with a worktree-polluted name + canonical (no clean
    duplicate) should be UPDATEd in place — not left alone."""
    conn, _ = tmp_db
    _seed_entity(
        conn,
        "file",
        "/Users/test/projects/hippo/.claude/worktrees/agent-old/brain/foo.py",
        "/users/test/projects/hippo/.claude/worktrees/agent-old/brain/foo.py",
    )

    stats = dedup.run(conn, dry_run=False)

    assert stats["rebranded"] == 1
    assert stats["deleted"] == 0
    row = conn.execute("SELECT name, canonical FROM entities WHERE type='file'").fetchone()
    assert ".claude/worktrees/" not in row[0]
    assert ".claude/worktrees/" not in row[1]
    assert row[1] == "brain/foo.py"


def test_lone_already_clean_row_not_touched(tmp_db, dedup):
    """If name and canonical are already in the canonical form the rules would
    produce, no UPDATE should fire — avoids needless churn on every run."""
    conn, _ = tmp_db
    _seed_entity(conn, "file", "brain/foo.py", "brain/foo.py")

    stats = dedup.run(conn, dry_run=False)

    assert stats["rebranded"] == 0
    assert stats["deleted"] == 0


def test_non_path_lone_row_with_worktree_substring_preserved(tmp_db, dedup):
    """`concept` entities (errors) may legitimately contain `.claude/worktrees/`
    inside diagnostic text. The script must not strip it — that would be lossy.
    """
    conn, _ = tmp_db
    error_msg = (
        "filenotfounderror: cannot stat /users/test/projects/hippo/.claude/worktrees/agent-x/foo.py"
    )
    _seed_entity(conn, "concept", error_msg, error_msg)

    stats = dedup.run(conn, dry_run=False)

    assert stats["rebranded"] == 0, "non-path lone row should not be rebranded"
    row = conn.execute("SELECT name FROM entities WHERE type='concept'").fetchone()
    # The error string survives intact.
    assert row[0] == error_msg


def test_duplicate_group_merge_also_strips_survivor_name(tmp_db, dedup):
    """When a merge happens, the kept row's `name` and `canonical` should both
    be re-canonicalized — the old behavior only updated canonical, leaving
    name with potentially-polluted display value."""
    conn, _ = tmp_db
    # Older row: polluted name + polluted canonical (pre-PR-100 state).
    # Use explicit created_at values 1ms apart so the survivor (oldest) is
    # deterministically selected — sleep-based ordering is unreliable.
    _seed_entity(
        conn,
        "file",
        "/Users/test/projects/hippo/.claude/worktrees/agent-old/brain/bar.py",
        "/users/test/projects/hippo/.claude/worktrees/agent-old/brain/bar.py",
        created_at=1_700_000_000_000,
    )
    # Newer row with the same logical file in canonical form.
    _seed_entity(conn, "file", "brain/bar.py", "brain/bar.py", created_at=1_700_000_001_000)

    stats = dedup.run(conn, dry_run=False)

    assert stats["deleted"] == 1
    rows = conn.execute("SELECT name, canonical FROM entities WHERE type='file'").fetchall()
    assert len(rows) == 1
    name, canonical = rows[0]
    assert ".claude/worktrees/" not in name, f"survivor name still polluted: {name}"
    assert canonical == "brain/bar.py"


def test_rebrand_handles_canonical_swap_without_unique_collision(tmp_db, dedup):
    """Two lone rows whose new_canonicals cross-collide with each other's
    current canonicals must not trip UNIQUE (type, canonical) during update.

    Setup mirrors a real failure mode in the live DB: row A's new canonical
    equals row B's current canonical (and B has its own different new
    canonical). A naïve sequential UPDATE collides; a two-phase placeholder
    pass does not.
    """
    conn, _ = tmp_db
    # Row A: current canonical reflects an old rule that no longer applies.
    # Its new canonical (after fresh canonicalize) becomes "alpha".
    _seed_entity(conn, "file", "/Users/test/projects/hippo/alpha", "stale_alpha_canonical")
    # Row B: current canonical is "alpha" — exactly what Row A is about to
    # become. Row B's own new canonical, however, is "beta". So both rows
    # are lone rebrand candidates with cross-colliding values.
    _seed_entity(conn, "file", "/Users/test/projects/hippo/beta", "alpha")

    stats = dedup.run(conn, dry_run=False)

    assert stats["rebranded"] == 2
    rows = conn.execute(
        "SELECT canonical FROM entities WHERE type='file' ORDER BY canonical"
    ).fetchall()
    canonicals = sorted(r[0] for r in rows)
    assert canonicals == ["alpha", "beta"]


def test_placeholder_does_not_collide_with_existing_entity_canonical(tmp_db, dedup):
    """The Phase-1 placeholder canonical must not collide with any pre-existing
    entity's canonical. Earlier versions used a deterministic
    `__dedup_pending_<id>__` token; if any existing entity of the same type
    already had that canonical, the parking UPDATE would trip UNIQUE.

    Defended against by embedding a per-run UUID4 into the placeholder so it
    cannot be predicted ahead of time.

    Setup: row 1 is a rebrand candidate ('file', polluted canonical). Row 2
    is an already-clean 'file' whose canonical happens to be exactly what the
    legacy placeholder for row 1 would have been. Under the old code the
    Phase-1 update for row 1 would collide with row 2; under the new code it
    cannot because row 1's placeholder embeds a UUID.
    """
    conn, _ = tmp_db
    # Row 1: needs rebranding. SQLite will assign id=1.
    _seed_entity(
        conn,
        "file",
        "/Users/test/projects/hippo/.claude/worktrees/agent-x/foo.py",
        "/users/test/projects/hippo/.claude/worktrees/agent-x/foo.py",
    )
    # Row 2: an already-clean 'file' whose canonical matches what the legacy
    # placeholder for id=1 would have been (`__dedup_pending_1__`). Because
    # canonicalize('file', '__dedup_pending_1__') == '__dedup_pending_1__'
    # (no path logic kicks in), this row is NOT a rebrand candidate — its
    # canonical stays put through the run, providing the adversarial
    # collision target.
    _seed_entity(conn, "file", "__dedup_pending_1__", "__dedup_pending_1__")

    # Should complete without sqlite3.IntegrityError.
    stats = dedup.run(conn, dry_run=False)

    assert stats["rebranded"] == 1
    # Row 2 still holds the canonical that would have collided.
    canonicals = {
        r[0] for r in conn.execute("SELECT canonical FROM entities WHERE type='file'").fetchall()
    }
    assert "__dedup_pending_1__" in canonicals
    # Row 1 ended up with its real new canonical, not a placeholder.
    assert "foo.py" in canonicals or any("foo.py" in c for c in canonicals)





def test_dry_run_makes_no_changes(tmp_db, dedup):
    conn, _ = tmp_db
    _seed_entity(
        conn,
        "file",
        "/Users/test/projects/hippo/.claude/worktrees/agent-x/foo.py",
        "/users/test/projects/hippo/.claude/worktrees/agent-x/foo.py",
    )

    stats = dedup.run(conn, dry_run=True)

    # In dry-run mode no mutations are applied, so the mutation counters
    # (`deleted`, `rebranded`) stay at 0 by design — they reflect work
    # actually performed, not work that would be performed.
    assert stats["rebranded"] == 0
    row = conn.execute("SELECT name, canonical FROM entities").fetchone()
    assert ".claude/worktrees/" in row[0]
    assert ".claude/worktrees/" in row[1]
