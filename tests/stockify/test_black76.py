"""M4.3: Black-76 and implied volatility in the oracle (C-13; A3 G17, G18, U-08)."""
from __future__ import annotations

import json
import math
import unittest
from decimal import Decimal
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine
from stockify.fno import black76 as B

VECTORS = Path(__file__).resolve().parents[1] / "fixtures" / "black76_vectors.json"


def load_vectors() -> list[dict]:
    return json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]


def t_of(v: dict) -> float:
    return v["t"] if "t" in v else v["days"] / 365.0


def tolerance(expected: str, rel: float, floor_abs: float = 0.0) -> float:
    """rel·|value| or half a unit in the last printed digit (or floor_abs), whichever is wider."""
    d = Decimal(expected)
    half_unit = 0.5 * 10.0 ** d.as_tuple().exponent
    return max(rel * abs(float(d)), half_unit, floor_abs)


def grid():  # noqa: ANN201
    """A3 G18: moneyness K/F 0.8–1.2, T 1–90 days, σ 5–80 %, calls and puts, F = 25,000, r = 6.5 %."""
    f, r = 25_000.0, 0.065
    for i in range(17):
        k = f * (0.8 + 0.025 * i)
        for days in (1, 2, 3, 5, 7, 14, 21, 30, 45, 60, 90):
            for sigma in (0.05, 0.08, 0.10, 0.15, 0.20, 0.30, 0.40, 0.60, 0.80):
                for is_call in (True, False):
                    yield is_call, f, k, days / 365.0, r, sigma


class G17ReferenceVectors(unittest.TestCase):
    def test_vectors(self):
        for v in load_vectors():
            with self.subTest(source=v["source"]):
                g = B.greeks(v["is_call"], v["f"], v["k"], t_of(v), v["r"], v["sigma"])
                self.assertAlmostEqual(g.price, float(v["price"]), delta=tolerance(v["price"], 0.0, 1e-6))
                self.assertEqual(B.price(v["is_call"], v["f"], v["k"], t_of(v), v["r"], v["sigma"]), g.price)
                for name in ("delta", "gamma", "vega"):
                    if name in v:
                        self.assertAlmostEqual(getattr(g, name), float(v[name]), delta=tolerance(v[name], 1e-9),
                                               msg=name)

    def test_identities(self):
        """Put-call parity C − P = D·(F − K), and the Greeks against central finite differences."""
        f, k, t, r, s = 24_000.0, 24_500.0, 21 / 365, 0.065, 0.18
        c, p = B.greeks(True, f, k, t, r, s), B.greeks(False, f, k, t, r, s)
        self.assertAlmostEqual(c.price - p.price, math.exp(-r * t) * (f - k), delta=1e-8)
        self.assertAlmostEqual(c.gamma, p.gamma, delta=1e-15)
        self.assertAlmostEqual(c.vega, p.vega, delta=1e-9)
        for is_call, g in ((True, c), (False, p)):
            h = 1e-3
            def at(**kw):  # noqa: ANN202, ANN003
                a = {"f": f, "t": t, "r": r, "sigma": s, **kw}
                return B.price(is_call, a["f"], k, a["t"], a["r"], a["sigma"])
            self.assertAlmostEqual(g.delta, (at(f=f + h) - at(f=f - h)) / (2 * h), delta=1e-6)
            self.assertAlmostEqual(g.gamma, (at(f=f + 1) - 2 * g.price + at(f=f - 1)) / 1.0, delta=1e-7)
            self.assertAlmostEqual(g.vega, (at(sigma=s + 1e-6) - at(sigma=s - 1e-6)) / 2e-6, delta=1e-3)
            self.assertAlmostEqual(g.rho, (at(r=r + 1e-6) - at(r=r - 1e-6)) / 2e-6, delta=1e-4)
            # theta is the change per year as time passes: −∂V/∂t
            self.assertAlmostEqual(g.theta, -(at(t=t + 1e-7) - at(t=t - 1e-7)) / 2e-7, delta=1e-2)


