"""Regression checks for agent source trust contracts (SNUG-122)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONTRACTS_JSON = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "hippo_brain"
    / "_fixtures"
    / "source_trust_contracts.json"
)
_CONTRACTS_MD = _REPO_ROOT / "docs" / "capture" / "source-trust-contracts.md"

_REQUIRED_FAMILIES = frozenset(
    {
        "shell",
        "claude-tool",
        "claude",
        "codex",
        "cursor",
        "opencode",
        "browser",
        "workflow",
        "claude-auto-memory",
        "source_health",
        "watchdog-probe",
    }
)

_REQUIRED_MD_HEADINGS = (
    "## Shell commands",
    "## Claude tool events",
    "## Agentic sessions",
    "## Browser visits",
    "## GitHub CI",
    "## Claude auto-memory",
    "## `source_health`",
    "## Watchdog and probe metadata",
)

_MIN_CITATION_EXAMPLES = 4


@pytest.fixture
def contracts_data() -> dict:
    return json.loads(_CONTRACTS_JSON.read_text())


@pytest.fixture
def contracts_md() -> str:
    return _CONTRACTS_MD.read_text()


def test_contracts_json_covers_required_families(contracts_data: dict) -> None:
    ids = {f["id"] for f in contracts_data["families"]}
    missing = _REQUIRED_FAMILIES - ids
    assert not missing, f"missing contract families: {sorted(missing)}"


def test_contracts_json_has_citation_examples(contracts_data: dict) -> None:
    with_examples = [f for f in contracts_data["families"] if f.get("citation_example")]
    assert len(with_examples) >= _MIN_CITATION_EXAMPLES


def test_contracts_json_entries_have_core_fields(contracts_data: dict) -> None:
    for family in contracts_data["families"]:
        assert "id" in family
        assert "tables" in family and family["tables"]
        assert "identity_fields" in family


def test_contracts_md_exists_and_has_sections(contracts_md: str) -> None:
    for heading in _REQUIRED_MD_HEADINGS:
        assert heading in contracts_md, f"missing section: {heading}"


def test_contracts_md_links_mcp_and_eligibility(contracts_md: str) -> None:
    assert "mcp-reference.md" in contracts_md
    assert "retrieval-eligibility.md" in contracts_md
    assert "issues/114" in contracts_md


def test_mcp_reference_links_trust_contracts() -> None:
    mcp_ref = (_REPO_ROOT / "docs" / "mcp-reference.md").read_text()
    assert "source-trust-contracts.md" in mcp_ref
