"""Agent-trust eval corpus loader and evidence-shape checker (SNUG-118).

This module defines the regression bar for agent-trust work: each case records
*expected evidence requirements* (source kinds, hit counts, gap semantics) rather
than prose answers tied to a live user database.

**Adding cases:** append to ``_fixtures/trust_eval_cases.json`` with a unique ``id``,
set ``evidence.source_kinds`` to the logical families you expect in
``SearchResult.linked_source_ids`` (``shell-``, ``claude-``, ``codex-``, etc.),
and extend the synthetic seeder in ``brain/tests/test_trust_eval.py`` when the case
needs an end-to-end retrieval assertion.

Run offline::

    hippo-trust-eval --validate
    pytest brain/tests/test_trust_eval.py -q
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from hippo_brain.retrieval import Filters, SearchResult, search

_DEFAULT_CASES = Path(__file__).parent / "_fixtures" / "trust_eval_cases.json"

_REQUIRED_SOURCE_FAMILIES = frozenset(
    {
        "shell",
        "claude",
        "codex",
        "cursor",
        "opencode",
        "browser",
        "workflow",
        "claude-auto-memory",
    }
)

_VALID_MODES = frozenset(
    {"what_known", "evidence_for", "recent_changes", "prior_decisions", "adversarial"}
)


@dataclass(frozen=True)
class EvidenceSpec:
    source_kinds: tuple[str, ...] = ()
    min_hits: int = 1
    max_hits: int | None = None
    require_captured_at: bool = False
    require_distinct_source_kinds: bool = False
    exclude_probe_linkage: bool = False
    expect_coverage_gap: bool = False
    gap_reason_contains: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EvidenceSpec:
        kinds = raw.get("source_kinds") or []
        gap = raw.get("gap_reason_contains") or []
        return cls(
            source_kinds=tuple(str(k) for k in kinds),
            min_hits=int(raw.get("min_hits", 1)),
            max_hits=raw.get("max_hits"),
            require_captured_at=bool(raw.get("require_captured_at", False)),
            require_distinct_source_kinds=bool(raw.get("require_distinct_source_kinds", False)),
            exclude_probe_linkage=bool(raw.get("exclude_probe_linkage", False)),
            expect_coverage_gap=bool(raw.get("expect_coverage_gap", False)),
            gap_reason_contains=tuple(str(g) for g in gap),
        )


@dataclass(frozen=True)
class TrustEvalCase:
    id: str
    question: str
    mode: str
    evidence: EvidenceSpec
    source_filter: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TrustEvalCase:
        return cls(
            id=str(raw["id"]),
            question=str(raw["question"]),
            mode=str(raw.get("mode", "what_known")),
            source_filter=raw.get("source_filter"),
            evidence=EvidenceSpec.from_dict(raw.get("evidence") or {}),
        )


@dataclass
class EvidenceCheckResult:
    case_id: str
    passed: bool
    detail: str


def load_cases(path: Path | str | None = None) -> list[TrustEvalCase]:
    """Load and parse trust eval cases from JSON."""
    p = Path(path) if path is not None else _DEFAULT_CASES
    data = json.loads(p.read_text(encoding="utf-8"))
    return [TrustEvalCase.from_dict(c) for c in data["cases"]]


def validate_corpus(cases: Sequence[TrustEvalCase]) -> list[str]:
    """Return human-readable validation errors; empty list means corpus is well-formed."""
    errors: list[str] = []
    if len(cases) < 12:
        errors.append(f"expected at least 12 cases, found {len(cases)}")

    seen: set[str] = set()
    covered: set[str] = set()
    has_negative = False

    for case in cases:
        if case.id in seen:
            errors.append(f"duplicate case id: {case.id}")
        seen.add(case.id)

        if case.mode not in _VALID_MODES:
            errors.append(f"{case.id}: invalid mode {case.mode!r}")

        if case.evidence.min_hits < 0:
            errors.append(f"{case.id}: min_hits must be >= 0")
        if case.evidence.max_hits is not None and case.evidence.max_hits < case.evidence.min_hits:
            errors.append(f"{case.id}: max_hits < min_hits")

        for kind in case.evidence.source_kinds:
            covered.add(kind)

        if case.evidence.expect_coverage_gap or case.mode == "adversarial":
            has_negative = True

    missing = _REQUIRED_SOURCE_FAMILIES - covered
    if missing:
        errors.append(f"missing source family coverage: {sorted(missing)}")

    if not has_negative:
        errors.append("corpus must include at least one negative/gap case")

    return errors


def _source_kind_from_link(link: str) -> str | None:
    if link.startswith("shell-"):
        return "shell"
    if link.startswith("browser-"):
        return "browser"
    if link.startswith("workflow-"):
        return "workflow"
    if link.startswith("memory-"):
        return "claude-auto-memory"
    for prefix in ("claude-", "codex-", "cursor-", "opencode-"):
        if link.startswith(prefix):
            return prefix.removesuffix("-")
    return None


def kinds_in_results(results: Sequence[SearchResult]) -> set[str]:
    kinds: set[str] = set()
    for r in results:
        for link in r.linked_source_ids:
            kind = _source_kind_from_link(link)
            if kind:
                kinds.add(kind)
    return kinds


def check_evidence(
    case: TrustEvalCase,
    results: Sequence[SearchResult],
    *,
    gap_reason: str | None = None,
) -> EvidenceCheckResult:
    """Assert retrieval results satisfy a case's evidence requirements."""
    spec = case.evidence
    n = len(results)

    if n < spec.min_hits:
        return EvidenceCheckResult(
            case.id,
            False,
            f"expected >= {spec.min_hits} hits, got {n}",
        )
    if spec.max_hits is not None and n > spec.max_hits:
        return EvidenceCheckResult(
            case.id,
            False,
            f"expected <= {spec.max_hits} hits, got {n}",
        )

    if spec.expect_coverage_gap:
        if not gap_reason:
            return EvidenceCheckResult(case.id, False, "expected coverage gap reason")
        lower = gap_reason.lower()
        if spec.gap_reason_contains and not any(
            tok.lower() in lower for tok in spec.gap_reason_contains
        ):
            return EvidenceCheckResult(
                case.id,
                False,
                f"gap reason {gap_reason!r} missing tokens {spec.gap_reason_contains}",
            )

    found_kinds = kinds_in_results(results)
    for required in spec.source_kinds:
        if required not in found_kinds:
            return EvidenceCheckResult(
                case.id,
                False,
                f"missing source kind {required!r} in {sorted(found_kinds)}",
            )

    if spec.require_distinct_source_kinds:
        needed = set(spec.source_kinds)
        if not needed.issubset(found_kinds):
            return EvidenceCheckResult(
                case.id,
                False,
                f"need distinct kinds {sorted(needed)}, got {sorted(found_kinds)}",
            )

    if spec.require_captured_at:
        if not any(r.captured_at > 0 for r in results):
            return EvidenceCheckResult(case.id, False, "no result has captured_at")

    if spec.exclude_probe_linkage:
        for r in results:
            for link in r.linked_source_ids:
                if "probe" in link.lower():
                    return EvidenceCheckResult(
                        case.id,
                        False,
                        f"probe linkage leaked: {link}",
                    )

    return EvidenceCheckResult(case.id, True, "ok")


def run_case_search(
    conn: sqlite3.Connection,
    case: TrustEvalCase,
    query_vec: Sequence[float],
    *,
    backend: Any,
    limit: int = 5,
) -> list[SearchResult]:
    """Run retrieval for one trust-eval case using an injected backend."""
    filters = Filters(source=case.source_filter) if case.source_filter else Filters()
    return search(
        conn,
        case.question,
        query_vec,
        filters,
        mode="hybrid",
        limit=limit,
        backend=backend,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hippo-trust-eval", description="Agent-trust eval corpus")
    parser.add_argument(
        "--cases",
        default=str(_DEFAULT_CASES),
        help="Path to trust_eval_cases.json",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate corpus schema and coverage (offline, no DB required)",
    )
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    errors = validate_corpus(cases)
    if errors:
        for err in errors:
            print(f"error: {err}", file=sys.stderr)
        return 1

    print(f"ok: {len(cases)} trust eval cases validated")
    if not args.validate:
        print(
            "hint: use --validate (default) or pytest brain/tests/test_trust_eval.py for retrieval checks"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
