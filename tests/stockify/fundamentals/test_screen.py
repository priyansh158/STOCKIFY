"""FS 5: the screen's order and its reasons: each stock against its industry (R3), no score in what leaves the
module."""
from __future__ import annotations

import json

from tests.stockify.fundamentals.screen_support import DAY, ScreenCase

from stockify.fundamentals import screen

PUBLIC = {"symbol", "sector", "peer_group", "ratios", "sector_medians", "reasons", "short_reason"}


def five_capital_goods(case: ScreenCase) -> None:
    """Five capital goods makers from cheap and healthy (GOOD) to dear and weak (BAD)."""
    case.company("GOOD", "Capital Goods", price=20000, profit=300, prior=200, equity=6000, debt=300, op=400)
    case.company("MID1", "Capital Goods", price=50000)
    case.company("MID2", "Capital Goods", price=45000)
    case.company("MID3", "Capital Goods", price=60000, op=250)
    case.company("BAD", "Capital Goods", price=90000, profit=150, prior=200, equity=3000, debt=4000, op=100)


class OrderTests(ScreenCase):
    def test_order_prefers_cheap_and_healthy(self):
        five_capital_goods(self)
        rows = screen.order(DAY)
        self.assertEqual([r["symbol"] for r in rows][0], "GOOD")
        self.assertEqual([r["symbol"] for r in rows][-1], "BAD")
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["peer_group"], "Industry: Capital Goods")
        self.assertEqual(rows[0]["sector"], "Capital Goods")
        self.assertAlmostEqual(rows[0]["sector_medians"]["pe"], 50.0)        # MID1: 50,000 cr / 1,000 cr

    def test_balance_moves_the_order(self):
        # CHEAP is the cheapest but weak; HEALTHY the healthiest but dear.
        five_capital_goods(self)
        self.company("CHEAP", "Capital Goods", price=10000, profit=200, prior=250, equity=6000, debt=3000, op=150)
        self.company("HEALTHY", "Capital Goods", price=150000, profit=400, prior=200, equity=6000, debt=0, op=600)
        by_value = [r["symbol"] for r in screen.order(DAY, balance=1.0)]
        by_quality = [r["symbol"] for r in screen.order(DAY, balance=0.0)]
        self.assertEqual(by_value[0], "CHEAP")
        self.assertEqual(by_quality[0], "HEALTHY")
        self.assertLess(by_value.index("CHEAP"), by_value.index("HEALTHY"))
        self.assertLess(by_quality.index("HEALTHY"), by_quality.index("CHEAP"))

    def test_small_sector_uses_all_stocks(self):
        # Ruling R3: two textile stocks are too few to compare among themselves: they are compared with every stock.
        five_capital_goods(self)
        self.company("TEX1", "Textiles", price=30000)
        self.company("TEX2", "Textiles", price=70000)
        rows = {r["symbol"]: r for r in screen.order(DAY)}
        self.assertEqual(rows["TEX1"]["peer_group"], "All stocks (Textiles has too few)")
        self.assertEqual(rows["GOOD"]["peer_group"], "Industry: Capital Goods")
        # The median P/E of all seven stocks: 16.7, 30, 45, 50, 60, 70, 150 → 50.
        self.assertAlmostEqual(rows["TEX1"]["sector_medians"]["pe"], 50.0)
        self.assertIn("most stocks", rows["TEX1"]["short_reason"])

    def test_needs_four_of_six(self):
        five_capital_goods(self)
        # No price (no P/E, no P/B) and no borrowings filed: three figures, left out.
        self.company("THREE", "Capital Goods", price=None, debt=None)
        # No price, but the four other figures: placed, without P/E or P/B.
        self.company("FOUR", "Capital Goods", price=None)
        result = screen.run(DAY)
        placed = {r["symbol"]: r for r in result["rows"]}
        self.assertNotIn("THREE", placed)
        self.assertIn({"symbol": "THREE", "reason": "too few figures"}, result["left_out"])
        self.assertIn("FOUR", placed)
        self.assertIsNone(placed["FOUR"]["ratios"]["pe"])
        self.assertIsNone(placed["FOUR"]["ratios"]["pb"])

    def test_stale_results_are_left_out(self):
        # Newest quarter March 2025: 245 days before 1 Dec 2025, more than 200.
        five_capital_goods(self)
        self.company("OLD", "Capital Goods", newest="2025-03-31")
        result = screen.run(DAY)
        self.assertNotIn("OLD", [r["symbol"] for r in result["rows"]])
        self.assertIn({"symbol": "OLD", "reason": "results are out of date"}, result["left_out"])

    def test_financials(self):
        # Banks: P/E, P/B, ROE and net interest income growth; three of the four place a bank.
        for n, (price, nii) in enumerate(((30000, 330), (40000, 300), (50000, 280), (60000, 260), (70000, 240))):
            self.company(f"BANK{n}", "Financial Services", financial=True, debt=None, price=price, nii=nii)
        self.company("NOPXBANK", "Financial Services", financial=True, debt=None, price=None)
        result = screen.run(DAY)
        rows = {r["symbol"]: r for r in result["rows"]}
        self.assertEqual(set(rows), {f"BANK{n}" for n in range(5)})
        self.assertIn({"symbol": "NOPXBANK", "reason": "too few figures"}, result["left_out"])
        self.assertEqual(result["rows"][0]["symbol"], "BANK0")
        self.assertEqual(rows["BANK0"]["peer_group"], "Industry: Financial Services")
        self.assertIsNone(rows["BANK0"]["ratios"]["debt_equity"])
        self.assertIsNone(rows["BANK0"]["ratios"]["op_margin"])
        self.assertAlmostEqual(rows["BANK0"]["ratios"]["nii_growth"], 330 / 250 - 1)
        self.assertNotIn("debt", rows["BANK0"]["short_reason"])
        self.assertNotIn("margin", rows["BANK0"]["short_reason"])


