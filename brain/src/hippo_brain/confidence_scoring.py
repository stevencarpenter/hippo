"""Explainable confidence scoring for retrieval hits (SNUG-126).

Combines evidence quality, freshness, recency, retrieval strength, and source
diversity into a bounded score with named factors and a readable explanation.
Answers without inspectable evidence are never rated high confidence.
"""

from __future__ import annotations

import time
from typing import Any, Sequence

CONFIDENCE_LEVELS = frozenset({"high", "medium", "low", "insufficient"})

_RECENCY_FRESH_MS = 7 * 24 * 3600 * 1000
_RECENCY_STALE_MS = 30 * 24 * 3600 * 1000

_UNHEALTHY_FRESHNESS = frozenset({"stale", "failing", "expected_absent", "unknown"})


def _factor(name: str, *, weight: float, score: float, detail: str) -> dict[str, Any]:
    contribution = round(weight * score, 4)
    return {
        "name": name,
        "weight": weight,
        "score": round(max(0.0, min(1.0, score)), 4),
        "contribution": contribution,
        "detail": detail,
    }


def _evidence_factor(evidence: Sequence[dict[str, Any]]) -> dict[str, Any]:
    count = len(evidence)
    if count == 0:
        return _factor("evidence", weight=0.30, score=0.0, detail="no inspectable evidence packets")
    if count >= 3:
        score = 1.0
        detail = f"{count} evidence packet(s)"
    elif count == 2:
        score = 0.8
        detail = "2 evidence packets"
    else:
        score = 0.55
        detail = "single evidence packet"
    return _factor("evidence", weight=0.30, score=score, detail=detail)


def _source_diversity_factor(evidence: Sequence[dict[str, Any]]) -> dict[str, Any]:
    kinds = {pkt.get("source_kind") for pkt in evidence if isinstance(pkt.get("source_kind"), str)}
    kinds.discard(None)
    n = len(kinds)
    if n >= 3:
        score, detail = 1.0, f"{n} independent source families"
    elif n == 2:
        score, detail = 0.75, "2 independent source families"
    elif n == 1:
        score, detail = 0.45, "single source family"
    else:
        score, detail = 0.0, "no source kinds on evidence"
    return _factor("source_diversity", weight=0.15, score=score, detail=detail)


def _recency_factor(captured_at: int, *, now_ms: int) -> dict[str, Any]:
    if captured_at <= 0:
        return _factor("recency", weight=0.15, score=0.2, detail="unknown capture time")
    age_ms = max(0, now_ms - captured_at)
    if age_ms <= _RECENCY_FRESH_MS:
        score, detail = 1.0, "captured within 7 days"
    elif age_ms <= _RECENCY_STALE_MS:
        score, detail = 0.5, "captured 7–30 days ago"
    else:
        score, detail = 0.15, "captured more than 30 days ago"
    return _factor("recency", weight=0.15, score=score, detail=detail)


def _retrieval_match_factor(retrieval_score: float) -> dict[str, Any]:
    score = max(0.0, min(1.0, retrieval_score))
    if score >= 0.8:
        detail = "strong lexical/semantic match"
    elif score >= 0.5:
        detail = "moderate retrieval match"
    else:
        detail = "weak retrieval match"
    return _factor("retrieval_match", weight=0.20, score=score, detail=detail)


def _capture_health_factor(evidence: Sequence[dict[str, Any]]) -> dict[str, Any]:
    statuses: list[str] = []
    for pkt in evidence:
        fresh = pkt.get("freshness")
        if isinstance(fresh, dict):
            status = fresh.get("status")
            if isinstance(status, str):
                statuses.append(status)
    if not statuses:
        return _factor(
            "capture_health",
            weight=0.15,
            score=0.5,
            detail="freshness metadata unavailable",
        )
    unhealthy = sum(1 for s in statuses if s in _UNHEALTHY_FRESHNESS)
    if unhealthy:
        score = max(0.0, 1.0 - unhealthy / len(statuses))
        detail = f"{unhealthy}/{len(statuses)} evidence source(s) stale, failing, or absent"
    elif all(s == "fresh" for s in statuses):
        score, detail = 1.0, "all cited sources fresh"
    else:
        score, detail = 0.7, "sources idle but historically present"
    return _factor("capture_health", weight=0.15, score=score, detail=detail)


