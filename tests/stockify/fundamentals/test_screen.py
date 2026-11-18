"""FS 5: the screen's order and its reasons: each stock against its industry (R3), no score in what leaves the
module."""
from __future__ import annotations

import json
import unittest

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
        # No borrowings, no finance costs (no operating margin) and a loss a year ago (no growth): P/E, P/B and ROE.
        self.company("THREE", "Capital Goods", debt=None, prior=-10, omit=("finance_costs",))
        # The same without the loss a year ago: four figures, placed; its missing two count as the median.
        self.company("FOUR", "Capital Goods", debt=None, omit=("finance_costs",))
        result = screen.run(DAY)
        placed = {r["symbol"]: r for r in result["rows"]}
        self.assertNotIn("THREE", placed)
        self.assertIn({"symbol": "THREE", "reason": "too few figures"}, result["left_out"])
        self.assertIn("FOUR", placed)
        self.assertIsNone(placed["FOUR"]["ratios"]["debt_equity"])
        self.assertIsNone(placed["FOUR"]["ratios"]["op_margin"])

    def test_a_missing_figure_never_lifts_a_stock(self):
        # Ruling R9 (c): FOUR is GOOD without its debt and margin figures (GOOD's best two); a missing figure counts
        # as the median, so FOUR ranks below GOOD, and no reason names what it did not file.
        five_capital_goods(self)
        self.company("FOUR", "Capital Goods", price=20000, profit=300, prior=200, equity=6000, debt=None,
                     omit=("finance_costs",))
        rows = screen.order(DAY)
        names = [r["symbol"] for r in rows]
        self.assertEqual(names[0], "GOOD")
        self.assertGreater(names.index("FOUR"), names.index("GOOD"))
        four = rows[names.index("FOUR")]
        self.assertNotIn("debt", four["short_reason"])
        self.assertNotIn("margin", four["short_reason"])

    def test_negative_equity_is_a_red_flag(self):
        # Ruling R9 d: negative equity takes the worst P/B, D/E and ROE places, so NEGEQ (MID1 but for its equity)
        # falls below every profitable peer with equity.
        five_capital_goods(self)
        self.company("NEGEQ", "Capital Goods", price=50000, equity=-1000, equity_prev=-500)
        rows = screen.order(DAY)
        names = [r["symbol"] for r in rows]
        for peer in ("GOOD", "MID1", "MID2", "MID3"):
            self.assertGreater(names.index("NEGEQ"), names.index(peer), peer)
        neg = rows[names.index("NEGEQ")]
        self.assertIsNone(neg["ratios"]["roe"])
        self.assertIsNone(neg["ratios"]["debt_equity"])
        self.assertTrue(neg["short_reason"].startswith("Negative equity; "), neg["short_reason"])

    def test_unpriced_stocks_are_left_out(self):
        # Ruling R9 (b): no market value on the day, no place, however good the other figures.
        five_capital_goods(self)
        self.company("NOPXGOOD", "Capital Goods", price=None, profit=300, prior=200, equity=6000, debt=300, op=400)
        self.company("NOTLISTED", "Capital Goods", listed=False)
        result = screen.run(DAY)
        self.assertEqual({r["symbol"] for r in result["rows"]}, {"GOOD", "MID1", "MID2", "MID3", "BAD"})
        for sym in ("NOPXGOOD", "NOTLISTED"):
            self.assertIn({"symbol": sym, "reason": "no market price to judge it by on this day"},
                          result["left_out"])

    def test_loss_maker_ranks_below_profitable_peers(self):
        # Ruling R9 (a): LOSS is MID1 with a loss; it takes the worst P/E place, not none.
        five_capital_goods(self)
        self.company("LOSS", "Capital Goods", price=50000, profit=-50, prior=-40)
        names = [r["symbol"] for r in screen.order(DAY)]
        for peer in ("GOOD", "MID1", "MID2", "MID3"):
            self.assertGreater(names.index("LOSS"), names.index(peer), peer)

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
        # No interest figures: P/E, P/B and ROE are three of four: placed.
        self.company("THREEBANK", "Financial Services", financial=True, debt=None, price=50000,
                     omit=("interest_earned",))
        # No interest figures and no balance sheet: P/E alone: left out.
        self.company("ONEBANK", "Financial Services", financial=True, debt=None, price=50000,
                     omit=("interest_earned", "total_equity"))
        result = screen.run(DAY)
        rows = {r["symbol"]: r for r in result["rows"]}
        self.assertEqual(set(rows), {f"BANK{n}" for n in range(5)} | {"THREEBANK"})
        self.assertIn({"symbol": "ONEBANK", "reason": "too few figures"}, result["left_out"])
        self.assertIsNone(rows["THREEBANK"]["ratios"]["nii_growth"])
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
        self.assertTrue(trap["short_reason"].startswith("Profits falling and debt rising; "), trap["short_reason"])
        self.assertLessEqual(len(trap["short_reason"]), 90)

    def test_rising_debt_is_never_also_less_debt(self):
        # M4: cheap, with less debt than most of its industry, but that debt has trebled in a year.
        five_capital_goods(self)
        # D/E 0.15, second lowest of six, against 0.05 a year earlier.
        self.company("UPDEBT", "Capital Goods", price=15000, debt=900, debt_prev=300, equity=6000, equity_prev=6000)
        row = next(r for r in screen.order(DAY) if r["symbol"] == "UPDEBT")
        self.assertTrue(row["short_reason"].startswith("Debt rising; "), row["short_reason"])
        self.assertNotIn("less debt", row["short_reason"])
        self.assertNotIn("debt-free", row["short_reason"])

    def test_swing_to_a_loss_is_said_so(self):
        # M3: profit 200 crore a year ago, a 100 crore loss now; still cheap on its four quarters' profit.
        five_capital_goods(self)
        self.company("SWING", "Capital Goods", price=10000, profit=-100, prior=200)
        rows = {r["symbol"]: r for r in screen.order(DAY)}
        self.assertTrue(any("swung to a loss" in s for s in rows["SWING"]["reasons"]), rows["SWING"]["reasons"])
        self.assertFalse(any("fell 150" in s for s in rows["SWING"]["reasons"]))

    def test_loss_maker_reason(self):
        five_capital_goods(self)
        self.company("LOSS", "Capital Goods", profit=-50, prior=-40, debt=0)
        rows = {r["symbol"]: r for r in screen.order(DAY)}
        self.assertIn("LOSS", rows)
        self.assertTrue(any("Loss-making" in s for s in rows["LOSS"]["reasons"]))
        self.assertTrue(rows["LOSS"]["short_reason"].startswith("Loss-making; "), rows["LOSS"]["short_reason"])
        self.assertLessEqual(len(rows["LOSS"]["short_reason"]), 90)
        self.assertIn("no debt", rows["LOSS"]["short_reason"])             # D/E 0: an absolute word is true

    def test_relative_words_for_relative_places(self):
        # I3: a power industry where everyone borrows heavily: the lowest D/E (1.5) is less debt than its
        # industry, never "little debt"; a margin is wider than its industry's, never just "wide".
        for n, (debt, op) in enumerate(((9000, 60), (10000, 50), (12000, 40), (14000, 30), (16000, 20))):
            self.company(f"PWR{n}", "Power", price=40000 + 5000 * n, debt=debt, op=op)
        rows = screen.order(DAY)
        top = rows[0]
        self.assertEqual(top["symbol"], "PWR0")
        self.assertAlmostEqual(top["ratios"]["debt_equity"], 1.5)
        for r in rows:
            for word in ("little debt", "wide margins", "high returns", "low debt"):
                self.assertNotIn(word, r["short_reason"].lower(), r["short_reason"])
        self.assertIn("less debt", top["short_reason"])
        self.assertIn("than its industry", top["short_reason"])

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


