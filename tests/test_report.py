"""报告层：合法 JSON、台账口径与差异说明。"""

import json
import math
import subprocess
import sys
import unittest
from pathlib import Path

from school_reserve.report import (
    actual_vs_probability,
    backtest_report,
    desk_report,
    promise_report,
    standing_forecast_report,
)
from school_reserve.tables import Dataset

ROOT = Path(__file__).resolve().parent.parent


class DeskReportShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Dataset.load()
        cls.day = cls.data.current_day()
        cls.report = desk_report(cls.data, cls.day, 560)

    def test_reports_signals_windows_and_reservations(self):
        self.assertIn("missing_signals", self.report)
        self.assertIn("occupied_windows", self.report)
        self.assertIn("windows", self.report)
        self.assertIn("reservations", self.report)
        self.assertTrue(self.report["windows"])
        self.assertTrue(self.report["reservations"])

    def test_each_executable_branch_is_explained(self):
        for item in self.report["blocked"]:
            self.assertTrue(item["blockers"])
        for item in self.report["waiting"]:
            self.assertTrue(item["missing_signals"])
        for decision in self.report["not_dispatched"]:
            self.assertTrue(decision["reason"])

    def test_exclusive_pairings_section(self):
        self.assertTrue(self.report["exclusive_pairings"])
        for pair in self.report["exclusive_pairings"]:
            self.assertEqual(len(pair["branches"]), 2)


class PromiseReportShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Dataset.load()
        cls.day = cls.data.current_day()
        cls.rows = promise_report(cls.data, cls.day)

    def test_unpromised_is_not_reported_as_unused_aid(self):
        rows = {row["class"]: row for row in self.rows}
        self.assertEqual(rows["F2"]["unused_reason"], "not_promised")
        self.assertEqual(rows["F3"]["unused_reason"], "not_promised")

    def test_promised_rows_keep_reservation_and_blockers(self):
        rows = {row["class"]: row for row in self.rows}
        self.assertTrue(rows["F1"]["promised"])
        self.assertTrue(rows["F1"]["reservations"])
        self.assertTrue(rows["F1"]["blocked_branches"])

    def test_late_release_reason_is_specific(self):
        rows = {row["class"]: row for row in self.rows}
        self.assertIn("release_after_deadline", rows["F1"]["blocked_branches"]["B1"])


class ProbabilityVsActual(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Dataset.load()
        cls.payload = actual_vs_probability(cls.data, 21, "clear")

    def test_two_tables_are_distinct(self):
        self.assertIn("probability_table", self.payload)
        self.assertIn("actual_call_table", self.payload)
        self.assertNotIn("dispatched", self.payload["probability_table"])
        self.assertTrue(self.payload["not_same_table_reasons"])

    def test_abstained_cells_absent_from_probability_commitment(self):
        table = self.payload["probability_table"]
        self.assertIn("F3", table["abstained_classes"])
        self.assertNotIn("F3", table["committable_classes"])


class JsonOutput(unittest.TestCase):
    def test_cli_all_writes_valid_json(self):
        result = subprocess.run(
            [sys.executable, "-m", "school_reserve.cli", "all", "--out", "reports"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        target = ROOT / "reports" / "all.json"
        text = target.read_text(encoding="utf-8")
        self.assertNotIn("NaN", text)
        payload = json.loads(text)
        for key in ("forecast", "backtest", "desk", "promises", "strategies", "determinism"):
            self.assertIn(key, payload)

    def test_backtest_numbers_are_finite_where_expected(self):
        result = backtest_report(Dataset.load())
        self.assertFalse(math.isnan(result["coverage"]))
        self.assertFalse(math.isnan(result["mae"]))


class StandingForecast(unittest.TestCase):
    def test_abstained_cell_reported_with_reason(self):
        rows = standing_forecast_report(Dataset.load(), 21, "clear")
        self.assertTrue(rows)
        reasons = {
            class_id: reason
            for row in rows
            for class_id, reason in row["abstained"].items()
        }
        self.assertEqual(reasons.get("F3"), "insufficient_cell_history")


if __name__ == "__main__":
    unittest.main()
