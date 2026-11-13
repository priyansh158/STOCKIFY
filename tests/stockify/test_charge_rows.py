"""CB 4.1 (Z-3, D-53): the shipped NSE exchange-transaction rows for equity are picked by date.

Before 2024-10-01: Zerodha's older 0.00322% (an older capture, unverified, labelled); from 2024-10-01: NSE's
flat 0.00297% (confirmed); from 2026-10-29 (observed): the current 0.00307% (the VERIFY row, A1 §7.2)."""
from __future__ import annotations

import unittest

import tests.support  # noqa: F401  — must precede project imports

from stockify import golden
from stockify.config import loader
from stockify.oracle import costs


def _cm(trade_date: str) -> dict:
    rows = loader.load_json("charges.json")["components"]["exchange_txn"]
    return costs.row_for(rows, {"segment": "CM", "product": "CNC", "side": "BUY", "kind": "EQ"}, trade_date,
                         "exchange_txn")


class ExchangeRowTests(unittest.TestCase):
    def test_each_side_of_each_date(self):
        cases = [("2015-01-01", "0.0000322", False), ("2022-06-01", "0.0000322", False),
                 ("2024-09-30", "0.0000322", False), ("2024-10-01", "0.0000297", True),
                 ("2026-10-28", "0.0000297", True), ("2026-10-29", "0.0000307", False),
                 ("2026-11-06", "0.0000307", False)]
        for d, pct, verified in cases:
            with self.subTest(date=d):
                row = _cm(d)
                self.assertEqual((row["pct"], row["verified"]), (pct, verified))

    def test_the_fo_rows_are_unchanged(self):
        rows = loader.load_json("charges.json")["components"]["exchange_txn"]
        fut = costs.row_for(rows, {"segment": "FO", "kind": "FUT", "product": "NRML", "side": "BUY"}, "2025-06-02",
                            "exchange_txn")
        self.assertEqual(fut["pct"], "0.0000173")


class GoldensUnchangedTests(unittest.TestCase):
    def test_every_simulated_golden_still_passes_on_the_oracle(self):
        failures = {}
        for _, g in golden.goldens(None, include_capture=False):
            problems = golden.run_one(g, "oracle")
            if problems:
                failures[g["id"]] = problems[:3]
        self.assertEqual(failures, {})


if __name__ == "__main__":
    unittest.main()
