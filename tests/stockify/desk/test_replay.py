"""HD 3.1: "what does it say now": each strategy's own on_bar replayed over the bars through a light context."""
from __future__ import annotations

import csv
import math
import random
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import strategy, timeutil
from stockify.data import history
from stockify.desk import replay
from stockify.oracle import run_bundle as oracle_run
from stockify.strategies import _swing as sw
from tests.stockify.test_engine_strategies import SPEC, _bar, _materialise, _weekdays, _world

DAY_NS = 86_400 * 10**9
PKG = "stockify.strategies."
SWING = ["rsi_pullback:RsiPullback", "macd_cross:MacdCross", "bollinger_reversion:BollingerReversion",
         "donchian_breakout:DonchianBreakout", "supertrend:Supertrend", "ema_trend:EmaTrend",
         "chandelier_exit:ChandelierExit"]
GOLDEN = ("sma_cross:SmaCross", {"fast": 50, "slow": 200})
VOTERS = [(PKG + r, {}) for r in SWING] + [(PKG + GOLDEN[0], dict(GOLDEN[1]))]


def _day_bars(days: list[str], rows: list[tuple[int, int, int, int]], iid: int = 1) -> list[strategy.Bar]:
    out = []
    for d, (o, h, lo, c) in zip(days, rows):
        start = timeutil.ist_ns(date.fromisoformat(d), "00:00")
        out.append(strategy.Bar(iid, 86400, start, start + DAY_NS, o, h, lo, c, 10**7, 0))
    return out


