"""Versioned study contracts and conservative feasibility gates. No inference."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from hippo_brain.bench.agent_benefit import score
from hippo_brain.jev import digest


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def load_contract(path: Path) -> dict:
    contract = json.loads(path.read_text())
    require(contract.get("version") == "agent-benefit-v2", "unsupported study contract")
    require(bool(contract.get("owner")), "study owner required")
    start, end = (datetime.fromisoformat(contract["window"][key]) for key in ("start", "end"))
    require(start.tzinfo is not None and end.tzinfo is not None and start < end, "invalid window")
    require(set(contract["studies"]) == {"availability", "guided"}, "separate studies required")
    for name, definition in contract["studies"].items():
        require(definition["select_for_known_history"] is False, "history is not eligibility")
        require(bool(definition["query_policy"]) == (name == "guided"), "invalid lookup policy")
    for phase in ("diagnostic", "development", "acceptance"):
        budget = contract["budgets"][phase]
        for key in (
            "max_pairs",
            "max_runs",
            "per_arm_seconds",
            "total_seconds",
            "max_total_tokens",
            "reviewer_hours",
        ):
            require(type(budget[key]) is int and budget[key] > 0, f"invalid budget: {phase}/{key}")
        require(budget["max_runs"] == 2 * budget["max_pairs"], "budget must include both arms")
    require(
        contract["decision"]
        == {
            "minimum_primary_n": 100,
            "minimum_gain": 0.05,
            "sizing_gain": 0.10,
            "minimum_power": 0.80,
            "confidence": 0.95,
        },
        "v2 retains existing decision rule",
    )
    require(
        contract["accuracy"]
        == {
            "natural_calls": 300,
            "answerable_families": 100,
            "absent_controls": 100,
            "joint_success_lower": 0.90,
            "absent_failure_upper": 0.05,
            "reviewer_kind": "human",
        },
        "v2 retains separate accuracy gate",
    )
    return contract


def study_identity(contract: dict, study: str, phase: str) -> str:
    require(study in contract["studies"] and phase in contract["budgets"], "unknown study/phase")
    return digest([contract, study, phase])


def report_pairs(
    contract: dict,
    study: str,
    phase: str,
    rows: list[dict],
    frame: list[dict],
    *,
    planned_n: int | None = None,
) -> dict:
    identity = study_identity(contract, study, phase)
    require(
        all(row.get("study_id") == identity for row in [*rows, *frame]),
        "mixed or unfrozen study identities",
    )
    result = score(rows, planned_primary_n=planned_n, frozen_frame=frame)
    if phase != "acceptance":
        result["verdict"] = "inconclusive"
    return {"study_id": identity, "study": study, "phase": phase, **result}


def feasibility(contract: dict, intake: dict) -> dict:
    """An absent observation is unknown, never evidence that a gate passed."""
    reasons = []
    if intake.get("canary_verified") is not True:
        reasons.append("canary_not_verified")
    if intake.get("evaluator_verified") is not True:
        reasons.append("evaluator_not_verified")
    target = contract["budgets"]["development"]["target_pairs"]
    if intake.get("eligible_prospective_families", 0) < target:
        reasons.append("insufficient_prospective_families")
    proposed = intake.get("proposed_acceptance_n")
    if (
        type(proposed) is not int
        or not contract["decision"]["minimum_primary_n"]
        <= proposed
        <= contract["budgets"]["acceptance"]["max_pairs"]
    ):
        reasons.append("acceptance_size_missing_or_outside_budget")
    power = intake.get("sizing_power")
    if type(power) not in (int, float) or not 0.80 <= power <= 1:
        reasons.append("adequate_power_not_established")
    if intake.get("independent_human_reviewers", 0) < 2:
        reasons.append("independent_human_labels_unavailable")
    return {
        "contract_sha256": digest(contract),
        "acceptance_feasible": not reasons,
        "disposition": "ready_to_freeze" if not reasons else "hold_inconclusive",
        "reasons": reasons,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=Path("config/agent-benefit-v2.json"))
    parser.add_argument("--intake", type=Path)
    args = parser.parse_args()
    contract = load_contract(args.contract)
    result = (
        feasibility(contract, json.loads(args.intake.read_text()))
        if args.intake
        else {
            "contract_sha256": digest(contract),
            "studies": {
                name: study_identity(contract, name, "development") for name in contract["studies"]
            },
        }
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
