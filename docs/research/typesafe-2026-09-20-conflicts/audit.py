"""Verify the archived experiment and regenerate per-case results without network access."""

import argparse
import hashlib
import json
import math
import os
import statistics
from collections import defaultdict
from pathlib import Path


def digest(value):
    raw = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def audit(root):
    manifest = json.loads((root / "manifest.json").read_text())
    inputs = [
        json.loads(line) for line in (root / "inputs.jsonl").read_text().splitlines()
    ]
    labels = json.loads((root / "labels.json").read_text())
    rules = json.loads((root / "rules.json").read_text())
    for name, data in (("corpus", inputs), ("labels", labels), ("rules", rules)):
        require(digest(data) == manifest[name + "_hash"], name + " hash mismatch")
    for name, expected in manifest["implementation_hashes"].items():
        require(
            hashlib.sha256((root / "source" / name).read_bytes()).hexdigest()
            == expected,
            "source hash mismatch: " + name,
        )
    planned = {}
    for case in inputs:
        for repeat in range(manifest["repeats"]):
            state = json.loads(json.dumps(case["state"]))
            if manifest["reverse"] and repeat % 2:
                state["hits"].reverse()
            for arm in manifest["arms"]:
                planned[(case["task"], case["id"], repeat, arm)] = state
    records = {}
    for arm in manifest["arms"]:
        path = root / (arm + ".jsonl")
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            row = json.loads(line)
            key = (row["task"], row["case_id"], row["repeat"], arm)
            require(
                row["arm"] == arm and key in planned and key not in records,
                "invalid row identity",
            )
            require(row["input_hash"] == digest(planned[key]), "input hash mismatch")
            require(row["status"] in ("ok", "error", "abstain"), "invalid status")
            if "request" in row:
                request = row["request"]
                require(row["request_hash"] == digest(request), "request hash mismatch")
                state = (
                    request["state"]
                    if "state" in request
                    else json.loads(request["messages"][1]["content"])
                )
                require(
                    state == planned[key],
                    "different inference input or leaked annotations",
                )
                if row["status"] == "ok":
                    response = row["response"]
                    verdict = (
                        response["answers"]["verdict"]["choice"]
                        if "state" in request
                        else json.loads(response["choices"][0]["message"]["content"])[
                            "verdict"
                        ]
                    )
                    require(
                        verdict == row["output"]["verdict"],
                        "recorded verdict differs from response",
                    )
            records[key] = row
    groups, cases, paired = [], [], []
    for task in sorted({case["task"] for case in inputs}):
        ids = [case["id"] for case in inputs if case["task"] == task]
        for arm in manifest["arms"]:
            rows = [
                row for key, row in records.items() if key[0] == task and key[3] == arm
            ]
            latency = sorted(row["elapsed_ms"] for row in rows)
            confusions = {
                "true_conflicts": 0,
                "true_compatible": 0,
                "false_conflicts": 0,
                "missed_conflicts": 0,
                "error": 0,
                "abstain": 0,
                "missing": 0,
            }
            consistent_correct = unstable = 0
            for case_id in ids:
                expected = labels[f"{task}:{case_id}"]["expected"]
                verdicts, confidences = [], []
                for repeat in range(manifest["repeats"]):
                    row = records.get((task, case_id, repeat, arm))
                    status = row["status"] if row else "missing"
                    verdict = row["output"]["verdict"] if status == "ok" else status
                    verdicts.append(verdict)
                    confidence = (
                        (row or {})
                        .get("output", {})
                        .get("judgments", {})
                        .get("verdict", {})
                        .get("confidence")
                    )
                    confidences.append(confidence)
                    if status != "ok":
                        confusions[status] += 1
                    elif expected == verdict:
                        confusions[
                            "true_conflicts"
                            if verdict == "conflict"
                            else "true_compatible"
                        ] += 1
                    else:
                        confusions[
                            "false_conflicts"
                            if verdict == "conflict"
                            else "missed_conflicts"
                        ] += 1
                consistent_correct += all(verdict == expected for verdict in verdicts)
                unstable += len(set(verdicts)) > 1
                cases.append(
                    {
                        "task": task,
                        "case_id": case_id,
                        "arm": arm,
                        "expected": expected,
                        "verdicts": verdicts,
                        "confidence": confidences,
                    }
                )
            groups.append(
                {
                    "task": task,
                    "arm": arm,
                    "unique_cases": len(ids),
                    "correct_in_both_orders": consistent_correct,
                    "changed_between_orders": unstable,
                    "correct_decisions": confusions["true_conflicts"]
                    + confusions["true_compatible"],
                    "planned_decisions": len(ids) * manifest["repeats"],
                    **confusions,
                    "network_calls": sum(row["network_calls"] for row in rows),
                    "p50_ms": statistics.median(latency) if latency else None,
                    "p95_ms": latency[math.ceil(len(latency) * 0.95) - 1]
                    if latency
                    else None,
                }
            )
        for arm, baseline in (
            ("jev", "current"),
            ("llm", "current"),
            ("jev", "llm"),
            ("rules_jev", "jev"),
            ("rules_llm", "llm"),
        ):
            if arm not in manifest["arms"] or baseline not in manifest["arms"]:
                continue
            wins, losses, ties = [], [], []
            for case_id in ids:
                expected = labels[f"{task}:{case_id}"]["expected"]
                correct = {}
                for candidate in (arm, baseline):
                    group = [
                        records.get((task, case_id, repeat, candidate))
                        for repeat in range(manifest["repeats"])
                    ]
                    correct[candidate] = all(
                        row
                        and row["status"] == "ok"
                        and row["output"]["verdict"] == expected
                        for row in group
                    )
                target = (
                    ties
                    if correct[arm] == correct[baseline]
                    else wins
                    if correct[arm]
                    else losses
                )
                target.append(case_id)
            paired.append(
                {
                    "task": task,
                    "arm": arm,
                    "baseline": baseline,
                    "consistent_correct_wins": wins,
                    "consistent_correct_losses": losses,
                    "tied_cases": ties,
                }
            )
    simulations = []
    if "current" in manifest["arms"] and "jev" in manifest["arms"]:
        for task in sorted({case["task"] for case in inputs}):
            simulation = {
                "task": task,
                "planned_decisions": 0,
                "correct": 0,
                "false_conflicts": 0,
                "missed_conflicts": 0,
                "simulated_jev_calls": 0,
                "unavailable_judgments": 0,
            }
            for case in inputs:
                if case["task"] != task:
                    continue
                expected = labels[f"{task}:{case['id']}"]["expected"]
                for repeat in range(manifest["repeats"]):
                    simulation["planned_decisions"] += 1
                    current = records.get((task, case["id"], repeat, "current"))
                    jev = records.get((task, case["id"], repeat, "jev"))
                    if not current or current["status"] != "ok":
                        simulation["unavailable_judgments"] += 1
                        continue
                    verdict = current["output"]["verdict"]
                    if verdict != "conflict":
                        simulation["simulated_jev_calls"] += 1
                        if jev and jev["status"] == "ok":
                            verdict = jev["output"]["verdict"]
                        else:
                            simulation["unavailable_judgments"] += 1
                    simulation["correct"] += verdict == expected
                    simulation["false_conflicts"] += (
                        verdict == "conflict" and expected == "compatible"
                    )
                    simulation["missed_conflicts"] += (
                        verdict == "compatible" and expected == "conflict"
                    )
            simulations.append(simulation)
    repeated_requests = defaultdict(list)
    for row in records.values():
        if row["status"] == "ok" and row.get("request_hash"):
            repeated_requests[row["request_hash"]].append(row)
    repeated = [rows for rows in repeated_requests.values() if len(rows) > 1]
    variations = []
    for rows in repeated:
        if len({row["output"]["verdict"] for row in rows}) > 1:
            variations.append(
                {
                    "request_hash": rows[0]["request_hash"],
                    "calls": [
                        {
                            "case_id": row["case_id"],
                            "arm": row["arm"],
                            "repeat": row["repeat"],
                            "verdict": row["output"]["verdict"],
                        }
                        for row in rows
                    ],
                }
            )
    summary_path = root / "summary.json"
    if summary_path.exists():
        saved = json.loads(summary_path.read_text())
        require(
            saved["observed_records"] == len(records), "saved record count mismatch"
        )
        require(
            saved["expected_records"] == len(planned), "saved planned count mismatch"
        )
        for group in groups:
            original = next(
                g
                for g in saved["groups"]
                if (g["task"], g["arm"]) == (group["task"], group["arm"])
            )
            for current_key, saved_key in (
                ("correct_decisions", "correct"),
                ("error", "errors"),
                ("abstain", "abstentions"),
                ("missing", "missing"),
                ("false_conflicts", "false_conflicts"),
                ("missed_conflicts", "missed_conflicts"),
                ("network_calls", "network_calls"),
            ):
                require(
                    group[current_key] == original[saved_key],
                    "saved metric mismatch: " + saved_key,
                )
    return {
        "run_id": manifest["run_id"],
        "validated_records": len(records),
        "planned_records": len(planned),
        "missing_records": len(planned) - len(records),
        "repeated_request_groups": len(repeated),
        "identical_request_variations": variations,
        "posthoc_preserve_warnings_simulation": simulations,
        "groups": groups,
        "paired": paired,
        "cases": cases,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    archive = (
        Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        / "hippo-bench/decisions/archive/typesafe-2026-09-20-conflicts"
    )
    parser.add_argument("run_dir", nargs="?", type=Path, default=archive / "run")
    args = parser.parse_args()
    print(json.dumps(audit(args.run_dir), indent=2, allow_nan=False))
