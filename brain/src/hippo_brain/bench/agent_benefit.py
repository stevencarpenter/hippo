"""Score independent, blindly adjudicated Hippo-versus-control task pairs."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def _binomial_cdf(x: int, n: int, p: float) -> float:
    if p == 0:
        return 1.0
    if p == 1:
        return float(x == n)
    return math.fsum(
        math.exp(
            math.lgamma(n + 1)
            - math.lgamma(k + 1)
            - math.lgamma(n - k + 1)
            + k * math.log(p)
            + (n - k) * math.log1p(-p)
        )
        for k in range(x + 1)
    )


def _proportion_bounds(x: int, n: int, tail: float = 0.0125) -> tuple[float, float]:
    """Exact binomial bounds with one-sided tail probability per limit."""

    def solve(k: int, target: float) -> float:
        lo, hi = 0.0, 1.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if _binomial_cdf(k, n, mid) > target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    lower = 0.0 if x == 0 else solve(x - 1, 1 - tail)
    upper = 1.0 if x == n else solve(x, tail)
    return lower, upper


def score(
    rows: list[dict],
    *,
    planned_primary_n: int | None = None,
    frozen_frame: list[dict] | None = None,
) -> dict:
    """Use one independent primary task per family; controls are reported separately."""
    if not isinstance(rows, list) or not rows:
        raise ValueError("expected a nonempty JSON array of paired task rows")
    ids: set[str] = set()
    families: set[str] = set()
    counts = {kind: {"wins": 0, "losses": 0, "ties": 0} for kind in ("primary", "control")}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each task row must be an object")
        task_id, family, kind = row.get("id"), row.get("family"), row.get("kind")
        if not isinstance(task_id, str) or not task_id or task_id in ids:
            raise ValueError(f"missing or duplicate task ID: {task_id!r}")
        if not isinstance(family, str) or not family:
            raise ValueError(f"missing family for {task_id}")
        if kind not in counts:
            raise ValueError(f"invalid kind for {task_id}: {kind!r}")
        if kind == "primary" and family in families:
            raise ValueError(f"dependent primary family: {family}")
        control, hippo = row.get("control_success"), row.get("hippo_success")
        if type(control) is not bool or type(hippo) is not bool:
            raise ValueError(f"both outcomes must be adjudicated booleans for {task_id}")
        ids.add(task_id)
        if kind == "primary":
            families.add(family)
        outcome = "wins" if hippo and not control else "losses" if control and not hippo else "ties"
        counts[kind][outcome] += 1
    primary = counts["primary"]
    n = sum(primary.values())
    if not n:
        raise ValueError("at least one primary task is required")
    if planned_primary_n is not None and planned_primary_n < n:
        raise ValueError("more primary rows than planned")
    frame_matched = False
    if frozen_frame is not None:
        if not isinstance(frozen_frame, list) or not frozen_frame:
            raise ValueError("expected a nonempty frozen task frame")
        frame_ids: set[str] = set()
        frame_primary_families: set[str] = set()
        frame_tasks: set[tuple[str, str, str]] = set()
        for task in frozen_frame:
            if not isinstance(task, dict):
                raise ValueError("each frozen task must be an object")
            task_id, family, kind = task.get("id"), task.get("family"), task.get("kind")
            if not isinstance(task_id, str) or not task_id or task_id in frame_ids:
                raise ValueError(f"missing or duplicate frozen task ID: {task_id!r}")
            if not isinstance(family, str) or not family or kind not in counts:
                raise ValueError(f"invalid frozen task: {task_id}")
            if kind == "primary" and family in frame_primary_families:
                raise ValueError(f"dependent frozen primary family: {family}")
            frame_ids.add(task_id)
            frame_tasks.add((task_id, family, kind))
            if kind == "primary":
                frame_primary_families.add(family)
        row_tasks = {(row["id"], row["family"], row["kind"]) for row in rows}
        if row_tasks != frame_tasks:
            raise ValueError("paired rows do not match the frozen task frame")
        if planned_primary_n is not None and planned_primary_n != len(frame_primary_families):
            raise ValueError("planned primary count does not match the frozen task frame")
        frame_matched = True
    win_lo, win_hi = _proportion_bounds(primary["wins"], n)
    loss_lo, loss_hi = _proportion_bounds(primary["losses"], n)
    gain = (primary["wins"] - primary["losses"]) / n
    lower, upper = win_lo - loss_hi, win_hi - loss_lo
    verdict = "inconclusive"
    if frame_matched and planned_primary_n == n and n >= 100:
        if gain >= 0.05 and lower > 0:
            verdict = "benefit"
        elif upper < 0.05:
            verdict = "gain_of_five_points_ruled_out"
    return {
        "primary": {"n": n, **primary},
        "controls": {"n": sum(counts["control"].values()), **counts["control"]},
        "gain": gain,
        "conservative_95_percent_interval": [lower, upper],
        "verdict": verdict,
        "planned_primary_n": planned_primary_n,
        "complete_against_plan": frame_matched and planned_primary_n == n,
        "frame_matched": frame_matched,
        "minimum_primary_n": 100,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="private JSON array of adjudicated paired rows")
    parser.add_argument("--planned-primary-n", type=int, help="preregistered primary task count")
    parser.add_argument(
        "--frame", type=Path, help="frozen JSON array of selected task IDs and families"
    )
    args = parser.parse_args()
    print(
        json.dumps(
            score(
                json.loads(args.input.read_text()),
                planned_primary_n=args.planned_primary_n,
                frozen_frame=json.loads(args.frame.read_text()) if args.frame else None,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
