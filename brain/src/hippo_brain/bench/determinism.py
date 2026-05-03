"""BT-29 / post-review: deterministic-rerun verification harness.

Reads N JSONL run files produced by `hippo-bench run`, extracts the
downstream-proxy MRR + Hit@1 per model, and reports the spread. The
trust budget (MRR delta < 0.02, Hit@1 delta < 0.02) comes verbatim from
the tracking doc's Definition of Done #1.

Pure data analysis: no real bench, no LM Studio, no prod-brain pause —
the 90-min real-bench run is the operator's job. This module makes the
pass/fail criterion unambiguous so the operator's runbook is one command
instead of "eyeball the JSONL."
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# Tracking doc Definition of Done #1: Hit@1 ± 0.02, MRR ± 0.02 across reruns.
DEFAULT_MRR_BUDGET = 0.02
DEFAULT_HIT_AT_1_BUDGET = 0.02


@dataclass
class ModelMetrics:
    """Per-(run, model) extracted scores from one JSONL row."""

    run_path: Path
    model_id: str
    mrr: float | None
    hit_at_1: float | None


@dataclass
class ModelDelta:
    """Spread of one model's metrics across N runs."""

    model_id: str
    n_runs: int
    mrr_values: list[float]
    hit_at_1_values: list[float]
    mrr_delta: float
    hit_at_1_delta: float

    def passes(self, mrr_budget: float, hit_at_1_budget: float) -> bool:
        return self.mrr_delta < mrr_budget and self.hit_at_1_delta < hit_at_1_budget


@dataclass
class DeterminismReport:
    """Comparison verdict across N JSONL run files."""

    runs: list[Path]
    deltas: list[ModelDelta] = field(default_factory=list)
    mrr_budget: float = DEFAULT_MRR_BUDGET
    hit_at_1_budget: float = DEFAULT_HIT_AT_1_BUDGET

    def passes(self) -> bool:
        # Empty deltas (no model with >= 2 runs) is treated as failing — that
        # means the operator pointed at unrelated runs by mistake, which is a
        # bigger signal than "all models pass."
        if not self.deltas:
            return False
        return all(d.passes(self.mrr_budget, self.hit_at_1_budget) for d in self.deltas)

    def render(self) -> str:
        lines = [
            "# BT-29 determinism report",
            "",
            f"Runs compared: {len(self.runs)}",
            f"Budget: MRR delta < {self.mrr_budget}, Hit@1 delta < {self.hit_at_1_budget}",
            "",
            "| model | n_runs | mrr range | mrr delta | hit@1 range | hit@1 delta | verdict |",
            "|---|---|---|---|---|---|---|",
        ]
        for d in self.deltas:
            mrr_range = (
                f"{min(d.mrr_values):.4f}–{max(d.mrr_values):.4f}" if d.mrr_values else "n/a"
            )
            hit_range = (
                f"{min(d.hit_at_1_values):.4f}–{max(d.hit_at_1_values):.4f}"
                if d.hit_at_1_values
                else "n/a"
            )
            verdict = "PASS" if d.passes(self.mrr_budget, self.hit_at_1_budget) else "FAIL"
            lines.append(
                f"| {d.model_id} | {d.n_runs} | {mrr_range} | {d.mrr_delta:.4f} | "
                f"{hit_range} | {d.hit_at_1_delta:.4f} | {verdict} |"
            )
        lines.append("")
        lines.append(f"**Overall: {'PASS' if self.passes() else 'FAIL'}**")
        return "\n".join(lines)


def _extract_metrics(jsonl_path: Path) -> list[ModelMetrics]:
    """Pull every `record_type=model_summary` row out of a JSONL run file."""
    out: list[ModelMetrics] = []
    with jsonl_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("record_type") != "model_summary":
                continue
            model_dict = rec.get("model") or {}
            model_id = model_dict.get("id") or model_dict.get("model_id") or "unknown"
            proxy = rec.get("downstream_proxy") or {}
            out.append(
                ModelMetrics(
                    run_path=jsonl_path,
                    model_id=model_id,
                    mrr=proxy.get("mrr"),
                    hit_at_1=proxy.get("hit_at_1"),
                )
            )
    return out


def compare_runs(
    jsonl_paths: list[Path],
    mrr_budget: float = DEFAULT_MRR_BUDGET,
    hit_at_1_budget: float = DEFAULT_HIT_AT_1_BUDGET,
) -> DeterminismReport:
    """Compare metrics across N JSONL run files.

    Each `model_id` appearing in 2+ runs gets a delta entry; models present
    in only one run are skipped (no spread to compute) — the `n_runs` column
    in the rendered report makes coverage visible. Treats `model_summary`
    records as the source of truth; other record types in the same JSONL
    are ignored.
    """
    if len(jsonl_paths) < 2:
        raise ValueError(f"compare_runs needs >= 2 JSONL files, got {len(jsonl_paths)}")

    by_model: dict[str, list[ModelMetrics]] = {}
    for path in jsonl_paths:
        for m in _extract_metrics(path):
            by_model.setdefault(m.model_id, []).append(m)

    deltas: list[ModelDelta] = []
    for model_id, metrics in sorted(by_model.items()):
        if len(metrics) < 2:
            continue
        mrrs = [m.mrr for m in metrics if m.mrr is not None]
        hits = [m.hit_at_1 for m in metrics if m.hit_at_1 is not None]
        mrr_delta = (max(mrrs) - min(mrrs)) if len(mrrs) >= 2 else 0.0
        hit_delta = (max(hits) - min(hits)) if len(hits) >= 2 else 0.0
        deltas.append(
            ModelDelta(
                model_id=model_id,
                n_runs=len(metrics),
                mrr_values=mrrs,
                hit_at_1_values=hits,
                mrr_delta=mrr_delta,
                hit_at_1_delta=hit_delta,
            )
        )

    return DeterminismReport(
        runs=jsonl_paths,
        deltas=deltas,
        mrr_budget=mrr_budget,
        hit_at_1_budget=hit_at_1_budget,
    )
