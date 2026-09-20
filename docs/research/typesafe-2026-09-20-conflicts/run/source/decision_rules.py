"""A bounded, declarative decision table for shadow experiments.

Rules cannot execute code or mutate facts. Highest priority wins; conflicting
outputs at that priority abstain. No match also abstains.
"""

from __future__ import annotations

import operator
import re

from hippo_brain.bench import decision_conflicts

OPERATORS = {"eq": operator.eq, "ne": operator.ne, "ge": operator.ge, "gt": operator.gt}
FACTS = {
    "ranking": {"token_overlap", "query_tokens", "exact_query"},
    "verification": {"empty_source", "identical_text"},
    **{task: {"comparable_records", "identical_records"} for task in decision_conflicts.KINDS},
}


def validate_rules(rules: list[dict]) -> None:
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
        conditions = rule["when"]
        if not isinstance(conditions, list) or not conditions:
            raise ValueError("when must contain at least one condition (AND semantics)")
        for condition in conditions:
            if not isinstance(condition, dict) or set(condition) != {"fact", "op", "value"}:
                raise ValueError("condition requires fact, op, value")
            if (
                not isinstance(condition["fact"], str)
                or not isinstance(condition["op"], str)
                or condition["fact"] not in FACTS[task]
                or condition["op"] not in OPERATORS
            ):
                raise ValueError("unknown fact or operator")
            if type(condition["value"]) not in (int, bool):
                raise ValueError("condition values must be integers or booleans")
        output = rule["output"]
        if task == "ranking":
            if type(output) is not int or not 0 <= output <= 3:
                raise ValueError("ranking rule output must be an integer in [0, 3]")
        else:
            options = (
                decision_conflicts.CRITERIA
                if task in decision_conflicts.KINDS
                else ("supports", "contradicts", "unsupported")
            )
            if not isinstance(output, str) or output not in options:
                raise ValueError("invalid verdict rule output")


def evaluate(rules: list[dict], task: str, facts: dict) -> dict:
    matches = [
        rule
        for rule in rules
        if rule["task"] == task
        and all(OPERATORS[c["op"]](facts[c["fact"]], c["value"]) for c in rule["when"])
    ]
    winners = []
    if matches:
        priority = max(rule["priority"] for rule in matches)
        winners = [rule for rule in matches if rule["priority"] == priority]
    outputs = {rule["output"] for rule in winners}
    return {
        "output": next(iter(outputs)) if len(outputs) == 1 else None,
        "matched_rules": [rule["id"] for rule in matches],
        "winning_rules": [rule["id"] for rule in winners],
        "conflict": len(outputs) > 1,
        "facts": facts,
    }


def judge(state: dict, task: str, rules: list[dict]) -> dict:
    if task in decision_conflicts.KINDS:
        trace = evaluate(rules, task, decision_conflicts.facts(state, task))
        return {"verdict": trace["output"], "trace": [trace]}
    if task == "verification":
        source, claim = state["source"].strip(), state["claim"].strip()
        trace = evaluate(
            rules, task, {"empty_source": not source, "identical_text": source == claim}
        )
        return {"verdict": trace["output"], "trace": [trace]}
    # ponytail: lexical facts cannot detect paraphrases; compare against labeled
    # semantic judgments before treating any rule as an inference shortcut.
    query = set(re.findall(r"\w+", state["query"].casefold()))
    traces = []
    for candidate in state["candidates"]:
        text = " ".join(candidate[k] for k in ("summary", "embed_text", "commands_raw"))
        tokens = set(re.findall(r"\w+", text.casefold()))
        traces.append(
            evaluate(
                rules,
                task,
                {
                    "token_overlap": len(query & tokens),
                    "query_tokens": len(query),
                    "exact_query": state["query"].casefold() in text.casefold(),
                },
            )
        )
    scores = [trace["output"] for trace in traces]
    order = None
    if all(score is not None for score in scores):
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return {"order": order, "scores": scores, "trace": traces}
