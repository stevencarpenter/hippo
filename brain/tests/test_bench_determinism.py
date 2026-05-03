"""BT-29 / post-review: tests for the determinism harness.

The 90-min real-bench run is the operator's responsibility (per ralph-state
last_error on BT-29: "blast radius is too high for autonomous loop"). What
*can* be tested autonomously is the comparison logic that decides pass/fail.
A regression here would let the operator's runbook silently report PASS on
a model that's actually flapping by 0.05 MRR run-to-run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hippo_brain.bench.determinism import (
    DEFAULT_HIT_AT_1_BUDGET,
    DEFAULT_MRR_BUDGET,
    compare_runs,
)


def _write_run(
    path: Path,
    rows: list[dict[str, object]],
) -> Path:
    """Helper: write rows as JSONL at `path`."""
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


def _summary_row(
    model_id: str, mrr: float | None = None, hit_at_1: float | None = None
) -> dict[str, object]:
    """Build a minimal model_summary record matching ModelSummaryRecordV2.to_dict shape."""
    proxy: dict[str, object] = {}
    if mrr is not None:
        proxy["mrr"] = mrr
    if hit_at_1 is not None:
        proxy["hit_at_1"] = hit_at_1
    return {
        "record_type": "model_summary",
        "run_id": "test-run",
        "model": {"id": model_id},
        "downstream_proxy": proxy,
    }


def test_three_stable_runs_pass(tmp_path: Path) -> None:
    """Canonical happy path: same model, three runs, MRR delta well under 0.02."""
    paths = [
        _write_run(tmp_path / "r1.jsonl", [_summary_row("model-A", mrr=0.40, hit_at_1=0.50)]),
        _write_run(tmp_path / "r2.jsonl", [_summary_row("model-A", mrr=0.405, hit_at_1=0.50)]),
        _write_run(tmp_path / "r3.jsonl", [_summary_row("model-A", mrr=0.41, hit_at_1=0.51)]),
    ]
    report = compare_runs(paths)

    assert report.passes(), f"deltas should be within budget; got {report.render()}"
    assert len(report.deltas) == 1
    delta = report.deltas[0]
    assert delta.model_id == "model-A"
    assert delta.n_runs == 3
    assert delta.mrr_delta == pytest.approx(0.01, abs=1e-9)
    assert delta.hit_at_1_delta == pytest.approx(0.01, abs=1e-9)


def test_mrr_blowout_fails(tmp_path: Path) -> None:
    """MRR ranges across runs by 0.05 — well above 0.02 budget. Must fail."""
    paths = [
        _write_run(tmp_path / "r1.jsonl", [_summary_row("model-B", mrr=0.40, hit_at_1=0.50)]),
        _write_run(tmp_path / "r2.jsonl", [_summary_row("model-B", mrr=0.45, hit_at_1=0.51)]),
    ]
    report = compare_runs(paths)

    assert not report.passes(), "0.05 MRR delta should fail the 0.02 budget"
    assert report.deltas[0].mrr_delta == pytest.approx(0.05, abs=1e-9)


def test_hit_at_1_blowout_fails_even_when_mrr_is_stable(tmp_path: Path) -> None:
    """Both metrics gate the verdict — blowing out one is enough to fail."""
    paths = [
        _write_run(tmp_path / "r1.jsonl", [_summary_row("model-C", mrr=0.50, hit_at_1=0.30)]),
        _write_run(tmp_path / "r2.jsonl", [_summary_row("model-C", mrr=0.501, hit_at_1=0.50)]),
    ]
    report = compare_runs(paths)

    assert not report.passes()
    delta = report.deltas[0]
    assert delta.mrr_delta == pytest.approx(0.001, abs=1e-9)
    assert delta.hit_at_1_delta == pytest.approx(0.20, abs=1e-9)


def test_models_only_in_one_run_skipped(tmp_path: Path) -> None:
    """Operator might mix run files with different model lineups — skip the
    singletons rather than flagging them as regressions (no spread to compute).
    """
    paths = [
        _write_run(
            tmp_path / "r1.jsonl",
            [
                _summary_row("model-A", mrr=0.4, hit_at_1=0.5),
                _summary_row("model-B", mrr=0.6, hit_at_1=0.7),
            ],
        ),
        _write_run(
            tmp_path / "r2.jsonl",
            [_summary_row("model-A", mrr=0.41, hit_at_1=0.51)],
        ),
    ]
    report = compare_runs(paths)

    # model-A appears in both, model-B in one — only A gets a delta entry.
    assert {d.model_id for d in report.deltas} == {"model-A"}
    assert report.passes()


def test_unrelated_runs_with_no_shared_model_fails(tmp_path: Path) -> None:
    """Bigger signal than "all models pass": empty deltas means coverage is zero.
    Treat that as failure so an operator who points the harness at the wrong
    files gets a loud error rather than a green check.
    """
    paths = [
        _write_run(tmp_path / "r1.jsonl", [_summary_row("model-X", mrr=0.4, hit_at_1=0.5)]),
        _write_run(tmp_path / "r2.jsonl", [_summary_row("model-Y", mrr=0.4, hit_at_1=0.5)]),
    ]
    report = compare_runs(paths)
    assert not report.passes()
    assert report.deltas == []


def test_at_least_two_runs_required(tmp_path: Path) -> None:
    """A single JSONL has nothing to compare against — refuse early."""
    paths = [_write_run(tmp_path / "r1.jsonl", [_summary_row("model-A", mrr=0.4, hit_at_1=0.5)])]
    with pytest.raises(ValueError, match="needs >= 2"):
        compare_runs(paths)


def test_non_summary_records_ignored(tmp_path: Path) -> None:
    """A run JSONL also contains `run_manifest` and `run_end` records; the
    harness must filter to `model_summary` only — otherwise a missing field
    on a manifest row would crash the comparison.
    """
    paths = [
        _write_run(
            tmp_path / "r1.jsonl",
            [
                {"record_type": "run_manifest", "run_id": "t", "started_at_iso": "2026-05-03"},
                _summary_row("model-A", mrr=0.40, hit_at_1=0.50),
                {"record_type": "run_end", "run_id": "t", "finished_at_iso": "2026-05-03"},
            ],
        ),
        _write_run(tmp_path / "r2.jsonl", [_summary_row("model-A", mrr=0.405, hit_at_1=0.51)]),
    ]
    report = compare_runs(paths)
    assert report.passes()
    assert len(report.deltas) == 1


def test_default_budgets_match_dod() -> None:
    """Pin the trust-budget defaults to tracking-doc DoD #1 — if these drift,
    BT-29's "trustworthy" claim drifts with them and tests should notice.
    """
    assert DEFAULT_MRR_BUDGET == 0.02
    assert DEFAULT_HIT_AT_1_BUDGET == 0.02


def test_render_includes_overall_verdict(tmp_path: Path) -> None:
    """Sanity: the rendered markdown must surface PASS/FAIL prominently — the
    operator runbook tells them to check the bottom line.
    """
    paths = [
        _write_run(tmp_path / "r1.jsonl", [_summary_row("model-A", mrr=0.4, hit_at_1=0.5)]),
        _write_run(tmp_path / "r2.jsonl", [_summary_row("model-A", mrr=0.55, hit_at_1=0.5)]),
    ]
    report = compare_runs(paths)
    rendered = report.render()
    assert "**Overall: FAIL**" in rendered
    assert "model-A" in rendered
