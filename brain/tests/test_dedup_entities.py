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


def _seed_entity(conn, etype, name, canonical):
    now_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO entities (type, name, canonical, first_seen, last_seen, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (etype, name, canonical, now_ms, now_ms, now_ms),
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
    _seed_entity(
        conn,
        "file",
        "/Users/test/projects/hippo/.claude/worktrees/agent-old/brain/bar.py",
        "/users/test/projects/hippo/.claude/worktrees/agent-old/brain/bar.py",
    )
    # Newer row with the same logical file in canonical form.
    time.sleep(0.001)  # ensure newer created_at
    _seed_entity(conn, "file", "brain/bar.py", "brain/bar.py")

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


def test_dry_run_makes_no_changes(tmp_db, dedup):
    conn, _ = tmp_db
    _seed_entity(
        conn,
        "file",
        "/Users/test/projects/hippo/.claude/worktrees/agent-x/foo.py",
        "/users/test/projects/hippo/.claude/worktrees/agent-x/foo.py",
    )

    stats = dedup.run(conn, dry_run=True)

    # Stats reflect the work that *would* be done, but no UPDATEs ran.
    assert stats["rebranded"] == 0
    row = conn.execute("SELECT name, canonical FROM entities").fetchone()
    assert ".claude/worktrees/" in row[0]
    assert ".claude/worktrees/" in row[1]
