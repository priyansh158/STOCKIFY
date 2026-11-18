"""HD 2.1: the seven swing strategies: entry and exit on synthetic bars, exit levels, catalogue entries, a real run."""
from __future__ import annotations

import importlib
import math
import random
import tempfile
import unittest
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import strategy
from stockify.oracle import run_bundle as oracle_run
from stockify.strategies import _catalogue
from stockify.strategies import _swing as sw
from tests.stockify.test_engine_strategies import SPEC, _bar, _materialise, _weekdays, _world

DAY_NS = 86_400 * 10**9
PKG = "stockify.strategies."
REFS = {
    "rsi_pullback:RsiPullback": "RSI Pullback", "macd_cross:MacdCross": "MACD Crossover",
    "bollinger_reversion:BollingerReversion": "Bollinger Reversion",
    "donchian_breakout:DonchianBreakout": "Donchian Breakout", "supertrend:Supertrend": "Supertrend",
    "ema_trend:EmaTrend": "EMA Trend", "chandelier_exit:ChandelierExit": "Chandelier Exit"}


def bars_of(closes, spread=50):
    out = []
    for i, c in enumerate(closes):
        out.append(strategy.Bar(instrument=1, interval_s=86400, start_ns=i * DAY_NS, t_ns=(i + 1) * DAY_NS,
                                open=c, high=c + spread, low=c - spread, close=c, volume=1000, oi=0))
    return out


def make(ref, **params):
    mod, cls = ref.split(":")
    obj = getattr(importlib.import_module(PKG + mod), cls)()
    obj.params = params
    return obj


def want(obj, bars, i):
    return obj.decide(bars[:i + 1])[0]


def series(*legs):
    """Closes (paise) from (steps, step) legs, starting at 100_000."""
    out, p = [], 100_000
    for n, step in legs:
        for _ in range(n):
            p += step
            out.append(p)
    return out


