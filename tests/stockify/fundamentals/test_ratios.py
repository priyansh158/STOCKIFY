"""FS 5: a stock's ratios on a day, from point-in-time figures (pit.py)."""
from __future__ import annotations

from tests.stockify.fundamentals.screen_support import DAY, ScreenCase

from stockify.fundamentals import ratios


class RatioTests(ScreenCase):
    def test_ratio_formulas(self):
        # Price ₹500 × 100 crore shares = ₹50,000 crore. TTM profit 4 × 250 = ₹1,000 crore. Equity 6,000 (Sep 2025)
        # and 4,000 (Mar 2025); debt 1,500. Operating profit 300 on revenue 1,000 each quarter. A year ago 200.
        self.company("ABB", "Capital Goods")
        r = ratios.for_stock("ABB", DAY)
        self.assertEqual(set(r), set(ratios.KEYS))
        self.assertAlmostEqual(r["pe"], 50.0)
        self.assertAlmostEqual(r["pb"], 50000 / 6000)
        self.assertAlmostEqual(r["roe"], 1000 / 5000)
        self.assertAlmostEqual(r["debt_equity"], 1500 / 6000)
        self.assertAlmostEqual(r["op_margin"], 0.3)
        self.assertAlmostEqual(r["profit_growth"], 0.25)
        self.assertIsNone(r["nii_growth"])

    def test_loss_maker_has_no_pe_and_is_flagged(self):
        self.company("LOSS", "Capital Goods", profit=-50, prior=-40)
        d = ratios.detail("LOSS", DAY)
        self.assertIsNone(d["ratios"]["pe"])
        self.assertIn("loss-making", d["flags"])
        self.assertAlmostEqual(d["ratios"]["roe"], -200 / 5000)
        self.assertIsNone(d["ratios"]["profit_growth"])          # growth from a loss means nothing
        self.assertIsNotNone(d["ratios"]["pb"])

    def test_no_market_cap_no_pe_or_pb(self):
        self.company("NOPX", "Capital Goods", price=None)
        d = ratios.detail("NOPX", DAY)
        self.assertIsNone(d["ratios"]["pe"])
        self.assertIsNone(d["ratios"]["pb"])
        self.assertIn("no market value", d["flags"])
        self.assertAlmostEqual(d["ratios"]["roe"], 0.2)

    def test_financials_use_nii_growth_not_debt_or_margin(self):
        # Net interest income ₹300 crore against ₹250 crore in the same quarter a year earlier: +20 %.
        self.company("HDFCBANK", "Financial Services", financial=True, debt=None)
        d = ratios.detail("HDFCBANK", DAY, sector="Financial Services")
        r = d["ratios"]
        self.assertTrue(d["financial"])
        self.assertAlmostEqual(r["nii_growth"], 0.2)
        self.assertIsNone(r["debt_equity"])
        self.assertIsNone(r["op_margin"])
        self.assertAlmostEqual(r["profit_growth"], 0.25)            # shown, not placed on (M2)
        self.assertAlmostEqual(r["pe"], 50.0)
        self.assertAlmostEqual(r["roe"], 0.2)

    def test_newest_quarter_and_debt_a_year_ago(self):
        self.company("ABB", "Capital Goods", debt_prev=600)
        d = ratios.detail("ABB", DAY)
        self.assertEqual(d["newest_quarter"], "2025-09-30")
        self.assertAlmostEqual(d["debt_equity_year_ago"], 600 / 4000)
