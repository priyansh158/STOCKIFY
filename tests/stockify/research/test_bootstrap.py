"""Stationary block bootstrap: seeded, deterministic 90% intervals for Sharpe, mean daily return and max drawdown."""
from __future__ import annotations

import dataclasses
import json
import random
import statistics
import unittest
from datetime import date

import tests.support as support  # noqa: F401  — must precede project imports

from stockify.research import bootstrap, seeding

LAST = date(2026, 11, 4)


def _noise(n: int, seed: int = 5, drift: float = 0.001, sd: float = 0.01) -> list[float]:
    rng = random.Random(seed)
    return [drift + rng.gauss(0, sd) for _ in range(n)]


def _call(r, x=None, **kw):
    args = dict(mean_block=5.0, resamples=200, ci=0.9, min_days=20, seed=7, last_date=LAST)
    args.update(kw)
    return bootstrap.bootstrap_ci(r, r if x is None else x, **args)


class PercentileTests(unittest.TestCase):
    def test_percentile_hand_checked(self):
        v = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(bootstrap.percentile(v, 0.0), 1.0)
        self.assertEqual(bootstrap.percentile(v, 0.5), 2.5)
        self.assertEqual(bootstrap.percentile(v, 1.0), 4.0)
        self.assertEqual(bootstrap.percentile(v, 0.25), 1.75)


class IndexTests(unittest.TestCase):
    def test_length_is_n_in_range_and_deterministic_property(self):
        meta = random.Random(99)
        for _ in range(60):
            n = meta.randint(1, 300)
            mb = meta.choice([1.0, 1.5, 2.0, 5.0, 10.0, 40.0])
            seed = meta.randint(0, 10**6)
            a = bootstrap.stationary_indices(n, mb, seeding.stream(seed, "bootstrap", LAST))
            b = bootstrap.stationary_indices(n, mb, seeding.stream(seed, "bootstrap", LAST))
            self.assertEqual(len(a), n)
            self.assertTrue(all(0 <= i < n for i in a))
            self.assertEqual(a, b)

    def test_length_is_n(self):
        idx = bootstrap.stationary_indices(37, 4.0, seeding.stream(1, "bootstrap", LAST))
        self.assertEqual(len(idx), 37)

    def test_mean_block_one_is_iid_resampling(self):
        n = 20
        # the series is 20,000 draws over n = 20 values: build them in one call with n = 20 repeated
        rng = seeding.stream(3, "bootstrap", LAST)
        idx = []
        while len(idx) < 20000:
            idx.extend(bootstrap.stationary_indices(n, 1.0, rng))
        idx = idx[:20000]
        # inside each chunk of n the pairs are consecutive draws of one call; chunk edges add negligible bias
        nxt = sum(1 for a, b in zip(idx, idx[1:]) if b == (a + 1) % n)
        self.assertAlmostEqual(nxt / (len(idx) - 1), 1 / n, delta=0.02)

    def test_mean_block_length_is_about_the_setting(self):
        n = 1000
        rng = seeding.stream(4, "bootstrap", LAST)
        idx = []
        while len(idx) < 50000:
            idx.extend(bootstrap.stationary_indices(n, 10.0, rng))
        runs, cur = [], 1
        for a, b in zip(idx, idx[1:]):
            if b == (a + 1) % n:
                cur += 1
            else:
                runs.append(cur)
                cur = 1
        runs.append(cur)
        self.assertAlmostEqual(sum(runs) / len(runs), 10.0, delta=1.0)

    def test_indices_wrap_circularly(self):
        # a huge mean block almost never restarts: the result is range(n) rotated to its start, wrapping n-1 -> 0
        n, wrapped = 8, 0
        for seed in range(1, 12):
            idx = bootstrap.stationary_indices(n, 1e9, seeding.stream(seed, "bootstrap", LAST))
            self.assertEqual(idx, [(idx[0] + k) % n for k in range(n)])
            wrapped += idx[0] != 0
        self.assertGreater(wrapped, 0)


class IntervalTests(unittest.TestCase):
    def test_same_seed_same_result_and_other_seed_differs(self):
        r = _noise(120)
        a, b = _call(r), _call(r)
        self.assertEqual(a, b)
        c = _call(r, seed=8)
        self.assertNotEqual(a.sharpe, c.sharpe)

    def test_constant_series_interval_collapses_to_the_point(self):
        r = [0.001] * 60
        res = _call(r)
        self.assertTrue(res.enough_days)
        self.assertEqual(res.sharpe, bootstrap.Interval(None, None, None))
        for iv in (res.mean_daily_return, res.max_drawdown):
            self.assertAlmostEqual(iv.lo, iv.point, delta=1e-12)
            self.assertAlmostEqual(iv.hi, iv.point, delta=1e-12)
        self.assertEqual(res.max_drawdown.point, 0.0)

    def test_intervals_are_ordered(self):
        res = _call(_noise(250))
        for iv in (res.sharpe, res.mean_daily_return, res.max_drawdown):
            self.assertLessEqual(iv.lo, iv.hi)
        self.assertTrue(0 <= res.max_drawdown.lo <= res.max_drawdown.hi <= 1)

    def test_interval_contains_the_point_for_mean_return_and_sharpe(self):
        """Mean and Sharpe are smooth statistics, so their percentile interval brackets the point. Drawdown is NOT
        asserted: a percentile bootstrap of a maximum is biased (resampled paths break up the original's worst run)."""
        res = _call(_noise(250), resamples=400)
        for iv in (res.sharpe, res.mean_daily_return):
            self.assertLessEqual(iv.lo, iv.point)
            self.assertLessEqual(iv.point, iv.hi)

    def test_mean_interval_coverage_on_a_known_distribution(self):
        true_mean, hits = 0.001, 0
        rng = seeding.stream(21, "bootstrap", LAST)
        nd = statistics.NormalDist(true_mean, 0.01)
        for rep in range(100):
            r = []
            for _ in range(60):
                u = (rng.next_u32() + 0.5) / 2**32          # strictly inside (0, 1)
                r.append(nd.inv_cdf(u))
            res = _call(r, seed=1000 + rep, mean_block=3.0, resamples=200, ci=0.9, min_days=20)
            mi = res.mean_daily_return
            if mi.lo <= true_mean <= mi.hi:
                hits += 1
        self.assertGreaterEqual(hits / 100, 0.75)
        self.assertLessEqual(hits / 100, 0.99)

    def test_short_series_bootstrap_says_so(self):
        res = _call([0.01, -0.01, 0.02, 0.0, 0.01], min_days=20)
        self.assertFalse(res.enough_days)
        none = bootstrap.Interval(None, None, None)
        self.assertEqual((res.sharpe, res.mean_daily_return, res.max_drawdown), (none, none, none))

    def test_zero_variance_resample_is_dropped_not_nan(self):
        r = [0.0] * 25 + [0.01, -0.02, 0.015, 0.005, -0.01]
        res = _call(r, mean_block=1.5, resamples=300, min_days=20)
        self.assertIsNotNone(res.sharpe.lo)
        for v in (res.sharpe.lo, res.sharpe.hi, res.sharpe.point):
            self.assertEqual(v, v)
            self.assertNotIn(v, (float("inf"), float("-inf")))

    def test_results_are_finite_json(self):
        for r in (_noise(100), [0.001] * 40, [0.0] * 3):
            res = _call(r)
            json.dumps(dataclasses.asdict(res), allow_nan=False)


if __name__ == "__main__":
    unittest.main()
