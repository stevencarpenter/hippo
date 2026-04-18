"""Tests for brain/scripts/backfill-git-repo.py — parse_owner_repo parity with git_repo.rs."""

from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

_SCRIPT = Path(__file__).resolve().parents[2] / "brain" / "scripts" / "backfill-git-repo.py"
_spec = importlib.util.spec_from_file_location("backfill_git_repo", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

parse_owner_repo = _mod.parse_owner_repo
resolve_git_repo = _mod.resolve_git_repo
run = _mod.run


# ---------------------------------------------------------------------------
# parse_owner_repo parity tests (must match git_repo.rs exactly)
# ---------------------------------------------------------------------------


def test_https_with_dot_git():
    assert parse_owner_repo("https://github.com/sjcarpenter/hippo.git") == "sjcarpenter/hippo"


def test_https_no_dot_git():
    assert parse_owner_repo("https://github.com/sjcarpenter/hippo") == "sjcarpenter/hippo"


def test_ssh_scp_style():
    assert parse_owner_repo("git@github.com:sjcarpenter/hippo.git") == "sjcarpenter/hippo"


def test_ssh_url_style():
    assert parse_owner_repo("ssh://git@github.com/sjcarpenter/hippo.git") == "sjcarpenter/hippo"


def test_trailing_slash():
    assert parse_owner_repo("https://github.com/sjcarpenter/hippo/") == "sjcarpenter/hippo"


def test_non_github_https():
    assert parse_owner_repo("https://gitlab.com/myorg/myrepo.git") == "myorg/myrepo"


def test_non_github_ssh():
    assert parse_owner_repo("git@gitlab.com:myorg/myrepo.git") == "myorg/myrepo"


def test_rejects_empty():
    assert parse_owner_repo("") is None
    assert parse_owner_repo("   ") is None


def test_rejects_single_segment():
    assert parse_owner_repo("hippo.git") is None


def test_rejects_local_absolute_path():
    assert parse_owner_repo("/home/me/hippo") is None
    assert parse_owner_repo("/home/me/hippo.git") is None


def test_rejects_file_url():
    assert parse_owner_repo("file:///home/me/hippo") is None
    assert parse_owner_repo("file:///home/me/hippo.git") is None


def test_rejects_relative_path():
    assert parse_owner_repo("../sibling-repo") is None
    assert parse_owner_repo("../sibling-repo.git") is None


# ---------------------------------------------------------------------------
# resolve_git_repo integration tests (real git subprocesses)
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-C", str(cwd)] + list(args), check=True, capture_output=True)


def test_resolve_uses_origin_remote():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git("init", "-b", "main", cwd=repo)
        _git("remote", "add", "origin", "git@github.com:sjcarpenter/hippo.git", cwd=repo)
        assert resolve_git_repo(str(repo)) == "sjcarpenter/hippo"


def test_resolve_falls_back_to_toplevel_basename():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "my-local-repo"
        repo.mkdir()
        _git("init", "-b", "main", cwd=repo)
        assert resolve_git_repo(str(repo)) == "my-local-repo"


def test_resolve_skips_local_path_remote():
    with tempfile.TemporaryDirectory() as tmp:
        upstream = Path(tmp) / "bare"
        upstream.mkdir()
        _git("init", "-b", "main", cwd=upstream)

        clone = Path(tmp) / "my-clone"
        clone.mkdir()
        _git("init", "-b", "main", cwd=clone)
        _git("remote", "add", "origin", str(upstream), cwd=clone)

        # Local path remote → parse_owner_repo returns None → falls back to basename
        assert resolve_git_repo(str(clone)) == "my-clone"


def test_resolve_returns_none_outside_repo():
    with tempfile.TemporaryDirectory() as tmp:
        assert resolve_git_repo(tmp) is None


def test_resolve_returns_none_empty_cwd():
    assert resolve_git_repo("") is None


# ---------------------------------------------------------------------------
# run() integration: synthetic SQLite DB
# ---------------------------------------------------------------------------


def _make_db(tmp: Path) -> Path:
    db = tmp / "hippo.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            cwd TEXT,
            git_repo TEXT
        )"""
    )
    conn.executemany(
        "INSERT INTO events (id, cwd, git_repo) VALUES (?, ?, ?)",
        [
            (1, "/some/nonexistent/dir", None),  # NULL — will be attempted
            (2, "/another/nonexistent", None),  # NULL — will be attempted
            (3, "/yet/another", "already/set"),  # non-NULL — must NOT change
        ],
    )
    conn.commit()
    conn.close()
    return db


def _mock_resolve(cwd: str) -> str | None:
    mapping = {
        "/some/nonexistent/dir": "owner/repo-a",
        "/another/nonexistent": None,  # simulates non-git dir
    }
    return mapping.get(cwd)


def test_run_updates_only_null_rows(tmp_path):
    db = _make_db(tmp_path)
    with patch.object(_mod, "resolve_git_repo", side_effect=_mock_resolve):
        result = run(db, dry_run=False)
    assert result == 0

    conn = sqlite3.connect(str(db))
    rows = {r[0]: r[1] for r in conn.execute("SELECT id, git_repo FROM events").fetchall()}
    conn.close()

    assert rows[1] == "owner/repo-a"  # resolved
    assert rows[2] is None  # unresolvable, left NULL
    assert rows[3] == "already/set"  # pre-existing, untouched


def test_run_dry_run_does_not_mutate(tmp_path):
    db = _make_db(tmp_path)
    with patch.object(_mod, "resolve_git_repo", side_effect=_mock_resolve):
        result = run(db, dry_run=True)
    assert result == 0

    conn = sqlite3.connect(str(db))
    rows = {r[0]: r[1] for r in conn.execute("SELECT id, git_repo FROM events").fetchall()}
    conn.close()

    # Everything stays as-is in dry-run
    assert rows[1] is None
    assert rows[2] is None
    assert rows[3] == "already/set"


def test_run_missing_db(tmp_path):
    result = run(tmp_path / "does_not_exist.db", dry_run=True)
    assert result == 1


def test_run_sets_sqlite_pragmas(tmp_path, monkeypatch):
    """Verify required pragmas are set on the connection (MED-1).

    sqlite3.Connection.execute is a read-only C attribute, so we inject a
    proxy module into sys.modules that wraps the real connection with tracking.
    """
    import sys
    import types

    db = _make_db(tmp_path)
    pragma_calls: list[str] = []
    real_sqlite3 = sys.modules["sqlite3"]

    class _TrackingConn:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self._c = conn

        def execute(self, sql: str, *args, **kw):
            pragma_calls.append(sql.strip())
            return self._c.execute(sql, *args, **kw)

        def executemany(self, sql: str, params, /):
            return self._c.executemany(sql, params)

        def close(self) -> None:
            self._c.close()

        def __enter__(self):
            self._c.__enter__()
            return self

        def __exit__(self, *a):
            return self._c.__exit__(*a)

        @property
        def row_factory(self):
            return self._c.row_factory

        @row_factory.setter
        def row_factory(self, v) -> None:
            self._c.row_factory = v

    fake_sqlite3 = types.ModuleType("sqlite3")
    fake_sqlite3.Row = real_sqlite3.Row  # type: ignore[attr-defined]
    fake_sqlite3.OperationalError = real_sqlite3.OperationalError  # type: ignore[attr-defined]
    fake_sqlite3.connect = lambda path, **kw: _TrackingConn(real_sqlite3.connect(path, **kw))  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "sqlite3", fake_sqlite3)

    with patch.object(_mod, "resolve_git_repo", side_effect=_mock_resolve):
        run(db, dry_run=True)

    assert "PRAGMA foreign_keys=ON" in pragma_calls
    assert "PRAGMA busy_timeout=5000" in pragma_calls
