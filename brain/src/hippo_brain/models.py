from dataclasses import dataclass, field


@dataclass
class EnrichmentResult:
    summary: str
    intent: str
    outcome: str
    entities: dict = field(
        default_factory=lambda: {
            "projects": [],
            "tools": [],
            "files": [],
            "services": [],
            "errors": [],
        }
    )
    relationships: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    embed_text: str = ""


_VALID_OUTCOMES = {"success", "partial", "failure", "unknown"}
_ENTITY_KEYS = ("projects", "tools", "files", "services", "errors")


def validate_enrichment_data(data: dict) -> EnrichmentResult:
    """Validate raw enrichment JSON and return a typed EnrichmentResult.

    Raises ValueError on structural violations so the caller can handle
    bad LLM output without letting invalid data reach the database.
    """
    # Required top-level string fields
    for field_name in ("summary", "intent", "embed_text"):
        value = data.get(field_name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"required string field '{field_name}' is missing or empty")

    # Outcome must be one of the allowed values
    outcome = data.get("outcome")
    if outcome not in _VALID_OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(_VALID_OUTCOMES)}, got {outcome!r}")

    # Entities must be a dict (default to empty dict if missing)
    raw_entities = data.get("entities", {})
    if not isinstance(raw_entities, dict):
        raise ValueError(f"entities must be a dict, got {type(raw_entities).__name__}")

    # Filter each entity list to contain only strings
    entities: dict[str, list[str]] = {}
    for key in _ENTITY_KEYS:
        raw_list = raw_entities.get(key, [])
        if not isinstance(raw_list, list):
            raw_list = []
        entities[key] = [item for item in raw_list if isinstance(item, str)]

    # Relationships must be a list (default to empty list if missing or wrong type)
    relationships = data.get("relationships", [])
    if not isinstance(relationships, list):
        relationships = []

    # Tags must be a list of strings (skip non-string items)
    raw_tags = data.get("tags", [])
    if not isinstance(raw_tags, list):
        raw_tags = []
    tags = [t for t in raw_tags if isinstance(t, str)]

    return EnrichmentResult(
        summary=data["summary"],
        intent=data["intent"],
        outcome=outcome,
        entities=entities,
        relationships=relationships,
        tags=tags,
        embed_text=data["embed_text"],
    )


ENRICHMENT_SCHEMA = {
    "type": "object",
    "required": ["summary", "intent", "outcome", "entities", "tags", "embed_text"],
    "properties": {
        "summary": {"type": "string"},
        "intent": {"type": "string"},
        "outcome": {"type": "string", "enum": ["success", "partial", "failure", "unknown"]},
        "entities": {
            "type": "object",
            "properties": {
                "projects": {"type": "array", "items": {"type": "string"}},
                "tools": {"type": "array", "items": {"type": "string"}},
                "files": {"type": "array", "items": {"type": "string"}},
                "services": {"type": "array", "items": {"type": "string"}},
                "errors": {"type": "array", "items": {"type": "string"}},
            },
        },
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "relationship": {"type": "string"},
                },
            },
        },
        "tags": {"type": "array", "items": {"type": "string"}},
        "embed_text": {"type": "string"},
    },
}

ENRICHMENT_FIXTURES = [
    {
        "input": {
            "command": "cargo test -p hippo-core",
            "exit_code": 0,
            "duration_ms": 3500,
            "cwd": "/Users/dev/projects/hippo",
            "git_branch": "main",
        },
        "expected": EnrichmentResult(
            summary="Ran Rust unit tests for hippo-core crate, all tests passed.",
            intent="testing",
            outcome="success",
            entities={
                "projects": ["hippo"],
                "tools": ["cargo", "rustc"],
                "files": [],
                "services": [],
                "errors": [],
            },
            relationships=[
                {"from": "cargo", "to": "hippo-core", "relationship": "tests"},
            ],
            tags=["rust", "testing", "hippo-core"],
            embed_text="cargo test hippo-core: all tests passed in hippo project on main branch",
        ),
    },
]
