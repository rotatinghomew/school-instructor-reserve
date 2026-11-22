"""通知截面、预报口径与回测的可检验性质。"""

import math
import unittest

from school_reserve.forecast import (
    backtest,
    eligible,
    forecast_standing,
    forecast_week,
    summary,
)
from school_reserve.tables import Dataset
from school_reserve.timeline import later_versions, notice_instant, snapshot


class SnapshotRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Dataset.load()

    def test_snapshot_excludes_versions_after_notice(self):
        for week in self.data.weeks:
            snap = snapshot(self.data, week)
            cutoff = notice_instant(self.data, week)
            self.assertTrue(all(record.seen <= cutoff for record in snap.records))
            changed = later_versions(self.data, week)
            for records in changed.values():
                self.assertTrue(all(record.seen > cutoff for record in records))

    def test_closed_class_not_treated_as_zero_demand(self):
        closed = [
            class_id
            for class_id, rows in later_versions(self.data, 0).items()
            if any(not record.open for record in rows)
        ]
        self.assertIn("00-2-1", closed)
        week_zero = snapshot(self.data, 0)
        self.assertTrue(any(record.class_id == "00-2-1" and record.open for record in week_zero.records))

    def test_receipt_missing_is_not_a_failure_confirm(self):
        missing = [
            class_id
            for class_id, outcome in self.data.outcomes.items()
            if outcome.state == "receipt_missing"
        ]
        self.assertTrue(missing)
        for class_id in missing:
            outcome = self.data.outcomes[class_id]
            self.assertIsNone(outcome.received)
            self.assertFalse(outcome.delivered)
            self.assertFalse(outcome.cancelled)


class ForecastRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Dataset.load()
        cls.origin = 21
        cls.forecasts = forecast_week(cls.data, cls.origin)

    def test_history_uses_only_confirmed_days(self):
        for forecast in self.forecasts:
            self.assertNotIn(forecast.abstain_reason, ("unknown_outcome",))
            if forecast.basis != "empty":
                self.assertGreaterEqual(forecast.sample, 1)

    def test_interval_covers_permitted_range(self):
        for forecast in self.forecasts:
            if forecast.abstain_reason is not None:
                continue
            self.assertLessEqual(forecast.low, forecast.high)
            self.assertGreaterEqual(forecast.low, 0)
            self.assertLessEqual(forecast.high, len(forecast.committable))
            self.assertAlmostEqual(sum(forecast.distribution.values()), 1.0, places=6)

    def test_insufficient_cells_abstain_without_cross_site_average(self):
        standing = forecast_standing(self.data, self.origin, "clear")
        abstained = {
            class_id
            for forecast in standing
            for class_id, reason in forecast.rejected.items()
        }
        self.assertTrue(abstained)
        for forecast in standing:
            for cell in forecast.cells:
                if cell.class_id in abstained:
                    self.assertFalse(cell.committable)
                    self.assertIsNone(cell.failure_probability)
                    self.assertEqual(cell.reason, "insufficient_cell_history")

    def test_joint_risk_uses_same_day_clustering(self):
        for forecast in self.forecasts:
            if forecast.joint_priority_risk is None:
                continue
            self.assertGreaterEqual(forecast.joint_priority_risk, 0.0)
            self.assertLessEqual(forecast.joint_priority_risk, 1.0)
            # 联合口径与独立边际对照不必相等，但都必须是合法概率。
            self.assertGreaterEqual(forecast.independent_priority_risk, 0.0)
            self.assertLessEqual(forecast.independent_priority_risk, 1.0)


class BacktestRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Dataset.load()
        cls.records = backtest(cls.data)
        cls.summary = summary(cls.records, cls.data.rules)

    def test_calibration_set_excludes_cancelled_and_unknown(self):
        included = eligible(self.records)
        self.assertTrue(included)
        for record in included:
            self.assertEqual(record.cancelled, 0)
            self.assertEqual(record.unknown, 0)
        self.assertGreater(self.summary["excluded_closed_days"], 0)
        self.assertGreater(self.summary["excluded_unknown_days"], 0)

    def test_reports_coverage_and_high_demand_losses(self):
        for key in ("coverage", "mae", "joint_priority_brier"):
            self.assertFalse(math.isnan(self.summary[key]))
        self.assertIn("high_demand", self.summary)
        self.assertGreaterEqual(self.summary["lost_minutes"], 0)
        self.assertLessEqual(
            self.summary["high_demand"]["lost_minutes"],
            self.summary["lost_minutes"],
        )

    def test_baselines_include_independent_sum_and_fixed_promise(self):
        for name in ("climatology", "independent_sites", "fixed_promise"):
            self.assertIn(name, self.summary["mae_baselines"])
            self.assertIn(name, self.summary["coverage_baselines"])
        independent = self.summary["mae_baselines"]["independent_sites"]
        self.assertGreater(independent, self.summary["mae"])

    def test_rolling_origins_are_forward_only(self):
        for record in self.records:
            self.assertGreaterEqual(record.origin, self.data.rules["min_history_weeks_per_type_class"])
            week = next(
                week
                for week, start in self.data.week_start.items()
                if start == record.day
            ) if record.day in self.data.week_start.values() else None
            if week is not None:
                self.assertGreaterEqual(record.origin, week)


if __name__ == "__main__":
    unittest.main()
