"""Bounded declarative decision tables. No-match and contradictory rules abstain."""

from __future__ import annotations

import json
import math
import operator
import re
from pathlib import Path
from typing import TypedDict


class Condition(TypedDict):
    fact: str
    op: str
    value: int | bool | float


class Rule(TypedDict):
    id: str
    task: str
    priority: int
    when: list[Condition]
    output: int | str


OPERATORS = {
    "eq": operator.eq,
    "ne": operator.ne,
    "ge": operator.ge,
    "gt": operator.gt,
    "le": operator.le,
    "lt": operator.lt,
}
FACTS = {
    "ranking": {"token_overlap": int, "query_tokens": int, "exact_query": bool},
    "verification": {"empty_source": bool, "identical_text": bool},
    "decision_conflict": {"comparable_records": int, "identical_records": bool},
    "outcome_conflict": {"comparable_records": int, "identical_records": bool},
    "decision": {"candidate_count": int, "valid_candidates": bool, "budget_available": bool},
    "refinement": {
        "ambiguous": bool,
        "new_evidence": bool,
        "budget_available": bool,
        "complete_order": bool,
    },
}
OUTPUTS = {
    "ranking": {0, 1, 2, 3},
    "verification": {"supports", "contradicts", "unsupported"},
    "decision_conflict": {"conflict", "compatible"},
    "outcome_conflict": {"conflict", "compatible"},
    "decision": {"preserve", "assess"},
    "refinement": {"refine", "stop"},
}


def validate_rules(rules: list[Rule]) -> None:
    if not isinstance(rules, list) or len(rules) > 1000:
        raise ValueError("rules must be a list with at most 1000 entries")
    ids = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"id", "task", "priority", "when", "output"}:
            raise ValueError("rule requires id, task, priority, when, output")
        if not isinstance(rule["id"], str) or not rule["id"] or rule["id"] in ids:
            raise ValueError("rule ids must be nonempty and unique")
        ids.add(rule["id"])
        task = rule["task"]
        if not isinstance(task, str) or task not in FACTS or type(rule["priority"]) is not int:
            raise ValueError("invalid rule task or priority")
        if not isinstance(rule["when"], list) or not rule["when"]:
            raise ValueError("when must contain at least one condition (AND semantics)")
        for condition in rule["when"]:
            if not isinstance(condition, dict) or set(condition) != {"fact", "op", "value"}:
                raise ValueError("condition requires fact, op, value")
            fact, op = condition["fact"], condition["op"]
            if (
                not isinstance(fact, str)
                or fact not in FACTS[task]
                or not isinstance(op, str)
                or op not in OPERATORS
            ):
                raise ValueError("unknown fact or operator")
            if type(condition["value"]) is not FACTS[task][fact]:
                raise ValueError("condition value type must match the fact")
        output = rule["output"]
        if type(output) is not (int if task == "ranking" else str) or output not in OUTPUTS[task]:
            raise ValueError("invalid rule output")


def evaluate(rules: list[Rule], task: str, facts: dict) -> dict:
    """Return an inspectable rule trace; malformed facts never produce a decision."""
    relevant = [rule for rule in rules if rule["task"] == task]
    required = {condition["fact"] for rule in relevant for condition in rule["when"]}
    invalid = sorted(
        fact
        for fact in required
        if type(facts.get(fact)) is not FACTS[task][fact]
        or (type(facts.get(fact)) is float and not math.isfinite(facts[fact]))
    )
    matches = (
        []
        if invalid
        else [
            rule
            for rule in relevant
            if all(OPERATORS[c["op"]](facts[c["fact"]], c["value"]) for c in rule["when"])
        ]
    )
    priority = max((rule["priority"] for rule in matches), default=None)
    winners = [rule for rule in matches if rule["priority"] == priority]
    outputs = {rule["output"] for rule in winners}
    return {
        "output": next(iter(outputs)) if len(outputs) == 1 else None,
        "matched_rules": [rule["id"] for rule in matches],
        "winning_rules": [rule["id"] for rule in winners],
        "conflict": len(outputs) > 1,
        "invalid_facts": invalid,
        "facts": facts,
    }


def ranking_rules() -> list[Rule]:
    rules = json.loads((Path(__file__).parent / "_fixtures" / "decision_rules.json").read_text())
    validate_rules(rules)
    return [rule for rule in rules if rule["task"] == "ranking"]


def rank(state: dict, rules: list[Rule]) -> dict:
    # ponytail: lexical facts cannot detect paraphrases; never use this baseline
    # to skip semantic inference without a separately qualified subset gate.
    query = set(re.findall(r"\w+", state["query"].casefold()))
    traces = []
    for candidate in state["candidates"]:
        text = " ".join(candidate.get(key, "") for key in ("summary", "embed_text", "commands_raw"))
        tokens = set(re.findall(r"\w+", text.casefold()))
        traces.append(
            evaluate(
                rules,
                "ranking",
                {
                    "token_overlap": len(query & tokens),
                    "query_tokens": len(query),
                    "exact_query": bool(query) and state["query"].casefold() in text.casefold(),
                },
            )
        )
    scores = [trace["output"] for trace in traces]
    order = (
        None
        if any(score is None for score in scores)
        else sorted(range(len(scores)), key=lambda index: scores[index], reverse=True)
    )
    return {"order": order, "scores": scores, "trace": traces}