class SwingStrategyTests(unittest.TestCase):
    def test_rsi_pullback_goes_long_on_its_entry_and_flat_on_its_exit(self):
        closes = series((20, 0), (15, -400), (10, 500), (20, 300), (15, -600))
        bars = bars_of(closes)
        obj = make("rsi_pullback:RsiPullback")
        rs = [strategy.rsi(closes[:i + 1], 14) for i in range(len(closes))]
        entry = next(i for i in range(15, len(rs)) if rs[i - 1] < 30 <= rs[i])
        exit_ = next(i for i in range(entry + 1, len(rs)) if rs[i - 1] >= 70 > rs[i])
        self.assertFalse(want(obj, bars, entry - 1))
        self.assertTrue(want(obj, bars, entry))
        self.assertTrue(want(obj, bars, exit_ - 1))
        self.assertFalse(want(obj, bars, exit_))

    def test_rsi_pullback_exit_level(self):
        # below 70 the level is a rise to reach; above 70 it is the fall that takes the RSI back down to it
        for legs, above in (((20, 0), (15, -400), (3, 300)), True), (((20, 0), (15, -400), (10, 500), (5, 200)), False):
            closes = series(*legs)
            level = make("rsi_pullback:RsiPullback").decide(bars_of(closes))[1]
            self.assertEqual(level > closes[-1], above)
            # the price at which appending a close makes RSI equal sell_above
            self.assertAlmostEqual(strategy.rsi(closes + [round(level)], 14), 70.0, delta=0.05)

    def test_macd_cross_goes_long_on_its_entry_and_flat_on_its_exit(self):
        closes = series((40, 0), (25, 300), (25, -300))
        bars = bars_of(closes)
        obj = make("macd_cross:MacdCross")
        line, sig = sw.macd(closes)
        entry = next(i for i in range(40, len(closes)) if line[i] > sig[i] and not line[i - 1] > sig[i - 1])
        exit_ = next(i for i in range(entry + 1, len(closes)) if not line[i] > sig[i])
        self.assertFalse(want(obj, bars, entry - 1))
        self.assertTrue(want(obj, bars, entry))
        self.assertFalse(want(obj, bars, exit_))

    def test_macd_cross_exit_level(self):
        bars = bars_of(series((40, 0), (25, 300)))
        yes, level, note = make("macd_cross:MacdCross").decide(bars)
        self.assertTrue(yes)
        self.assertIsNone(level)
        self.assertIn("above", note)

    def test_bollinger_reversion_goes_long_on_its_entry_and_flat_on_its_exit(self):
        closes = series((25, 0), (1, -3000), (3, 400), (10, 500))
        bars = bars_of(closes)
        obj = make("bollinger_reversion:BollingerReversion")
        lower, mid, _ = sw.bollinger(closes)
        entry = next(i for i in range(20, len(closes)) if closes[i] < lower[i])
        exit_ = next(i for i in range(entry + 1, len(closes)) if closes[i] >= mid[i])
        self.assertFalse(want(obj, bars, entry - 1))
        self.assertTrue(want(obj, bars, entry))
        self.assertTrue(want(obj, bars, exit_ - 1))
        self.assertFalse(want(obj, bars, exit_))

    def test_bollinger_reversion_exit_level(self):
        closes = series((25, 0), (1, -3000), (2, 100))
        level = make("bollinger_reversion:BollingerReversion").decide(bars_of(closes))[1]
        self.assertAlmostEqual(level, sw.bollinger(closes)[1][-1])

    def test_donchian_breakout_goes_long_on_its_entry_and_flat_on_its_exit(self):
        closes = series((25, 0), (6, 500), (8, 0), (4, -400))
        bars = bars_of(closes)
        obj = make("donchian_breakout:DonchianBreakout")
        hi = [b.high for b in bars]
        lo = [b.low for b in bars]
        hh, _ = sw.donchian(hi, lo, 20)
        _, ll = sw.donchian(hi, lo, 10)
        entry = next(i for i in range(20, len(closes)) if closes[i] > hh[i])
        exit_ = next(i for i in range(entry + 1, len(closes)) if closes[i] < ll[i])
        self.assertFalse(want(obj, bars, entry - 1))
        self.assertTrue(want(obj, bars, entry))
        self.assertTrue(want(obj, bars, exit_ - 1))
        self.assertFalse(want(obj, bars, exit_))

    def test_donchian_breakout_exit_level(self):
        bars = bars_of(series((25, 0), (6, 500)))
        level = make("donchian_breakout:DonchianBreakout").decide(bars)[1]
        _, ll = sw.donchian([b.high for b in bars], [b.low for b in bars], 10)
        self.assertEqual(level, ll[-1])

    def test_supertrend_goes_long_on_its_entry_and_flat_on_its_exit(self):
        closes = series((30, -200), (30, 400), (30, -500))
        bars = bars_of(closes, spread=100)
        obj = make("supertrend:Supertrend")
        line, up = sw.supertrend([b.high for b in bars], [b.low for b in bars], closes)
        flips = [i for i in range(11, len(closes)) if up[i] != up[i - 1]]
        self.assertGreaterEqual(len(flips), 2)
        self.assertNotEqual(want(obj, bars, flips[0] - 1), want(obj, bars, flips[0]))
        for i in range(12, len(closes), 7):
            self.assertEqual(want(obj, bars, i), up[i], i)

    def test_supertrend_exit_level(self):
        bars = bars_of(series((30, -200), (30, 400)), spread=100)
        line, _ = sw.supertrend([b.high for b in bars], [b.low for b in bars], [b.close for b in bars])
        self.assertEqual(make("supertrend:Supertrend").decide(bars)[1], line[-1])

    def test_ema_trend_goes_long_on_its_entry_and_flat_on_its_exit(self):
        closes = series((60, -100), (40, 400), (40, -400))
        bars = bars_of(closes)
        obj = make("ema_trend:EmaTrend")
        f, s = sw.ema_series(closes, 20), sw.ema_series(closes, 50)
        entry = next(i for i in range(50, len(closes)) if f[i] > s[i])
        exit_ = next(i for i in range(entry + 1, len(closes)) if not f[i] > s[i])
        self.assertFalse(want(obj, bars, entry - 1))
        self.assertTrue(want(obj, bars, entry))
        self.assertFalse(want(obj, bars, exit_))

    def test_ema_trend_exit_level(self):
        closes = series((60, -100), (40, 400))
        level = make("ema_trend:EmaTrend").decide(bars_of(closes))[1]
        self.assertAlmostEqual(level, sw.ema_series(closes, 50)[-1])

    def test_chandelier_exit_goes_long_on_its_entry_and_flat_on_its_exit(self):
        closes = series((30, 0), (8, 500), (10, 200), (6, -900))
        bars = bars_of(closes)
        obj = make("chandelier_exit:ChandelierExit")
        hi, lo = [b.high for b in bars], [b.low for b in bars]
        hh, _ = sw.donchian(hi, lo, 20)
        ch = sw.chandelier(hi, lo, closes, 22, 3.0)
        entry = next(i for i in range(23, len(closes)) if closes[i] > hh[i])
        exit_ = next(i for i in range(entry + 1, len(closes)) if closes[i] < ch[i])
        self.assertFalse(want(obj, bars, entry - 1))
        self.assertTrue(want(obj, bars, entry))
        self.assertTrue(want(obj, bars, exit_ - 1))
        self.assertFalse(want(obj, bars, exit_))

    def test_chandelier_exit_exit_level(self):
        bars = bars_of(series((30, 0), (8, 500)))
        ch = sw.chandelier([b.high for b in bars], [b.low for b in bars], [b.close for b in bars], 22, 3.0)
        self.assertAlmostEqual(make("chandelier_exit:ChandelierExit").decide(bars)[1], ch[-1])

    def test_the_answer_depends_only_on_the_bars_passed(self):
        closes = series((20, 0), (15, -400), (10, 500), (20, 300))
        bars = bars_of(closes)
        for ref in REFS:
            a, b = make(ref), make(ref)
            a.decide(bars[:30])                              # a has seen other bars first; it must not matter
            self.assertEqual(a.decide(bars), b.decide(bars), ref)

    def test_too_few_bars_is_flat_with_no_level(self):
        for ref in REFS:
            yes, level, note = make(ref).decide(bars_of([100_000] * 3))
            self.assertEqual((yes, level), (False, None), ref)
            self.assertTrue(note, ref)

    def test_every_new_strategy_is_in_the_catalogue(self):
        got = {e["ref"]: e for e in _catalogue.python_strategies()}
        for ref, name in REFS.items():
            e = got[PKG + ref]
            self.assertEqual(e["display_name"], name)
            cls = _catalogue._class_of(e["kind"], e["ref"])
            self.assertTrue(cls.DESCRIPTION.endswith("."), ref)
            self.assertTrue(issubclass(cls, sw.SwingLong), ref)
            names = [p["name"] for p in e["params"]]
            self.assertEqual(names[0], "symbol")
            self.assertEqual(names[-1], "interval_s")
            for p in e["params"]:
                self.assertNotIn("_", p["label"], ref)

    def test_each_runs_a_daily_oracle_backtest(self):
        days = _weekdays("2026-01-05", 300)
        closes, p = [], 100_000
        for i in range(300):                                  # slow waves: every strategy trades at least once
            p = max(40_000, p + int(900 * math.sin(i / 9.0) + 120 * math.sin(i / 2.3)) // 5 * 5)
            closes.append(p)
        evs, prev = [], closes[0]
        for d, c in zip(days, closes):
            evs.append(_bar(1, 86400, f"{d}T00:00:00+05:30", prev, max(prev, c) + 100, min(prev, c) - 100, c, 10**7))
            prev = c
        inst = [{"id": 1, "kind": "EQ", "tradingsymbol": "TEST", "specs": [dict(s) for s in SPEC]}]
        g = _world("HD21", evs, inst, days, random.Random(1), run={"capital_paise": 100_000_000})
        for ref in REFS:
            with self.subTest(ref=ref), tempfile.TemporaryDirectory(prefix="swing-") as tmp:
                folder = Path(tmp)
                pack, b = _materialise(g, {"symbol": "NSE:TEST"}, folder)
                result = oracle_run(b, [pack], folder / "run", strategy=strategy.load(PKG + ref), report=False)
                self.assertGreaterEqual(result.metrics["trades"], 1, ref)
                plots = (folder / "run" / "plots.csv").read_text(encoding="utf-8")
                if ref.split(":")[0] != "macd_cross":
                    self.assertIn("exit_level", plots, ref)


if __name__ == "__main__":
    unittest.main()
