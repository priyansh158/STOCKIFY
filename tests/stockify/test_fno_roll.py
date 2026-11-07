"""M4.2 (WB 4.1): expiries, the futures chain, and the rollover helpers in stockify/fno/roll.py.

The helpers are plain Python for strategies, not engine rules. They count trading days with the shipped trading
calendar (stockify.data.calendar) and use a contract's actual expiry when NSE expired it early (D-41)."""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden
from stockify.fno import roll
from stockify.oracle import run_bundle as oracle_run
from stockify.strategy import Strategy

MAR = {"id": 11, "kind": "FUT", "underlying": "NIFTY", "expiry": "2026-03-30", "actual_expiry": None}
APR = {"id": 12, "kind": "FUT", "underlying": "NIFTY", "expiry": "2026-04-28", "actual_expiry": None}
MAY = {"id": 13, "kind": "FUT", "underlying": "NIFTY", "expiry": "2026-05-26", "actual_expiry": None}
BANK = {"id": 21, "kind": "FUT", "underlying": "BANKNIFTY", "expiry": "2026-04-28", "actual_expiry": None}
C_MAR = {"id": 31, "kind": "CE", "underlying": "NIFTY", "expiry": "2026-03-30", "strike_paise": 2500000}
C_APR = {"id": 32, "kind": "CE", "underlying": "NIFTY", "expiry": "2026-04-28", "strike_paise": 2500000}
C_APR_OTHER = {"id": 33, "kind": "CE", "underlying": "NIFTY", "expiry": "2026-04-28", "strike_paise": 2600000}
P_APR = {"id": 34, "kind": "PE", "underlying": "NIFTY", "expiry": "2026-04-28", "strike_paise": 2500000}
EARLY = {"id": 41, "kind": "FUT", "underlying": "ACME", "expiry": "2026-03-30", "actual_expiry": "2026-03-20"}


class RollHelperTests(unittest.TestCase):
    def test_days_to_expiry(self):
        self.assertEqual(roll.days_to_expiry(MAR, date(2026, 3, 25)), 5)
        self.assertEqual(roll.days_to_expiry(MAR, "2026-03-30"), 0)
        self.assertEqual(roll.days_to_expiry(MAR, date(2026, 4, 1)), -2)
        self.assertEqual(roll.days_to_expiry(EARLY, date(2026, 3, 16)), 4)          # actual expiry (D-41)

    def test_trading_days_follow_the_calendar(self):
        # after 2026-03-25: the 26th is Shri Ram Navami, 28-29 a weekend, so only the 27th and the 30th remain
        self.assertEqual(roll.trading_days_to_expiry(MAR, date(2026, 3, 25)), 2)
        self.assertEqual(roll.trading_days_to_expiry(MAR, date(2026, 3, 27)), 1)
        self.assertEqual(roll.trading_days_to_expiry(MAR, date(2026, 3, 30)), 0)
        self.assertEqual(roll.trading_days_to_expiry(EARLY, date(2026, 3, 18)), 2)   # the 19th and the 20th

    def test_roll_due(self):
        self.assertTrue(roll.roll_due(MAR, 2, date(2026, 3, 25)))
        self.assertFalse(roll.roll_due(MAR, 1, date(2026, 3, 25)))
        self.assertTrue(roll.roll_due(MAR, 1, date(2026, 3, 27)))
        self.assertTrue(roll.roll_due(MAR, 0, date(2026, 3, 30)))                    # expiry day itself
        self.assertTrue(roll.roll_due(MAR, 0, date(2026, 4, 2)))                     # already past
        self.assertFalse(roll.roll_due(EARLY, 1, date(2026, 3, 18)))
        self.assertTrue(roll.roll_due(EARLY, 1, date(2026, 3, 19)))
        with self.assertRaises(ValueError):
            roll.roll_due(MAR, -1, date(2026, 3, 25))

    def test_next_contract(self):
        rows = [MAY, C_APR_OTHER, APR, BANK, C_MAR, MAR, P_APR, C_APR]                # any order
        self.assertEqual(roll.next_contract(MAR, rows), APR)
        self.assertEqual(roll.next_contract(APR, rows), MAY)
        self.assertIsNone(roll.next_contract(MAY, rows))
        self.assertEqual(roll.next_contract(C_MAR, rows), C_APR)                     # same strike and right
        self.assertIsNone(roll.next_contract(BANK, rows))


class FoLookupStrategy(Strategy):
    def on_start(self, ctx):  # noqa: ANN001
        ctx.log("info", f"fut {ctx.futures_chain('NIFTY')} exp {ctx.expiries('NIFTY', 'FUT')} "
                        f"opt {ctx.expiries('NIFTY', 'OPT')} ce {ctx.expiries('NIFTY', 'CE')}")


class ExpiryLookupTests(unittest.TestCase):
    """M4.2 box 1: ctx.expiries and ctx.futures_chain (sorted by expiry) from the bundle, on both engines."""

    def test_sorted_from_the_bundle_on_both_implementations(self):
        spec = [{"from": "2026-01-01", "lot": 65, "tick_paise": 10}]
        inst = [{"id": 1, "kind": "INDEX", "tradingsymbol": "NIFTY 50"}]
        for iid, exp in ((2, "2026-05-26"), (3, "2026-03-30"), (4, "2026-04-28")):          # not in expiry order
            inst.append({"id": iid, "kind": "FUT", "tradingsymbol": f"NF{iid}", "underlying": "NIFTY",
                         "underlying_id": 1, "expiry": exp, "specs": spec})
        inst.append({"id": 5, "kind": "PE", "tradingsymbol": "NP", "underlying": "NIFTY", "underlying_id": 1,
                     "expiry": "2026-03-10", "strike_paise": 2500000, "specs": spec})
        g = {"id": "M42", "bundle": {"dates": ["2026-03-02"], "instruments": inst},
             "run": {"fill_model": {"kind": "bar"}, "capital_paise": 10_000_000, "price_basis": "raw"},
             "events": [{"kind": "BAR", "instrument": 3, "interval_s": 60, "start": "2026-03-02T09:15:00+05:30",
                         "o": 100, "h": 100, "l": 100, "c": 100, "v": 1}], "strategy": {"actions": []}}
        logs = {}
        with tempfile.TemporaryDirectory(prefix="m42-") as tmp:
            folder = Path(tmp)
            pack, b = golden.build_pack(g, folder), golden.build_bundle(g)
            runners = [("oracle", oracle_run)] + ([("engine", engine.run_bundle)] if engine.available() else [])
            for name, run in runners:
                run(b, [pack], folder / name, strategy=FoLookupStrategy(), report=False)
                logs[name] = [x for x in (folder / name / "events_log.txt").read_text().splitlines() if " fut " in x]
        line = logs["oracle"][0]
        self.assertIn("fut [3, 4, 2]", line)
        self.assertIn("exp ['2026-03-30', '2026-04-28', '2026-05-26']", line)
        self.assertIn("opt ['2026-03-10']", line)
        self.assertIn("ce []", line)
        if "engine" in logs:
            self.assertEqual(logs["engine"], logs["oracle"])


if __name__ == "__main__":
    unittest.main()
