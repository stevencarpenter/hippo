"""Offline regression checks for the saved TypeSafe evaluation results."""

import json
from pathlib import Path
import runpy
import statistics
import unittest

ROOT = Path(__file__).resolve().parent
HARNESS = runpy.run_path(str(ROOT / "run.py"))


class SummaryTests(unittest.TestCase):
    def test_saved_summaries_replay(self) -> None:
        for cases_name, results_name in (
            ("cases", "results"),
            ("holdout", "holdout-results"),
        ):
            with self.subTest(corpus=cases_name):
                cases = json.loads((ROOT / f"{cases_name}.json").read_text())
                rows = json.loads((ROOT / f"{results_name}.json").read_text())["rows"]
                expected = json.loads((ROOT / f"{results_name}-summary.json").read_text())
                self.assertEqual(HARNESS["summarize"](rows, cases), expected)

    def test_failed_forward_request_excluded_from_both_baselines(self) -> None:
        cases = json.loads((ROOT / "cases.json").read_text())
        rows = json.loads((ROOT / "results.json").read_text())["rows"]
        for row in rows:
            if row["kind"] == "ranking" and row["id"] == "missing" and not row["reverse"]:
                del row["response"]
                row["error"] = {"type": "TimeoutError"}
        summary = HARNESS["summarize"](rows, cases)
        ranking = summary["ranking"]
        self.assertEqual(len(summary["errors"]), 1)
        self.assertEqual(ranking["top1"], [6, 6])
        self.assertEqual(ranking["baseline_original_order_top1"], [0, 6])
        expected_ndcg = statistics.mean(
            HARNESS["ndcg"](case["grades"], range(len(case["grades"])))
            for case in cases["ranking"]
            if case["id"] != "missing" and max(case["grades"])
        )
        self.assertAlmostEqual(ranking["baseline_original_order_mean_ndcg"], expected_ndcg)

    def test_empty_ranking_cohorts_preserve_other_results(self) -> None:
        cases = json.loads((ROOT / "cases.json").read_text())
        expected = json.loads((ROOT / "results-summary.json").read_text())
        zero_grade_ids = {case["id"] for case in cases["ranking"] if not max(case["grades"])}
        for cohort in ("reverse_only", "zero_grade_only"):
            with self.subTest(cohort=cohort):
                rows = json.loads((ROOT / "results.json").read_text())["rows"]
                for row in rows:
                    if row["kind"] != "ranking":
                        continue
                    keep = (
                        row["reverse"]
                        if cohort == "reverse_only"
                        else row["id"] in zero_grade_ids and not row["reverse"]
                    )
                    if not keep:
                        del row["response"]
                        row["error"] = {"type": "TimeoutError"}
                summary = HARNESS["summarize"](rows, cases)
                ranking = summary["ranking"]
                self.assertEqual(ranking["top1"], [0, 0])
                self.assertEqual(ranking["baseline_original_order_top1"], [0, 0])
                self.assertIsNone(ranking["mean_ndcg"])
                self.assertIsNone(ranking["baseline_original_order_mean_ndcg"])
                self.assertGreater(ranking["answerable_accuracy_at_0.5"][1], 0)
                for kind in ("verification", "routing"):
                    self.assertEqual(summary[kind], expected[kind])


if __name__ == "__main__":
    unittest.main()
