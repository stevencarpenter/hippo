"""Tests for GitHub Actions MCP tools (get_ci_status, get_lessons)."""

import asyncio
import time

import pytest

from hippo_brain.mcp import _state, get_ci_status, get_lessons, mcp


class TestToolRegistration:
    def test_get_ci_status_registered(self):
        assert "get_ci_status" in mcp._tool_manager._tools

    def test_get_lessons_registered(self):
        assert "get_lessons" in mcp._tool_manager._tools


@pytest.fixture()
def workflow_db(tmp_db):
    """Insert a workflow run with job and annotation into the shared tmp_db fixture."""
    conn, db_path = tmp_db
    conn.execute("""
        INSERT INTO workflow_runs
          (id, repo, head_sha, head_branch, event, status, conclusion,
           html_url, raw_json, first_seen_at, last_seen_at)
        VALUES (1, 'me/r', 'abc123', 'main', 'push', 'completed', 'failure',
                'https://github.com/me/r/actions/runs/1', '{}', 1000, 2000)
    """)
    conn.execute("""
        INSERT INTO workflow_jobs (id, run_id, name, status, conclusion, raw_json)
        VALUES (10, 1, 'lint', 'completed', 'failure', '{}')
    """)
    conn.execute("""
        INSERT INTO workflow_annotations
          (job_id, level, tool, rule_id, path, start_line, message)
        VALUES (10, 'failure', 'ruff', 'F401', 'src/main.py', 3, 'F401 unused import')
    """)
    conn.commit()
    return conn, db_path


@pytest.fixture()
def lessons_db(tmp_db):
    """Insert a lesson into the shared tmp_db fixture."""
    conn, db_path = tmp_db
    now_ms = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO lessons
          (repo, tool, rule_id, path_prefix, summary, fix_hint,
           occurrences, first_seen_at, last_seen_at)
        VALUES ('me/r', 'ruff', 'F401', 'src/', 'unused imports in src/',
                'remove unused imports', 3, ?, ?)
        """,
        (now_ms - 10000, now_ms),
    )
    conn.commit()
    return conn, db_path


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch, tmp_path):
    """Save and restore _state between tests; keep the developer's project aliases out."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    old_db = _state.db_path
    yield
    _state.db_path = old_db


class TestGetCIStatusTool:
    def test_returns_status_for_known_sha(self, workflow_db):
        _, db_path = workflow_db
        _state.db_path = str(db_path)

        result = asyncio.run(get_ci_status(repo="me/r", sha="abc123"))
        assert result["conclusion"] == "failure"
        assert len(result["jobs"]) == 1
        assert result["jobs"][0]["annotations"][0]["rule_id"] == "F401"

    def test_returns_empty_dict_for_missing_sha(self, workflow_db):
        _, db_path = workflow_db
        _state.db_path = str(db_path)

        result = asyncio.run(get_ci_status(repo="me/r", sha="notfound"))
        assert result == {}

    def test_returns_status_by_branch(self, workflow_db):
        _, db_path = workflow_db
        _state.db_path = str(db_path)

        result = asyncio.run(get_ci_status(repo="me/r", branch="main"))
        assert result["head_sha"] == "abc123"

    def test_raises_on_missing_sha_and_branch(self, workflow_db):
        _, db_path = workflow_db
        _state.db_path = str(db_path)

        with pytest.raises(ValueError):
            asyncio.run(get_ci_status(repo="me/r"))


