"""FS 6: the Fundamentals basket strategy: equal weights of the month's list, rebalanced on each month's first daily
bar, through the oracle on a synthetic three-stock world."""
from __future__ import annotations

import csv
import random
import tempfile
import unittest
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import strategy
from stockify.oracle import run_bundle as oracle_run
from stockify.strategies import _catalogue
from tests.stockify.test_engine_strategies import SPEC, _bar, _materialise, _weekdays, _world

REF = "stockify.strategies.fundamentals_basket:FundamentalsBasket"
SYMBOLS = {1: "AAA", 2: "BBB", 3: "CCC"}
START = {1: 10_000, 2: 25_000, 3: 40_000}             # paise


def ranking() -> dict:
    jan = {"date": "2026-01-01", "symbols": ["ZZZ", "AAA", "BBB", "CCC"]}     # ZZZ is not in the bundle
    feb = {"date": "2026-02-02", "symbols": ["CCC", "BBB", "AAA"]}
    mar = {"date": "2026-03-02", "symbols": ["CCC", "BBB", "AAA"]}
    return {"0.5": [jan, feb, mar], "0.3": [{"date": "2026-01-01", "symbols": ["CCC"]}]}


def three_stock_world() -> dict:
    days = _weekdays("2026-01-05", 50)                 # 2026-01-05 .. 2026-03-13
    rng = random.Random(6)
    evs, price = [], dict(START)
    for d in days:
        for iid in SYMBOLS:
            o = price[iid]
            c = max(1_000, o + rng.choice([-1, 0, 1]) * (o // 200) // 5 * 5)
            evs.append(_bar(iid, 86400, f"{d}T00:00:00+05:30", o, max(o, c) + 5, min(o, c) - 5, c, 10**7))
            price[iid] = c
    inst = [{"id": iid, "kind": "EQ", "tradingsymbol": s, "specs": [dict(x) for x in SPEC]} for iid, s in SYMBOLS.items()]
    return _world("FS6", evs, inst, days, rng, run={"capital_paise": 10_000_000})


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


class FundamentalsBasketTests(unittest.TestCase):
    def _run(self, folder: Path, **params) -> Path:
        g = three_stock_world()
        pack, b = _materialise(g, {"ranking": ranking(), "balance": "0.5", "basket_size": 2, **params}, folder)
        oracle_run(b, [pack], folder / "run", strategy=strategy.load(REF), report=False)
        return folder / "run"

    def test_basket_rebalances_monthly_equal_weight(self):
        with tempfile.TemporaryDirectory(prefix="fs6-") as tmp:
            run = self._run(Path(tmp))
            pos = _rows(run / "positions_eod.csv")
            held = lambda day: {int(r["instrument_id"]): (int(r["qty"]), int(r["mark_paise"]))  # noqa: E731
                                for r in pos if r["trade_date"] == day and int(r["qty"]) > 0}
            nav = {r["trade_date"]: int(r["nav_paise"]) for r in _rows(run / "equity.csv")}

            jan = held("2026-01-30")                    # January's list: ZZZ skipped, so AAA and BBB
            self.assertEqual(set(jan), {1, 2})
            feb = held("2026-02-27")                    # February's: CCC and BBB; AAA sold
            self.assertEqual(set(feb), {2, 3})
            for day, h in (("2026-01-30", jan), ("2026-02-27", feb)):
                values = [q * m for q, m in h.values()]
                share = nav[day] * 95 // 100 // 2
                for v in values:                       # each near its equal share of 95 % of the value
                    self.assertGreater(v, share * 85 // 100, (day, values, share))
                    self.assertLess(v, share * 110 // 100, (day, values, share))

            # Orders only on a month's first two bars (the rebalance and the next day's top-up).
            firsts = {"2026-01-05", "2026-01-06", "2026-02-02", "2026-02-03", "2026-03-02", "2026-03-03"}
            orders = _rows(run / "orders.csv")
            self.assertTrue(orders)
            from stockify import timeutil
            for o in orders:
                # a day's bar arrives at its end (the next midnight IST); the orders follow it by a nanosecond
                bar_day = timeutil.ns_to_ist(int(o["t_submit_ns"]) - 86_400 * 10**9).date().isoformat()
                self.assertIn(bar_day, firsts, o)
            sold_aaa = [o for o in orders if int(o["instrument_id"]) == 1 and o["side"] == "SELL"]
            self.assertEqual(len(sold_aaa), 1)
            self.assertTrue(all(o["product"] == "CNC" for o in orders))
            self.assertIn("ZZZ", (run / "events_log.txt").read_text(encoding="utf-8"))

    def test_the_balance_picks_its_own_list(self):
        with tempfile.TemporaryDirectory(prefix="fs6-") as tmp:
            run = self._run(Path(tmp), balance="0.3")
            pos = _rows(run / "positions_eod.csv")
            self.assertEqual({int(r["instrument_id"]) for r in pos if int(r["qty"]) > 0}, {3})

    def test_a_short_list_keeps_equal_slots_and_cash(self):
        # Ruling R11: one usable stock with basket_size 2 gets half of 95 % of the value; the rest stays in cash.
        with tempfile.TemporaryDirectory(prefix="fs6-") as tmp:
            run = self._run(Path(tmp), balance="0.3")
            pos = _rows(run / "positions_eod.csv")
            nav = {r["trade_date"]: int(r["nav_paise"]) for r in _rows(run / "equity.csv")}
            day = "2026-01-30"
            held = [int(r["qty"]) * int(r["mark_paise"]) for r in pos if r["trade_date"] == day and int(r["qty"]) > 0]
            self.assertEqual(len(held), 1)
            share = nav[day] * 95 // 100 // 2
            self.assertGreater(held[0], share * 85 // 100, (held, share))
            self.assertLess(held[0], share * 110 // 100, (held, share))

    def test_an_unknown_balance_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="fs6-") as tmp, self.assertRaisesRegex(Exception, "0.9"):
            self._run(Path(tmp), balance="0.9")

    def test_catalogue_entry(self):
        e = {s["ref"]: s for s in _catalogue.python_strategies()}[REF]
        self.assertEqual(e["display_name"], "Fundamentals basket")
        self.assertEqual(e["doc"], "Holds the best-placed stocks from the fundamentals screen, rebalanced each month.")
        by = {p["name"]: p for p in e["params"]}
        self.assertEqual(by["ranking"]["type"], "ranking")
        self.assertTrue(by["ranking"]["required"])
        self.assertEqual((by["basket_size"]["min"], by["basket_size"]["max"]), ("5", "50"))
        self.assertEqual(_catalogue.validate_params(e["params"], {"ranking": ranking(), "balance": "0.5",
                                                                  "basket_size": 20}), [])
        self.assertTrue(_catalogue.validate_params(e["params"], {"ranking": ranking(), "basket_size": 51}))


if __name__ == "__main__":
    unittest.main()
