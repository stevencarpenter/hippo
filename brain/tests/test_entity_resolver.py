"""Unit and integration tests for entity_resolver.canonicalize."""

import sqlite3
import tempfile
from pathlib import Path

from hippo_brain.entity_resolver import canonicalize

SCHEMA_PATH = Path(__file__).parent.parent.parent / "crates" / "hippo-core" / "src" / "schema.sql"


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class TestCanonicalizeNonPath:
    def test_lowercase(self):
        assert canonicalize("tool", "Cargo") == "cargo"

    def test_strip_whitespace(self):
        assert canonicalize("concept", "  SQL  ") == "sql"

    def test_collapse_internal_whitespace(self):
        assert canonicalize("project", "my  project") == "my project"

    def test_trailing_slash_stripped(self):
        assert canonicalize("service", "postgres/") == "postgres"

    def test_non_path_type_unaffected_by_path_logic(self):
        # Even if the value looks like a path, non-path types are not stripped.
        roots = ["/users/carpenter/projects/hippo"]
        result = canonicalize("project", "/users/carpenter/projects/hippo/foo", project_roots=roots)
        assert result == "/users/carpenter/projects/hippo/foo"

    def test_concept_unchanged(self):
        assert canonicalize("concept", "ModuleNotFoundError") == "modulenotfounderror"


class TestCanonicalizePathType:
    def test_absolute_path_stripped_to_repo_relative(self):
        roots = ["/users/carpenter/projects/hippo-postgres"]
        result = canonicalize(
            "file", "/users/carpenter/projects/hippo-postgres/src/foo.rs", project_roots=roots
        )
        assert result == "src/foo.rs"

    def test_hippo_variant_stripped(self):
        roots = ["/users/carpenter/projects/hippo"]
        result = canonicalize(
            "file", "/users/carpenter/projects/hippo/src/storage.rs", project_roots=roots
        )
        assert result == "src/storage.rs"

    def test_both_worktree_variants_resolve_same(self):
        roots = [
            "/users/carpenter/projects/hippo",
            "/users/carpenter/projects/hippo-postgres",
        ]
        a = canonicalize(
            "file", "/users/carpenter/projects/hippo/src/storage.rs", project_roots=roots
        )
        b = canonicalize(
            "file", "/users/carpenter/projects/hippo-postgres/src/storage.rs", project_roots=roots
        )
        assert a == b == "src/storage.rs"

    def test_trailing_slash_on_directory(self):
        roots = ["/users/carpenter/projects/hippo"]
        result = canonicalize(
            "directory", "/users/carpenter/projects/hippo/crates/", project_roots=roots
        )
        assert result == "crates"

    def test_tilde_expansion(self, monkeypatch):
        monkeypatch.setenv("HOME", "/users/carpenter")
        roots = ["~/projects/hippo"]
        result = canonicalize("file", "~/projects/hippo/src/main.rs", project_roots=roots)
        assert result == "src/main.rs"

    def test_path_without_matching_root_unchanged(self):
        roots = ["/users/carpenter/projects/hippo"]
        result = canonicalize("file", "/other/path/file.rs", project_roots=roots)
        assert result == "/other/path/file.rs"

    def test_exact_root_match_returns_empty(self):
        roots = ["/users/carpenter/projects/hippo"]
        result = canonicalize("file", "/users/carpenter/projects/hippo", project_roots=roots)
        assert result == ""

    def test_empty_project_roots_leaves_path_as_is(self):
        result = canonicalize("file", "/some/absolute/path.rs", project_roots=[])
        assert result == "/some/absolute/path.rs"

    def test_case_folded_before_prefix_match(self):
        roots = ["/Users/Carpenter/Projects/Hippo"]
        result = canonicalize(
            "file", "/Users/Carpenter/Projects/Hippo/src/lib.rs", project_roots=roots
        )
        assert result == "src/lib.rs"


# ---------------------------------------------------------------------------
# Integration test: dedup script against real tmpdir SQLite
# ---------------------------------------------------------------------------


def _make_db() -> tuple[sqlite3.Connection, Path]:
    schema = SCHEMA_PATH.read_text()
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(schema)
    conn.commit()
    return conn, db_path


def test_dedup_merges_worktree_fragments(monkeypatch):
    """Two storage.rs entities from different worktree paths should merge to one."""
    monkeypatch.setenv(
        "HIPPO_PROJECT_ROOTS",
        "/users/carpenter/projects/hippo:/users/carpenter/projects/hippo-postgres",
    )

    conn, db_path = _make_db()

    now_ms = 1_700_000_000_000
    # Insert two entities with old-style canonicals (plain lower/strip, no prefix stripping).
    conn.execute(
        "INSERT INTO entities (type, name, canonical, first_seen, last_seen, created_at) VALUES (?,?,?,?,?,?)",
        (
            "file",
            "/users/carpenter/projects/hippo/src/storage.rs",
            "/users/carpenter/projects/hippo/src/storage.rs",
            now_ms,
            now_ms,
            now_ms,
        ),
    )
    conn.execute(
        "INSERT INTO entities (type, name, canonical, first_seen, last_seen, created_at) VALUES (?,?,?,?,?,?)",
        (
            "file",
            "/users/carpenter/projects/hippo-postgres/src/storage.rs",
            "/users/carpenter/projects/hippo-postgres/src/storage.rs",
            now_ms + 1,
            now_ms + 1,
            now_ms + 1,
        ),
    )
    conn.commit()

    import importlib.util
    import sys

    scripts_root = Path(__file__).parent.parent / "scripts"
    spec = importlib.util.spec_from_file_location(
        "dedup_entities", scripts_root / "dedup-entities.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dedup_entities"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    stats = mod.run(conn, dry_run=False)

    rows = conn.execute("SELECT id, type, canonical FROM entities").fetchall()
    assert len(rows) == 1, f"Expected 1 entity, got {len(rows)}: {[dict(r) for r in rows]}"
    assert rows[0]["canonical"] == "src/storage.rs"
    assert stats["deleted"] == 1

    conn.close()
    db_path.unlink(missing_ok=True)
    db_path.with_suffix(".db-wal").unlink(missing_ok=True)
    db_path.with_suffix(".db-shm").unlink(missing_ok=True)
