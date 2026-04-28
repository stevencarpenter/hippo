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
            "env_vars": [],
        }
    )
    tags: list = field(default_factory=list)
    embed_text: str = ""
    key_decisions: list = field(default_factory=list)
    problems_encountered: list = field(default_factory=list)
    # Structured "considered X, chose Y, reason Z" alternatives that were
    # weighed during the work session. Each entry is a dict with keys
    # "considered", "chosen", and "reason" (all str). Issue #98 F3.
    design_decisions: list = field(default_factory=list)


@dataclass
class CIAnnotation:
    level: str
    tool: str | None
    rule_id: str | None
    path: str | None
    start_line: int | None
    message: str


@dataclass
class CIJob:
    id: int
    name: str
    conclusion: str | None
    started_at: int | None
    completed_at: int | None
    annotations: list[CIAnnotation] = field(default_factory=list)


@dataclass
class CIStatus:
    run_id: int
    repo: str
    head_sha: str
    head_branch: str | None
    status: str
    conclusion: str | None
    started_at: int | None
    completed_at: int | None
    html_url: str
    jobs: list[CIJob] = field(default_factory=list)


@dataclass
class Lesson:
    id: int
    repo: str
    tool: str
    rule_id: str
    path_prefix: str
    summary: str
    fix_hint: str | None
    occurrences: int
    first_seen_at: int
    last_seen_at: int


_VALID_OUTCOMES = {"success", "partial", "failure", "unknown"}
_ENTITY_KEYS = ("projects", "tools", "files", "services", "errors", "env_vars")


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

    # Key decisions must be a list of strings (optional, default [])
    raw_decisions = data.get("key_decisions", [])
    if not isinstance(raw_decisions, list):
        raw_decisions = []
    key_decisions = [d for d in raw_decisions if isinstance(d, str)]

    # Problems encountered must be a list of strings (optional, default [])
    raw_problems = data.get("problems_encountered", [])
    if not isinstance(raw_problems, list):
        raw_problems = []
    problems_encountered = [p for p in raw_problems if isinstance(p, str)]

    # Design decisions: list of {considered, chosen, reason} objects (optional).
    # Skip entries that aren't dicts or lack the three string keys — a partial
    # entry is worse than no entry because a future agent can't trust it.
    raw_design = data.get("design_decisions", [])
    if not isinstance(raw_design, list):
        raw_design = []
    design_decisions: list[dict[str, str]] = []
    for entry in raw_design:
        if not isinstance(entry, dict):
            continue
        considered = entry.get("considered")
        chosen = entry.get("chosen")
        reason = entry.get("reason")
        if not (
            isinstance(considered, str)
            and considered
            and isinstance(chosen, str)
            and chosen
            and isinstance(reason, str)
            and reason
        ):
            continue
        design_decisions.append({"considered": considered, "chosen": chosen, "reason": reason})

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
        tags=tags,
        embed_text=data["embed_text"],
        key_decisions=key_decisions,
        problems_encountered=problems_encountered,
        design_decisions=design_decisions,
    )
