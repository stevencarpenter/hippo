from hippo_brain.bench.schemas import SOURCE_SCHEMAS, validate_against_schema


def test_all_four_sources_present():
    assert set(SOURCE_SCHEMAS.keys()) == {"shell", "claude", "browser", "workflow"}


def test_shell_schema_accepts_canonical_payload():
    payload = {
        "summary": "Ran cargo test for hippo-core",
        "intent": "verify",
        "outcome": "success",
        "entities": {
            "projects": ["hippo"],
            "tools": ["cargo"],
            "files": [],
            "services": [],
            "errors": [],
        },
    }
    ok, errors = validate_against_schema(payload, "shell")
    assert ok, errors


def test_shell_schema_rejects_missing_summary():
    payload = {"intent": "verify", "outcome": "success", "entities": {}}
    ok, errors = validate_against_schema(payload, "shell")
    assert not ok
    assert any("summary" in e for e in errors)


def test_shell_schema_rejects_bad_outcome():
    payload = {
        "summary": "x",
        "intent": "y",
        "outcome": "maybe",
        "entities": {"projects": [], "tools": [], "files": [], "services": [], "errors": []},
    }
    ok, errors = validate_against_schema(payload, "shell")
    assert not ok
    assert any("outcome" in e for e in errors)


def test_claude_schema_accepts_canonical_payload():
    payload = {
        "summary": "Session about enrichment bugfix",
        "entities": {
            "projects": ["hippo"],
            "topics": ["enrichment"],
            "files": ["brain/src/hippo_brain/enrichment.py"],
            "decisions": [],
            "errors": [],
        },
    }
    ok, errors = validate_against_schema(payload, "claude")
    assert ok, errors


def test_browser_schema_accepts_canonical_payload():
    payload = {
        "summary": "Read Rust docs on trait objects",
        "entities": {
            "topics": ["rust", "trait objects"],
            "urls": [],
            "projects": [],
        },
    }
    ok, errors = validate_against_schema(payload, "browser")
    assert ok, errors


def test_workflow_schema_accepts_canonical_payload():
    payload = {
        "summary": "CI run for commit abc123: tests passed",
        "entities": {
            "projects": ["hippo"],
            "jobs": ["test"],
            "errors": [],
        },
    }
    ok, errors = validate_against_schema(payload, "workflow")
    assert ok, errors


def test_rejects_non_dict():
    ok, errors = validate_against_schema("not a dict", "shell")
    assert not ok


def test_rejects_non_string_summary():
    payload = {"summary": 42, "intent": "x", "outcome": "success", "entities": {}}
    ok, errors = validate_against_schema(payload, "shell")
    assert not ok
