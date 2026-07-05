from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hippo_brain.auto_memory import ingest_memory_file
from hippo_brain.auto_memory_discovery import (
    decode_claude_project_slug,
    deduplicate_memory_roots,
    discover_default_claude_project_roots,
    discover_memory_roots,
    discover_settings_memory_roots,
    discovery_config_from_dict,
    memory_roots_to_file_sources,
    merge_configured_sources,
)
from hippo_brain.auto_memory_reconcile import inventory_from_config, load_sources_from_config


@pytest.fixture
def conn(tmp_db):
    connection, _path = tmp_db
    yield connection


def test_decode_claude_project_slug_reverses_encoded_paths() -> None:
    assert decode_claude_project_slug("-Users-carpenter-projects-hippo") == (
        "/Users/carpenter/projects/hippo"
    )


def test_discover_default_claude_project_memory_dirs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    memory_a = home / ".claude/projects/-repo-a/memory"
    memory_b = home / ".claude/projects/-repo-b/memory"
    memory_a.mkdir(parents=True)
    memory_b.mkdir(parents=True)
    (memory_a / "MEMORY.md").write_text("# A\n")
    (memory_b / "project_notes.md").write_text("# Notes\n")

    roots = discover_default_claude_project_roots(home)
    assert {root.memory_dir for root in roots} == {memory_a.resolve(), memory_b.resolve()}


def test_custom_auto_memory_directory_from_user_settings(tmp_path: Path) -> None:
    home = tmp_path / "home"
    custom = home / "shared-memory"
    custom.mkdir(parents=True)
    settings = home / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"autoMemoryDirectory": str(custom)}))

    roots = discover_settings_memory_roots(home)
    assert len(roots) == 1
    assert roots[0].memory_dir == custom.resolve()
    assert roots[0].origin == "claude-settings-user"


def test_deduplicate_shared_memory_directory_across_slugs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    shared = home / "shared-memory"
    shared.mkdir(parents=True)
    settings_a = home / ".claude/projects/-repo-a/settings.local.json"
    settings_b = home / ".claude/projects/-repo-b/settings.local.json"
    settings_a.parent.mkdir(parents=True)
    settings_b.parent.mkdir(parents=True)
    payload = json.dumps({"autoMemoryDirectory": str(shared)})
    settings_a.write_text(payload)
    settings_b.write_text(payload)

    roots = deduplicate_memory_roots(discover_settings_memory_roots(home))
    assert len(roots) == 1
    assert roots[0].memory_dir == shared.resolve()


def test_include_and_exclude_patterns_filter_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    keep = home / ".claude/projects/-Users-me-hippo/memory"
    skip = home / ".claude/projects/-Users-me-archive/memory"
    keep.mkdir(parents=True)
    skip.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    config = discovery_config_from_dict(
        {
            "discovery": {
                "include_patterns": ["*hippo*"],
                "exclude_patterns": ["*archive*"],
            }
        }
    )
    roots = discover_memory_roots(config, home=home)
    assert {root.memory_dir for root in roots} == {keep.resolve()}


def test_load_sources_from_config_merges_explicit_and_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    discovered = home / ".claude/projects/-repo-a/memory"
    discovered.mkdir(parents=True)
    (discovered / "MEMORY.md").write_text("# Memory\n")
    monkeypatch.setenv("HOME", str(home))

    explicit_file = tmp_path / "explicit.md"
    explicit_file.write_text("# Explicit\n")

    config = {
        "auto_memory": {
            "enabled": True,
            "sources": [
                {
                    "path": str(explicit_file),
                    "repository": "example/explicit",
                    "logical_path": "explicit.md",
                }
            ],
            "discovery": {"enabled": True},
        }
    }
    sources = load_sources_from_config(config)
    paths = {source["path"] for source in sources}
    assert str(explicit_file.resolve()) in paths
    assert str((discovered / "MEMORY.md").resolve()) in paths


def test_inventory_dry_run_reports_roots_without_ingest(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    memory = home / ".claude/projects/-repo-a/memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("# Memory\n")
    monkeypatch.setenv("HOME", str(home))

    inventory = inventory_from_config(
        {
            "auto_memory": {
                "enabled": True,
                "discovery": {"enabled": True},
            }
        }
    )
    assert inventory["file_sources"] >= 1
    assert inventory["roots"][0]["memory_dir"] == str(memory.resolve())


def test_fleet_discovery_ingests_without_duplicate_documents(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    repo_a = tmp_path / "repos" / "alpha"
    repo_b = tmp_path / "repos" / "beta"
    shared = tmp_path / "shared-memory"
    shared.mkdir(parents=True)
    repo_a.mkdir(parents=True)
    repo_b.mkdir(parents=True)

    (shared / "MEMORY.md").write_text("# Shared index\n")
    (shared / "project_notes.md").write_text("# Notes\nUse cargo.\n")

    ingest_memory_file(conn, shared / "MEMORY.md", repository="example/alpha", now_ms=1000)
    ingest_memory_file(conn, shared / "project_notes.md", repository="example/alpha", now_ms=1000)
    ingest_memory_file(conn, shared / "MEMORY.md", repository="example/beta", now_ms=2000)

    doc_count = conn.execute("SELECT COUNT(*) FROM memory_documents").fetchone()[0]
    assert doc_count == 3

    explicit = memory_roots_to_file_sources(
        deduplicate_memory_roots(
            [
                *discover_default_claude_project_roots(tmp_path),
            ]
        )
    )
    merged = merge_configured_sources([], explicit)
    assert len({source["path"] for source in merged}) == len(merged)