class G18RoundTrip(unittest.TestCase):
    def test_grid(self):
        """Price → IV → price. A point the price cannot identify (an out-of-the-money price, or an in-the-money
        time value, under one tick) returns NaN with BELOW_TICK or LOW_VEGA; every other point round-trips."""
        counts = {"round_trip": 0, "below_tick": 0, "low_vega": 0}
        for is_call, f, k, t, r, s in grid():
            value = B.price(is_call, f, k, t, r, s)
            iv, why = B.implied_vol(is_call, value, f, k, t, r)
            intrinsic = math.exp(-r * t) * max((f - k) if is_call else (k - f), 0.0)
            time_value = value - intrinsic
            where = (is_call, k, t, s, value)
            if why is not None:
                self.assertTrue(math.isnan(iv), where)
                self.assertIn(why, (B.BELOW_TICK, B.LOW_VEGA), where)
                self.assertLess(time_value, B.DEFAULT_TICK, where)
                counts["below_tick" if why == B.BELOW_TICK else "low_vega"] += 1
                continue
            back = B.price(is_call, f, k, t, r, iv)
            self.assertLessEqual(abs(back - value), 1e-8 * value, where)
            self.assertLessEqual(abs(back - value), B.PRICE_TOL, where)
            counts["round_trip"] += 1
        self.assertGreater(counts["round_trip"], 2000)
        self.assertGreater(counts["below_tick"], 500)
        self.assertGreater(counts["low_vega"], 400)

    def test_sigma_recovered_where_vega_is_healthy(self):
        for is_call, f, k, t, r, s in grid():
            g = B.greeks(is_call, f, k, t, r, s)
            if g.price >= 1.0 and g.vega >= 10.0:
                iv, why = B.implied_vol(is_call, g.price, f, k, t, r)
                self.assertIsNone(why)
                self.assertLess(abs(iv - s), 1e-9, (is_call, k, t, s))


