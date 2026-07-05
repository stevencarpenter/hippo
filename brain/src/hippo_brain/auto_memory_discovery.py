"""Discover Claude Code auto-memory directories across projects and worktrees."""

from __future__ import annotations

import fnmatch
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hippo_brain.auto_memory_constants import SOURCE_KIND
from hippo_brain.auto_memory_ingest import derive_repository_identity

CLAUDE_PROJECTS_DIR = Path(".claude") / "projects"
MEMORY_DIR_NAME = "memory"
TRUSTED_SETTINGS_FILES = (
    "settings.json",
    "settings.local.json",
)


@dataclass(frozen=True)
class DiscoveryConfig:
    """Fleet discovery knobs under ``[auto_memory.discovery]``."""

    enabled: bool = True
    claude_projects: bool = True
    include_patterns: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    read_claude_settings: bool = True


@dataclass(frozen=True)
class MemoryRoot:
    """One logical auto-memory directory (deduplicated by resolved path)."""

    memory_dir: Path
    origin: str
    claude_project_slug: str | None = None
    encoded_path_hint: str | None = None
    settings_path: str | None = None
    accessible: bool = True
    error: str | None = None

    @property
    def inventory_key(self) -> str:
        return str(self.memory_dir.resolve())


@dataclass
class InventoryEntry:
    memory_dir: str
    origin: str
    claude_project_slug: str | None
    encoded_path_hint: str | None
    repository: str | None
    file_count: int
    accessible: bool
    error: str | None = None
    last_success_ts: int | None = None
    last_error_ts: int | None = None
    last_error_msg: str | None = None
    reconciled_files: int = 0
    changed_files: int = 0


@dataclass
class DiscoveryInventory:
    discovered_at_ms: int
    roots: list[InventoryEntry] = field(default_factory=list)
    file_sources: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "discovered_at_ms": self.discovered_at_ms,
            "file_sources": self.file_sources,
            "roots": [
                {
                    "memory_dir": entry.memory_dir,
                    "origin": entry.origin,
                    "claude_project_slug": entry.claude_project_slug,
                    "encoded_path_hint": entry.encoded_path_hint,
                    "repository": entry.repository,
                    "file_count": entry.file_count,
                    "accessible": entry.accessible,
                    "error": entry.error,
                    "last_success_ts": entry.last_success_ts,
                    "last_error_ts": entry.last_error_ts,
                    "last_error_msg": entry.last_error_msg,
                    "reconciled_files": entry.reconciled_files,
                    "changed_files": entry.changed_files,
                }
                for entry in self.roots
            ],
        }


def discovery_config_from_dict(auto_memory: dict[str, Any]) -> DiscoveryConfig:
    raw = auto_memory.get("discovery", {})
    if not isinstance(raw, dict):
        raw = {}
    include = raw.get("include_patterns", raw.get("include", []))
    exclude = raw.get("exclude_patterns", raw.get("exclude", []))
    return DiscoveryConfig(
        enabled=bool(raw.get("enabled", True)),
        claude_projects=bool(raw.get("claude_projects", True)),
        include_patterns=tuple(str(item) for item in include) if include else (),
        exclude_patterns=tuple(str(item) for item in exclude) if exclude else (),
        read_claude_settings=bool(raw.get("read_claude_settings", True)),
    )


def decode_claude_project_slug(slug: str) -> str | None:
    """Best-effort decode of Claude's encoded project directory name."""
    if not slug or slug in {".", ".."}:
        return None
    if slug.startswith("-"):
        decoded = "/" + slug[1:].replace("-", "/")
        return decoded if decoded != "/" else None
    return None


def _read_settings_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _expand_memory_directory(value: str, *, base_dir: Path | None = None) -> Path | None:
    text = value.strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = (base_dir / path).resolve()
    else:
        path = path.resolve()
    return path


