"""Render knowledge nodes and entities into Obsidian-compatible markdown.

This module is pure: it takes already-fetched rows and returns strings. All
SQLite access lives in vault_export.py. Filenames derive from a STABLE source
key, never the node uuid (re-enrichment re-mints uuids — see the design spec).
"""

from __future__ import annotations

import re

# Source priority when a node links several source types. Highest first.
_SOURCE_PRIORITY = ("agentic", "workflow", "browser", "shell", "lesson")

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase, replace any run of non-alphanumerics with '-', trim dashes."""
    s = _SLUG_STRIP.sub("-", text.strip().lower()).strip("-")
    return s or "unnamed"


def entity_slug(entity_type: str, name: str, canonical: str | None, entity_id: int) -> str:
    """Stable slug for an entity file: canonical, else name, else id."""
    base = (canonical or "").strip() or (name or "").strip()
    if not base:
        return f"entity-{entity_id}"
    return slugify(base)


def node_source_key(links: dict, node_type: str, uuid: str) -> str:
    """Derive a stable, content-independent filename slug for a knowledge node.

    ``links`` maps source kind -> list of stable identifiers:
      agentic  -> list of (harness, session_id, segment_index)
      workflow -> list of run_id (int)
      browser  -> list of browser_event_id (int)
      shell    -> list of event_id (int)
      lesson   -> list of lesson_id (int)
    """
    for kind in _SOURCE_PRIORITY:
        items = links.get(kind)
        if not items:
            continue
        if kind == "agentic":
            harness, session_id, segment = min(items, key=lambda t: (t[1], t[2]))
            base = f"{harness}-{session_id}-{segment}"
        else:
            prefix = {"workflow": "wf", "browser": "web", "shell": "evt", "lesson": "lesson"}[kind]
            base = f"{prefix}-{min(items)}"
        # change_outcome (CI) nodes can co-link the same agentic session as an
        # observation node; discriminate so they never collide on one filename.
        if node_type == "change_outcome":
            base += "-co"
        return base
    return f"node-{uuid}"
