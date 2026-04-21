"""Per-source enrichment JSON schemas for Tier 0 structural validation.

Kept independent from hippo_brain.models (which is tuned to the live
enrichment pipeline's needs) so bench can evolve gate thresholds
without touching production code paths.
"""

from __future__ import annotations

from dataclasses import dataclass

_VALID_OUTCOMES = {"success", "partial", "failure", "unknown"}


@dataclass(frozen=True)
class SourceSchema:
    required_top_level: tuple[str, ...]
    entity_categories: tuple[str, ...]
    constrained_enums: dict[str, frozenset[str]]  # field name -> allowed values
    summary_min_chars: int = 1
    summary_max_chars: int = 2000


SOURCE_SCHEMAS: dict[str, SourceSchema] = {
    "shell": SourceSchema(
        required_top_level=("summary", "intent", "outcome", "entities"),
        entity_categories=("projects", "tools", "files", "services", "errors"),
        constrained_enums={"outcome": frozenset(_VALID_OUTCOMES)},
    ),
    "claude": SourceSchema(
        required_top_level=("summary", "entities"),
        entity_categories=("projects", "topics", "files", "decisions", "errors"),
        constrained_enums={},
    ),
    "browser": SourceSchema(
        required_top_level=("summary", "entities"),
        entity_categories=("topics", "urls", "projects"),
        constrained_enums={},
    ),
    "workflow": SourceSchema(
        required_top_level=("summary", "entities"),
        entity_categories=("projects", "jobs", "errors"),
        constrained_enums={},
    ),
}


def validate_against_schema(payload: object, source: str) -> tuple[bool, list[str]]:
    """Return (passed, errors). Never raises."""
    errors: list[str] = []
    schema = SOURCE_SCHEMAS.get(source)
    if schema is None:
        return False, [f"unknown source {source!r}"]

    if not isinstance(payload, dict):
        return False, [f"expected dict, got {type(payload).__name__}"]

    for field in schema.required_top_level:
        if field not in payload:
            errors.append(f"missing required field {field!r}")

    summary = payload.get("summary")
    if summary is not None:
        if not isinstance(summary, str):
            errors.append(f"summary must be a string, got {type(summary).__name__}")
        else:
            n = len(summary)
            if n < schema.summary_min_chars:
                errors.append(f"summary too short ({n} chars)")
            if n > schema.summary_max_chars:
                errors.append(f"summary too long ({n} chars)")

    entities = payload.get("entities")
    if entities is not None:
        if not isinstance(entities, dict):
            errors.append(f"entities must be a dict, got {type(entities).__name__}")
        else:
            for cat in schema.entity_categories:
                v = entities.get(cat, [])
                if not isinstance(v, list):
                    errors.append(f"entities.{cat} must be a list")
                    continue
                for i, item in enumerate(v):
                    if not isinstance(item, str):
                        errors.append(f"entities.{cat}[{i}] must be a string")

    for field_name, allowed in schema.constrained_enums.items():
        if field_name in payload and payload[field_name] not in allowed:
            errors.append(
                f"{field_name} must be one of {sorted(allowed)}, got {payload[field_name]!r}"
            )

    return (not errors), errors