class ReasonTests(ScreenCase):
    def test_short_reason_names_the_strongest_points(self):
        five_capital_goods(self)
        good = screen.order(DAY)[0]
        self.assertTrue(good["short_reason"].startswith("Cheaper than its industry on earnings"),
                        good["short_reason"])
        self.assertLessEqual(len(good["short_reason"]), 90)

    def test_value_trap_reason(self):
        five_capital_goods(self)
        # The cheapest on earnings, but its profit fell from 400 to 250 crore and its debt doubled in a year.
        self.company("TRAP", "Capital Goods", price=15000, profit=250, prior=400, debt=3000, debt_prev=1500)
        trap = next(r for r in screen.order(DAY) if r["symbol"] == "TRAP")
        text = " ".join(trap["reasons"])
        self.assertIn("profit fell", text)
        self.assertIn("debt has risen", text)
        self.assertIn("but", trap["short_reason"])
        self.assertLessEqual(len(trap["short_reason"]), 90)

    def test_loss_maker_reason(self):
        five_capital_goods(self)
        self.company("LOSS", "Capital Goods", profit=-50, prior=-40, debt=0)
        rows = {r["symbol"]: r for r in screen.order(DAY)}
        self.assertIn("LOSS", rows)
        self.assertTrue(any("Loss-making" in s for s in rows["LOSS"]["reasons"]))

    def test_no_score_in_public_rows(self):
        five_capital_goods(self)
        self.company("TEX1", "Textiles", price=30000)
        rows = screen.order(DAY)
        for r in rows:
            self.assertEqual(set(r), PUBLIC)
            blob = json.dumps(r).lower()
            for word in ("composite", "percentile", "score", "rank", "valuation", "quality"):
                self.assertNotIn(word, blob, r["symbol"])
            self.assertLessEqual(len(r["short_reason"]), 90)
            self.assertFalse(any(ch.isdigit() for ch in r["short_reason"]), r["short_reason"])


class SaveTests(ScreenCase):
    def test_save_and_latest(self):
        self.assertEqual(screen.latest(), (None, []))
        five_capital_goods(self)
        rows = screen.order(DAY)
        screen.save(DAY, rows)
        screen.save(DAY, rows)                                   # saving the day again replaces it
        day, back = screen.latest()
        self.assertEqual(day, DAY.isoformat())
        self.assertEqual(back, json.loads(json.dumps(rows)))