class TestGetLessonsTool:
    def test_returns_lessons_for_repo(self, lessons_db):
        _, db_path = lessons_db
        _state.db_path = str(db_path)

        results = asyncio.run(get_lessons(repo="me/r"))
        assert len(results) == 1
        assert results[0]["summary"] == "unused imports in src/"
        assert results[0]["occurrences"] == 3

    def test_returns_empty_list_for_unknown_repo(self, lessons_db):
        _, db_path = lessons_db
        _state.db_path = str(db_path)

        results = asyncio.run(get_lessons(repo="other/repo"))
        assert results == []

    def test_filter_by_tool(self, lessons_db):
        _, db_path = lessons_db
        _state.db_path = str(db_path)

        results = asyncio.run(get_lessons(repo="me/r", tool="ruff"))
        assert len(results) == 1

        results = asyncio.run(get_lessons(repo="me/r", tool="clippy"))
        assert results == []

    def test_outside_a_repository_no_arguments_return_every_project(self, lessons_db, monkeypatch):
        _, db_path = lessons_db
        _state.db_path = str(db_path)
        monkeypatch.setattr("hippo_brain.mcp._server_project", lambda: None)

        results = asyncio.run(get_lessons())
        assert [r["repo"] for r in results] == ["me/r"]

    def test_a_deleted_server_directory_means_no_default_project(self, monkeypatch):
        from hippo_brain import mcp as mcp_module

        def gone():
            raise FileNotFoundError("the worktree was removed")

        monkeypatch.setattr(mcp_module.Path, "cwd", gone)
        assert mcp_module._server_project() is None

    def test_empty_tool_means_no_tool_filter(self, lessons_db):
        _, db_path = lessons_db
        _state.db_path = str(db_path)

        assert len(asyncio.run(get_lessons(repo="me/r", tool=""))) == 1

    def test_watchdog_alarm_without_a_source_is_a_capture_alarm(self, lessons_db):
        conn, db_path = lessons_db
        _state.db_path = str(db_path)
        conn.execute(
            """INSERT INTO lessons (repo, tool, rule_id, path_prefix, summary, occurrences,
                                    first_seen_at, last_seen_at)
               VALUES ('me/r', 'hippo-watchdog', 'I-7', 'docs/capture/', 'alarm', 9, 1, 2)"""
        )
        conn.commit()

        results = asyncio.run(get_lessons(repo="me/r"))
        assert [r["source"] for r in results] == ["capture-alarm", "ci"]


