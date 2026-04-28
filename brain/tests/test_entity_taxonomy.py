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

This test discovers every top-level `*_ENTITY_TYPE_MAP` constant in the
`hippo_brain` package and asserts every value belongs to exactly one of the
two classification tuples. It runs at unit-test time so the failure surfaces
on the PR that introduces the new type, not on the PR that wonders why a
type isn't appearing in RAG output.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import hippo_brain
from hippo_brain.enrichment import (
    IDENTIFIER_ENTITY_TYPES,
    NON_IDENTIFIER_ENTITY_TYPES,
)


def _discover_entity_type_maps() -> dict[str, dict[str, str]]:
    """Walk every module under hippo_brain and return module-level
    `*_ENTITY_TYPE_MAP` dicts, keyed by f"{module}.{name}"."""
    discovered: dict[str, dict[str, str]] = {}
    for module_info in pkgutil.walk_packages(
        hippo_brain.__path__, prefix=f"{hippo_brain.__name__}."
    ):
        module = importlib.import_module(module_info.name)
        for name, value in inspect.getmembers(module):
            if not name.endswith("_ENTITY_TYPE_MAP"):
                continue
            if not isinstance(value, dict):
                continue
            discovered[f"{module_info.name}.{name}"] = value
    return discovered


def test_at_least_shell_entity_type_map_is_discovered():
    """Sanity: the discovery walk must find SHELL_ENTITY_TYPE_MAP. If this
    test ever stops finding it, the introspection has broken (e.g., the
    package layout changed) and the rest of this file gives a false-pass."""
    maps = _discover_entity_type_maps()
    shell_keys = [k for k in maps if k.endswith(".SHELL_ENTITY_TYPE_MAP")]
    assert shell_keys, f"discovery walk did not find SHELL_ENTITY_TYPE_MAP; got keys={list(maps)}"


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
