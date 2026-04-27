"""Unit and integration tests for entity_resolver.canonicalize."""

import logging
import sqlite3
import tempfile
from pathlib import Path

from hippo_brain.entity_resolver import (
    _cached_fallback_roots,
    _resolve_project_roots,
    canonicalize,
    strip_worktree_prefix,
)

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

    def test_exact_root_match_returns_basename(self):
        roots = ["/users/carpenter/projects/hippo"]
        result = canonicalize("file", "/users/carpenter/projects/hippo", project_roots=roots)
        assert result == "hippo"

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


# ---------------------------------------------------------------------------
# Project root fallback chain tests (MED-2)
# ---------------------------------------------------------------------------


class TestProjectRootFallbackChain:
    """Verify the env var > config.toml > auto-detect precedence chain."""

    def setup_method(self):
        _cached_fallback_roots.cache_clear()

    def teardown_method(self):
        _cached_fallback_roots.cache_clear()

    def test_override_bypasses_env_and_config(self, monkeypatch):
        monkeypatch.setenv("HIPPO_PROJECT_ROOTS", "/env/root")
        roots = _resolve_project_roots(["/override/root"])
        assert roots == ["/override/root"]

    def test_env_var_takes_precedence_over_config_and_auto(self, monkeypatch):
        monkeypatch.setenv("HIPPO_PROJECT_ROOTS", "/env/root")
        monkeypatch.setattr(
            "hippo_brain.entity_resolver._load_config_roots", lambda: ["/config/root"]
        )
        roots = _resolve_project_roots(None)
        assert roots == ["/env/root"]

    def test_config_takes_precedence_over_auto_detect(self, monkeypatch):
        monkeypatch.delenv("HIPPO_PROJECT_ROOTS", raising=False)
        monkeypatch.setattr(
            "hippo_brain.entity_resolver._load_config_roots", lambda: ["/config/root"]
        )
        monkeypatch.setattr(
            "hippo_brain.entity_resolver._auto_detect_roots", lambda: ["/auto/root"]
        )
        roots = _resolve_project_roots(None)
        assert roots == ["/config/root"]

    def test_auto_detect_fallback_finds_git_dirs(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HIPPO_PROJECT_ROOTS", raising=False)
        monkeypatch.setattr("hippo_brain.entity_resolver._load_config_roots", lambda: [])
        proj_a = tmp_path / "proj-a"
        proj_b = tmp_path / "proj-b"
        (proj_a / ".git").mkdir(parents=True)
        (proj_b / ".git").mkdir(parents=True)
        monkeypatch.setattr(
            "hippo_brain.entity_resolver._auto_detect_roots",
            lambda: sorted([str(proj_a), str(proj_b)]),
        )
        roots = _resolve_project_roots(None)
        assert str(proj_a) in roots
        assert str(proj_b) in roots

    def test_warning_logged_when_all_sources_empty(self, monkeypatch, caplog):
        monkeypatch.delenv("HIPPO_PROJECT_ROOTS", raising=False)
        monkeypatch.setattr("hippo_brain.entity_resolver._load_config_roots", lambda: [])
        monkeypatch.setattr("hippo_brain.entity_resolver._auto_detect_roots", lambda: [])
        with caplog.at_level(logging.WARNING, logger="hippo_brain.entity_resolver"):
            roots = _resolve_project_roots(None)
        assert roots == []
        assert any("inactive" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# LOW-2 guard: exact root match produces basename not ""
# ---------------------------------------------------------------------------


class TestExactRootMatchBasename:
    def test_exact_root_match_produces_basename_not_empty(self):
        roots = ["/users/carpenter/projects/hippo-postgres"]
        result = canonicalize(
            "file", "/users/carpenter/projects/hippo-postgres", project_roots=roots
        )
        assert result == "hippo-postgres"

    def test_no_unique_constraint_violation_on_same_type(self):
        # Two entities whose name IS the project root should both canonicalize to the
        # same non-empty string (the basename), so only one row survives on conflict.
        roots = ["/users/carpenter/projects/hippo"]
        r1 = canonicalize("file", "/users/carpenter/projects/hippo", project_roots=roots)
        r2 = canonicalize("directory", "/users/carpenter/projects/hippo", project_roots=roots)
        assert r1 == r2 == "hippo"
        assert r1 != ""


# ---------------------------------------------------------------------------
# Worktree-prefix stripping (issue #98 — F1: ephemeral parallel-agent worktree
# pollution). The worktree subdirectory naming scheme is NOT just `agent-*`;
# Claude Code names worktrees with a mix of schemes, so the stripping must be
# segment-name agnostic.
# ---------------------------------------------------------------------------


class TestStripWorktreePrefix:
    def test_strip_agent_worktree(self):
        assert (
            strip_worktree_prefix(
                "/users/carpenter/projects/hippo/.claude/worktrees/agent-ac83d4d3/src/foo.rs"
            )
            == "/users/carpenter/projects/hippo/src/foo.rs"
        )

    def test_strip_feat_worktree(self):
        # `feat-p1.1a-watchdog-core` style — does NOT start with `agent-`.
        assert (
            strip_worktree_prefix(
                "/users/carpenter/projects/hippo/.claude/worktrees/feat-p1.1a-watchdog-core/src/foo.rs"
            )
            == "/users/carpenter/projects/hippo/src/foo.rs"
        )

    def test_strip_adjective_noun_worktree(self):
        # `gracious-williamson-8c3e1f` style — Claude Code's adjective-noun-hex namer.
        assert (
            strip_worktree_prefix(
                "/users/carpenter/projects/hippo/.claude/worktrees/gracious-williamson-8c3e1f/src/foo.rs"
            )
            == "/users/carpenter/projects/hippo/src/foo.rs"
        )

    def test_strip_noop_on_non_worktree_path(self):
        assert (
            strip_worktree_prefix("/users/carpenter/projects/hippo/src/foo.rs")
            == "/users/carpenter/projects/hippo/src/foo.rs"
        )

    def test_strip_handles_relative_path(self):
        assert (
            strip_worktree_prefix(".claude/worktrees/agent-XX/crates/hippo-daemon/src/foo.rs")
            == "crates/hippo-daemon/src/foo.rs"
        )

    def test_strip_only_one_segment(self):
        # Only the segment immediately after `worktrees/` is stripped — nested
        # worktrees would be unusual but the regex must not greedily eat more.
        assert (
            strip_worktree_prefix(
                "/repo/.claude/worktrees/agent-XX/.claude/worktrees/agent-YY/file.txt"
            )
            == "/repo/file.txt"
        )

    def test_strip_does_not_match_directory_named_like_worktree(self):
        # A directory literally named ".claude/worktrees/foo" with no trailing
        # slash (i.e. the worktree IS the leaf) is preserved — without a
        # trailing slash there's no segment AFTER the worktree dir to anchor on.
        assert (
            strip_worktree_prefix("/repo/.claude/worktrees/agent-XX")
            == "/repo/.claude/worktrees/agent-XX"
        )


class TestCanonicalizeStripsWorktreeBeforeProjectRoot:
    """canonicalize() must strip the worktree segment BEFORE the project-root
    strip, otherwise the project-root no longer matches the prefix of the
    worktree path.
    """

    def test_agent_worktree_collapses_to_canonical(self):
        roots = ["/users/carpenter/projects/hippo"]
        result = canonicalize(
            "file",
            "/users/carpenter/projects/hippo/.claude/worktrees/agent-ac83d4d3/crates/hippo-daemon/src/schema_handshake.rs",
            project_roots=roots,
        )
        assert result == "crates/hippo-daemon/src/schema_handshake.rs"

    def test_canonical_path_and_worktree_path_resolve_same(self):
        roots = ["/users/carpenter/projects/hippo"]
        canonical_input = (
            "/users/carpenter/projects/hippo/crates/hippo-daemon/src/schema_handshake.rs"
        )
        worktree_input = "/users/carpenter/projects/hippo/.claude/worktrees/agent-ac83d4d3/crates/hippo-daemon/src/schema_handshake.rs"
        a = canonicalize("file", canonical_input, project_roots=roots)
        b = canonicalize("file", worktree_input, project_roots=roots)
        assert a == b == "crates/hippo-daemon/src/schema_handshake.rs"

    def test_three_different_worktree_styles_all_collapse(self):
        roots = ["/users/carpenter/projects/hippo"]
        paths = [
            "/users/carpenter/projects/hippo/.claude/worktrees/agent-ac83d4d3/src/foo.rs",
            "/users/carpenter/projects/hippo/.claude/worktrees/feat-p1.1a-watchdog/src/foo.rs",
            "/users/carpenter/projects/hippo/.claude/worktrees/gracious-williamson-8c3e1f/src/foo.rs",
        ]
        results = {canonicalize("file", p, project_roots=roots) for p in paths}
        assert results == {"src/foo.rs"}

    def test_dedup_collapses_worktree_polluted_entities(self, monkeypatch):
        """End-to-end: existing dedup-entities.py picks up worktree-polluted
        rows now that canonicalize strips the worktree segment.
        """
        monkeypatch.setenv("HIPPO_PROJECT_ROOTS", "/users/carpenter/projects/hippo")

        conn, db_path = _make_db()
        try:
            now_ms = 1_700_000_000_000
            # Insert canonical + 3 worktree variants of the same file.
            conn.execute(
                "INSERT INTO entities (type, name, canonical, first_seen, last_seen, created_at) VALUES (?,?,?,?,?,?)",
                (
                    "file",
                    "/users/carpenter/projects/hippo/crates/hippo-daemon/src/schema_handshake.rs",
                    "/users/carpenter/projects/hippo/crates/hippo-daemon/src/schema_handshake.rs",
                    now_ms,
                    now_ms,
                    now_ms,
                ),
            )
            for i, wt in enumerate(
                [
                    "agent-ac83d4d3",
                    "agent-a5642c4b",
                    "feat-p1.1a-watchdog",
                ],
                start=1,
            ):
                conn.execute(
                    "INSERT INTO entities (type, name, canonical, first_seen, last_seen, created_at) VALUES (?,?,?,?,?,?)",
                    (
                        "file",
                        f"/users/carpenter/projects/hippo/.claude/worktrees/{wt}/crates/hippo-daemon/src/schema_handshake.rs",
                        f"/users/carpenter/projects/hippo/.claude/worktrees/{wt}/crates/hippo-daemon/src/schema_handshake.rs",
                        now_ms + i,
                        now_ms + i,
                        now_ms + i,
                    ),
                )
            conn.commit()

            import importlib.util
            import sys

            scripts_root = Path(__file__).parent.parent / "scripts"
            spec = importlib.util.spec_from_file_location(
                "dedup_entities_worktree", scripts_root / "dedup-entities.py"
            )
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            sys.modules["dedup_entities_worktree"] = mod
            spec.loader.exec_module(mod)  # type: ignore[union-attr]

            stats = mod.run(conn, dry_run=False)

            rows = conn.execute("SELECT canonical FROM entities").fetchall()
            assert len(rows) == 1
            assert rows[0]["canonical"] == "crates/hippo-daemon/src/schema_handshake.rs"
            assert stats["deleted"] == 3
        finally:
            conn.close()
            db_path.unlink(missing_ok=True)
            db_path.with_suffix(".db-wal").unlink(missing_ok=True)
            db_path.with_suffix(".db-shm").unlink(missing_ok=True)
