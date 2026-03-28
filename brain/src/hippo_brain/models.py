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
