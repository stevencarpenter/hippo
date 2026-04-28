"""Entity canonicalization for hippo enrichment pipeline.

Canonical form rules:
- Expand ~ in values
- Lowercase + strip surrounding whitespace
- Strip trailing slashes
- Collapse internal whitespace runs to single space
- For path-like types (file, directory, path):
    1. Strip Claude Code parallel-agent worktree segments
       (`.claude/worktrees/<anything>/`). These are ephemeral worktrees
       created by the Task/TeamCreate tools that get deleted after the
       agent's work merges or is discarded; entity rows pointing inside
       them rot. Stripping them collapses N copies of the same logical
       file (one per agent run) to a single canonical row.
    2. Strip known project root prefixes so the same file under different
       project root paths (e.g. `hippo` vs. `hippo-postgres`) resolves to
       the same canonical.

Project root precedence (highest to lowest):
1. Explicit project_roots= passed to canonicalize() — useful in tests and scripts
2. HIPPO_PROJECT_ROOTS env var (colon-separated absolute paths)
3. [entities] project_roots in ~/.config/hippo/config.toml
4. Auto-detected: immediate subdirectories of ~/projects/ that contain .git

If all sources are empty a WARNING is logged once per process and path
canonicalization falls through without prefix stripping.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from functools import lru_cache
from pathlib import Path

_PATH_TYPES = frozenset({"file", "directory", "path"})
_logger = logging.getLogger(__name__)

# Matches `/.claude/worktrees/<single-segment>/` or the same pattern at the
# start of a relative path. `<single-segment>` is any non-slash directory name
# — Claude Code worktrees are named with varied schemes (`agent-XXXX`,
# `feat-XXXX`, adjective-noun-hex, etc.), so we cannot rely on a fixed prefix.
_WORKTREE_SEGMENT_RE = re.compile(r"(^|/)\.claude/worktrees/[^/]+(/|$)")


def _replace_worktree_match(match: re.Match[str]) -> str:
    """Preserve path separators while dropping one worktree directory segment.

    group(1) is the leading separator (or start-of-string), and group(2) is the
    separator after the worktree name (or end-of-string). When the worktree is a
    leaf path segment, we drop the whole match; otherwise we keep the leading
    separator so the parent path still joins cleanly to the remaining suffix.
    """
    return "" if match.group(2) == "" else match.group(1)


def _load_config_roots() -> list[str]:
    """Load [entities] project_roots from ~/.config/hippo/config.toml."""
    config_path = Path.home() / ".config" / "hippo" / "config.toml"
    try:
        if not config_path.exists():
            return []
        with config_path.open("rb") as f:
            cfg = tomllib.load(f)
        roots = cfg.get("entities", {}).get("project_roots", [])
        return [str(r) for r in roots if str(r).strip()]
    except Exception:
        return []


def _auto_detect_roots() -> list[str]:
    """Scan ~/projects/*/.git and return parent directories as project roots."""
    projects_dir = Path.home() / "projects"
    if not projects_dir.is_dir():
        return []
    return sorted(
        str(candidate)
        for candidate in projects_dir.iterdir()
        if candidate.is_dir() and (candidate / ".git").exists()
    )


@lru_cache(maxsize=1)
def _cached_fallback_roots() -> tuple[str, ...]:
    """Config + auto-detect fallback, cached after first call.

    Call _cached_fallback_roots.cache_clear() in tests that exercise this path.
    """
    config_roots = _load_config_roots()
    if config_roots:
        return tuple(r.rstrip("/") for r in config_roots)
    auto_roots = _auto_detect_roots()
    if auto_roots:
        return tuple(r.rstrip("/") for r in auto_roots)
    _logger.warning(
        "HIPPO_PROJECT_ROOTS is not set, no [entities].project_roots in config.toml, "
        "and no git repos found under ~/projects/ — worktree-prefix canonicalization inactive"
    )
    return ()


def _resolve_project_roots(override: list[str] | None) -> list[str]:
    # Precedence: explicit override > HIPPO_PROJECT_ROOTS env var > config.toml > auto-detect
    if override is not None:
        return [r.rstrip("/") for r in override if r.strip()]
    env = os.environ.get("HIPPO_PROJECT_ROOTS", "")
    if env:
        return [r.rstrip("/") for r in env.split(":") if r.strip()]
    return list(_cached_fallback_roots())


def is_path_type(entity_type: str) -> bool:
    """Return True for entity types whose values are filesystem paths.

    Used by callers that want to apply path-only transforms (e.g.
    `strip_worktree_prefix`) without rewriting non-path values like error
    messages or concept strings, which may legitimately embed path-like
    substrings inside diagnostic text.
    """
    return entity_type in _PATH_TYPES


def strip_worktree_prefix(path: str) -> str:
    """Strip every `.claude/worktrees/<X>/` segment from `path`.

    Worktree subdirectory names vary (`agent-*`, `feat-*`, adjective-noun-hex
    pairs from Claude Code's namer, etc.), so the stripping is segment-name
    agnostic — anything between `.claude/worktrees/` and the next `/` (or the
    end of the path) is removed.
    """
    stripped = path
    max_passes = path.count(".claude/worktrees/") + 1
    for _ in range(max_passes):
        next_value = _WORKTREE_SEGMENT_RE.sub(_replace_worktree_match, stripped)
        if next_value == stripped:
            return next_value
        stripped = next_value
    return stripped


def canonicalize(
    entity_type: str,
    value: str,
    project_roots: list[str] | None = None,
) -> str:
    """Return the canonical form of an entity value.

    project_roots overrides HIPPO_PROJECT_ROOTS when provided.
    """
    v = os.path.expanduser(value)
    v = v.strip().lower()
    v = v.rstrip("/")
    v = re.sub(r"\s+", " ", v)

    if entity_type in _PATH_TYPES:
        # Strip worktree segments first so a path like
        #   /users/carpenter/projects/hippo/.claude/worktrees/agent-XX/src/foo.rs
        # collapses to
        #   /users/carpenter/projects/hippo/src/foo.rs
        # before the project-root strip turns it into `src/foo.rs`.
        v = strip_worktree_prefix(v)

        roots = _resolve_project_roots(project_roots)
        for root in roots:
            normalized = os.path.expanduser(root).lower().rstrip("/")
            if v.startswith(normalized + "/"):
                v = v[len(normalized) + 1 :]
                break
            if v == normalized:
                v = Path(normalized).name  # e.g. "hippo-postgres" rather than ""
                break

    return v
