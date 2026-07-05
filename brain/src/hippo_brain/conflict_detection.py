"""Conflict and staleness analysis for evidence-backed responses (SNUG-127).

Surfaces competing evidence and stale-only result sets so agents do not treat
older or contradictory knowledge as current fact.
"""

from __future__ import annotations

import time
from typing import Any, Sequence

_STALE_STATUSES = frozenset({"stale", "suppressed_idle", "expected_absent", "unknown", "failing"})


def _evidence_packets(hits: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    packets: list[dict[str, Any]] = []
    for hit in hits:
        packets.extend(hit.get("evidence") or [])
    return packets


def _staleness_warning(hits: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    packets = _evidence_packets(hits)
    if not packets:
        return None
    statuses = [
        pkt.get("freshness", {}).get("status")
        for pkt in packets
        if isinstance(pkt.get("freshness"), dict)
    ]
    if not statuses:
        return None
    if all(s in _STALE_STATUSES for s in statuses if isinstance(s, str)):
        return {
            "kind": "stale_evidence",
            "message": "All cited evidence is stale, idle, or from absent capture sources.",
            "evidence_refs": [p.get("ref") for p in packets if p.get("ref")],
        }
    return None


def _outcome_conflict(hits: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    sides: list[dict[str, Any]] = []
    for hit in hits:
        outcome = hit.get("outcome")
        if outcome not in {"success", "failure"}:
            continue
        sides.append(
            {
                "outcome": outcome,
                "uuid": hit.get("uuid"),
                "captured_at": hit.get("captured_at"),
                "summary": hit.get("summary", ""),
                "evidence_refs": [
                    p.get("ref") for p in (hit.get("evidence") or []) if p.get("ref")
                ],
            }
        )
    outcomes = {s["outcome"] for s in sides}
    if "success" not in outcomes or "failure" not in outcomes:
        return None
    return {
        "kind": "outcome_disagreement",
        "message": "Knowledge nodes disagree on whether the work succeeded or failed.",
        "sides": sorted(sides, key=lambda s: s.get("captured_at") or 0),
    }


def _decision_conflict(hits: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    entries: list[dict[str, Any]] = []
    for hit in hits:
        for dd in hit.get("design_decisions") or []:
            chosen = dd.get("chosen") if isinstance(dd, dict) else None
            if not chosen:
                continue
            entries.append(
                {
                    "chosen": str(chosen),
                    "considered": dd.get("considered") if isinstance(dd, dict) else None,
                    "reason": dd.get("reason") if isinstance(dd, dict) else None,
                    "uuid": hit.get("uuid"),
                    "captured_at": hit.get("captured_at"),
                    "evidence_refs": [
                        p.get("ref") for p in (hit.get("evidence") or []) if p.get("ref")
                    ],
                }
            )
    chosen_values = {e["chosen"] for e in entries}
    if len(chosen_values) < 2:
        return None
    ordered = sorted(entries, key=lambda e: e.get("captured_at") or 0)
    older = ordered[0]
    newer = ordered[-1]
    if older["chosen"] == newer["chosen"]:
        return None
    return {
        "kind": "decision_contradiction",
        "message": (
            f"Newer evidence ({newer['chosen']!r} @ {newer.get('captured_at')}) "
            f"contradicts an older decision ({older['chosen']!r} @ {older.get('captured_at')})."
        ),
        "sides": [older, newer],
    }


def analyze_conflicts(hits: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Return conflict/staleness report for a bounded hit list."""
    conflicts: list[dict[str, Any]] = []
    staleness = _staleness_warning(hits)
    for detector in (_outcome_conflict, _decision_conflict):
        found = detector(hits)
        if found:
            conflicts.append(found)

    has_unresolved = bool(conflicts)
    summary_parts: list[str] = []
    if staleness:
        summary_parts.append(staleness["message"])
    for conflict in conflicts:
        summary_parts.append(conflict["message"])

    return {
        "staleness": staleness,
        "conflicts": conflicts,
        "has_unresolved_conflicts": has_unresolved,
        "summary": " ".join(summary_parts) if summary_parts else "",
    }


def apply_conflict_confidence_caps(hits: list[dict[str, Any]], report: dict[str, Any]) -> None:
    """Lower per-hit confidence when unresolved conflicts or stale-only evidence exist."""
    if not report.get("has_unresolved_conflicts") and not report.get("staleness"):
        return
    for hit in hits:
        conf = hit.get("confidence")
        if not isinstance(conf, dict) or not conf:
            continue
        level = conf.get("level")
        if report.get("has_unresolved_conflicts"):
            if level == "high":
                conf["level"] = "medium"
            elif level == "medium":
                conf["level"] = "low"
            conf["explanation"] = (
                conf.get("explanation", "") + " Capped due to unresolved evidence conflict."
            ).strip()
        elif report.get("staleness") and level == "high":
            conf["level"] = "medium"
            conf["explanation"] = (
                conf.get("explanation", "") + " Capped: all evidence is stale or idle."
            ).strip()
