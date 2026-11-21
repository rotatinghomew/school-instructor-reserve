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
