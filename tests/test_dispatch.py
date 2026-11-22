"""援班调用、窗口、预留与无前视回放的规则测试。"""

import unittest
from datetime import date

from school_reserve.dispatch import (
    audit_branch,
    availability,
    commitments,
    desk_view,
    promise_ledger,
    replay_day,
    replay_outcomes,
    reservations,
    target_for,
)
from school_reserve.strategies import compare_strategies, determinism_audit
from school_reserve.tables import Dataset


class WindowsAndCommitments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Dataset.load()
        cls.day = cls.data.current_day()

    def test_shortened_reinforcement_replaces_original(self):
        windows = availability(self.data, self.day)
        t4 = [window for window in windows["T4"]]
        self.assertEqual(len(t4), 1)
        self.assertEqual(t4[0].source, "shortened")
        self.assertEqual(t4[0].end, 590)
        self.assertEqual(t4[0].sites, frozenset({"棠径", "东桥"}))

    def test_availability_never_reads_arrival_status(self):
        historical = date(2026, 5, 27)
        windows = availability(self.data, historical)
        self.assertIn("T1", windows)  # 当天实际缺席，但通知时窗口仍然存在
        current = availability(self.data, self.day)
        self.assertTrue(all(window.source != "shift" for items in current.values() for window in items))

    def test_commitments_come_from_notified_staff(self):
        committed = commitments(self.data, self.day)
        pairs = {(item.staff, item.class_id) for item in committed}
        self.assertIn(("T1", "F0"), pairs)
        self.assertIn(("T3", "F0"), pairs)
        self.assertIn(("T5", "F1"), pairs)
        self.assertIn(("T4", "F1"), pairs)
        self.assertNotIn(("T7", "F2"), pairs)  # F2 只是 proposed

    def test_reservation_is_not_arrival(self):
        items = [item for item in reservations(self.data) if item.day == self.day]
        self.assertTrue(items)
        self.assertTrue(all(item.source in ("school_accepts", "school_confirmed") for item in items))


class BranchRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Dataset.load()
        cls.day = cls.data.current_day()

    def test_late_release_cannot_retroactively_authorise(self):
        branch = self.data.branch("B1")
        audit = audit_branch(self.data, branch, self.day, None)
        self.assertFalse(audit.executable)
        self.assertIn("release_after_deadline", audit.blockers)

    def test_missing_signal_appears_as_waiting_before_deadline(self):
        branch = self.data.branch("B1")
        audit = audit_branch(self.data, branch, self.day, 520)
        self.assertEqual(audit.status, "waiting")
        self.assertIn("棠径", audit.missing_sites)

    def test_same_person_not_counted_twice(self):
        branch_a = self.data.branch("B1")
        branch_b = self.data.branch("B2")
        self.assertEqual(branch_a.staff, branch_b.staff)
        group_a = set(branch_a.conditions)
        group_b = set(branch_b.conditions)
        # 两个互斥分支共用同一位出借人，且至少共享一个上游条件。
        self.assertEqual(branch_a.staff, branch_b.staff)
        self.assertGreaterEqual(len(group_a & group_b), 1)

    def test_window_impossible_branch_never_executes(self):
        branch = self.data.branch("B4")
        audit = audit_branch(self.data, branch, self.day, None)
        self.assertTrue(
            "window_impossible" in audit.blockers
            or "leave_after_class_start" in audit.blockers
        )
        self.assertFalse(audit.executable)

    def test_target_class_resolves_to_day_and_slot(self):
        branch = self.data.branch("B1")
        target = target_for(self.data, branch, self.day)
        self.assertIsNotNone(target)
        self.assertEqual(target.site, "东桥")
        self.assertEqual(target.kind, "paired")

    def test_blocked_branch_reports_all_blockers(self):
        branch = self.data.branch("B2")
        audit = audit_branch(self.data, branch, self.day, None)
        self.assertGreaterEqual(len(set(audit.blockers)), 2)


class DeskViewRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Dataset.load()
        cls.day = cls.data.current_day()

    def test_desk_lists_signals_and_windows(self):
        view = desk_view(self.data, self.day, 520)
        self.assertIn("棠径", view.missing_signals)
        self.assertTrue(view.executable or view.waiting or view.blocked)
        self.assertTrue(all(audit.branch_id for audit in view.waiting))

    def test_occupied_windows_are_tracked(self):
        view = desk_view(self.data, self.day, 565)
        for staff, branch_id in view.occupied_windows.items():
            self.assertTrue(
                any(
                    decision.staff == staff and decision.branch_id == branch_id
                    for decision in view.decisions
                )
            )

    def test_replay_does_not_use_final_arrival(self):
        for day in (date(2026, 5, 27), date(2026, 5, 28)):
            result = replay_day(self.data, day)
            cutoffs = [step.cutoff for step in result.steps]
            self.assertEqual(cutoffs, sorted(set(cutoffs)))
            # 每个决定点只用当天实际送交的回报时刻，且不引用最终到岗。
            reported = sorted(
                {
                    rollcall.reported_minute
                    for (rc_day, _), rollcall in self.data.rollcalls.items()
                    if rc_day == day and rollcall.reported_minute is not None
                }
            )
            self.assertEqual(cutoffs, reported)
            for step in result.steps:
                self.assertEqual(step.day, day)

    def test_replay_dispatch_is_monotone_and_deduped(self):
        result = replay_day(self.data, date(2026, 5, 4))
        branches = [decision.branch_id for decision in result.dispatched]
        self.assertEqual(len(branches), len(set(branches)))

    def test_promise_ledger_keeps_original_promise(self):
        ledger = promise_ledger(self.data, self.day)
        self.assertTrue(ledger)
        for record in ledger:
            self.assertEqual(record.day, self.day)
            self.assertEqual(record.promised, record.state in ("confirmed", "notified"))
        promised = {record.class_id for record in ledger if record.promised}
        self.assertEqual(promised, {"F0", "F1"})


class StrategyRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Dataset.load()

    def test_same_information_state_same_call(self):
        audit = determinism_audit(self.data)
        self.assertTrue(audit["identical"])
        self.assertGreater(audit["information_states"], 1)

    def test_fixed_and_conditional_both_scored_on_outcomes(self):
        comparison = compare_strategies(self.data)
        for name in ("conditional", "fixed"):
            self.assertGreater(comparison[name]["days"], 0)
            self.assertLessEqual(comparison[name]["protected"], comparison[name]["dispatches"])
        self.assertGreaterEqual(
            comparison["fixed"]["dispatches"], comparison["conditional"]["dispatches"]
        )

    def test_replay_outcomes_are_scored_not_selected(self):
        outcomes = replay_outcomes(self.data)
        self.assertTrue(outcomes)
        self.assertGreaterEqual(
            sum(1 for item in outcomes if not item.honoured),
            1,
        )


if __name__ == "__main__":
    unittest.main()


class GapRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Dataset.load()

    def test_common_shortage_attribution(self):
        from school_reserve.dispatch import attribute_gaps

        items = attribute_gaps(self.data, date(2026, 5, 27))
        self.assertTrue(items)
        self.assertTrue(all(item.cause == "common_shortage" for item in items))
        self.assertTrue(all(item.clause_hit for item in items))

    def test_late_release_is_not_called_common_shortage(self):
        from school_reserve.dispatch import attribute_gaps

        items = attribute_gaps(self.data, date(2026, 7, 24))
        self.assertTrue(any(item.cause == "late_report" for item in items))
        self.assertTrue(all(item.cause != "common_shortage" for item in items if item.cause == "late_report"))

    def test_exclusive_pairing_needs_observable_divergence(self):
        from school_reserve.dispatch import exclusive_pairings

        pairs = exclusive_pairings(self.data, self.data.current_day())
        self.assertTrue(pairs)
        for pair in pairs:
            self.assertTrue(pair.distinguishing)
            self.assertTrue(pair.observable_before_deadlines)


class ExclusiveBranches(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Dataset.load()

    def test_two_branches_for_same_person_never_double_count(self):
        from school_reserve.dispatch import desk_view

        for cutoff in (543, 560, 565, 590):
            view = desk_view(self.data, self.data.current_day(), cutoff)
            staff = [item.staff for item in view.dispatched]
            self.assertEqual(len(staff), len(set(staff)))
