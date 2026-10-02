"""Freeze pre-task memory and Git state for a prospective paired agent case."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path
from shutil import copyfile

from hippo_brain.schema_version import EXPECTED_SCHEMA_VERSION


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def freeze(
    db: Path,
    repo: Path,
    out: Path,
    *,
    memory_manifest: Path | None = None,
    task_start_ms: int | None = None,
    prompt_file: Path | None = None,
    attachments: tuple[Path, ...] = (),
) -> dict:
    """Freeze memory before a request, then freeze its task from that memory copy."""
    if (task_start_ms is None) != (prompt_file is None):
        raise ValueError("task start and full prompt file must be supplied together")
    if memory_manifest is not None and task_start_ms is None:
        raise ValueError("a prior memory manifest requires a task start and prompt")
    if attachments and task_start_ms is None:
        raise ValueError("attachments require a task start and prompt")
    if task_start_ms is not None and not 0 < task_start_ms <= time.time_ns() // 1_000_000:
        raise ValueError("task start must be a past Unix epoch millisecond timestamp")
    db, repo, out = db.resolve(strict=True), repo.resolve(strict=True), out.resolve()
    prior = None
    if memory_manifest is not None:
        prior = json.loads(memory_manifest.resolve(strict=True).read_text())
        if Path(prior["snapshot"]).resolve(strict=True) != db:
            raise ValueError("memory manifest does not identify the source database")
        if _sha256(db) != prior["snapshot_sha256"]:
            raise ValueError("source memory changed after its manifest was written")
    if prompt_file is not None:
        prompt_file = prompt_file.resolve(strict=True)
    attachments = tuple(path.resolve(strict=True) for path in attachments)
    git_root = Path(os.fsdecode(_git(repo, "rev-parse", "--show-toplevel")).strip()).resolve()
    if out.is_relative_to(git_root):
        raise ValueError("output must be outside the task repository")
    head_before = os.fsdecode(_git(git_root, "rev-parse", "HEAD")).strip()
    status_before = _git(git_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")

    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    snapshot = out / "hippo.sqlite"
    started_ms = time.time_ns() // 1_000_000
    with (
        closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(snapshot)) as destination,
    ):
        source.execute("PRAGMA query_only=ON")
        source.execute("BEGIN")
        source.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        source.backup(destination, pages=-1)
    ended_ms = time.time_ns() // 1_000_000
    snapshot.chmod(0o600)
    with closing(sqlite3.connect(snapshot)) as frozen:
        quick_check = frozen.execute("PRAGMA quick_check").fetchone()[0]
        version = frozen.execute("PRAGMA user_version").fetchone()[0]
    if quick_check != "ok":
        raise ValueError(f"snapshot integrity check failed: {quick_check}")
    snapshot_sha = _sha256(snapshot)
    if prior is not None and snapshot_sha != prior["snapshot_sha256"]:
        raise ValueError("source memory changed while copying it")

    copied_attachments = []
    if prompt_file is not None:
        prompt_copy = out / "prompt.txt"
        copyfile(prompt_file, prompt_copy)
        prompt_copy.chmod(0o600)
        for index, attachment in enumerate(attachments):
            attachment_copy = out / f"attachment-{index:02d}{attachment.suffix}"
            copyfile(attachment, attachment_copy)
            attachment_copy.chmod(0o600)
            copied_attachments.append(
                {
                    "source": str(attachment),
                    "copy": str(attachment_copy),
                    "sha256": _sha256(attachment_copy),
                    "source_mtime_ms": attachment.stat().st_mtime_ns // 1_000_000,
                }
            )

    head_after = os.fsdecode(_git(git_root, "rev-parse", "HEAD")).strip()
    status_after = _git(git_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    repo_ready = (
        version == EXPECTED_SCHEMA_VERSION
        and not status_before
        and not status_after
        and head_before == head_after
    )
    if repo_ready:
        clone = out / "repo"
        clone.mkdir(mode=0o700)
        _git(clone, "init", "--quiet")
        _git(clone, "fetch", "--quiet", "--no-tags", str(git_root), head_before)
        _git(clone, "-c", "advice.detachedHead=false", "checkout", "--quiet", head_before)
        (clone / ".git/FETCH_HEAD").unlink(missing_ok=True)
        repo_ready = (
            os.fsdecode(_git(git_root, "rev-parse", "HEAD")).strip() == head_before
            and not _git(git_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
            and os.fsdecode(_git(clone, "rev-parse", "HEAD")).strip() == head_before
            and not _git(clone, "status", "--porcelain=v1", "-z")
        )
    elif status_before:
        (out / "git-status-before.bin").write_bytes(status_before)
        (out / "git-status-before.bin").chmod(0o600)

    with Path(__file__).open("rb") as stream:
        intake_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    memory_cutoff_ms = prior["snapshot_finished_ms"] if prior is not None else ended_ms
    memory_predates_task = task_start_ms is not None and memory_cutoff_ms < task_start_ms
    attachments_predate_task = task_start_ms is not None and all(
        item["source_mtime_ms"] <= task_start_ms for item in copied_attachments
    )
    snapshot_ready = (
        repo_ready and prompt_file is not None and memory_predates_task and attachments_predate_task
    )
    manifest = {
        "intake_code_sha256": intake_sha,
        "source_db": str(db),
        "snapshot": str(snapshot),
        "snapshot_started_ms": started_ms,
        "snapshot_finished_ms": ended_ms,
        "snapshot_sha256": snapshot_sha,
        "memory_source_manifest": str(memory_manifest.resolve(strict=True))
        if memory_manifest is not None
        else None,
        "memory_cutoff_ms": memory_cutoff_ms,
        "memory_predates_task": memory_predates_task,
        "task_start_ms": task_start_ms,
        "prompt_source": str(prompt_file) if prompt_file is not None else None,
        "prompt_copy": str(out / "prompt.txt") if prompt_file is not None else None,
        "prompt_sha256": _sha256(out / "prompt.txt") if prompt_file is not None else None,
        "attachments": copied_attachments,
        "attachments_predate_task": attachments_predate_task,
        "schema_version": version,
        "expected_schema_version": EXPECTED_SCHEMA_VERSION,
        "quick_check": quick_check,
        "repo": str(git_root),
        "head_before": head_before,
        "head_after_backup": head_after,
        "git_status_before_sha256": hashlib.sha256(status_before).hexdigest(),
        "git_status_after_sha256": hashlib.sha256(status_after).hexdigest(),
        "repo_clean_before": not status_before,
        "repo_ready": repo_ready,
        "snapshot_ready": snapshot_ready,
        "clone": str(out / "repo") if repo_ready else None,
    }
    path = out / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    path.chmod(0o600)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--memory-manifest", type=Path)
    parser.add_argument("--task-start-ms", type=int)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--attachment", type=Path, action="append", default=[])
    args = parser.parse_args()
    os.umask(0o077)
    result = freeze(
        args.db,
        args.repo,
        args.out,
        memory_manifest=args.memory_manifest,
        task_start_ms=args.task_start_ms,
        prompt_file=args.prompt_file,
        attachments=tuple(args.attachment),
    )
    print(
        json.dumps(
            {
                "manifest": str(args.out / "manifest.json"),
                "snapshot_ready": result["snapshot_ready"],
            }
        )
    )


if __name__ == "__main__":
    main()