def _auto_memory_directory_from_settings(
    settings_path: Path, *, base_dir: Path | None = None
) -> Path | None:
    payload = _read_settings_json(settings_path)
    if payload is None:
        return None
    raw = payload.get("autoMemoryDirectory")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return _expand_memory_directory(raw, base_dir=base_dir)


def _root_passes_filters(root: MemoryRoot, config: DiscoveryConfig) -> bool:
    candidates = [
        root.claude_project_slug or "",
        root.encoded_path_hint or "",
        str(root.memory_dir),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if config.exclude_patterns and any(
            fnmatch.fnmatchcase(candidate, pattern) for pattern in config.exclude_patterns
        ):
            return False
    if not config.include_patterns:
        return True
    return any(
        fnmatch.fnmatchcase(candidate, pattern)
        for candidate in candidates
        if candidate
        for pattern in config.include_patterns
    )


def discover_default_claude_project_roots(home: Path) -> list[MemoryRoot]:
    """Discover ``~/.claude/projects/<slug>/memory`` directories."""
    projects_root = home / CLAUDE_PROJECTS_DIR
    if not projects_root.is_dir():
        return []
    roots: list[MemoryRoot] = []
    for project_dir in sorted(projects_root.iterdir()):
        if not project_dir.is_dir():
            continue
        slug = project_dir.name
        memory_dir = project_dir / MEMORY_DIR_NAME
        if not memory_dir.is_dir():
            continue
        roots.append(
            MemoryRoot(
                memory_dir=memory_dir.resolve(),
                origin="claude-default",
                claude_project_slug=slug,
                encoded_path_hint=decode_claude_project_slug(slug),
            )
        )
    return roots


def discover_settings_memory_roots(home: Path) -> list[MemoryRoot]:
    """Read trusted Claude settings scopes for ``autoMemoryDirectory`` overrides."""
    roots: list[MemoryRoot] = []
    user_settings = home / ".claude" / "settings.json"
    custom = _auto_memory_directory_from_settings(user_settings)
    if custom is not None:
        roots.append(
            MemoryRoot(
                memory_dir=custom,
                origin="claude-settings-user",
                settings_path=str(user_settings),
            )
        )

    projects_root = home / CLAUDE_PROJECTS_DIR
    if projects_root.is_dir():
        for project_dir in sorted(projects_root.iterdir()):
            if not project_dir.is_dir():
                continue
            for name in TRUSTED_SETTINGS_FILES:
                settings_path = project_dir / name
                if not settings_path.is_file():
                    continue
                custom = _auto_memory_directory_from_settings(
                    settings_path, base_dir=project_dir
                )
                if custom is None:
                    continue
                roots.append(
                    MemoryRoot(
                        memory_dir=custom,
                        origin="claude-settings-project",
                        claude_project_slug=project_dir.name,
                        encoded_path_hint=decode_claude_project_slug(project_dir.name),
                        settings_path=str(settings_path),
                    )
                )
    return roots


def deduplicate_memory_roots(roots: list[MemoryRoot]) -> list[MemoryRoot]:
    """Keep one root per resolved memory directory (first wins)."""
    seen: dict[str, MemoryRoot] = {}
    for root in roots:
        key = root.inventory_key
        if key not in seen:
            seen[key] = root
    return list(seen.values())


def discover_memory_roots(
    config: DiscoveryConfig,
    *,
    home: Path | None = None,
) -> list[MemoryRoot]:
    """Return deduplicated memory roots from Claude defaults and settings."""
    if not config.enabled:
        return []
    home_dir = (home or Path.home()).expanduser()
    discovered: list[MemoryRoot] = []
    if config.claude_projects:
        discovered.extend(discover_default_claude_project_roots(home_dir))
    if config.read_claude_settings:
        discovered.extend(discover_settings_memory_roots(home_dir))
    deduped = deduplicate_memory_roots(discovered)
    return [root for root in deduped if _root_passes_filters(root, config)]


def list_memory_markdown_files(memory_dir: Path) -> list[Path]:
    """List Markdown files directly inside a memory directory."""
    if not memory_dir.is_dir():
        return []
    files = [path.resolve() for path in sorted(memory_dir.glob("*.md")) if path.is_file()]
    return files


def repository_for_memory_root(root: MemoryRoot) -> str:
    """Derive repository identity from MEMORY.md or the memory directory."""
    memory_md = root.memory_dir / "MEMORY.md"
    if memory_md.is_file():
        return derive_repository_identity(memory_md)
    return derive_repository_identity(root.memory_dir)


def memory_roots_to_file_sources(roots: list[MemoryRoot]) -> list[dict[str, Any]]:
    """Expand memory roots into per-file ingest sources."""
    sources: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for root in roots:
        if not root.accessible:
            continue
        try:
            repository = repository_for_memory_root(root)
            for path in list_memory_markdown_files(root.memory_dir):
                resolved = str(path)
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)
                sources.append(
                    {
                        "path": resolved,
                        "repository": repository,
                        "logical_path": path.name,
                        "memory_dir": str(root.memory_dir),
                        "origin": root.origin,
                        "claude_project_slug": root.claude_project_slug,
                    }
                )
        except OSError:
            continue
    return sources