class U08Edges(unittest.TestCase):
    F, K, T, R = 25_000.0, 25_000.0, 30 / 365, 0.065

    def test_bounds(self):
        d = math.exp(-self.R * self.T)
        f, k = 25_000.0, 24_000.0
        # below the discounted intrinsic value, for an ITM call and an ITM put
        self.assertEqual(B.implied_vol(True, d * (f - k) - 1.0, f, k, self.T, self.R)[1], B.BELOW_INTRINSIC)
        self.assertEqual(B.implied_vol(False, d * (25_500.0 - f) - 1.0, f, 25_500.0, self.T, self.R)[1],
                         B.BELOW_INTRINSIC)
        # above the upper bounds: a call above D·F, a put above D·K
        self.assertEqual(B.implied_vol(True, d * f + 0.01, f, k, self.T, self.R)[1], B.ABOVE_FORWARD)
        self.assertEqual(B.implied_vol(False, d * k + 0.01, f, k, self.T, self.R)[1], B.ABOVE_STRIKE)
        # under one tick
        self.assertEqual(B.implied_vol(True, 0.04, f, 30_000.0, self.T, self.R)[1], B.BELOW_TICK)
        self.assertEqual(B.implied_vol(False, 0.0, f, 20_000.0, self.T, self.R)[1], B.BELOW_TICK)
        # inside the bounds but needing σ > 5: a 30-day ATM call at 80 % of D·F (D-56)
        iv, why = B.implied_vol(True, 0.8 * d * f, f, f, self.T, self.R)
        self.assertTrue(math.isnan(iv))
        self.assertEqual(why, B.ABOVE_MAX_VOL)
        # prices made at the edges of the σ bracket come back
        for s in (0.05, B.SIGMA_MAX):
            value = B.price(True, f, f, self.T, self.R, s)
            iv, why = B.implied_vol(True, value, f, f, self.T, self.R)
            self.assertIsNone(why)
            self.assertAlmostEqual(iv, s, delta=1e-8)

    def test_deep_itm_and_otm(self):
        f = 25_000.0
        # deep ITM, one day, low vol: price = discounted intrinsic, no time value → LOW_VEGA, never a made-up σ
        value = B.price(True, f, 20_000.0, 1 / 365, self.R, 0.05)
        self.assertEqual(B.implied_vol(True, value, f, 20_000.0, 1 / 365, self.R)[1], B.LOW_VEGA)
        # deep OTM with real time value (90 days, 60 %): recovered
        value = B.price(True, f, 30_000.0, 90 / 365, self.R, 0.6)
        iv, why = B.implied_vol(True, value, f, 30_000.0, 90 / 365, self.R)
        self.assertIsNone(why)
        self.assertAlmostEqual(iv, 0.6, delta=1e-9)

    def test_t_to_zero(self):
        """At the one-second floor an ATM option still has ~0.4·F·σ·√t of value (₹0.36 here) and gives σ back;
        an option 100 points out of the money is worth under a tick and has no IV."""
        t = B.year_fraction(10, 10)
        self.assertEqual(t, 1 / (365 * 86_400))
        atm = B.price(True, self.F, self.K, t, self.R, 0.2)
        self.assertAlmostEqual(atm, 0.4 * self.F * 0.2 * math.sqrt(t), delta=0.01)
        iv, why = B.implied_vol(True, atm, self.F, self.K, t, self.R)
        self.assertIsNone(why)
        self.assertAlmostEqual(iv, 0.2, delta=1e-6)
        otm = B.price(True, self.F, self.F + 100.0, t, self.R, 0.2)
        self.assertLess(otm, B.DEFAULT_TICK)
        self.assertEqual(B.implied_vol(True, otm, self.F, self.F + 100.0, t, self.R)[1], B.BELOW_TICK)
        self.assertEqual(B.year_fraction(0, 86_400 * 10**9), 1 / 365)
        self.assertEqual(B.year_fraction(5 * 10**9, 0), 1 / (365 * 86_400))  # past the last moment: still 1 s

    def test_bad_inputs_raise(self):
        for args in ((0.0, 1.0, 1.0, 0.1), (1.0, 0.0, 1.0, 0.1), (1.0, 1.0, 0.0, 0.1), (1.0, 1.0, 1.0, 0.0),
                     (math.nan, 1.0, 1.0, 0.1), (1.0, 1.0, math.inf, 0.1)):
            with self.assertRaises(ValueError):
                B.price(True, args[0], args[1], args[2], 0.05, args[3])
        with self.assertRaises(ValueError):
            B.implied_vol(True, math.nan, 1.0, 1.0, 1.0, 0.05)

    def test_rate_on_dated_rows(self):
        rows = [{"effective_from": "2015-01-01", "rate_ppb": 65_000_000},
                {"effective_from": "2026-04-01", "rate_ppb": 55_000_000}]
        self.assertEqual(B.rate_on(rows, "2026-03-31"), 0.065)
        self.assertEqual(B.rate_on(rows, "2026-04-01"), 0.055)
        self.assertEqual(B.rate_on(rows, "2014-12-31"), 0.0)
        self.assertEqual(B.rate_on([], "2026-04-01"), 0.0)

    def test_pricing_config_has_a_rate(self):
        from stockify.config import loader
        rows = loader.load_json("pricing.json")["risk_free"]
        self.assertTrue(rows and all("rate" in r and "effective_from" in r for r in rows))


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class EngineCrossTests(unittest.TestCase):
    """The C++ implementation (independent, C-10) against this one on a seeded random grid (A3 §3: ≤ 1e-12
    relative). Measured: price, delta and rho bit-equal; gamma and vega within ~5e-16 (n(x) is computed as
    e^{-x²/2}·(1/√2π) here and e^{-x²/2}/√2π there); theta within ~4e-14 (its two terms cancel); every implied
    volatility and every reason identical."""

    def test_random_grid(self):
        import random
        t_mod = engine.load().testing
        rng = random.Random(4300)
        outcomes = set()
        for _ in range(20_000):
            is_call = rng.random() < 0.5
            f = rng.choice([20.0, 500.0, 2_500.0, 25_000.0, 50_000.0]) * rng.uniform(0.9, 1.1)
            k = f * rng.uniform(0.5, 1.5)
            t = rng.choice([1 / (365 * 86_400), rng.uniform(1, 400) / 365])
            r, s = rng.uniform(0.0, 0.12), rng.uniform(0.01, 2.0)
            mine = B.greeks(is_call, f, k, t, r, s)
            theirs = t_mod.b76_greeks(is_call, f, k, t, r, s)
            for name, a, b in zip(("price", "delta", "gamma", "vega", "theta", "rho"),
                                  (mine.price, mine.delta, mine.gamma, mine.vega, mine.theta, mine.rho), theirs):
                self.assertLessEqual(abs(a - b), 1e-12 * max(abs(a), abs(b)), (name, is_call, f, k, t, r, s))
            self.assertEqual(t_mod.b76_price(is_call, f, k, t, r, s), mine.price)
            target = mine.price * rng.choice([1.0, 1.0, 1.001, 0.999, 1.5, 0.5])
            want = B.implied_vol(is_call, target, f, k, t, r)
            got = t_mod.b76_implied_vol(is_call, target, f, k, t, r)
            self.assertEqual(got[1], want[1], (is_call, target, f, k, t, r))
            if want[1] is None:
                self.assertLessEqual(abs(got[0] - want[0]), 1e-12 * want[0])
            outcomes.add(want[1])
        self.assertEqual(outcomes, {None, B.BELOW_INTRINSIC, B.ABOVE_FORWARD, B.ABOVE_STRIKE, B.BELOW_TICK,
                                    B.ABOVE_MAX_VOL, B.LOW_VEGA})

    def test_year_fraction(self):
        t_mod = engine.load().testing
        for now, last in ((0, 86_400 * 10**9), (10, 10), (5 * 10**9, 0), (0, 7 * 86_400 * 10**9 + 123_456_789)):
            self.assertAlmostEqual(t_mod.b76_year_fraction(now, last), B.year_fraction(now, last), delta=1e-18)

    def test_bad_inputs_raise_value_error(self):
        t_mod = engine.load().testing
        with self.assertRaises(ValueError):
            t_mod.b76_price(True, 0.0, 1.0, 1.0, 0.05, 0.1)
        with self.assertRaises(ValueError):
            t_mod.b76_implied_vol(True, math.nan, 1.0, 1.0, 1.0, 0.05)


if __name__ == "__main__":
    unittest.main()
