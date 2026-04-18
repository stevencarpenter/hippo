"""Entity canonicalization for hippo enrichment pipeline.

Canonical form rules:
- Expand ~ in values
- Lowercase + strip surrounding whitespace
- Strip trailing slashes
- Collapse internal whitespace runs to single space
- For path-like types (file, directory, path): strip known project root prefixes
  so the same file under different worktree paths resolves to the same canonical.

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
