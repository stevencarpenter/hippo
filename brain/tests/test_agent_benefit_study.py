from pathlib import Path

import pytest

from hippo_brain.bench.agent_benefit_study import (
    feasibility,
    load_contract,
    report_pairs,
    study_identity,
)


def test_contract_separates_policies_and_preserves_inconclusive_ties():
    contract = load_contract(Path(__file__).parents[2] / "config/agent-benefit-v2.json")
    identity = study_identity(contract, "availability", "development")
    rows = [
        {
            "id": str(i),
            "family": str(i),
            "kind": "primary",
            "study_id": identity,
            "control_success": True,
            "hippo_success": True,
        }
        for i in range(12)
    ]
    frame = [{k: r[k] for k in ("id", "family", "kind", "study_id")} for r in rows]
    result = report_pairs(contract, "availability", "development", rows, frame, planned_n=12)
    assert result["verdict"] == "inconclusive"
    assert result["conservative_95_percent_interval"][0] < 0
    assert result["conservative_95_percent_interval"][1] > 0
    rows[0]["study_id"] = study_identity(contract, "guided", "development")
    with pytest.raises(ValueError, match="mixed"):
        report_pairs(contract, "availability", "development", rows, frame)
    result = feasibility(contract, {})
    assert result["disposition"] == "hold_inconclusive"
    assert "insufficient_prospective_families" in result["reasons"]
    assert "independent_human_labels_unavailable" in result["reasons"]
    assert "adequate_power_not_established" in result["reasons"]
