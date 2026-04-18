"""Entity canonicalization for hippo enrichment pipeline.

Canonical form rules:
- Expand ~ in values
- Lowercase + strip surrounding whitespace
- Strip trailing slashes
- Collapse internal whitespace runs to single space
- For path-like types (file, directory, path): strip known project root prefixes
  so the same file under different worktree paths resolves to the same canonical.

Project roots are read from HIPPO_PROJECT_ROOTS (colon-separated absolute paths).
Pass project_roots= directly to canonicalize() to override the env var (useful in tests).
"""

from __future__ import annotations

import os
import re

_PATH_TYPES = frozenset({"file", "directory", "path"})


def _resolve_project_roots(override: list[str] | None) -> list[str]:
    if override is not None:
        return [r.rstrip("/") for r in override if r.strip()]
    env = os.environ.get("HIPPO_PROJECT_ROOTS", "")
    if env:
        return [r.rstrip("/") for r in env.split(":") if r.strip()]
    return []


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
                v = ""
                break

    return v