class TestSessionLessons:
    """Problems recorded by enriched sessions are lessons for every project."""

    @staticmethod
    def _fake_retrieval(monkeypatch, hits):
        captured: dict = {}

        async def fake_retrieve_filtered(**kwargs):
            if not captured:  # the first lookup; a no-match fallback may follow it
                captured.update(kwargs)
            return hits

        monkeypatch.setattr("hippo_brain.mcp._retrieve_filtered", fake_retrieve_filtered)
        return captured

    def test_session_problems_are_returned_as_lessons(self, lessons_db, monkeypatch):
        _, db_path = lessons_db
        _state.db_path = str(db_path)
        captured = self._fake_retrieval(
            monkeypatch,
            [
                {
                    "uuid": "n1",
                    "problems_encountered": [
                        "sqlx offline cache was stale. Resolved by `cargo sqlx prepare`.",
                        "SQLX OFFLINE cache was stale.  Resolved by `cargo sqlx prepare`.",
                    ],
                    "cwd": "/Users/me/projects/whistlepost/backend",
                    "git_branch": "main",
                    "captured_at": 5,
                },
                {"uuid": "n2", "problems_encountered": [], "cwd": "/x", "captured_at": 6},
            ],
        )

        results = asyncio.run(
            get_lessons(repo="stevencarpenter/whistlepost", tool="sqlx", query="offline cache")
        )

        assert captured["project"] == "stevencarpenter/whistlepost"
        assert captured["mode"] == "hybrid"
        assert "sqlx" in captured["query"] and "offline cache" in captured["query"]
        assert results == [
            {
                "source": "session",
                "summary": "sqlx offline cache was stale. Resolved by `cargo sqlx prepare`.",
                "cwd": "/Users/me/projects/whistlepost/backend",
                "git_branch": "main",
                "last_seen_at": 5,
                "uuid": "n1",
            }
        ]

    def test_problems_from_another_project_are_dropped(self, lessons_db, monkeypatch):
        """Older shell nodes mix commands from projects that were active together."""
        _, db_path = lessons_db
        _state.db_path = str(db_path)
        conn, _ = lessons_db
        conn.execute(
            "INSERT INTO sessions (id, start_time, shell, hostname, username) "
            "VALUES (1, 1, 'zsh', 'h', 'u')"
        )
        conn.executemany(
            "INSERT INTO events (session_id, timestamp, command, duration_ms, cwd, hostname, "
            "shell, git_repo) VALUES (1, 1, 'gfp', 1, ?, 'h', 'zsh', ?)",
            [("/p/windrow", "me/windrow"), ("/p/dotfiles-claude-1", "me/dotfiles")],
        )
        conn.commit()
        self._fake_retrieval(
            monkeypatch,
            [
                {"uuid": "n1", "problems_encountered": ["gfp failed"], "cwd": "/p/windrow"},
                {"uuid": "n2", "problems_encountered": ["hook"], "cwd": "/p/dotfiles-claude-1"},
                {"uuid": "n3", "problems_encountered": ["ci failed"], "cwd": ""},
                {"uuid": "n4", "problems_encountered": ["unknown dir"], "cwd": "/tmp/scratch"},
            ],
        )

        results = asyncio.run(get_lessons(repo="stevencarpenter/dotfiles"))

        assert [r["uuid"] for r in results] == ["n2", "n3", "n4"]

    def test_a_node_that_mixes_repositories_is_not_a_project_lesson(self, lessons_db, monkeypatch):
        """Shell nodes written before chunks were grouped by repository hold both projects."""
        conn, db_path = lessons_db
        _state.db_path = str(db_path)
        conn.execute(
            "INSERT INTO sessions (id, start_time, shell, hostname, username) "
            "VALUES (1, 1, 'zsh', 'h', 'u')"
        )
        conn.executemany(
            "INSERT INTO events (id, session_id, timestamp, command, duration_ms, cwd, hostname, "
            "shell, git_repo) VALUES (?, 1, 1, 'gfp', 1, ?, 'h', 'zsh', ?)",
            [
                (1, "/p/dotfiles", "me/dotfiles"),
                (2, "/p/windrow", "me/windrow"),
                (3, "/p/dotfiles", "sj/dotfiles"),  # another owner, the same project
            ],
        )
        conn.commit()
        self._fake_retrieval(
            monkeypatch,
            [
                {
                    "uuid": "mixed",
                    "problems_encountered": ["a"],
                    "cwd": "/p/dotfiles",
                    "linked_event_ids": [1, 2],
                },
                {
                    "uuid": "one-repo",
                    "problems_encountered": ["b"],
                    "cwd": "/p/dotfiles",
                    "linked_event_ids": [1, 3],
                },
                {"uuid": "no-events", "problems_encountered": ["c"], "cwd": ""},
            ],
        )

        results = asyncio.run(get_lessons(repo="me/dotfiles"))

        assert [r["uuid"] for r in results] == ["one-repo", "no-events"]

    def test_a_problem_from_a_ci_run_is_not_labelled_as_a_session_lesson(
        self, lessons_db, monkeypatch
    ):
        _, db_path = lessons_db
        _state.db_path = str(db_path)
        self._fake_retrieval(
            monkeypatch,
            [
                {
                    "uuid": "run",
                    "problems_encountered": ["clippy failed"],
                    "cwd": "",
                    "linked_source_ids": ["workflow-7", "shell-3"],
                },
                {
                    "uuid": "work",
                    "problems_encountered": ["hook failed"],
                    "cwd": "",
                    "linked_source_ids": ["claude-9"],
                },
            ],
        )

        results = asyncio.run(get_lessons(repo="other/repo"))

        assert [(r["uuid"], r["source"]) for r in results] == [
            ("run", "ci-run"),
            ("work", "session"),
        ]

    def test_a_worktree_path_as_repo_resolves_to_its_main_checkout(
        self, lessons_db, monkeypatch, tmp_path
    ):
        _, db_path = lessons_db
        _state.db_path = str(db_path)
        main = tmp_path / "r"
        (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
        worktree = tmp_path / "r-claude-123"
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt\n")
        captured = self._fake_retrieval(monkeypatch, [])

        results = asyncio.run(get_lessons(repo=str(worktree)))

        assert captured["project"] == str(main)
        assert [r["repo"] for r in results] == ["me/r"]

    def test_without_a_topic_the_most_recent_problems_are_used(self, lessons_db, monkeypatch):
        _, db_path = lessons_db
        _state.db_path = str(db_path)
        captured = self._fake_retrieval(monkeypatch, [])

        asyncio.run(get_lessons(repo="me/r"))

        assert captured["mode"] == "recent"
        assert captured["query"] == ""

    def test_a_topic_with_no_match_falls_back_to_recent_project_problems(
        self, lessons_db, monkeypatch
    ):
        _, db_path = lessons_db
        _state.db_path = str(db_path)
        modes: list[str] = []

        async def fake_retrieve_filtered(**kwargs):
            modes.append(kwargs["mode"])
            if kwargs["mode"] == "hybrid":
                return []
            return [{"uuid": "n1", "problems_encountered": ["recent problem"], "cwd": ""}]

        monkeypatch.setattr("hippo_brain.mcp._retrieve_filtered", fake_retrieve_filtered)

        results = asyncio.run(get_lessons(repo="other/repo", path="skills/strip_context.py"))
        assert modes == ["hybrid", "recent"]
        assert [r["summary"] for r in results] == ["recent problem"]

        # Without a project, recent problems of every project would be unrelated.
        modes.clear()
        assert asyncio.run(get_lessons(tool="nonexistent-tool")) == []
        assert modes == ["hybrid"]

    def test_recurring_and_session_lessons_share_the_limit(self, lessons_db, monkeypatch):
        _, db_path = lessons_db
        _state.db_path = str(db_path)
        self._fake_retrieval(
            monkeypatch,
            [
                {
                    "uuid": f"n{i}",
                    "problems_encountered": [f"problem {i}"],
                    "cwd": "/p/r",
                    "captured_at": i,
                }
                for i in range(10)
            ],
        )

        results = asyncio.run(get_lessons(repo="me/r", limit=4))

        assert [r["source"] for r in results] == ["ci", "session", "session", "session"]
        assert results[0]["summary"] == "unused imports in src/"

    def test_no_arguments_scopes_to_the_project_the_server_runs_in(
        self, lessons_db, monkeypatch, tmp_path
    ):
        _, db_path = lessons_db
        _state.db_path = str(db_path)
        repo_root = tmp_path / "r"
        (repo_root / ".git").mkdir(parents=True)
        (repo_root / "src").mkdir()
        monkeypatch.chdir(repo_root / "src")
        captured = self._fake_retrieval(monkeypatch, [])

        results = asyncio.run(get_lessons())

        assert captured["project"] == str(repo_root)
        assert [r["repo"] for r in results] == ["me/r"]

    def test_a_topic_without_a_repo_searches_every_project(self, lessons_db, monkeypatch):
        _, db_path = lessons_db
        _state.db_path = str(db_path)
        captured = self._fake_retrieval(monkeypatch, [])

        asyncio.run(get_lessons(tool="postgres"))

        assert captured["project"] == ""

    def test_a_free_text_topic_alone_returns_no_unrelated_recurring_lessons(
        self, lessons_db, monkeypatch
    ):
        _, db_path = lessons_db
        _state.db_path = str(db_path)
        self._fake_retrieval(monkeypatch, [])

        assert asyncio.run(get_lessons(query="launchd plist")) == []
        assert len(asyncio.run(get_lessons(query="launchd plist", repo="me/r"))) == 1

    def test_an_absolute_file_path_names_its_checkout_and_the_path_inside_it(
        self, lessons_db, monkeypatch, tmp_path
    ):
        _, db_path = lessons_db
        _state.db_path = str(db_path)
        main = tmp_path / "r"
        (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
        (main / "src").mkdir()
        (main / "docs").mkdir()
        worktree = tmp_path / "r-claude-123"  # linked worktree with an unrelated name
        (worktree / "src").mkdir(parents=True)
        (worktree / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt\n")
        captured = self._fake_retrieval(monkeypatch, [])

        for file in (main / "src" / "main.py", worktree / "src" / "main.py"):
            captured.clear()
            results = asyncio.run(get_lessons(path=str(file)))
            assert captured["project"] == str(main)
            assert "src/main.py" in captured["query"]
            assert [r["path_prefix"] for r in results] == ["src/"]
        # The path filter still applies when the repository is named as well.
        assert asyncio.run(get_lessons(repo="me/r", path=str(main / "docs" / "x.md"))) == []

    def test_a_failed_session_lookup_still_returns_recurring_lessons(self, lessons_db, monkeypatch):
        _, db_path = lessons_db
        _state.db_path = str(db_path)

        async def broken(**_kwargs):
            raise RuntimeError("retrieval down")

        monkeypatch.setattr("hippo_brain.mcp._retrieve_filtered", broken)

        results = asyncio.run(get_lessons(repo="me/r"))

        assert [r["source"] for r in results] == ["ci"]