def _context_alignment_factor(cwd: str, git_branch: str) -> dict[str, Any]:
    has_cwd = bool(cwd and cwd.strip())
    has_branch = bool(git_branch and git_branch.strip())
    if has_cwd and has_branch:
        score, detail = 1.0, f"cwd + branch ({git_branch})"
    elif has_cwd:
        score, detail = 0.7, "cwd present"
    elif has_branch:
        score, detail = 0.5, "git branch only"
    else:
        score, detail = 0.3, "no project context on hit"
    return _factor("context_alignment", weight=0.05, score=score, detail=detail)


def _level_from_score(
    composite: float,
    *,
    has_evidence: bool,
    unhealthy_capture: bool,
) -> str:
    if not has_evidence:
        return "insufficient"
    if unhealthy_capture and composite < 0.55:
        return "low"
    if composite >= 0.72 and not unhealthy_capture:
        return "high"
    if composite >= 0.45:
        return "medium"
    return "low"


def _compose_explanation(level: str, factors: Sequence[dict[str, Any]]) -> str:
    if level == "insufficient":
        return "Confidence withheld: no inspectable evidence packets back this hit."
    top = sorted(factors, key=lambda f: f["contribution"], reverse=True)[:3]
    parts = [f"{f['name']} ({f['detail']})" for f in top]
    prefix = {
        "high": "High confidence",
        "medium": "Medium confidence",
        "low": "Low confidence",
    }.get(level, "Confidence")
    return f"{prefix}: " + "; ".join(parts) + "."


def assess_confidence(
    *,
    retrieval_score: float,
    evidence: Sequence[dict[str, Any]],
    cwd: str = "",
    git_branch: str = "",
    captured_at: int = 0,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Return bounded confidence dict: level, score, factors, explanation."""
    now_ms = now_ms or int(time.time() * 1000)
    factors = [
        _evidence_factor(evidence),
        _source_diversity_factor(evidence),
        _recency_factor(captured_at, now_ms=now_ms),
        _retrieval_match_factor(retrieval_score),
        _capture_health_factor(evidence),
        _context_alignment_factor(cwd, git_branch),
    ]
    composite = round(sum(f["contribution"] for f in factors), 4)

    statuses = [
        pkt.get("freshness", {}).get("status")
        for pkt in evidence
        if isinstance(pkt.get("freshness"), dict)
    ]
    unhealthy_capture = any(s in _UNHEALTHY_FRESHNESS for s in statuses if isinstance(s, str))

    has_evidence = len(evidence) > 0
    level = _level_from_score(
        composite,
        has_evidence=has_evidence,
        unhealthy_capture=unhealthy_capture,
    )

    if not has_evidence:
        composite = min(composite, 0.2)
    elif level == "high" and (composite < 0.72 or unhealthy_capture):
        level = "medium"
    elif level == "high" and len(evidence) < 2:
        # Single packet cannot reach high without strong health + match
        if composite < 0.78 or unhealthy_capture:
            level = "medium"

    if unhealthy_capture and level == "high":
        level = "medium"

    explanation = _compose_explanation(level, factors)
    withheld = level == "insufficient"

    return {
        "level": level,
        "score": composite,
        "factors": factors,
        "explanation": explanation,
        "withheld": withheld,
    }


def attach_confidence_to_results(
    results: Sequence[Any],
    *,
    now_ms: int | None = None,
) -> None:
    """Mutate retrieval results in place with a ``confidence`` dict."""
    now_ms = now_ms or int(time.time() * 1000)
    for result in results:
        result.confidence = assess_confidence(
            retrieval_score=float(getattr(result, "score", 0.0) or 0.0),
            evidence=list(getattr(result, "evidence", None) or []),
            cwd=str(getattr(result, "cwd", "") or ""),
            git_branch=str(getattr(result, "git_branch", "") or ""),
            captured_at=int(getattr(result, "captured_at", 0) or 0),
            now_ms=now_ms,
        )
