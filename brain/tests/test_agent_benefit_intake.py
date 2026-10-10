import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from hippo_brain.bench.agent_benefit_intake import freeze
from hippo_brain.schema_version import EXPECTED_SCHEMA_VERSION


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_freeze_keeps_pre_task_memory_and_code_and_records_dirty_repos(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "file.txt").write_text("before\n")
    _git(repo, "add", "file.txt")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
    head = _git(repo, "rev-parse", "HEAD")

    db = tmp_path / "live.sqlite"
    with sqlite3.connect(db) as source:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute(f"PRAGMA user_version={EXPECTED_SCHEMA_VERSION}")
        source.execute("CREATE TABLE evidence (value TEXT)")
        source.execute("INSERT INTO evidence VALUES ('before')")
        source.commit()
        clean = freeze(db, repo, tmp_path / "clean-case")
        source.execute("INSERT INTO evidence VALUES ('after')")
        source.commit()

    assert clean["repo_ready"] is True
    assert clean["snapshot_ready"] is False
    assert clean["head_before"] == head
    assert not _git(tmp_path / "clean-case/repo", "remote")
    assert not (tmp_path / "clean-case/repo/.git/FETCH_HEAD").exists()
    assert (tmp_path / "clean-case/repo/file.txt").read_text() == "before\n"
    with sqlite3.connect(tmp_path / "clean-case/hippo.sqlite") as frozen:
        assert frozen.execute("SELECT value FROM evidence").fetchall() == [("before",)]

    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Investigate the repository change in full.\n")
    while time.time_ns() // 1_000_000 <= clean["snapshot_finished_ms"]:
        time.sleep(0.001)
    task_start_ms = time.time_ns() // 1_000_000
    from_pretask = freeze(
        tmp_path / "clean-case/hippo.sqlite",
        repo,
        tmp_path / "task-case",
        memory_manifest=tmp_path / "clean-case/manifest.json",
        task_start_ms=task_start_ms,
        prompt_file=prompt,
    )
    assert from_pretask["snapshot_ready"] is True
    assert from_pretask["memory_predates_task"] is True
    assert (tmp_path / "task-case/prompt.txt").read_text() == prompt.read_text()
    with sqlite3.connect(tmp_path / "task-case/hippo.sqlite") as frozen:
        assert frozen.execute("SELECT value FROM evidence").fetchall() == [("before",)]
    with pytest.raises(ValueError, match="task start and full prompt"):
        freeze(db, repo, tmp_path / "missing-prompt", task_start_ms=task_start_ms)
    too_late = freeze(
        db,
        repo,
        tmp_path / "late-memory",
        task_start_ms=task_start_ms,
        prompt_file=prompt,
    )
    assert too_late["snapshot_ready"] is False

    attachment = tmp_path / "research.md"
    attachment.write_text("revised after the request\n")
    attachment.touch()
    late_attachment = freeze(
        tmp_path / "clean-case/hippo.sqlite",
        repo,
        tmp_path / "late-attachment",
        memory_manifest=tmp_path / "clean-case/manifest.json",
        task_start_ms=task_start_ms,
        prompt_file=prompt,
        attachments=(attachment,),
    )
    assert late_attachment["attachments_predate_task"] is False
    assert late_attachment["snapshot_ready"] is False

    with sqlite3.connect(tmp_path / "clean-case/hippo.sqlite") as altered:
        altered.execute("INSERT INTO evidence VALUES ('tampered')")
    with pytest.raises(ValueError, match="source memory changed"):
        freeze(
            tmp_path / "clean-case/hippo.sqlite",
            repo,
            tmp_path / "tampered-memory",
            memory_manifest=tmp_path / "clean-case/manifest.json",
            task_start_ms=task_start_ms,
            prompt_file=prompt,
        )

    (repo / "file.txt").write_text("later\n")
    _git(repo, "add", "file.txt")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "later",
    )
    later = _git(repo, "rev-parse", "HEAD")
    assert (
        subprocess.run(
            ["git", "-C", str(tmp_path / "clean-case/repo"), "cat-file", "-e", later],
            capture_output=True,
        ).returncode
        != 0
    )

    (repo / "file.txt").write_text("dirty\n")
    dirty = freeze(db, repo, tmp_path / "dirty-case")
    assert dirty["snapshot_ready"] is False
    assert dirty["clone"] is None
    assert (tmp_path / "dirty-case/git-status-before.bin").exists()

    nested = repo / "subdir"
    nested.mkdir()
    with pytest.raises(ValueError, match="outside the task repository"):
        freeze(db, nested, repo / "inside-repo")
