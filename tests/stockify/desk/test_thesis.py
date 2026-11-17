"""HD 5.1: the rule-based verdict from each strategy's signal and its grade on the stock."""
from __future__ import annotations

import unittest

import tests.support as support  # noqa: F401  — must precede project imports

from stockify.desk import replay, thesis

PRICE = 100_000          # paise
ATR = 2_000.0


def sig(ref, *, inn=True, buy=False, level=None):
    return replay.Signal(ref=ref, display_name=ref.upper(), in_position=inn, buying_today=buy,
                         exit_level_paise=level, note="")


def build(spec):
    """spec: list of (ref, grade, in?) -> (signals, grades)."""
    sigs = [sig(r, inn=i) for r, _g, i in spec]
    return sigs, {r: g for r, g, _i in spec}


class VerdictTests(unittest.TestCase):
    def test_weights(self):
        self.assertEqual(thesis.WEIGHTS, {"none": 0, "weak": 1, "some": 2, "strong": 3})

    def test_all_none_grades_is_no_reliable_signal(self):
        sigs, grades = build([("a", "none", False), ("b", "none", False), ("c", "none", True)])
        v = thesis.decide(sigs, grades, PRICE, ATR)
        self.assertEqual(v.label, "No reliable signal")
        self.assertEqual(v.exit_share, 0.0)

    def test_missing_grade_counts_as_none(self):
        v = thesis.decide([sig("a", inn=False)], {}, PRICE, ATR)
        self.assertEqual(v.label, "No reliable signal")

    def test_exit_with_strong_out_votes(self):
        sigs, grades = build([("a", "strong", False), ("b", "some", False), ("c", "weak", True)])
        v = thesis.decide(sigs, grades, PRICE, ATR)
        self.assertEqual(v.label, "Exit")
        self.assertAlmostEqual(v.exit_share, 5 / 6)
        self.assertEqual(v.counts, "2 of 3 tested rules say exit; the 1 with the best record on this stock "
                                   "is among them")

    def test_exit_needs_a_some_or_strong_out_vote(self):
        spec = [(f"w{i}", "weak", False) for i in range(7)] + [("s", "strong", True)]
        sigs, grades = build(spec)
        v = thesis.decide(sigs, grades, PRICE, ATR)
        self.assertAlmostEqual(v.exit_share, 0.7)
        self.assertEqual(v.label, "Trim")

    def test_trim_band(self):
        sigs, grades = build([("a", "some", False), ("b", "some", True), ("c", "weak", True)])
        v = thesis.decide(sigs, grades, PRICE, ATR)
        self.assertAlmostEqual(v.exit_share, 0.4)
        self.assertEqual(v.label, "Trim")

    def test_hold_when_below_trim_and_far_from_exit_level(self):
        sigs = [sig("a", level=90_000), sig("b", inn=False)]
        v = thesis.decide(sigs, {"a": "strong", "b": "weak"}, PRICE, ATR)
        self.assertEqual(v.label, "Hold")
        self.assertEqual(v.nearest_exit_paise, 90_000)

    def test_tighten_stop_within_one_atr(self):
        sigs = [sig("a", level=98_500), sig("b", level=90_000), sig("c", inn=False, level=99_900)]
        v = thesis.decide(sigs, {"a": "strong", "b": "strong", "c": "weak"}, PRICE, ATR)
        self.assertEqual(v.label, "Tighten stop")
        self.assertEqual(v.nearest_exit_paise, 98_500)       # the out strategy's level is ignored
        self.assertTrue(any("₹985.00" in r for r in v.reasons))

    def test_level_above_price_or_zero_weight_ignored(self):
        sigs = [sig("a", level=101_000), sig("b", level=99_000), sig("c", inn=True, level=None)]
        v = thesis.decide(sigs, {"a": "strong", "b": "none", "c": "strong"}, PRICE, ATR)
        self.assertEqual(v.label, "Hold")
        self.assertIsNone(v.nearest_exit_paise)

    def test_buying_today_counts_in(self):
        sigs = [sig("a", inn=False, buy=True), sig("b", inn=False, buy=True)]
        v = thesis.decide(sigs, {"a": "strong", "b": "strong"}, PRICE, ATR)
        self.assertEqual(v.exit_share, 0.0)
        self.assertEqual(v.label, "Hold")

    def test_reasons_never_mention_cost_or_profit(self):
        cases = [
            build([("a", "strong", False), ("b", "some", False)]),
            build([("a", "some", False), ("b", "some", True), ("c", "weak", True)]),
            build([("a", "none", True)]),
            ([sig("a", level=99_000)], {"a": "strong"}),
            ([sig("a")], {"a": "strong"}),
        ]
        for sigs, grades in cases:
            v = thesis.decide(sigs, grades, PRICE, ATR)
            texts = v.reasons + v.for_ + v.against + [v.counts, v.label]
            self.assertTrue(texts)
            for t in texts:
                for bad in ("cost", "profit", "p&l", "gain"):
                    self.assertNotIn(bad, t.lower())


if __name__ == "__main__":
    unittest.main()
