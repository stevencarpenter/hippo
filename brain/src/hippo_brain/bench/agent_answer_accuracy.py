"""Compute conditional answer statistics; external evidence must qualify acceptance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hippo_brain.bench.agent_benefit import _proportion_bounds

STATUSES = {"answer", "abstention", "timeout", "tool_error"}
KINDS = {"natural", "absent_control"}


def _identity(row: dict) -> tuple[str, str, str]:
    if not isinstance(row, dict):
        raise ValueError("each call must be an object")
    task_id, family, kind = row.get("id"), row.get("family"), row.get("kind")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("missing call ID")
    if not isinstance(family, str) or not family or kind not in KINDS:
        raise ValueError(f"invalid family or kind for {task_id}")
    return task_id, family, kind


def _boolean(row: dict, field: str) -> bool:
    value = row.get(field)
    if type(value) is not bool:
        raise ValueError(f"{row['id']}: {field} requires an adjudicated boolean")
    return value


def score(rows: list[dict], frame: list[dict]) -> dict:
    """Use the first framed natural call per family and every absent control.

    Labels and frame identities permit statistical scoring, but do not establish
    source support, human reviewer provenance, or prospective sampling.
    """
    if not isinstance(frame, list) or not frame or not isinstance(rows, list):
        raise ValueError("expected frozen frame and adjudicated row arrays")
    frame_ids = [_identity(item) for item in frame]
    row_ids = [_identity(item) for item in rows]
    if len(set(frame_ids)) != len(frame_ids) or len({item[0] for item in frame_ids}) != len(
        frame_ids
    ):
        raise ValueError("duplicate frozen call ID")
    if len(set(row_ids)) != len(row_ids) or len({item[0] for item in row_ids}) != len(row_ids):
        raise ValueError("duplicate adjudicated call ID")
    if set(frame_ids) != set(row_ids):
        raise ValueError("adjudicated rows do not match frozen frame")
    by_id = {row["id"]: row for row in rows}
    natural = [item for item in frame_ids if item[2] == "natural"]
    controls = [item for item in frame_ids if item[2] == "absent_control"]
    natural_families = {item[1] for item in natural}
    control_families = {item[1] for item in controls}
    if len(control_families) != len(controls) or natural_families & control_families:
        raise ValueError("absent controls must have independent families")

    for task_id, _, kind in frame_ids:
        row = by_id[task_id]
        reviewers = row.get("reviewers")
        if (
            not isinstance(reviewers, list)
            or len(reviewers) != 2
            or any(not isinstance(name, str) or not name.strip() for name in reviewers)
            or reviewers[0].strip().casefold() == reviewers[1].strip().casefold()
        ):
            raise ValueError(f"{task_id}: two distinct reviewers required")
        if row.get("response_status") not in STATUSES:
            raise ValueError(f"{task_id}: invalid response status")
        if kind == "natural":
            if _boolean(row, "answerable"):
                _boolean(row, "correct")
                _boolean(row, "fully_supported")
                _boolean(row, "superseded")
        else:
            _boolean(row, "unsupported_claim")

    primary = {}
    for task_id, family, _ in natural:
        primary.setdefault(family, by_id[task_id])
    answerable = [row for row in primary.values() if row["answerable"]]
    success = sum(
        row["response_status"] == "answer"
        and row["correct"]
        and row["fully_supported"]
        and not row["superseded"]
        for row in answerable
    )
    failures = sum(
        by_id[task_id]["response_status"] != "abstention" or by_id[task_id]["unsupported_claim"]
        for task_id, _, _ in controls
    )
    ready = (
        len(natural) == 300
        and len(primary) >= 100
        and len(answerable) >= 100
        and len(controls) == 100
    )
    success_lower = (
        _proportion_bounds(success, len(answerable), tail=0.025)[0] if answerable else None
    )
    failure_upper = _proportion_bounds(failures, len(controls), tail=0.025)[1] if controls else None
    verdict = "inconclusive"
    if ready:
        verdict = "pass" if success_lower >= 0.90 and failure_upper <= 0.05 else "fail"
    return {
        "natural_calls": len(natural),
        "primary_families": len(primary),
        "answerable_primary_families": len(answerable),
        "jointly_successful_answers": success,
        "joint_success_lower_97_5_percent": success_lower,
        "absent_controls": len(controls),
        "absent_control_failures": failures,
        "absent_failure_upper_97_5_percent": failure_upper,
        "verdict": verdict,
        "verdict_scope": "conditional_statistics",
        "acceptance_qualified": False,
        "unverified_prerequisites": [
            "prospectively_frozen_frame",
            "consecutive_capture_window",
            "independent_source_audit",
            "independent_task_families",
            "independent_blinded_human_reviews",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rows", type=Path, help="private adjudicated JSON array")
    parser.add_argument("--frame", type=Path, required=True, help="frozen ordered call frame")
    args = parser.parse_args()
    print(
        json.dumps(
            score(json.loads(args.rows.read_text()), json.loads(args.frame.read_text())), indent=2
        )
    )


if __name__ == "__main__":
    main()
