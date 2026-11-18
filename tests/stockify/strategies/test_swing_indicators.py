"""HD 1.1: the swing indicator functions and the SwingLong base class (hand-computed fixtures)."""
from __future__ import annotations

import math
import unittest
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import strategy
from stockify.strategies import _swing as sw


def _nan(x: float) -> bool:
    return x != x


class SwingIndicatorTests(unittest.TestCase):
    def test_ema_matches_hand_values(self):
        out = sw.ema_series([1000, 1100, 1200, 1300, 1400], 3)
        self.assertTrue(_nan(out[0]) and _nan(out[1]))
        # seed = SMA(1000,1100,1200) = 1100; alpha = 2/(3+1) = 0.5
        self.assertAlmostEqual(out[2], 1100.0)
        self.assertAlmostEqual(out[3], 1200.0)
        self.assertAlmostEqual(out[4], 1300.0)

    def test_macd_signal_cross_on_known_series(self):
        closes = [10000] * 40 + [10000 + 100 * i for i in range(1, 16)] + [11500 - 200 * i for i in range(1, 16)]
        line, sig = sw.macd(closes)
        self.assertEqual(len(line), len(closes))
        self.assertTrue(_nan(line[24]) and not _nan(line[25]))          # slow - 1 = 25
        self.assertTrue(_nan(sig[32]) and not _nan(sig[33]))            # 25 + signal - 1 = 33
        fast, slow = sw.ema_series(closes, 12), sw.ema_series(closes, 26)
        self.assertAlmostEqual(line[50], fast[50] - slow[50])
        self.assertAlmostEqual(line[39], 0.0)                           # flat: both EMAs equal the price
        diff = [m - s for m, s in zip(line, sig) if not _nan(s)]
        self.assertAlmostEqual(diff[0], 0.0)
        self.assertGreater(max(diff), 0)                                # crossed above in the rise
        self.assertLess(diff[-1], 0)                                    # and back below after the fall

    def test_bollinger_middle_is_sma_and_bands_symmetric(self):
        closes = [100, 200, 300, 400, 500]
        lo, mid, up = sw.bollinger(closes, 4, 2.0)
        self.assertTrue(all(_nan(x) for x in (lo[2], mid[2], up[2])))
        self.assertAlmostEqual(mid[3], 250.0)
        sigma = math.sqrt(((150 ** 2) + (50 ** 2) * 2 + (150 ** 2)) / 4)   # population: sqrt(12500)
        self.assertAlmostEqual(up[3], 250 + 2 * sigma)
        self.assertAlmostEqual(lo[3], 250 - 2 * sigma)
        self.assertAlmostEqual(mid[4], 350.0)
        for i in (3, 4):
            self.assertAlmostEqual(up[i] - mid[i], mid[i] - lo[i])

    def test_donchian_excludes_current_bar(self):
        highs = [10, 12, 11, 30, 13]
        lows = [5, 6, 4, 7, 8]
        hh, ll = sw.donchian(highs, lows, 3)
        self.assertTrue(all(_nan(x) for x in hh[:3]) and all(_nan(x) for x in ll[:3]))
        self.assertEqual(hh[3], 12.0)       # bars 0..2, not the 30 on bar 3
        self.assertEqual(ll[3], 4.0)
        self.assertEqual(hh[4], 30.0)
        self.assertEqual(ll[4], 4.0)

    def test_atr_wilder_first_value_is_mean_true_range(self):
        highs = [110, 115, 120, 118]
        lows = [100, 105, 108, 110]
        closes = [105, 112, 110, 115]
        # TR: 10, max(10,|115-105|,|105-105|)=10, max(12,|120-112|,|108-112|)=12, max(8,|118-110|,|110-110|)=8
        atr = sw.atr_series(highs, lows, closes, 3)
        self.assertTrue(_nan(atr[0]) and _nan(atr[1]))
        self.assertAlmostEqual(atr[2], 32 / 3)
        self.assertAlmostEqual(atr[3], (32 / 3 * 2 + 8) / 3)

    def test_supertrend_flips_down_after_a_drop_below_the_lower_band(self):
        highs = [1010] * 8 + [510]
        lows = [990] * 8 + [490]
        closes = [1000] * 8 + [500]
        line, up = sw.supertrend(highs, lows, closes, 3, 1.0)
        self.assertEqual(len(line), 9)
        self.assertTrue(_nan(line[0]) and not up[0])
        self.assertTrue(up[7])
        self.assertAlmostEqual(line[7], 1000 - 20)           # hl2 - 1 * ATR(20)
        self.assertFalse(up[8])
        self.assertGreater(line[8], closes[8])                # the line is now the upper band, above price

    def test_chandelier_is_high_minus_3_atr(self):
        highs = [110, 115, 120, 118]
        lows = [100, 105, 108, 110]
        closes = [105, 112, 110, 115]
        atr = sw.atr_series(highs, lows, closes, 3)
        out = sw.chandelier(highs, lows, closes, 3, 3.0)
        self.assertTrue(_nan(out[1]))
        self.assertAlmostEqual(out[2], 120 - 3 * atr[2])
        self.assertAlmostEqual(out[3], 120 - 3 * atr[3])

    def test_warmup_values_are_nan(self):
        c = list(range(1000, 1060))
        h, l = [x + 5 for x in c], [x - 5 for x in c]
        self.assertEqual(sum(_nan(x) for x in sw.ema_series(c, 10)), 9)
        self.assertEqual(sum(_nan(x) for x in sw.atr_series(h, l, c, 10)), 9)
        self.assertEqual(sum(_nan(x) for x in sw.bollinger(c, 20)[1]), 19)
        self.assertEqual(sum(_nan(x) for x in sw.donchian(h, l, 20)[0]), 20)
        self.assertEqual(sum(_nan(x) for x in sw.chandelier(h, l, c, 22)), 21)
        self.assertEqual(sum(_nan(x) for x in sw.supertrend(h, l, c)[0]), 9)
        self.assertEqual(sum(_nan(x) for x in sw.macd(c)[1]), 33)
        self.assertTrue(all(_nan(x) for x in sw.ema_series([1, 2], 5)))          # shorter than n: all warm-up

    def test_swinglong_passes_the_source_guard(self):
        self.assertEqual(strategy.check_source(Path(sw.__file__).read_text()), [])


if __name__ == "__main__":
    unittest.main()
