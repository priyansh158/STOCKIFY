"""Oracle building blocks (M2.2/M2.3) and every golden scenario (M2.5)."""
from __future__ import annotations

import unittest

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import golden, strategy as S
from stockify.oracle import costs, metrics
from stockify.oracle.ledger import Ledger
from stockify.oracle.matcher import first_reach, participation_cap, protection_limit, slipped
from stockify.oracle.rng import Pcg32


class RngTests(unittest.TestCase):
    def test_reference_sequence(self):
        """pcg32-demo, seed (42, 54): the first six outputs of pcg_basic.c."""
        r = Pcg32(42, 54)
        self.assertEqual([r.next_u32() for _ in range(6)],
                         [0xa15c02b7, 0x7b47f409, 0xba1d3330, 0x83d2f293, 0xbfa4784b, 0xcbed606e])

    def test_below_random_and_copy(self):
        a = Pcg32(7, 1)
        vals = [a.below(6) for _ in range(1000)]
        self.assertTrue(all(0 <= v < 6 for v in vals) and len(set(vals)) == 6)
        b = a.copy()
        self.assertEqual([a.next_u32() for _ in range(5)], [b.next_u32() for _ in range(5)])
        x = Pcg32(1, 2).random()
        self.assertTrue(0.0 <= x < 1.0)
        self.assertNotEqual(Pcg32(42, 1).next_u32(), Pcg32(42, 2).next_u32())   # streams differ


class LedgerTests(unittest.TestCase):
    def test_average_and_exact_realised(self):
        led = Ledger(10_000, 10_000)
        led.apply_fill(1, "CNC", "premium", "BUY", 3, 100)
        led.apply_fill(1, "CNC", "premium", "BUY", 1, 103)
        p = led.position(1, "CNC")
        self.assertEqual((p.q, p.c, p.avg_price), (4, 403, 100))
        led.apply_fill(1, "CNC", "premium", "SELL", 2, 110)
        self.assertEqual(p.realised, 2 * 110 - 201)                    # floor(403 * 2 / 4) = 201
        led.apply_fill(1, "CNC", "premium", "SELL", 2, 90)
        self.assertEqual(p.realised, 400 - 403)                        # exact over the round trip
        self.assertEqual(led.cash, 10_000 - 403 + 400)

    def test_flip_and_variation(self):
        led = Ledger(0, 0)
        led.apply_fill(2, "NRML", "variation", "BUY", 2, 100)
        self.assertEqual(led.cash, 0)                                  # nothing moves at a futures fill
        led.settle_variation(led.position(2, "NRML"), 105, "ltp")
        self.assertEqual(led.cash, 10)
        led.apply_fill(2, "NRML", "variation", "SELL", 3, 103)          # close 2, open short 1
        p = led.position(2, "NRML")
        self.assertEqual((p.qty, p.c), (-1, 103))
        self.assertEqual(p.realised, 6)
        self.assertEqual(led.nav(lambda i: 103), 6)                    # cash 10, unrealised since mark -4
        led.apply_fill(2, "NRML", "variation", "BUY", 1, 101)
        self.assertEqual(led.cash, 8)                                  # +6 long, +2 short, all in cash when flat


class CostTests(unittest.TestCase):
    ROWS = {"components": {
        "brokerage": [{"applies": {"segment": "CM"}, "effective_from": "2015-01-01", "kind": "zero"}],
        "stt": [{"applies": {"segment": "CM"}, "effective_from": "2015-01-01", "buy_ppb": 1_000_000, "sell_ppb": 1_000_000},
                {"applies": {"segment": "CM"}, "effective_from": "2026-01-01", "buy_ppb": 0, "sell_ppb": 2_000_000}],
        "exchange_txn": [{"applies": {}, "effective_from": "2015-01-01", "pct_ppb": 30_700}],
        "sebi": [{"applies": {}, "effective_from": "2015-01-01", "pct_ppb": 1_000}],
        "stamp": [{"applies": {"segment": "CM"}, "effective_from": "2015-01-01", "buy_ppb": 150_000}],
        "gst": [{"applies": {}, "effective_from": "2015-01-01", "rate_ppb": 180_000_000, "on": ["brokerage", "exchange_txn", "sebi"]}],
    }}

    def test_rows_by_date_and_side(self):
        c = costs.Costs(self.ROWS, {})
        old = c.order("EQ", "CNC", "SELL", 10_000_000, "2025-06-02")
        new = c.order("EQ", "CNC", "SELL", 10_000_000, "2026-06-02")
        self.assertEqual((old["stt"], new["stt"]), (10_000, 20_000))
        self.assertEqual(c.order("EQ", "CNC", "BUY", 10_000_000, "2025-06-02")["stamp"], 1_500)
        self.assertEqual(old["exchange"], 307)
        self.assertEqual(old["gst"], 57)                               # 18 % of (0 + 307 + 10) = 57.06 → 57

    def test_zero_or_two_rows_is_an_error(self):
        rows = {"components": {**self.ROWS["components"],
                               "brokerage": [{"applies": {"segment": "FO"}, "effective_from": "2015-01-01", "kind": "zero"}]}}
        with self.assertRaisesRegex(costs.CostError, "no row"):
            costs.Costs(rows, {}).order("EQ", "CNC", "BUY", 100, "2025-01-01")
        two = {"components": {**self.ROWS["components"], "brokerage": [
            {"applies": {"segment": "CM"}, "effective_from": "2015-01-01", "kind": "zero"},
            {"applies": {"product": "CNC"}, "effective_from": "2015-01-01", "kind": "zero"}]}}
        with self.assertRaisesRegex(costs.CostError, "2 rows"):
            costs.Costs(two, {}).order("EQ", "CNC", "BUY", 100, "2025-01-01")