def _wave_world(n: int):
    """n weekdays of slow waves (every strategy trades), as oracle events and as the Bars the desk would read."""
    days = _weekdays("2025-01-06", n)
    closes, p = [], 100_000
    for i in range(n):
        p = max(40_000, p + int(900 * math.sin(i / 9.0) + 120 * math.sin(i / 2.3) + 200 * math.sin(i / 41.0)) // 5 * 5)
        closes.append(p)
    rows, prev = [], closes[0]
    for c in closes:
        rows.append((prev, max(prev, c) + 100, min(prev, c) - 100, c))
        prev = c
    evs = [_bar(1, 86400, f"{d}T00:00:00+05:30", o, h, lo, c, 10**7) for d, (o, h, lo, c) in zip(days, rows)]
    return days, evs, _day_bars(days, rows)


def _flat_then(legs, spread=50) -> list[strategy.Bar]:
    closes, p = [], 100_000
    for k, step in legs:
        for _ in range(k):
            p += step
            closes.append(p)
    days = _weekdays("2026-01-05", len(closes))
    return _day_bars(days, [(c, c + spread, c - spread, c) for c in closes])


class NowMatchesTheBacktestTests(unittest.TestCase):
    def test_now_matches_the_last_position_of_a_full_oracle_backtest(self):
        # Ruling R1: the oracle fills one bar after the signal; the replay fills at the signal bar's close. A daily
        # bar reaches the strategy at its end (00:00 IST the next day) and the order fills with the next bar, at
        # that bar's end, so the replay over bars[:k] (last bar k - 1) agrees with the oracle's end-of-day position
        # on days[k + 1].
        n = 400
        days, evs, bars = _wave_world(n)
        inst = [{"id": 1, "kind": "EQ", "tradingsymbol": "TEST", "specs": [{**s, "from": "2020-01-01"} for s in SPEC]}]
        g = _world("HD31", evs, inst, days, random.Random(1), run={"capital_paise": 100_000_000})
        for ref, extra in VOTERS:
            params = {"symbol": "NSE:TEST", **extra}
            with self.subTest(ref=ref), tempfile.TemporaryDirectory(prefix="desk-") as tmp:
                folder = Path(tmp)
                pack, b = _materialise(g, params, folder)
                oracle_run(b, [pack], folder / "run", strategy=strategy.load(ref), report=False)
                eod = {}
                with open(folder / "run" / "positions_eod.csv", encoding="utf-8", newline="") as f:
                    for row in csv.DictReader(f):
                        if row["instrument_id"] == "1" and row["product"] == "CNC":
                            eod[row["trade_date"]] = int(row["qty"])
                self.assertTrue(any(q > 0 for q in eod.values()), ref)      # the rule did hold at some point
                warm = 201 if "sma_cross" in ref else 60
                # One replay per prefix is O(n^2) bars: walk() gives the same answers in one pass, and a sample of
                # prefixes pins walk() to now().
                steps = replay.walk(ref, params, bars)
                for k in range(warm, n - 1):
                    got = steps[k - 1].in_position
                    self.assertEqual(got, eod.get(days[k + 1], 0) > 0, f"{ref} k={k} {days[k + 1]}")
                for k in range(warm, n, 37):
                    self.assertEqual(replay.now(ref, params, bars[:k]), steps[k - 1], f"{ref} k={k}")
                self.assertGreater(len({eod.get(d, 0) > 0 for d in days[warm:]}), 1, ref)   # it was in and out


class SignalTests(unittest.TestCase):
    def test_buying_today_when_the_last_bar_enters(self):
        bars = _flat_then([(25, 0), (1, 3000), (2, 100)])            # a 20-day high breaks on bar 26
        ref = PKG + "donchian_breakout:DonchianBreakout"
        before = replay.now(ref, {"symbol": "NSE:X"}, bars[:25])
        entry = replay.now(ref, {"symbol": "NSE:X"}, bars[:26])
        after = replay.now(ref, {"symbol": "NSE:X"}, bars)
        self.assertEqual((before.in_position, before.buying_today), (False, False))
        self.assertEqual((entry.in_position, entry.buying_today), (True, True))
        self.assertEqual((after.in_position, after.buying_today), (True, False))
        self.assertTrue(entry.note.startswith("Bought "), entry.note)
        self.assertEqual(entry.ref, ref)
        self.assertEqual(entry.display_name, "Donchian Breakout")

    def test_exit_level_comes_from_the_plot(self):
        bars = _flat_then([(25, 0), (6, 500)])
        for r in SWING:
            ref = PKG + r
            with self.subTest(ref=ref):
                sig = replay.now(ref, {"symbol": "NSE:X"}, bars)
                obj = strategy.load(ref)
                obj.params = {}
                level = obj.decide(bars[-obj.LOOKBACK:])[1]
                self.assertEqual(sig.exit_level_paise, None if level is None or level != level else int(level))
        _, ll = sw.donchian([b.high for b in bars], [b.low for b in bars], 10)
        self.assertEqual(replay.now(PKG + "donchian_breakout:DonchianBreakout", {"symbol": "NSE:X"}, bars)
                         .exit_level_paise, int(ll[-1]))
        self.assertIsNone(replay.now(PKG + "macd_cross:MacdCross", {"symbol": "NSE:X"}, bars).exit_level_paise)

    def test_golden_cross_and_baseline_names(self):
        bars = _flat_then([(210, 50)])
        g = replay.now(PKG + GOLDEN[0], {"symbol": "NSE:X", **GOLDEN[1]}, bars)
        self.assertEqual(g.display_name, "Golden Cross (50/200)")
        self.assertTrue(g.in_position)
        self.assertIsNone(g.exit_level_paise)
        b = replay.now(PKG + "buy_and_hold:BuyAndHold", {"symbol": "NSE:X"}, bars)
        self.assertEqual((b.display_name, b.in_position), ("Buy and Hold", True))
        self.assertEqual([r for r, _ in replay.VOTERS], [r for r, _ in VOTERS])
        self.assertEqual(replay.BASELINE[0], PKG + "buy_and_hold:BuyAndHold")

    def test_no_bars_is_out_with_a_reason(self):
        sig = replay.now(PKG + "supertrend:Supertrend", {"symbol": "NSE:X"}, [])
        self.assertEqual((sig.in_position, sig.buying_today, sig.exit_level_paise), (False, False, None))
        self.assertTrue(sig.note)


class LiveCandleTests(unittest.TestCase):
    def test_live_candle_replaces_todays_partial_bar(self):
        bars = _flat_then([(30, 0)])
        last_day = timeutil.ns_to_ist(bars[-1].start_ns).date()
        today = last_day + timedelta(days=1 if last_day.weekday() < 4 else 3)
        t1 = timeutil.ist_ns(today, "10:30")
        one = replay.with_live_candle(bars, 101_000, t1, 100_500, 101_200, 100_100)
        self.assertEqual(len(one), len(bars) + 1)
        self.assertEqual(one[:-1], bars)
        live = one[-1]
        start = timeutil.ist_ns(today, "00:00")
        self.assertEqual((live.start_ns, live.t_ns, live.interval_s), (start, start + DAY_NS, 86400))
        self.assertEqual((live.open, live.high, live.low, live.close, live.built), (100_500, 101_200, 100_100, 101_000, True))
        two = replay.with_live_candle(one, 101_500, t1 + 60 * 10**9, 100_500, 101_200, 100_100)
        self.assertEqual(len(two), len(one))                          # replaced, not appended again
        self.assertEqual((two[-1].close, two[-1].high), (101_500, 101_500))    # the high takes in the new price
        self.assertEqual(two[:-1], bars)
        # history already holding today's (partial) day bar: replaced too
        same = replay.with_live_candle(bars, 99_000, bars[-1].start_ns + 5 * 3600 * 10**9, 100_000, 100_050, 98_000)
        self.assertEqual(len(same), len(bars))
        self.assertEqual((same[-1].close, same[-1].low, same[-1].built), (99_000, 98_000, True))
        with self.assertRaises(ValueError):
            replay.with_live_candle(bars, 99_000, bars[-2].start_ns + 3600 * 10**9, 100_000, 100_050, 98_000)
        self.assertEqual(replay.with_live_candle([], 99_000, t1, 99_000, 99_000, 99_000), [])


SPLIT_TREND = ["supertrend:Supertrend", "ema_trend:EmaTrend", "donchian_breakout:DonchianBreakout",
               "chandelier_exit:ChandelierExit"]


class SplitTests(support.SandboxedDBTest):
    db_modules = (history,)

    def test_a_split_in_adjusted_history_does_not_trigger_an_exit(self):
        # A steady rise with a 1:2 split ten days before the end: the raw prices halve overnight, Kite's adjusted
        # history halves every price before the split instead. The desk reads the adjusted basis.
        n, split_at, iid = 300, 290, 7
        days = _weekdays("2025-06-02", n)
        raw = [200_000 + 400 * i for i in range(n)]
        raw = [p if i < split_at else p // 2 for i, p in enumerate(raw)]
        adjusted = [p // 2 if i < split_at else p for i, p in enumerate(raw)]
        rows = [{"date": datetime.fromisoformat(f"{d}T00:00:00"), "open": c / 100, "high": (c + 50) / 100,
                 "low": (c - 50) / 100, "close": c / 100, "volume": 1000, "oi": None} for d, c in zip(days, adjusted)]
        conn = history._connect()
        history._store(conn, iid, "day", rows, "2026-11-17")
        conn.commit()
        got = replay.daily_bars(iid)
        self.assertEqual([b.close for b in got], adjusted)
        self.assertEqual(got[0].start_ns, timeutil.ist_ns(date.fromisoformat(days[0]), "00:00"))
        self.assertEqual(got[0].t_ns, got[0].start_ns + DAY_NS)
        self.assertEqual((got[0].instrument, got[0].interval_s, got[0].built), (iid, 86400, False))
        self.assertEqual(len(replay.daily_bars(iid, until_ns=got[9].start_ns)), 10)
        raw_bars = _day_bars(days, [(c, c + 50, c - 50, c) for c in raw], iid)
        for r in SPLIT_TREND + [GOLDEN[0]]:
            ref = PKG + r
            params = {"symbol": "NSE:X", **(GOLDEN[1] if r == GOLDEN[0] else {})}
            with self.subTest(ref=ref):
                self.assertTrue(replay.now(ref, params, got).in_position, ref)
        for r in SPLIT_TREND:                    # the fixture means something: on raw prices the split is a crash
            with self.subTest(raw=r):
                self.assertFalse(replay.now(PKG + r, {"symbol": "NSE:X"}, raw_bars).in_position, r)


class ContextTests(unittest.TestCase):
    def _ctx(self):
        bars = _flat_then([(5, 0)])
        ctx = replay.ReplayContext({"symbol": "NSE:X"}, bars[0].instrument)
        ctx.advance(bars[0])
        return ctx, bars

    def test_unsupported_ctx_call_raises_plainly(self):
        ctx, _ = self._ctx()
        for name, args in (("chain", ("NIFTY", "2026-12-31")), ("schedule_at", (1,)), ("modify", (1,)),
                           ("cancel", (1,)), ("last_tick", (1,)), ("rng", ()), ("expiries", ("NIFTY", "CE")),
                           ("subscribe_ticks", (1,)), ("positions", ()), ("session_vwap", (1,))):
            with self.subTest(name=name), self.assertRaises(NotImplementedError) as e:
                getattr(ctx, name)(*args)
            self.assertIn("not available on the desk", str(e.exception))

    def test_place_fills_at_the_close_and_checks_its_arguments(self):
        ctx, bars = self._ctx()
        cash = ctx.funds().available
        ctx.place(1, "BUY", 10, "CNC", "MARKET", note="in")
        self.assertEqual(ctx.position(1, "CNC").qty, 10)
        self.assertEqual(ctx.funds().available, cash - 10 * bars[0].close)
        ctx.place(1, "SELL", 10, "CNC", "MARKET")
        self.assertEqual(ctx.position(1, "CNC").qty, 0)
        self.assertEqual(ctx.funds().available, cash)
        with self.assertRaises(TypeError):
            ctx.place(1, "BUY", 1.5, "CNC", "MARKET")
        with self.assertRaises(NotImplementedError):
            ctx.place(1, "BUY", 1, "CNC", "LIMIT", 100_000)
        with self.assertRaises(KeyError):
            ctx.resolve("NSE:OTHER")
        self.assertEqual(ctx.resolve("NSE:X"), 1)
        self.assertIsNone(ctx.position(1, "MIS"))


if __name__ == "__main__":
    unittest.main()
