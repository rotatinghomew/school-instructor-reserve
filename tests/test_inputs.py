import unittest

from inspect_inputs import read_tables


class SourceRecords(unittest.TestCase):
    def test_separate_notice_and_result_times(self):
        tables = read_tables()
        first = {}
        for row in tables['timetable']:
            first[row['class']] = min(row['seen'],first.get(row['class'],row['seen']))
        for row in tables['outcomes']:
            self.assertIn(row['class'], first)
            self.assertTrue(not row['received'] or row['received'] > first[row['class']])
        self.assertGreater(len({row['week'] for row in tables['timetable']}),10)

    def test_available_window_and_later_school_decision(self):
        tables = read_tables()
        received = {row['event']:row for row in tables['reinforcements']}
        shortened = next(row for row in received.values() if row['action']=='shortened')
        original = received[shortened['reference']]
        self.assertEqual(shortened['staff'],original['staff'])
        self.assertLess(int(shortened['end']),int(original['end']))
        acceptance = next(row for row in received.values() if row['action']=='school_accepts')
        self.assertGreater(acceptance['received'],shortened['received'])
        self.assertIn(acceptance['reference'], {row['class'] for row in tables['standing']})

    def test_branch_receipts_preserve_when_decisions_were_possible(self):
        tables = read_tables()
        branches = {row['branch']:row for row in tables['mutual_aid']}
        for receipt in tables['call_round']:
            self.assertIn(receipt['target'],branches)
        self.assertTrue(any(not row['reported_minute'] for row in tables['rollcalls']))
        role_time = next(row['recorded_at'] for row in tables['call_round'] if row['kind']=='release_receipt')
        self.assertGreater(role_time,'2026-10-05T09:20')
