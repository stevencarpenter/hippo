"""A bounded, declarative decision table for shadow experiments.

Rules cannot execute code or mutate facts. Highest priority wins; conflicting
outputs at that priority abstain. No match also abstains.
"""

from __future__ import annotations

from hippo_brain.bench import decision_conflicts
from hippo_brain.decision_rules import (
    FACTS as FACTS,
    OPERATORS as OPERATORS,
    evaluate as evaluate,
    rank,
    validate_rules as validate_rules,
)


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
    return rank(state, rules)
