from hippo_brain.bench.gates import check_refusal_pathology, check_schema_validity


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


def test_refusal_detected_on_cannot_help():
    r = check_refusal_pathology(
        raw_output="I'm sorry, but I cannot help with that request.",
        input_text="ls -la",
        parsed=None,
    )
    assert r.refusal_detected
    assert "cannot" in " ".join(r.refusal_patterns_matched).lower()


def test_refusal_detected_on_as_an_ai():
    r = check_refusal_pathology(
        raw_output="As an AI, I cannot answer this.",
        input_text="cargo test",
        parsed=None,
    )
    assert r.refusal_detected


def test_no_refusal_on_normal_output():
    r = check_refusal_pathology(
        raw_output='{"summary": "Normal output"}',
        input_text="ls",
        parsed={"summary": "Normal output"},
    )
    assert not r.refusal_detected


def test_trivial_summary_flagged():
    r = check_refusal_pathology(
        raw_output='{"summary": "ok"}',
        input_text="ran a big test suite",
        parsed={"summary": "ok"},
    )
    assert r.trivial_summary


def test_empty_summary_flagged():
    r = check_refusal_pathology(
        raw_output='{"summary": ""}',
        input_text="x",
        parsed={"summary": ""},
    )
    assert r.trivial_summary


def test_whitespace_only_summary_flagged():
    r = check_refusal_pathology(
        raw_output='{"summary": "   "}',
        input_text="x",
        parsed={"summary": "   "},
    )
    assert r.trivial_summary


def test_echo_similarity_high_when_output_matches_input():
    prompt = "cargo test --release ran for 42 seconds and 103 tests passed"
    r = check_refusal_pathology(
        raw_output=prompt,
        input_text=prompt,
        parsed=None,
    )
    assert r.echo_similarity > 0.8


def test_echo_similarity_low_when_output_distinct():
    r = check_refusal_pathology(
        raw_output='{"summary": "Ran tests"}',
        input_text="Completely unrelated long-form prose about databases.",
        parsed={"summary": "Ran tests"},
    )
    assert r.echo_similarity < 0.3
