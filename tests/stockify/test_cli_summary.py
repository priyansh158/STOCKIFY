"""REG 5.4 (audit M-5, part): both console summary lines (`oracle run` and `run`) say how many orders were rejected."""
from __future__ import annotations

import unittest

import tests.support  # noqa: F401  — must precede project imports

from stockify import __main__ as cli


class SummaryLineTests(unittest.TestCase):
    M = {"nav_start_paise": 100000000, "nav_end_paise": 101234500, "trades": 3, "costs_total_paise": 4567,
         "rejects": {"RMS_FUNDS": 2, "FREEZE_QTY": 1}}

    def test_the_line_says_rejects_after_trades(self):
        line = cli._summary_line(self.M, ["approximate"])
        self.assertEqual(line, "NAV 1,000,000.00 -> 1,012,345.00; trades 3; rejects 3; costs 45.67; labels approximate")

    def test_no_rejects_is_zero_and_a_missing_metric_is_zero(self):
        self.assertIn("rejects 0;", cli._summary_line({**self.M, "rejects": {}}, []))
        self.assertIn("rejects 0;", cli._summary_line({}, []))


if __name__ == "__main__":
    unittest.main()
