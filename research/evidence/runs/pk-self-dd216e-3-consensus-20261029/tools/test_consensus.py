"""Synthetic-only tests; no stored verdicts or labels are opened."""
import itertools
import unittest

from consensus import decide, run_vote, sign_p, wilson


class ConsensusTests(unittest.TestCase):
    def test_majority_three_valued(self):
        for votes in itertools.product((False, True, None), repeat=3):
            available = [v for v in votes if v is not None]
            self.assertEqual(decide(votes, "majority"), None if len(available) < 2 else sum(available) >= 2)

    def test_sensitivities(self):
        self.assertIs(decide([True, None], "and"), None)
        self.assertIs(decide([False, None], "and"), False)
        self.assertIs(decide([True, None], "union"), True)
        self.assertIs(decide([False, None], "union"), None)
        self.assertIs(decide([False, False], "union"), False)

    def test_run_unknown_and_alarm_priority(self):
        self.assertIs(run_vote([]), False)
        self.assertIs(run_vote([False, None]), None)
        self.assertIs(run_vote([None, True]), True)
        self.assertIs(run_vote([False, False]), False)

    def test_statistics(self):
        self.assertIsNone(sign_p(0, 0))
        self.assertEqual(sign_p(3, 0), 0.25)
        self.assertAlmostEqual(wilson(0, 70)[1], 0.0520, places=3)
        self.assertAlmostEqual(wilson(2, 70)[1], 0.0984, places=3)


if __name__ == "__main__":
    unittest.main()