class MatcherTests(unittest.TestCase):
    def test_protection_and_slippage(self):
        self.assertEqual(protection_limit("BUY", 10050, 200, 5), 10250)
        self.assertEqual(protection_limit("SELL", 10100, 200, 5), 9900)
        self.assertEqual(slipped("BUY", 10000, 5, {"kind": "fixed_ticks", "k": 2}, None, 10005), 10005)  # capped
        self.assertEqual(slipped("SELL", 10000, 5, {"kind": "bps", "bp": 5}, None, None), 9995)
        self.assertEqual(participation_cap(1000, 100_000, 65), 65)

    def test_first_reach(self):
        from fractions import Fraction
        self.assertEqual(first_reach([100, 110, 90, 105], 95, down=True), (1, Fraction(15, 20)))
        self.assertIsNone(first_reach([100, 110, 95, 105], 90, down=True))


class IndicatorTests(unittest.TestCase):
    def test_values_by_hand(self):
        self.assertEqual(S.sma([1, 2, 3, 4], 3), 3.0)
        self.assertEqual(S.ema([1, 2, 3, 4, 5], 3), 4.0)
        self.assertAlmostEqual(S.rsi([10, 11, 12, 11, 13], 2), 100 - 100 / 6, places=12)
        self.assertEqual(S.rsi([1, 2, 3], 2), 100.0)
        self.assertEqual(S.atr([(10, 8, 9), (11, 9, 10), (12, 10, 11), (14, 9, 10)], 2), 3.5)
        self.assertTrue(S.is_nan(S.sma([1], 3)))


class SourceCheckTests(unittest.TestCase):
    def test_banned(self):
        for code in ("import time", "from os import path", "import random", "x = open('f')",
                     "from datetime import datetime\nt = datetime.now()"):
            with self.subTest(code=code), self.assertRaises(S.StrategyCodeError):
                S.check_source(code)
        S.check_source("from stockify.strategy import Strategy\nclass A(Strategy):\n    pass\n")


class MetricTests(unittest.TestCase):
    def test_no_trades_and_flat(self):
        m = metrics.compute(capital=100, equity=[("2026-01-01", 100, 100, 0, 0, 0)], fills=[], orders={}, charges=[],
                            risk_free=[], rejects={}, participation_ppm=0, exposure=0.0)
        self.assertEqual((m["trades"], m["sharpe"], m["win_rate"], m["cagr"]), (0, None, None, 0.0))


class GoldenTests(unittest.TestCase):
    def test_every_golden(self):
        found = golden.goldens()
        self.assertGreaterEqual(len(found), 14)
        for path, g in found:
            with self.subTest(golden=g["id"]):
                self.assertEqual(golden.run_one(g), [], g["id"])


if __name__ == "__main__":
    unittest.main()


class CaptureCompareTests(unittest.TestCase):
    def test_kite_note_mapping(self):
        import json
        import tempfile
        from pathlib import Path
        from stockify.tools import capture_charges
        req = {"case": "fut_sell", "kind": "FUT", "product": "NRML", "transaction_type": "SELL", "quantity": 65,
               "price_paise": 2500000}
        # Kite's real answer for this order on 2026-11-06 (exact components, D-46), with its float noise
        note = {"charges": {"brokerage": 20, "transaction_tax": 812.5, "exchange_turnover_charge": 29.7375,
                            "sebi_turnover_charge": 1.625, "stamp_duty": 0, "gst": {"total": 9.245249999999999},
                            "total": 873.10775}}
        path = Path(tempfile.mkdtemp(dir=str(support.SANDBOX_ROOT))) / "2026-11-06.json"
        path.write_text(json.dumps({"captured_on": "2026-11-06", "cases": [{"request": req, "response": note}]}))
        self.assertEqual(capture_charges.compare_capture(path), [])
        note["charges"]["transaction_tax"], note["charges"]["total"] = 812.6, 873.20775
        path.write_text(json.dumps({"captured_on": "2026-11-06", "cases": [{"request": req, "response": note}]}))
        self.assertEqual(capture_charges.compare_capture(path),
                         ["fut_sell stt: ours 812.500000 vs Kite 812.600000", "fut_sell total: ours 87311 paise vs Kite 87321"])

    def test_stamp_rounds_to_the_rupee(self):
        """D-46: stamp duty is a whole rupee; the order total is rounded once."""
        rows = {"components": {
            "brokerage": [{"applies": {}, "effective_from": "2015-01-01", "kind": "zero"}],
            "stt": [{"applies": {}, "effective_from": "2015-01-01", "buy_ppb": 1_000_000, "sell_ppb": 1_000_000}],
            "exchange_txn": [{"applies": {}, "effective_from": "2015-01-01", "pct_ppb": 30_700}],
            "sebi": [{"applies": {}, "effective_from": "2015-01-01", "pct_ppb": 1_000}],
            "stamp": [{"applies": {}, "effective_from": "2015-01-01", "buy_ppb": 150_000}],
            "gst": [{"applies": {}, "effective_from": "2015-01-01", "rate_ppb": 180_000_000,
                     "on": ["brokerage", "exchange_txn", "sebi"]}]}}
        c = costs.Costs(rows, {})
        odd = c.order("EQ", "CNC", "BUY", 7 * 149900, "2026-11-06")    # Kite: stamp 2, total 12.885501
        self.assertEqual((odd["stamp"], odd["total"]), (200, 1289))
        self.assertEqual(c.order("EQ", "CNC", "BUY", 1_000_000, "2026-11-06")["stamp"], 200)   # 1.50 -> 2
