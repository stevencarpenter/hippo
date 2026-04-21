from hippo_brain.bench.gates import check_schema_validity


def test_schema_validity_passes_valid_shell_payload():
    raw = (
        '{"summary": "x", "intent": "verify", "outcome": "success",'
        ' "entities": {"projects": [], "tools": [], "files": [],'
        ' "services": [], "errors": []}}'
    )
    r = check_schema_validity(raw, "shell")
    assert r.passed
    assert r.parsed is not None
    assert r.errors == []


def test_schema_validity_fails_unparseable():
    r = check_schema_validity("not { json", "shell")
    assert not r.passed
    assert r.parsed is None
    assert any("parse" in e.lower() or "json" in e.lower() for e in r.errors)


def test_schema_validity_fails_missing_field():
    r = check_schema_validity('{"summary": "x"}', "shell")
    assert not r.passed
    assert r.parsed is not None
    assert any("required" in e for e in r.errors)


def test_schema_validity_strips_fence_blocks():
    fenced = (
        "```json\n"
        '{"summary": "x", "intent": "y", "outcome": "success",'
        ' "entities": {"projects": [], "tools": [], "files": [],'
        ' "services": [], "errors": []}}\n'
        "```"
    )
    r = check_schema_validity(fenced, "shell")
    assert r.passed, r.errors
