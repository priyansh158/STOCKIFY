"""M6.11 (LF 6.1): the probability of backtest overfitting by combinatorially symmetric cross-validation."""
from __future__ import annotations

import dataclasses
import itertools
import json
import math
import random
import statistics
import time
import unittest

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import numpy as np

import stockify.store as store
from stockify.data import refdata
from stockify.research import pbo, plain, registry, report, seeding
from tests.stockify.research.fixtures import make_run, weekdays
from tests.stockify.research.test_plain import _bad_tokens


def noise(t: int, n: int, purpose_seed: int = 1, scale: float = 0.02) -> np.ndarray:
    rng = seeding.stream(purpose_seed, "pbo")
    return np.array([[(rng.random() - 0.5) * scale for _ in range(n)] for _ in range(t)])


def reference(x: np.ndarray, slices: int):
    """A slow pure-Python CSCV: the paper's steps one by one, Sharpe from the raw rows."""
    t, n = x.shape
    size = t // slices
    rows = [[list(map(float, x[b * size:(b + 1) * size, j])) for b in range(slices)] for j in range(n)]

    def sharpe(j: int, blocks) -> float:
        vals = [v for b in blocks for v in rows[j][b]]
        if min(vals) == max(vals):
            return 0.0
        return statistics.mean(vals) / statistics.stdev(vals)

    lambdas, losses = [], 0
    for chosen in itertools.combinations(range(slices), slices // 2):
        rest = [b for b in range(slices) if b not in chosen]
        ins = [sharpe(j, chosen) for j in range(n)]
        outs = [sharpe(j, rest) for j in range(n)]
        star = ins.index(max(ins))
        v = outs[star]
        rank = sum(1 for o in outs if o < v) + (sum(1 for o in outs if o == v) + 1) / 2
        w = rank / (n + 1)
        lambdas.append(math.log(w / (1 - w)))
        losses += v < 0
    return (sum(1 for lam in lambdas if lam <= 0) / len(lambdas), statistics.median(lambdas),
            losses / len(lambdas))


class CscvTests(unittest.TestCase):
    def test_combination_count_is_c_s_s_over_2(self):
        r = pbo.cscv(noise(320, 4), slices=16)
        self.assertEqual(r.combinations, 12_870)
        self.assertEqual(r.combinations, math.comb(16, 8))
        self.assertEqual((r.n_trials, r.slices), (4, 16))
        self.assertEqual(pbo.cscv(noise(40, 3), slices=4).combinations, 6)

    def test_dominating_trial_gives_pbo_zero(self):
        x = noise(320, 6)
        x[:, 0] += 0.05
        r = pbo.cscv(x)
        self.assertEqual(r.pbo, 0.0)
        self.assertGreater(r.median_logit, 0)
        self.assertEqual(r.prob_loss, 0.0)

    def test_pure_noise_pbo_is_about_one_half(self):
        # One noise draw swings from about 0.15 to 0.75 (the whole-sample winner persists in both halves), so the
        # seeded check is on the mean of 16 draws, 320 days x 20 tries each; over many draws it is exactly one half.
        mean = sum(pbo.cscv(noise(320, 20, purpose_seed=s)).pbo for s in range(1, 17)) / 16
        self.assertTrue(0.35 <= mean <= 0.65, mean)

    def test_pure_noise_averages_one_half_over_many_draws(self):
        rs = np.random.RandomState(5)
        mean = sum(pbo.cscv(rs.standard_normal((160, 20)), slices=8).pbo for _ in range(100)) / 100
        self.assertTrue(0.42 <= mean <= 0.58, mean)

    def test_matches_an_independent_brute_force_reference(self):
        for seed, (t, n) in ((3, (40, 3)), (4, (45, 5)), (5, (64, 3))):
            x = noise(t, n, purpose_seed=seed)
            r = pbo.cscv(x, slices=4)
            p, m, loss = reference(x, 4)
            self.assertAlmostEqual(r.pbo, p, delta=1e-12)
            self.assertAlmostEqual(r.median_logit, m, delta=1e-12)
            self.assertAlmostEqual(r.prob_loss, loss, delta=1e-12)

    def test_reference_agrees_on_ties_and_flat_columns(self):
        x = noise(40, 4, purpose_seed=9)
        x[:, 2] = x[:, 1]                       # two identical trials: tied everywhere
        x[:, 3] = 0.001                         # a flat trial: performance 0.0 on every union
        r = pbo.cscv(x, slices=4)
        p, m, loss = reference(x, 4)
        self.assertAlmostEqual(r.pbo, p, delta=1e-12)
        self.assertAlmostEqual(r.median_logit, m, delta=1e-12)
        self.assertAlmostEqual(r.prob_loss, loss, delta=1e-12)
        self.assertTrue(math.isfinite(r.median_logit))

    def test_permuting_trials_does_not_change_pbo(self):
        x = noise(160, 9, purpose_seed=6)
        base = pbo.cscv(x, slices=8)
        perm = np.random.RandomState(0).permutation(9)
        other = pbo.cscv(x[:, perm], slices=8)
        self.assertEqual((base.pbo, base.median_logit, base.prob_loss),
                         (other.pbo, other.median_logit, other.prob_loss))

    def test_result_is_deterministic(self):
        x = noise(160, 7, purpose_seed=8)
        self.assertEqual(pbo.cscv(x, slices=8), pbo.cscv(x.copy(), slices=8))

    def test_remainder_rows_are_reported(self):
        r = pbo.cscv(noise(330, 3), slices=16)
        self.assertEqual(r.rows_dropped, 10)
        self.assertEqual(r.n_days, 320)
        self.assertEqual(pbo.cscv(noise(320, 3)).rows_dropped, 0)

    def test_odd_slices_short_series_and_single_trial_refused(self):
        x = noise(320, 3)
        for bad in (15, 2, 0, -4):
            with self.assertRaises(ValueError):
                pbo.cscv(x, slices=bad)
        with self.assertRaises(ValueError):
            pbo.cscv(noise(31, 3), slices=16)                   # T < 2 * slices
        pbo.cscv(noise(32, 3), slices=16)
        with self.assertRaises(ValueError):
            pbo.cscv(noise(320, 1))                             # N < min_trials
        with self.assertRaises(ValueError):
            pbo.cscv(np.zeros(320))                             # not a T x N matrix

    def test_a_hundred_trials_stay_fast(self):
        x = noise(320, 100, purpose_seed=2)
        t0 = time.perf_counter()
        r = pbo.cscv(x)
        elapsed = time.perf_counter() - t0
        print(f"\n[pbo] 12,870 combinations x 100 trials: {elapsed:.3f} s (pbo {r.pbo:.3f})")
        self.assertLess(elapsed, 5.0)


class FamilyTests(SandboxedDBTest):
    db_modules = (store, refdata)

    def test_trial_matrix_uses_common_dates_only_and_lists_dropped_and_flat(self):
        fam = "sweep/jm"
        registry.ensure_family(fam)
        days = weekdays("2026-01-05", 30)
        rng = random.Random(1)

        def rets(n):
            return [rng.uniform(-0.01, 0.01) for _ in range(n)]
        make_run("a", fam, daily_returns=rets(30), name="a", start=days[0])
        make_run("b", fam, daily_returns=rets(20), name="b", start=days[10])
        make_run("c", fam, daily_returns=[0.0] * 25, name="c", start=days[0])      # flat
        make_run("d", fam, daily_returns=rets(20), name="d", start="2027-01-04")   # shares no day
        m = pbo.trial_matrix(fam)
        self.assertEqual(m.run_ids, ["a", "b"])
        self.assertEqual(m.dropped, ["d"])
        self.assertEqual(m.flat, ["c"])
        self.assertEqual(m.dates, days[10:25])
        self.assertEqual(m.x.shape, (15, 2))
        self.assertTrue(np.isfinite(m.x).all())

    def test_a_sweep_family_has_a_pbo_and_a_walkforward_family_has_a_note(self):
        sweep, wf = registry.job_family_id("sweep", "j1"), registry.job_family_id("walkforward", "j2")
        for tag, fam in (("s", sweep), ("w", wf)):
            registry.ensure_family(fam)
            for i in range(4):
                rng = random.Random(i)
                make_run(f"{tag}{i}", fam, daily_returns=[rng.uniform(-0.01, 0.01) for _ in range(40)],
                         name=f"t{i}")
        rep = report.family_report(sweep)
        self.assertIsNotNone(rep.pbo)
        self.assertIsNone(rep.pbo_note)
        self.assertTrue(0.0 <= rep.pbo.pbo <= 1.0)
        self.assertEqual(rep.pbo.n_trials, 4)
        self.assertIn("pbo", rep.plain)
        wrep = report.family_report(wf)
        self.assertIsNone(wrep.pbo)
        self.assertIn("different days", wrep.pbo_note)
        self.assertNotIn("pbo", wrep.plain)
        json.dumps(dataclasses.asdict(rep), allow_nan=False)

    def test_notes_for_a_single_try_and_for_too_few_common_days(self):
        one, short = "sweep/one", "sweep/short"
        registry.ensure_family(one)
        registry.ensure_family(short)
        make_run("o1", one, daily_returns=[0.001, -0.002] * 20, name="o1")
        self.assertEqual(report.family_report(one).pbo_note, "fewer than 2 tries")
        for i in range(3):
            rng = random.Random(i)
            make_run(f"s{i}", short, daily_returns=[rng.uniform(-0.01, 0.01) for _ in range(20)], name=f"s{i}")
        rep = report.family_report(short)
        self.assertIsNone(rep.pbo)
        self.assertEqual(rep.pbo_note, "too few common days")


class SentenceTests(unittest.TestCase):
    def test_pbo_sentence_plain(self):
        s = plain.pbo_sentence(0.62, 20)
        self.assertEqual(_bad_tokens(s), [], s)
        self.assertIn("62%", s)
        self.assertIn("20 tries", s)
        self.assertNotIn("\n", s)
        self.assertEqual(plain.pbo_sentence(None, 20), plain.TOO_FEW_DAYS)
        self.assertEqual(plain.pbo_sentence(float("nan"), 20), plain.TOO_FEW_DAYS)
        for p in (0.0, 1.0, 0.001, 0.999, 0.5):
            t = plain.pbo_sentence(p, 5)
            self.assertEqual(_bad_tokens(t), [], t)
        self.assertNotIn("100%", plain.pbo_sentence(0.9999, 5))
        self.assertNotIn(" 0%", plain.pbo_sentence(0.0001, 5))
        self.assertIn(" 0%", plain.pbo_sentence(0.0, 5))


if __name__ == "__main__":
    unittest.main()
