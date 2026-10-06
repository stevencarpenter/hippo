import pytest

from hippo_brain.bench.agent_answer_accuracy import score


def sample():
    frame = []
    rows = []
    for i in range(300):
        item = {"id": f"n{i}", "family": f"family-{i // 3}", "kind": "natural"}
        frame.append(item)
        rows.append(
            {
                **item,
                "reviewers": ["reviewer-a", "reviewer-b"],
                "response_status": "answer",
                "answerable": True,
                "correct": i // 3 >= 4,
                "fully_supported": True,
                "superseded": False,
            }
        )
    for i in range(100):
        item = {"id": f"c{i}", "family": f"control-{i}", "kind": "absent_control"}
        frame.append(item)
        rows.append(
            {
                **item,
                "reviewers": ["reviewer-a", "reviewer-b"],
                "response_status": "abstention",
                "unsupported_claim": False,
            }
        )
    return rows, frame


def test_accuracy_gate_passes_with_first_call_per_family():
    rows, frame = sample()
    result = score(rows, frame)
    assert result["verdict"] == "pass"
    assert result["jointly_successful_answers"] == 96
    assert result["joint_success_lower_97_5_percent"] >= 0.90
    assert result["absent_failure_upper_97_5_percent"] <= 0.05


@pytest.mark.parametrize("reviewer_kind", [None, "AI", "human"])
def test_passing_statistics_do_not_qualify_acceptance(reviewer_kind: str | None) -> None:
    rows, frame = sample()
    if reviewer_kind is not None:
        for row in rows:
            row["reviewer_kind"] = reviewer_kind

    result = score(rows, frame)

    assert result["verdict"] == "pass"
    assert result["verdict_scope"] == "conditional_statistics"
    assert result["acceptance_qualified"] is False
    assert result["unverified_prerequisites"] == [
        "prospectively_frozen_frame",
        "consecutive_capture_window",
        "independent_source_audit",
        "independent_task_families",
        "independent_blinded_human_reviews",
    ]


@pytest.mark.parametrize(
    ("index", "field"),
    [(0, field) for field in ("answerable", "correct", "fully_supported", "superseded")]
    + [(-1, "unsupported_claim")],
)
def test_rejects_missing_judgments(index: int, field: str) -> None:
    rows, frame = sample()
    del rows[index][field]
    with pytest.raises(ValueError, match=f"{field} requires an adjudicated boolean"):
        score(rows, frame)


@pytest.mark.parametrize("duplicate_frame", [False, True])
def test_rejects_duplicate_call_ids(duplicate_frame: bool) -> None:
    rows, frame = sample()
    data = frame if duplicate_frame else rows
    data[1]["id"] = data[0]["id"]
    with pytest.raises(ValueError, match="duplicate .* call ID"):
        score(rows, frame)


def test_rejects_control_family_overlap_with_natural_calls() -> None:
    rows, frame = sample()
    rows[-1]["family"] = frame[-1]["family"] = frame[0]["family"]
    with pytest.raises(ValueError, match="independent families"):
        score(rows, frame)


def test_one_absent_control_failure_fails_and_missing_family_is_inconclusive():
    rows, frame = sample()
    rows[-1]["response_status"] = "tool_error"
    assert score(rows, frame)["verdict"] == "fail"
    assert score(rows[3:], frame[3:])["verdict"] == "inconclusive"
    extra = {**frame[0], "id": "extra", "family": "extra-family"}
    assert score(rows + [{**rows[0], **extra}], frame + [extra])["verdict"] == "inconclusive"


def test_unsupported_or_superseded_answer_fails_joint_gate():
    rows, frame = sample()
    rows[12]["fully_supported"] = False
    assert score(rows, frame)["verdict"] == "fail"
    rows[12]["fully_supported"] = True
    rows[12]["superseded"] = True
    assert score(rows, frame)["verdict"] == "fail"


def test_rejects_missing_rows_labels_and_dependent_controls():
    rows, frame = sample()
    with pytest.raises(ValueError, match="do not match"):
        score(rows[:-1], frame)
    rows[0]["reviewers"] = ["reviewer-a", "reviewer-a"]
    with pytest.raises(ValueError, match="two distinct"):
        score(rows, frame)
    rows[0]["reviewers"] = ["reviewer-a", "reviewer-b"]
    frame[-1]["family"] = frame[-2]["family"]
    rows[-1]["family"] = rows[-2]["family"]
    with pytest.raises(ValueError, match="independent families"):
        score(rows, frame)
    with pytest.raises(ValueError, match="each call must be an object"):
        score(rows, [0])