def merge_configured_sources(
    explicit: list[dict[str, Any]],
    discovered: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge explicit operator sources with discovered file sources (explicit wins)."""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in explicit + discovered:
        path = str(Path(source["path"]).expanduser().resolve())
        if path in seen:
            continue
        seen.add(path)
        item = dict(source)
        item["path"] = path
        merged.append(item)
    return merged


def build_discovery_inventory(
    roots: list[MemoryRoot],
    *,
    file_sources: list[dict[str, Any]],
    conn: sqlite3.Connection | None = None,
    now_ms: int | None = None,
) -> DiscoveryInventory:
    """Compose a dry-run or post-reconcile inventory snapshot."""
    observed_at = now_ms if now_ms is not None else int(time.time() * 1000)
    entries: list[InventoryEntry] = []
    files_by_root: dict[str, list[dict[str, Any]]] = {}
    for source in file_sources:
        memory_dir = source.get("memory_dir")
        if memory_dir:
            files_by_root.setdefault(str(Path(memory_dir).resolve()), []).append(source)

    health_by_repo: dict[str, tuple[int | None, int | None, str | None]] = {}
    if conn is not None:
        for row in conn.execute(
            "SELECT repository, MAX(observed_at), MAX(updated_at) "
            "FROM memory_documents WHERE source_kind = ? AND state != 'tombstoned' "
            "GROUP BY repository",
            (SOURCE_KIND,),
        ).fetchall():
            health_by_repo[str(row[0])] = (int(row[1]), None, None)

    for root in roots:
        memory_dir = str(root.memory_dir)
        repository: str | None = None
        file_count = 0
        error = root.error
        accessible = root.accessible
        if accessible:
            try:
                files = list_memory_markdown_files(root.memory_dir)
                file_count = len(files)
                if files:
                    repository = repository_for_memory_root(root)
            except OSError as exc:
                accessible = False
                error = str(exc)
        last_success_ts, last_error_ts, last_error_msg = (None, None, None)
        if repository and repository in health_by_repo:
            last_success_ts = health_by_repo[repository][0]
        entries.append(
            InventoryEntry(
                memory_dir=memory_dir,
                origin=root.origin,
                claude_project_slug=root.claude_project_slug,
                encoded_path_hint=root.encoded_path_hint,
                repository=repository,
                file_count=file_count,
                accessible=accessible,
                error=error,
                last_success_ts=last_success_ts,
                last_error_ts=last_error_ts,
                last_error_msg=last_error_msg,
                reconciled_files=len(files_by_root.get(root.inventory_key, [])),
            )
        )
    return DiscoveryInventory(
        discovered_at_ms=observed_at,
        roots=entries,
        file_sources=len(file_sources),
    )
