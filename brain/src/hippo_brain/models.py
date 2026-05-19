import re
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
            "domains": [],
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
_ENTITY_KEYS = ("projects", "tools", "files", "services", "errors", "env_vars", "domains")
_ENV_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", re.IGNORECASE)
_FILE_SUFFIX_RE = re.compile(r"\.[A-Za-z0-9]{1,12}(?::\d+)?$")


def _empty_entities() -> dict[str, list[str]]:
    return {key: [] for key in _ENTITY_KEYS}


def _append_unique(entities: dict[str, list[str]], key: str, value: str) -> None:
    value = value.strip()
    if value and value not in entities[key]:
        entities[key].append(value)


def _entity_key_from_type(raw_type) -> str | None:
    if not isinstance(raw_type, str):
        return None
    normalized = raw_type.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "project": "projects",
        "projects": "projects",
        "tool": "tools",
        "tools": "tools",
        "cli": "tools",
        "file": "files",
        "files": "files",
        "path": "files",
        "paths": "files",
        "service": "services",
        "services": "services",
        "api": "services",
        "database": "services",
        "error": "errors",
        "errors": "errors",
        "exception": "errors",
        "env_var": "env_vars",
        "env_vars": "env_vars",
        "environment_variable": "env_vars",
        "domain": "domains",
        "domains": "domains",
        "hostname": "domains",
    }
    return aliases.get(normalized)


def _infer_entity_key(value: str) -> str:
    lower = value.lower()
    if _ENV_VAR_RE.match(value):
        return "env_vars"
    if "/" in value or value.startswith(".") or _FILE_SUFFIX_RE.search(value):
        return "files"
    if "error" in lower or "exception" in lower or "failed" in lower or "traceback" in lower:
        return "errors"
    if "://" not in value and " " not in value and _DOMAIN_RE.match(value):
        return "domains"
    return "tools"


def _coerce_entity_list(raw_entities: list) -> dict[str, list[str]]:
    """Recover common local-LLM output: `entities` as a flat list."""
    entities = _empty_entities()
    for item in raw_entities:
        if isinstance(item, str):
            value = item.strip()
            if value:
                _append_unique(entities, _infer_entity_key(value), value)
            continue
        if isinstance(item, dict):
            name = item.get("name") or item.get("value") or item.get("entity") or item.get("text")
            if not isinstance(name, str) or not name.strip():
                continue
            key = _entity_key_from_type(
                item.get("type") or item.get("category") or item.get("kind")
            )
            _append_unique(entities, key or _infer_entity_key(name), name)
    return entities


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

    # Entities should be a dict, but local LLMs sometimes return a flat list.
    raw_entities = data.get("entities", {})
    if isinstance(raw_entities, list):
        raw_entities = _coerce_entity_list(raw_entities)
    elif not isinstance(raw_entities, dict):
        raise ValueError(f"entities must be a dict or list, got {type(raw_entities).__name__}")

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
