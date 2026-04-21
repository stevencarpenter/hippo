import math

import pytest

from hippo_brain.bench.gates import (
    check_entity_sanity,
    check_refusal_pathology,
    check_schema_validity,
    mean_pairwise_cosine,
    self_consistency_score,
)


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


def test_entity_sanity_accepts_path_like_files():
    payload = {
        "entities": {
            "files": ["src/main.rs", "brain/src/hippo_brain/enrichment.py", ".env"],
            "tools": ["cargo"],
            "projects": ["hippo"],
            "services": ["launchd"],
            "errors": [],
        }
    }
    r = check_entity_sanity(payload, "shell")
    assert r.passed
    assert r.files_path_rate >= 0.9


def test_entity_sanity_flags_sentence_in_files():
    payload = {
        "entities": {
            "files": ["The summary of this command output is a file", "ok.py"],
            "tools": [],
            "projects": [],
            "services": [],
            "errors": [],
        }
    }
    r = check_entity_sanity(payload, "shell")
    assert r.files_path_rate <= 0.5


def test_entity_sanity_flags_long_tool_names():
    payload = {
        "entities": {
            "files": [],
            "tools": [
                "cargo",
                "This is a sentence pretending to be a tool name that should fail.",
            ],
            "projects": [],
            "services": [],
            "errors": [],
        }
    }
    r = check_entity_sanity(payload, "shell")
    assert r.tools_sanity_rate <= 0.6


def test_entity_sanity_no_entities_is_vacuously_pass():
    payload = {"entities": {}}
    r = check_entity_sanity(payload, "shell")
    assert r.passed
    assert r.per_category_rates == {}


def test_mean_pairwise_cosine_identical_vectors():
    v = [1.0, 0.0, 0.0]
    score = mean_pairwise_cosine([v, v, v, v])
    assert math.isclose(score, 1.0, abs_tol=1e-6)


def test_mean_pairwise_cosine_orthogonal_vectors():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    score = mean_pairwise_cosine([a, b])
    assert math.isclose(score, 0.0, abs_tol=1e-6)


def test_mean_pairwise_cosine_single_vector_returns_nan_marker():
    score = mean_pairwise_cosine([[1.0, 0.0]])
    assert score is None


def test_self_consistency_score_aggregates_per_event():
    # Two events, each with three runs. First event converges; second diverges.
    per_event_vectors = [
        [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]],  # perfect
        [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]],  # partial
    ]
    r = self_consistency_score(per_event_vectors)
    assert 0.0 < r.mean < 1.0
    assert r.min < r.mean
    assert r.max > r.mean
    # First event: all pairs identical → 1.0
    # Second event: cos(a,b)=0, cos(a,a)=1, cos(b,a)=0 → mean = (0+1+0)/3 = 0.3333
    assert r.per_event_scores == pytest.approx([1.0, 1 / 3], abs=0.05)
