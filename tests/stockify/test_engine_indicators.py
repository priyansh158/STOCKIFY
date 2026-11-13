"""M3.10: the engine's indicators equal stockify.strategy's bit for bit (A2 §4.3, A3 U-05).

Same seeded random price series into both; every result is compared as its float64 bit pattern, so a NaN
during warm-up must be NaN on both sides and a value must agree to the last bit. Skipped when the engine is
not built (`run.ps1 -BuildEngine`).
"""
from __future__ import annotations

import random
import struct
import unittest

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, strategy as S


def _bits(x: float) -> int:
    return struct.unpack("<Q", struct.pack("<d", x))[0]


def _series(rng: random.Random) -> list[int]:
    """A random walk in paise: penny stocks to index levels, flat stretches, and the odd jump."""
    n = rng.choice([0, 1, 2, 3, 5, 14, 15, 40, 300])
    p = rng.choice([5, 1_000, 250_000, 2_500_000, 10**15])
    out = []
    for _ in range(n):
        move = rng.choice([0, 0, rng.randint(-50, 50), rng.randint(-5_000, 5_000), rng.randint(-p // 3, p // 3)])
        p = max(1, p + move)
        out.append(p)
    return out


def _hlc(rng: random.Random, closes: list[int]) -> list[tuple[int, int, int]]:
    out = []
    for c in closes:
        up, down = rng.randint(0, 1 + c // 50), rng.randint(0, 1 + c // 50)
        out.append((c + up, max(1, c - down), c))
    return out


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class IndicatorParityTests(unittest.TestCase):
    def setUp(self):
        self.t = engine.load().testing

    def assert_same(self, got: float, want: float, what: str):
        self.assertEqual(_bits(got), _bits(want), f"{what}: engine {got!r} oracle {want!r}")

    def test_random_series(self):
        rng = random.Random(510)
        checked = 0
        for case in range(3000):
            closes = _series(rng)
            hlc = _hlc(rng, closes)
            for n in sorted({0, 1, 2, 3, 14, rng.randint(1, 50), len(closes), len(closes) + 1}):
                if n < 0:
                    continue
                what = f"case {case} n={n} len={len(closes)}"
                self.assert_same(self.t.sma(closes, n), S.sma(closes, n), "sma " + what)
                self.assert_same(self.t.ema(closes, n), S.ema(closes, n), "ema " + what)
                self.assert_same(self.t.rsi(closes, n), S.rsi(closes, n), "rsi " + what)
                self.assert_same(self.t.atr(hlc, n), S.atr(hlc, n), "atr " + what)
                checked += 4
        self.assertGreater(checked, 50_000)

    def test_edges(self):
        flat = [100] * 20
        self.assertEqual(self.t.rsi(flat, 14), 100.0)  # no losses at all
        self.assertEqual(self.t.rsi(list(range(100, 80, -1)), 14), 0.0)
        for f in (self.t.sma, self.t.ema, self.t.rsi):
            self.assertTrue(S.is_nan(f([], 1)))
            self.assertTrue(S.is_nan(f(flat, 0)))
            self.assertTrue(S.is_nan(f(flat, -3)))
        self.assertTrue(S.is_nan(self.t.rsi(flat[:14], 14)))  # needs n + 1 closes
        self.assertTrue(S.is_nan(self.t.atr([(1, 1, 1)] * 14, 14)))
        # Sums wider than 64 bits are exact before the one rounding to float, as Python's are.
        big = [2**62, 2**62, 2**62 + 1]
        self.assert_same(self.t.sma(big, 3), S.sma(big, 3), "sma of a 2^63+ sum")
        self.assert_same(self.t.ema(big, 2), S.ema(big, 2), "ema of a 2^63+ sum")

    def test_overflowing_steps_fail_instead_of_wrapping(self):
        lo, hi = -(2**63), 2**63 - 1
        with self.assertRaises(OverflowError):
            self.t.rsi([lo, hi], 1)  # a change wider than int64
        with self.assertRaises(OverflowError):
            self.t.atr([(hi, lo, 0), (hi, lo, 0)], 1)


if __name__ == "__main__":
    unittest.main()
