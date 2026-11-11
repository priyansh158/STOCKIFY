"""Reference strategies (M2.4), the run report (M2.6) and compare-artifacts (M2.7)."""
from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import compare, golden, strategy as S
from stockify.oracle import run_bundle

EQ = {"id": 1, "kind": "EQ", "tradingsymbol": "TEST", "specs": [{"from": "2026-01-01", "lot": 1, "tick_paise": 5}]}


def world(events, instruments=(EQ,), dates=("2026-03-02",), run=None, **bundle):
    return {"id": "T", "bundle": {"dates": list(dates), "instruments": list(instruments), **bundle},
            "run": {"fill_model": {"kind": "bar"}, "capital_paise": 10_000_000, "price_basis": "raw", **(run or {})},
            "events": events, "strategy": {"actions": []}}


def daily(day: str, c: int, o: int | None = None, v: int = 100_000) -> dict:
    o = c if o is None else o
    return {"kind": "BAR", "instrument": 1, "interval_s": 86400, "start": f"{day}T00:00:00+05:30",
            "o": o, "h": max(o, c), "l": min(o, c), "c": c, "v": v}


def minute(day: str, hhmm: str, o: int, h: int, lo: int, c: int, iid: int = 1, v: int = 10_000) -> dict:
    return {"kind": "BAR", "instrument": iid, "interval_s": 60, "start": f"{day}T{hhmm}:00+05:30",
            "o": o, "h": h, "l": lo, "c": c, "v": v}


def execute(g: dict, strat, params: dict, folder: Path, report: bool = False):  # noqa: ANN001
    pack = golden.build_pack(g, folder)
    b = golden.build_bundle(g)
    b["strategy"]["params"] = params
    return run_bundle(b, [pack], folder / "run", strategy=strat, report=report)


def rows(path: Path) -> list[dict]:
    return list(csv.DictReader(path.open(encoding="utf-8")))


DAYS = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06", "2026-03-09", "2026-03-10",
        "2026-03-11", "2026-03-12", "2026-03-13"]


