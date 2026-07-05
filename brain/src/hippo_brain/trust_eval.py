"""Agent-trust eval corpus loader and evidence-shape checker (SNUG-118).

This module defines the regression bar for agent-trust work: each case records
*expected evidence requirements* (source kinds, hit counts) rather than prose
answers tied to a live user database.

Cases marked ``"pending": true`` document future trust behavior (gap synthesis,
auto-memory linkage) but are excluded from the enforced retrieval regression
subset until later roadmap steps wire those paths.

**Adding cases:** append to ``_fixtures/trust_eval_cases.json`` with a unique
``id``, set ``evidence.source_kinds`` to logical families from
:func:`hippo_brain.source_filters.source_kind_from_linked_id`, and extend the
synthetic seeder in ``brain/tests/test_trust_eval.py`` for end-to-end checks.

Run offline::

    hippo-trust-eval
    pytest brain/tests/test_trust_eval.py -q
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from hippo_brain.retrieval import Filters, SearchResult, search
from hippo_brain.source_filters import source_kind_from_linked_id

_DEFAULT_CASES = Path(__file__).parent / "_fixtures" / "trust_eval_cases.json"
_EXPECTED_SCHEMA_VERSION = 1
_MIN_CORPUS_CASES = 12

# Enforced on non-pending cases only (claude-auto-memory pending until retrieval
# hydrates memory linked_source_ids).
_REQUIRED_SOURCE_FAMILIES = frozenset(
    {
        "shell",
        "claude",
        "codex",
        "cursor",
        "opencode",
        "browser",
        "workflow",
    }
)


class _SearchBackend(Protocol):
    def knn_search(
        self,
        conn: sqlite3.Connection,
        query_vec: Sequence[float],
        column: str = ...,
        limit: int = ...,
    ) -> list[dict]: ...

    def fts_search(
        self,
        conn: sqlite3.Connection,
        query: str,
        limit: int = ...,
    ) -> list[dict]: ...


@dataclass(frozen=True)
class EvidenceSpec:
    source_kinds: tuple[str, ...] = ()
    min_hits: int = 1
    max_hits: int | None = None
    require_captured_at: bool = False
    min_distinct_source_kinds: int = 0
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
            min_distinct_source_kinds=int(raw.get("min_distinct_source_kinds", 0)),
            expect_coverage_gap=bool(raw.get("expect_coverage_gap", False)),
            gap_reason_contains=tuple(str(g) for g in gap),
        )


@dataclass(frozen=True)
class TrustEvalCase:
    id: str
    question: str
    evidence: EvidenceSpec
    source_filter: str | None = None
    pending: bool = False
    mode: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TrustEvalCase:
        return cls(
            id=str(raw["id"]),
            question=str(raw["question"]),
            source_filter=raw.get("source_filter"),
            pending=bool(raw.get("pending", False)),
            mode=raw.get("mode"),
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
    if data.get("schema_version") != _EXPECTED_SCHEMA_VERSION:
        msg = f"unsupported schema_version {data.get('schema_version')!r}"
        raise ValueError(msg)
    return [TrustEvalCase.from_dict(c) for c in data["cases"]]


def validate_corpus(cases: Sequence[TrustEvalCase]) -> list[str]:
    """Return human-readable validation errors; empty list means corpus is well-formed."""
    errors: list[str] = []
    if len(cases) < _MIN_CORPUS_CASES:
        errors.append(f"expected at least {_MIN_CORPUS_CASES} cases, found {len(cases)}")

    seen: set[str] = set()
    covered: set[str] = set()

    for case in cases:
        if case.id in seen:
            errors.append(f"duplicate case id: {case.id}")
        seen.add(case.id)

        if case.evidence.min_hits < 0:
            errors.append(f"{case.id}: min_hits must be >= 0")
        if case.evidence.max_hits is not None and case.evidence.max_hits < case.evidence.min_hits:
            errors.append(f"{case.id}: max_hits < min_hits")

        if case.pending:
            continue

        for kind in case.evidence.source_kinds:
            covered.add(kind)

    missing = _REQUIRED_SOURCE_FAMILIES - covered
    if missing:
        errors.append(f"missing active source family coverage: {sorted(missing)}")

    has_negative_doc = any(
        c.evidence.expect_coverage_gap or c.mode == "adversarial" for c in cases
    )
    if not has_negative_doc:
        errors.append("corpus must document at least one negative/gap case (pending ok)")

    return errors


def kinds_in_results(results: Sequence[SearchResult]) -> set[str]:
    kinds: set[str] = set()
    for r in results:
        for link in r.linked_source_ids:
            kind = source_kind_from_linked_id(link)
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

    if spec.min_distinct_source_kinds > 0 and len(found_kinds) < spec.min_distinct_source_kinds:
        return EvidenceCheckResult(
            case.id,
            False,
            f"expected >= {spec.min_distinct_source_kinds} distinct source kinds, "
            f"got {sorted(found_kinds)}",
        )

    if spec.require_captured_at:
        if not any(r.captured_at > 0 for r in results):
            return EvidenceCheckResult(case.id, False, "no result has captured_at")

    return EvidenceCheckResult(case.id, True, "ok")


def run_case_search(
    conn: sqlite3.Connection,
    case: TrustEvalCase,
    query_vec: Sequence[float],
    *,
    backend: _SearchBackend,
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
    parser = argparse.ArgumentParser(
        prog="hippo-trust-eval",
        description="Validate agent-trust eval corpus (offline, no DB required)",
    )
    parser.add_argument(
        "--cases",
        default=str(_DEFAULT_CASES),
        help="Path to trust_eval_cases.json",
    )
    args = parser.parse_args(argv)

    try:
        cases = load_cases(args.cases)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    errors = validate_corpus(cases)
    if errors:
        for err in errors:
            print(f"error: {err}", file=sys.stderr)
        return 1

    active = sum(1 for c in cases if not c.pending)
    pending = len(cases) - active
    print(f"ok: {len(cases)} trust eval cases validated ({active} active, {pending} pending)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
