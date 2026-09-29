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
