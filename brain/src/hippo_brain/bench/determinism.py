"""BT-29 / post-review: deterministic-rerun verification harness.

Reads N JSONL run files produced by `hippo-bench run`, extracts the
downstream-proxy MRR + Hit@1 per model from the canonical retrieval mode,
and reports the spread. The trust budget (MRR delta ≤ 0.02, Hit@1 delta
≤ 0.02) comes verbatim from the tracking doc's Definition of Done #1.

Pure data analysis: no real bench, no LM Studio, no prod-brain pause —
the 90-min real-bench run is the operator's job. This module makes the
pass/fail criterion unambiguous so the operator's runbook is one command
instead of "eyeball the JSONL."

Real `downstream_proxy` shape (per `run_downstream_proxy_pass`):

    {
      "modes": {
        "hybrid":   {"hit_at_1": ..., "mrr": ..., "ndcg_at_10": ..., ...},
        "semantic": {...},
        "lexical":  {...},
      },
      "qa_count": int, "k": int, "per_item": [...],
    }

The harness defaults to comparing `hybrid` (the production retrieval path)
but the operator can pin a different mode via `--mode`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# Tracking doc Definition of Done #1: Hit@1 ± 0.02, MRR ± 0.02 across reruns.
# "Within 0.02" is the inclusive convention — `<=` not `<` (post-review C-3).
DEFAULT_MRR_BUDGET = 0.02
DEFAULT_HIT_AT_1_BUDGET = 0.02

# Float-comparison epsilon. Real bench outputs accumulate MRR/Hit@1 as means
# over scored items, so the actual numeric range is ~0.0–1.0 and arithmetic
# rounding stays at ~1e-16. 1e-9 is comfortably above that noise floor and
# still seven orders of magnitude tighter than the 0.02 budget — operators
# won't notice it, but tests near the boundary become deterministic.
_BUDGET_EPSILON = 1e-9

# Hybrid is the canonical bench retrieval path (matches BT-19 golden test
# expectations and is what production callers use). Operator can override.
DEFAULT_MODE = "hybrid"


@dataclass
class ModelMetrics:
    """Per-(run, model) extracted scores from one JSONL row."""

    run_path: Path
    model_id: str
    mrr: float | None
    hit_at_1: float | None


@dataclass
class ModelDelta:
    """Spread of one model's metrics across N runs.

    `missing_metric` is set when one or more of the compared runs lacked
    `mrr` or `hit_at_1` for the chosen mode. In that case `passes()` is
    False regardless of the deltas — determinism cannot be assessed when
    the data isn't there (post-review C-6 / CC-2).
    """

    model_id: str
    n_runs: int
    mrr_values: list[float]
    hit_at_1_values: list[float]
    mrr_delta: float
    hit_at_1_delta: float
    missing_metric: str | None = None  # e.g. "mrr in 1 of 3 runs"

    def passes(self, mrr_budget: float, hit_at_1_budget: float) -> bool:
        if self.missing_metric is not None:
            return False
        # `<=` because "within 0.02" is the inclusive convention (post-review C-3);
        # a model with exactly 0.02 spread is at-budget, not over-budget. The
        # additive epsilon form (`delta <= budget + eps` rather than the
        # mathematically-equivalent `delta - budget <= eps`) better signals
        # "we're padding the budget" to a future reader (post-review M3).
        return (
            self.mrr_delta <= mrr_budget + _BUDGET_EPSILON
            and self.hit_at_1_delta <= hit_at_1_budget + _BUDGET_EPSILON
        )


@dataclass
class DeterminismReport:
    """Comparison verdict across N JSONL run files."""

    runs: list[Path]
    deltas: list[ModelDelta] = field(default_factory=list)
    mrr_budget: float = DEFAULT_MRR_BUDGET
    hit_at_1_budget: float = DEFAULT_HIT_AT_1_BUDGET
    mode: str = DEFAULT_MODE

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
            f"Mode: {self.mode}",
            f"Budget: MRR delta ≤ {self.mrr_budget}, Hit@1 delta ≤ {self.hit_at_1_budget}",
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
            if d.missing_metric is not None:
                verdict = f"FAIL (missing: {d.missing_metric})"
            elif d.passes(self.mrr_budget, self.hit_at_1_budget):
                verdict = "PASS"
            else:
                verdict = "FAIL"
            lines.append(
                f"| {d.model_id} | {d.n_runs} | {mrr_range} | {d.mrr_delta:.4f} | "
                f"{hit_range} | {d.hit_at_1_delta:.4f} | {verdict} |"
            )
        lines.append("")
        lines.append(f"**Overall: {'PASS' if self.passes() else 'FAIL'}**")
        return "\n".join(lines)


def _extract_metrics(jsonl_path: Path, mode: str = DEFAULT_MODE) -> list[ModelMetrics]:
    """Pull every `record_type=model_summary` row out of a JSONL run file.

    Reads from `downstream_proxy["modes"][mode]` — the real shape produced by
    `run_downstream_proxy_pass`. Top-level `mrr`/`hit_at_1` on the proxy dict
    do not exist; reading them returned `None` for every real run, which
    silently default-deltaed to zero and produced false PASS verdicts
    (post-review C-7).

    `mrr` / `hit_at_1` are `None` for a row when:
      - The row has no `downstream_proxy` (e.g. embedding_fn was None on
        that run, or the proxy step raised and was captured to errors[]).
      - The chosen `mode` isn't in `downstream_proxy["modes"]`.
      - The mode dict lacks the metric key.

    `compare_runs` flags any of those cases as a per-model failure rather
    than silently skipping (post-review C-6 / CC-2).
    """
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
            mode_metrics = ((rec.get("downstream_proxy") or {}).get("modes") or {}).get(mode) or {}
            out.append(
                ModelMetrics(
                    run_path=jsonl_path,
                    model_id=model_id,
                    mrr=mode_metrics.get("mrr"),
                    hit_at_1=mode_metrics.get("hit_at_1"),
                )
            )
    return out


def compare_runs(
    jsonl_paths: list[Path],
    mrr_budget: float = DEFAULT_MRR_BUDGET,
    hit_at_1_budget: float = DEFAULT_HIT_AT_1_BUDGET,
    mode: str = DEFAULT_MODE,
) -> DeterminismReport:
    """Compare metrics across N JSONL run files.

    Each `model_id` appearing in 2+ runs gets a delta entry; models present
    in only one run are skipped (no spread to compute) — the `n_runs` column
    in the rendered report makes coverage visible. Treats `model_summary`
    records as the source of truth; other record types in the same JSONL
    are ignored.

    A model where any compared run lacks `mrr` or `hit_at_1` for the chosen
    mode is marked `missing_metric` and fails — determinism cannot be
    assessed when the metric is absent (post-review C-6 / CC-2).
    """
    if len(jsonl_paths) < 2:
        raise ValueError(f"compare_runs needs >= 2 JSONL files, got {len(jsonl_paths)}")

    by_model: dict[str, list[ModelMetrics]] = {}
    for path in jsonl_paths:
        for m in _extract_metrics(path, mode=mode):
            by_model.setdefault(m.model_id, []).append(m)

    deltas: list[ModelDelta] = []
    for model_id, metrics in sorted(by_model.items()):
        if len(metrics) < 2:
            continue
        mrrs = [m.mrr for m in metrics if m.mrr is not None]
        hits = [m.hit_at_1 for m in metrics if m.hit_at_1 is not None]

        # Post-review C-6 / CC-2: any missing metric across the compared runs
        # disqualifies this model. The previous default-to-0.0 behavior could
        # silently certify "deterministic" when one run failed to produce the
        # metric at all.
        missing_parts: list[str] = []
        if len(mrrs) != len(metrics):
            missing_parts.append(f"mrr in {len(metrics) - len(mrrs)} of {len(metrics)} runs")
        if len(hits) != len(metrics):
            missing_parts.append(f"hit_at_1 in {len(metrics) - len(hits)} of {len(metrics)} runs")
        missing_metric = "; ".join(missing_parts) if missing_parts else None

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
                missing_metric=missing_metric,
            )
        )

    return DeterminismReport(
        runs=jsonl_paths,
        deltas=deltas,
        mrr_budget=mrr_budget,
        hit_at_1_budget=hit_at_1_budget,
        mode=mode,
    )