class WordingTests(unittest.TestCase):
    """The short reason's body reads as English for each mix of points (re-review, fix round 2)."""
    R = {"pe": 12.0, "pb": 1.5, "roe": 0.2, "debt_equity": 0.0, "op_margin": 0.3, "profit_growth": 0.1,
         "nii_growth": None}

    def body(self, picks, **r):
        return screen._body(picks, {**self.R, **r}, "its industry")

    def test_mixes(self):
        cases = [
            (["debt_equity", "op_margin"], {}, "wider margins than its industry, with no debt"),
            (["debt_equity"], {}, "debt-free"),
            (["debt_equity", "profit_growth"], {}, "debt-free, with rising profits"),
            (["debt_equity", "op_margin"], {"debt_equity": 0.4}, "less debt and wider margins than its industry"),
            (["pe", "debt_equity", "op_margin"], {}, "cheaper than its industry on earnings, with wider margins "
                                                     "and no debt"),
            (["pe", "roe", "op_margin"], {}, "cheaper than its industry on earnings, with a higher return on "
                                             "equity and wider margins"),
            (["pe", "debt_equity"], {}, "cheaper than its industry on earnings, with no debt"),
        ]
        for picks, r, want in cases:
            self.assertEqual(self.body(picks, **r), want, picks)
            self.assertNotIn("and debt-free", self.body(picks, **r))
            self.assertNotIn("with debt-free", self.body(picks, **r))

    def test_changes(self):
        self.assertEqual(screen._change(-1.0), "fell to zero")
        self.assertEqual(screen._change(-1.5), "swung to a loss")
        self.assertEqual(screen._change(-0.25), "down 25.0%")
        line = screen._figure_line("profit_growth", -1.5, -1.2, False)
        self.assertEqual(line, "Profit swung to a loss on the same quarter a year earlier (half or more of its "
                               "industry fell to zero or a loss).")
        self.assertIn("(its industry: up 10.0%)", screen._figure_line("profit_growth", 0.2, 0.1, False))


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
