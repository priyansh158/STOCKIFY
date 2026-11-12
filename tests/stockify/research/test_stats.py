"""Sharpe, probabilistic and deflated Sharpe: stdlib only, checked by hand and against metrics.json."""
from __future__ import annotations

import dataclasses
import math
import random
import unittest

import tests.support as support  # noqa: F401  — must precede project imports

from stockify.oracle import metrics
from stockify.research import stats


def _all_finite(obj) -> bool:
    for f in dataclasses.fields(obj):
        v = getattr(obj, f.name)
        if isinstance(v, float) and not math.isfinite(v):
            return False
    return True


class DeflatedSharpeTests(unittest.TestCase):
    def test_dsr_matches_the_hand_computed_example(self):
        sr0 = stats.expected_max_sharpe(10, 0.0004)
        self.assertAlmostEqual(sr0, 0.031491966026915, delta=1e-9)
        dsr = stats.probabilistic_sharpe(0.1, 250, -0.5, 4.0, benchmark=sr0)
        psr = stats.probabilistic_sharpe(0.1, 250, -0.5, 4.0, benchmark=0.0)
        self.assertAlmostEqual(dsr, 0.8534253186458305, delta=1e-9)
        self.assertAlmostEqual(psr, 0.9375434364545412, delta=1e-9)

    def test_one_trial_dsr_equals_psr(self):
        rng = random.Random(3)
        x = [rng.gauss(0.001, 0.01) for _ in range(120)]
        sr = stats.sharpe_per_period(x)
        res = stats.deflated_sharpe(x, [sr], 1)
        self.assertEqual(res.sr0, 0.0)
        self.assertEqual(res.dsr, res.psr)

    def test_identical_trials_have_zero_expected_max(self):
        self.assertEqual(stats.expected_max_sharpe(10, 0.0), 0.0)
        rng = random.Random(4)
        x = [rng.gauss(0.001, 0.01) for _ in range(60)]
        sr = stats.sharpe_per_period(x)
        res = stats.deflated_sharpe(x, [sr, sr, sr], 3)
        self.assertEqual(res.var_sharpe, 0.0)
        self.assertEqual(res.sr0, 0.0)

    def test_more_trials_raise_the_bar(self):
        v = 0.0004
        self.assertGreater(stats.expected_max_sharpe(100, v), stats.expected_max_sharpe(10, v))
        self.assertGreater(stats.expected_max_sharpe(10, v), stats.expected_max_sharpe(2, v))

    def test_flat_trial_counts_in_n_not_in_v(self):
        rng = random.Random(5)
        x = [rng.gauss(0.001, 0.01) for _ in range(60)]
        res = stats.deflated_sharpe(x, [0.1, None, 0.3], 3)
        mean = (0.1 + 0.3) / 2
        expected = (0.1 - mean) ** 2 + (0.3 - mean) ** 2     # ddof 1 over two values
        self.assertAlmostEqual(res.var_sharpe, expected, delta=1e-15)
        self.assertEqual(res.n_trials, 3)

    def test_radicand_not_positive_gives_none(self):
        # 1 - skew*sr + (kurt-1)/4*sr^2 = 1 - 2 + 0 < 0
        self.assertIsNone(stats.probabilistic_sharpe(2.0, 100, 0.5, 1.0))
        self.assertIsNone(stats.probabilistic_sharpe(0.1, 1, 0.0, 3.0))


class MomentsTests(unittest.TestCase):
    def test_moments_known_series(self):
        self.assertAlmostEqual(stats.moments([0.0, 0.0, 3.0]).skew, 0.7071067811865476, delta=1e-12)
        self.assertAlmostEqual(stats.moments([-1.0, 1.0, -1.0, 1.0]).kurt, 1.0, delta=1e-12)
        self.assertAlmostEqual(stats.moments([-1.0, 0.0, 0.0, 1.0]).kurt, 2.0, delta=1e-12)
        self.assertIsNone(stats.moments([2.0, 2.0, 2.0]).skew)
        self.assertIsNone(stats.moments([2.0, 2.0, 2.0]).kurt)