def walk(closes: list[int]) -> list[dict]:
    """Daily bars that open at the previous close (moves stay inside the 2 % protection band)."""
    out, prev = [], closes[0]
    for d, c in zip(DAYS, closes):
        out.append(daily(d, c, o=prev))
        prev = c
    return out


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=str(support.SANDBOX_ROOT)))

    def test_sources_pass_the_load_check(self):
        for ref in ("stockify.strategies.buy_and_hold:BuyAndHold", "stockify.strategies.sma_cross:SmaCross",
                    "stockify.strategies.opening_range_breakout:OpeningRangeBreakout",
                    "stockify.strategies.eod_short_straddle:EodShortStraddle"):
            self.assertIsInstance(S.load(ref), S.Strategy)

    def test_buy_and_hold_buys_once(self):
        from stockify.strategies.buy_and_hold import BuyAndHold
        closes = [10000, 10100, 10200, 10300]
        g = world([daily(d, c) for d, c in zip(DAYS, closes)], dates=DAYS[:4])
        execute(g, BuyAndHold(), {"symbol": "NSE:TEST"}, self.tmp)
        fills = rows(self.tmp / "run" / "fills.csv")
        self.assertEqual(len(fills), 1)
        self.assertEqual(int(fills[0]["qty"]), (10_000_000 * 95 // 100) // (10000 * 103 // 100))

    def test_sma_cross_enters_and_exits(self):
        from stockify.strategies.sma_cross import SmaCross
        closes = [10000, 10000, 10000, 10100, 10200, 10300, 10150, 10000, 9900, 9850]   # up, then down
        g = world(walk(closes), dates=DAYS)
        execute(g, SmaCross(), {"symbol": "NSE:TEST", "fast": 2, "slow": 3}, self.tmp)
        orders = rows(self.tmp / "run" / "orders.csv")
        self.assertEqual([(o["side"], o["tag"], o["status"], o["avg_price_paise"]) for o in orders],
                         [("BUY", "entry", "COMPLETE", "10100"), ("SELL", "exit", "COMPLETE", "10000")])

    def test_opening_range_breakout(self):
        from stockify.strategies.opening_range_breakout import OpeningRangeBreakout
        d = "2026-03-02"
        evs = [minute(d, "09:15", 10000, 10050, 9950, 10000), minute(d, "09:16", 10000, 10060, 9980, 10040),
               minute(d, "09:17", 10040, 10120, 10040, 10100),                 # closes above the 100.60 high
               minute(d, "09:18", 10100, 10150, 10090, 10140), minute(d, "15:10", 10200, 10210, 10190, 10200),
               minute(d, "15:11", 10200, 10210, 10190, 10200)]
        g = world(evs, mis_cutoff={"CM": "15:20"})
        execute(g, OpeningRangeBreakout(), {"symbol": "NSE:TEST", "range_minutes": 2, "qty": 10}, self.tmp)
        orders = {o["tag"]: o for o in rows(self.tmp / "run" / "orders.csv")}
        self.assertEqual(orders["orb_long"]["status"], "COMPLETE")
        self.assertEqual((orders["orb_stop"]["type"], orders["orb_stop"]["trigger_paise"], orders["orb_stop"]["status"]),
                         ("SL-M", "9950", "CANCELLED"))
        self.assertEqual(orders["orb_exit"]["status"], "COMPLETE")

    def _orb_world(self, breakout_volume: int = 10_000, **run):
        d = "2026-03-02"
        evs = [minute(d, "09:15", 10000, 10050, 9950, 10000), minute(d, "09:16", 10000, 10060, 9980, 10040),
               minute(d, "09:17", 10040, 10120, 10040, 10100, v=breakout_volume),
               minute(d, "09:18", 10100, 10150, 10090, 10140, v=breakout_volume), minute(d, "15:10", 10200, 10210, 10190, 10200),
               minute(d, "15:11", 10200, 10210, 10190, 10200)]
        return world(evs, mis_cutoff={"CM": "15:20"}, run=run)

    def test_orb_rejected_entry_leaves_no_stop_and_no_position(self):
        from stockify.strategies.opening_range_breakout import OpeningRangeBreakout
        g = self._orb_world(capital_paise=1_000)
        execute(g, OpeningRangeBreakout(), {"symbol": "NSE:TEST", "range_minutes": 2, "qty": 10}, self.tmp)
        orders = rows(self.tmp / "run" / "orders.csv")
        self.assertEqual([(o["tag"], o["status"], o["reason"]) for o in orders], [("orb_long", "REJECTED", "R_MARGIN")])
        self.assertEqual(rows(self.tmp / "run" / "fills.csv"), [])

    def test_orb_partial_fill_gets_a_stop_of_exactly_the_filled_quantity(self):
        from stockify.strategies.opening_range_breakout import OpeningRangeBreakout
        g = self._orb_world(breakout_volume=40)
        execute(g, OpeningRangeBreakout(), {"symbol": "NSE:TEST", "range_minutes": 2, "qty": 10}, self.tmp)
        orders = rows(self.tmp / "run" / "orders.csv")
        fills = rows(self.tmp / "run" / "fills.csv")
        entry_fills = [f for f in fills if f["order_id"] == next(o["order_id"] for o in orders if o["tag"] == "orb_long")]
        self.assertEqual([f["qty"] for f in entry_fills], ["4", "6"], "the entry must fill in two parts")
        stops = [o for o in orders if o["tag"] == "orb_stop"]
        self.assertEqual(sum(int(o["qty"]) for o in stops), sum(int(f["qty"]) for f in entry_fills))
        self.assertEqual([o["qty"] for o in stops], [f["qty"] for f in entry_fills])

    def test_eod_short_straddle_sells_and_settles(self):
        from stockify.strategies.eod_short_straddle import EodShortStraddle
        days = ["2026-03-02", "2026-03-03", "2026-03-04"]
        inst = [{"id": 1, "kind": "INDEX", "tradingsymbol": "NIFTY 50"},
                {"id": 2, "kind": "CE", "tradingsymbol": "NC", "underlying": "NIFTY", "underlying_id": 1,
                 "expiry": "2026-03-04", "strike_paise": 2400000, "specs": [{"from": "2026-01-01", "lot": 65, "tick_paise": 5}]},
                {"id": 3, "kind": "PE", "tradingsymbol": "NP", "underlying": "NIFTY", "underlying_id": 1,
                 "expiry": "2026-03-04", "strike_paise": 2400000, "specs": [{"from": "2026-01-01", "lot": 65, "tick_paise": 5}]}]

        def eod(iid: int, day: str, c: int) -> dict:
            return {"kind": "BAR", "instrument": iid, "interval_s": 86400, "start": f"{day}T00:00:00+05:30",
                    "t": f"{day}T19:00:00+05:30", "o": c, "h": c, "l": c, "c": c, "v": 100_000}
        evs = []
        for day, spot, ce, pe in zip(days, (2400000, 2402000, 2410000), (10000, 9000, 10000), (10000, 8000, 0)):
            evs += [eod(2, day, ce), eod(3, day, pe), eod(1, day, spot)]
        evs.append({"kind": "SETTLE", "instrument": 1, "t": "2026-03-04T16:00:00+05:30", "date": "2026-03-04",
                    "price": 2410000, "subtype": "final"})
        g = world(evs, instruments=inst, dates=days,
                  run={"fill_model": {"kind": "eod_close", "half_spread_ticks": 1}, "capital_paise": 100_000_000})
        execute(g, EodShortStraddle(), {"min_days": 1}, self.tmp)
        fills = rows(self.tmp / "run" / "fills.csv")
        self.assertEqual([(f["instrument_id"], f["side"], f["qty"], f["price_paise"], f["evidence"]) for f in fills],
                         [("2", "SELL", "65", "8995", "eod_close"), ("3", "SELL", "65", "7995", "eod_close")])
        eq = rows(self.tmp / "run" / "equity.csv")[-1]
        # premium 65 × (8995 + 7995) received; the call settles at intrinsic 100.00 (paid), the put at 0
        self.assertEqual(int(eq["cash_paise"]), 100_000_000 + 65 * (8995 + 7995) - 65 * 10000)


class ReportCompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=str(support.SANDBOX_ROOT)))

    def test_report_and_compare(self):
        from stockify.strategies.buy_and_hold import BuyAndHold
        g = world([daily(d, c) for d, c in zip(DAYS[:3], (10000, 10100, 10200))], dates=DAYS[:3])
        a, b = self.tmp / "a", self.tmp / "b"
        a.mkdir(), b.mkdir()
        execute(g, BuyAndHold(), {"symbol": "NSE:TEST"}, a, report=True)
        execute(g, BuyAndHold(), {"symbol": "NSE:TEST"}, b)
        text = (a / "run" / "report.md").read_text(encoding="utf-8")
        self.assertIn("₹1,00,000.00 →", text)
        self.assertTrue((a / "run" / "equity.png").exists())
        self.assertEqual(compare.compare(a / "run", b / "run"), [])
        fills = b / "run" / "fills.csv"
        lines = fills.read_text(encoding="utf-8").splitlines()
        cells = lines[1].split(",")
        cells[6] = str(int(cells[6]) + 5)                                     # price_paise
        lines[1] = ",".join(cells)
        fills.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
        problems = compare.compare(a / "run", b / "run")
        self.assertEqual(len(problems), 1)
        self.assertIn("price_paise", problems[0])
        self.assertEqual(compare.compare(a / "run", b / "run", tol_paise=5), [])


if __name__ == "__main__":
    unittest.main()
