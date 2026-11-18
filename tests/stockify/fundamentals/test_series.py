"""FS 9: the details page's quarter-by-quarter figures (series.py), as ratios.py computes them."""
from __future__ import annotations

import unittest
from datetime import date

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.fundamentals.screen_support import CR, DAY, ScreenCase

from stockify.fundamentals import ratios, series


class SeriesTests(ScreenCase):

    def test_negative_equity_quarter_has_no_roe_point(self):
        # Newest sheet (Sep 2025) equity -100 Cr, the one before +500 Cr: the mean is positive, the equity is not.
        self.company("NEG", "Capital Goods", equity=-100, equity_prev=500)
        figures = series.quarterly("NEG", DAY, financial=False)
        self.assertNotIn("2025-09-30", figures["roe"])
        self.assertIn("2025-06-30", figures["roe"])             # its newest sheet is Mar 2025, +500 Cr
        self.assertIsNone(ratios.detail("NEG", DAY)["ratios"]["roe"])
        self.assertNotIn("2025-09-30", figures["pb"])
        self.assertNotIn("2025-09-30", figures["debt_equity"])

    def test_a_financial_draws_interest_income_not_revenue_or_debt(self):
        self.company("BANK", ratios.FINANCIAL_SECTOR, financial=True, nii=300)
        figures = series.quarterly("BANK", DAY, financial=True)
        self.assertEqual(figures["nii"]["2025-09-30"], 300 * CR)
        self.assertEqual(figures["revenue"], {})
        self.assertEqual(figures["debt_equity"], {})
        self.assertEqual(figures["op_margin"], {})
        keys = [s["key"] for s in series.peer_series("BANK", DAY, financial=True, peers=[])]
        self.assertIn("nii", keys)
        self.assertNotIn("revenue", keys)

    def test_precompute_saves_each_listed_stock_with_its_peer_medians(self):
        for sym, revenue in (("A1", 900), ("A2", 1000), ("A3", 1100), ("A4", 1200), ("A5", 1300)):
            self.company(sym, "Capital Goods", revenue=revenue)
        self.company("SOLO", "Textiles", revenue=2000)
        rows = [{"symbol": s, "sector": "Capital Goods"} for s in ("A1", "A2", "A3", "A4", "A5")]
        rows.append({"symbol": "SOLO", "sector": "Textiles"})
        series.precompute(DAY, rows, min_peers=5)
        rev = lambda sym: next(s for s in series.saved(DAY.isoformat(), sym) if s["key"] == "revenue")  # noqa: E731
        self.assertEqual(rev("A1")["points"][-1]["sector_median"], 1100 * CR)
        # SOLO's industry has too few: all six listed stocks; 900..1300 and 2000: the middle two's mean
        self.assertEqual(rev("SOLO")["points"][-1]["sector_median"], 1150 * CR)
        self.assertIsNone(series.saved(DAY.isoformat(), "NOPE"))
        self.assertIsNone(series.saved(date(2025, 1, 1).isoformat(), "A1"))


if __name__ == "__main__":
    unittest.main()