class SharpeTests(unittest.TestCase):
    def test_flat_series_has_no_sharpe_but_a_sentence(self):
        for x in ([0.0] * 30, [-0.001] * 30):
            self.assertIsNone(stats.sharpe_per_period(x))
            self.assertIsNone(stats.deflated_sharpe(x, [None], 1))
        self.assertIsNone(stats.sharpe_per_period([0.01]))
        self.assertIsNone(stats.deflated_sharpe([0.01], [None], 1))
        for x in ([0.0] * 30, [-0.001] * 30, [0.01]):
            self.assertTrue(_all_finite(stats.moments(x)))

    def _check_against_metrics(self, rf):
        capital = 100_000_000
        navs = [capital + 100, capital + 5_000, capital - 2_000, capital + 7_500, capital + 6_000, capital + 9_900]
        dates = ["2025-03-03", "2025-03-04", "2025-03-05", "2025-03-06", "2025-03-07", "2025-03-10"]
        m = metrics.compute(capital=capital, equity=list(zip(dates, navs)), fills=[], orders={}, charges=[],
                            risk_free=rf, rejects={}, participation_ppm=0, exposure=None)
        x = stats.excess_returns(dates, stats.returns_from_nav(capital, navs), rf)
        self.assertIsNotNone(m["sharpe"])
        self.assertAlmostEqual(stats.annualised(stats.sharpe_per_period(x)), m["sharpe"], delta=1e-12)

    def test_sharpe_matches_metrics_json(self):
        self._check_against_metrics([])
        self._check_against_metrics([{"effective_from": "2025-01-01", "rate_ppb": 70_000_000}])
        capital = 100_000_000
        dates = ["2025-03-03", "2025-03-04", "2025-03-05"]
        m = metrics.compute(capital=capital, equity=[(d, capital) for d in dates], fills=[], orders={}, charges=[],
                            risk_free=[], rejects={}, participation_ppm=0, exposure=None)
        self.assertIsNone(m["sharpe"])
        x = stats.excess_returns(dates, stats.returns_from_nav(capital, [capital] * 3), [])
        self.assertIsNone(stats.sharpe_per_period(x))

    def test_returns_from_nav(self):
        self.assertEqual(stats.returns_from_nav(100, [110, 99]), [110 / 100 - 1, 99 / 110 - 1])
        self.assertEqual(stats.returns_from_nav(100, []), [])


class MetricsRefactorTests(unittest.TestCase):
    def test_metrics_refactor_changes_nothing(self):
        navs = [100.0, 120.0, 90.0, 130.0, 80.0]
        # one entry per day 1..T (day 0 is the starting NAV): new peak, 90, new peak, 80
        self.assertEqual(metrics.drawdown_series(navs), [0.0, 0.25, 0.0, 1 - 80 / 130])
        self.assertEqual(metrics.max_drawdown(navs), (1 - 80 / 130, 1))
        self.assertEqual(metrics.max_drawdown([100.0, 90.0, 80.0, 95.0, 100.0, 101.0]), (1 - 80.0 / 100.0, 3))
        self.assertEqual(metrics.max_drawdown([100.0]), (0.0, 0))
        self.assertEqual(metrics.max_drawdown([100.0, 110.0]), (0.0, 0))
        self.assertIs(metrics._rate_on, metrics.rate_on)
        rows = [{"effective_from": "2025-01-01", "rate_ppb": 70_000_000}]
        self.assertEqual(metrics.rate_on(rows, "2024-12-31"), 0.0)
        self.assertEqual(metrics.rate_on(rows, "2025-01-01"), 0.07)


class PropertyLoopTests(unittest.TestCase):
    def test_random_series_invariants(self):
        rng = random.Random(20261112)
        for case in range(400):
            n = rng.randint(0, 300)
            kind = case % 4
            if kind == 0:
                x = [rng.gauss(0.0005, 0.01) for _ in range(n)]
            elif kind == 1:
                x = [rng.choice([-0.001, 0.0, 0.002]) for _ in range(n)]
            elif kind == 2:
                c = rng.uniform(-0.01, 0.01)
                x = [c] * n
            else:
                c = rng.uniform(-0.01, 0.01)
                x = [c + (1e-12 if rng.random() < 0.05 else 0.0) for _ in range(n)]
            self.assertTrue(_all_finite(stats.moments(x)))
            sr = stats.sharpe_per_period(x)
            if sr is not None:
                self.assertTrue(math.isfinite(sr))
            trials = [sr] + [rng.choice([None, rng.gauss(0, 0.1)]) for _ in range(rng.randint(0, 5))]
            res = stats.deflated_sharpe(x, trials, len(trials))
            if sr is None:
                self.assertIsNone(res)
            if res is not None:
                self.assertTrue(_all_finite(res))
                for v in (res.psr, res.dsr):
                    self.assertTrue(v is None or 0.0 <= v <= 1.0)
                one = stats.deflated_sharpe(x, [sr], 1)
                self.assertEqual(one.dsr, one.psr)
        prev = stats.expected_max_sharpe(1, 0.0007)
        for n_trials in range(2, 200):
            cur = stats.expected_max_sharpe(n_trials, 0.0007)
            self.assertGreaterEqual(cur, prev)
            prev = cur


if __name__ == "__main__":
    unittest.main()
