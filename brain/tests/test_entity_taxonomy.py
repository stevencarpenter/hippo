"""Taxonomy guard: every entity-type-map value must be classified.

The RAG `Entities:` line (rag.py::_render_entities_line) and retrieval-layer
SQL filter (retrieval.py::_fetch_details) both rely on
`enrichment.IDENTIFIER_ENTITY_TYPES` as the single source of truth for which
entity types carry user-bindable identifiers. Concepts (errors and other
prose-like values) live in `NON_IDENTIFIER_ENTITY_TYPES` and are deliberately
omitted from the line.

Adding a new value to any `*_ENTITY_TYPE_MAP` without classifying it as
identifier-bearing-or-not creates a silent failure: the SQL filter drops it
from retrieval results and the renderer never has a chance to surface it.

This test imports the (small, explicit) set of modules known to define
`*_ENTITY_TYPE_MAP` constants and asserts every value belongs to exactly
one of the two classification tuples. Walking the whole package via
`pkgutil.walk_packages` would execute every module's import-time code
(e.g. `server.py` calls `logging.basicConfig(...)` at import) — that's
expensive and flaky for a unit test. Instead, the allowlist below is the
contract: a future maintainer who adds a new map module must extend
`_MODULES_WITH_ENTITY_MAPS`. That extension is the same diff that adds
the map, so the cost is proximate and obvious.
"""

from __future__ import annotations

import importlib
import inspect

from hippo_brain.enrichment import (
    IDENTIFIER_ENTITY_TYPES,
    NON_IDENTIFIER_ENTITY_TYPES,
)

# Modules expected to define module-level `*_ENTITY_TYPE_MAP` constants.
# Listed explicitly to avoid importing every module under `hippo_brain`
# (which would trigger import-time side effects). Extend when a new map
# module is added — the taxonomy assertions below run only against the
# modules listed here.
_MODULES_WITH_ENTITY_MAPS: tuple[str, ...] = (
    "hippo_brain.enrichment",
    "hippo_brain.browser_enrichment",
)


def _discover_entity_type_maps() -> dict[str, dict[str, str]]:
    """Return module-level `*_ENTITY_TYPE_MAP` dicts from the allowlisted
    modules, keyed by f"{module}.{name}"."""
    discovered: dict[str, dict[str, str]] = {}
    for module_name in _MODULES_WITH_ENTITY_MAPS:
        module = importlib.import_module(module_name)
        for name, value in inspect.getmembers(module):
            if not name.endswith("_ENTITY_TYPE_MAP"):
                continue
            if not isinstance(value, dict):
                continue
            discovered[f"{module_name}.{name}"] = value
    return discovered


def test_shell_and_browser_entity_type_maps_are_discovered():
    """Sanity: the allowlist must surface both SHELL_ENTITY_TYPE_MAP and
    BROWSER_ENTITY_TYPE_MAP. If either ever stops being a top-level
    constant (e.g. someone reverts BROWSER back to an inline dict literal
    in `write_browser_knowledge_node`), the rest of this file gives a
    false-pass on browser-specific types like `domain`."""
    maps = _discover_entity_type_maps()
    shell_keys = [k for k in maps if k.endswith(".SHELL_ENTITY_TYPE_MAP")]
    browser_keys = [k for k in maps if k.endswith(".BROWSER_ENTITY_TYPE_MAP")]
    assert shell_keys, f"allowlist did not surface SHELL_ENTITY_TYPE_MAP; got keys={list(maps)}"
    assert browser_keys, (
        f"allowlist did not surface BROWSER_ENTITY_TYPE_MAP — extracting it as a "
        f"module-level constant in browser_enrichment.py is what lets this guard cover "
        f"browser-specific types like `domain`. got keys={list(maps)}"
    )


def test_every_entity_type_map_value_is_classified():
    """Every value in every discovered `*_ENTITY_TYPE_MAP` must appear in
    `IDENTIFIER_ENTITY_TYPES + NON_IDENTIFIER_ENTITY_TYPES`. The next type
    addition fails this test if neither tuple is updated."""
    classified = set(IDENTIFIER_ENTITY_TYPES) | set(NON_IDENTIFIER_ENTITY_TYPES)
    unclassified: list[tuple[str, str]] = []
    for map_name, map_dict in _discover_entity_type_maps().items():
        for value in map_dict.values():
            if value not in classified:
                unclassified.append((map_name, value))
    assert not unclassified, (
        "Unclassified entity-type values found. Add each to either "
        "IDENTIFIER_ENTITY_TYPES (bindable identifier) or "
        "NON_IDENTIFIER_ENTITY_TYPES (prose / not bindable) in "
        f"hippo_brain.enrichment. Offenders: {unclassified}"
    )


def test_classification_tuples_are_disjoint():
    """A value in both tuples is a contradiction and means the renderer's
    behavior depends on tuple ordering, which would be bad."""
    overlap = set(IDENTIFIER_ENTITY_TYPES) & set(NON_IDENTIFIER_ENTITY_TYPES)
    assert not overlap, f"types appear in both classification tuples: {overlap}"
