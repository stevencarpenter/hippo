import pytest

from hippo_brain.bench.agent_benefit import score


def test_all_ties_remain_inconclusive_with_nonzero_uncertainty():
    rows = [
        {
            "id": str(i),
            "family": str(i),
            "kind": "primary",
            "control_success": True,
            "hippo_success": True,
        }
        for i in range(12)
    ]
    result = score(rows)
    assert result["verdict"] == "inconclusive"
    assert result["primary"] == {"n": 12, "wins": 0, "losses": 0, "ties": 12}
    assert result["conservative_95_percent_interval"][0] < 0
    assert result["conservative_95_percent_interval"][1] > 0


def test_large_paired_gain_and_no_gain_have_distinct_verdicts():
    rows = [
        {
            "id": str(i),
            "family": str(i),
            "kind": "primary",
            "control_success": False,
            "hippo_success": i < 50,
        }
        for i in range(100)
    ]
    frame = [{key: row[key] for key in ("id", "family", "kind")} for row in rows]
    assert score(rows, planned_primary_n=100, frozen_frame=frame)["verdict"] == "benefit"
    for row in rows:
        row["hippo_success"] = False
    assert (
        score(rows, planned_primary_n=100, frozen_frame=frame)["verdict"]
        == "gain_of_five_points_ruled_out"
    )
    assert score(rows, planned_primary_n=100)["verdict"] == "inconclusive"
    assert score(rows)["verdict"] == "inconclusive"


def test_observed_gain_must_reach_preregistered_five_points():
    rows = [
        {
            "id": str(i),
            "family": str(i),
            "kind": "primary",
            "control_success": False,
            "hippo_success": i < 40,
        }
        for i in range(1000)
    ]
    frame = [{key: row[key] for key in ("id", "family", "kind")} for row in rows]
    assert score(rows, planned_primary_n=1000, frozen_frame=frame)["verdict"] == "inconclusive"
    for row in rows[40:60]:
        row["hippo_success"] = True
    assert score(rows, planned_primary_n=1000, frozen_frame=frame)["verdict"] == "benefit"


def test_missing_outcomes_and_dependent_families_fail_closed():
    row = {"id": "a", "family": "one", "kind": "primary", "control_success": True}
    with pytest.raises(ValueError, match="adjudicated booleans"):
        score([row])
    row["hippo_success"] = False
    with pytest.raises(ValueError, match="dependent primary family"):
        score([row, {**row, "id": "b"}])


def test_frozen_frame_rejects_omitted_tasks_and_changed_plan():
    rows = [
        {
            "id": "a",
            "family": "a",
            "kind": "primary",
            "control_success": False,
            "hippo_success": True,
        },
        {
            "id": "b",
            "family": "b",
            "kind": "primary",
            "control_success": False,
            "hippo_success": False,
        },
    ]
    frame = [{key: row[key] for key in ("id", "family", "kind")} for row in rows]
    with pytest.raises(ValueError, match="do not match"):
        score(rows[:1], planned_primary_n=2, frozen_frame=frame)
    with pytest.raises(ValueError, match="planned primary count"):
        score(rows, planned_primary_n=3, frozen_frame=frame)
    assert score(rows, frozen_frame=frame)["verdict"] == "inconclusive"
    assert score(rows, planned_primary_n=2, frozen_frame=frame)["frame_matched"] is True
