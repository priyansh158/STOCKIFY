"""Tracked defects — confirmed bugs, written as the behaviour that *should* hold.

Each test here is marked `@unittest.expectedFailure`, which means:

  * While the bug exists, the test fails and unittest records it as "expected
    failure" — the suite stays green, so these never block a push.
  * The moment someone fixes the bug, the test passes and unittest reports an
    "unexpected success", which makes the run **non-zero exit**. That is the
    signal to delete the decorator and promote the test into the real suite.

So this file is a to-do list that tells you when it is done.

None of these were introduced by the test suite; each was reproduced against
the module directly. Reproduction notes are inline.
"""
from __future__ import annotations

import unittest

import tests.support  # noqa: F401  — must precede project imports

import india_utils


# ── DEFECT 1 — format_inr_full loses the carry when rounding up ──────────────
#
# Severity: low, cosmetic, but it renders a wrong number.
#
# The integer and fractional parts are formatted independently:
#
#     int_part = int(n); frac = n - int_part
#     ...
#     f"{grouped}.{int(round(frac * 10**decimals)):0{decimals}d}"
#
# When `frac` rounds up to a full unit, the carry never reaches `grouped` and
# the fraction overflows its width:
#
#     >>> india_utils.format_inr_full(0.999, decimals=2)
#     '0.100'                 # expected '1.00'
#
# Fix sketch: round the whole value once up front
# (`n = round(n, decimals)`) before splitting into int/frac parts.

class InrRoundingCarryDefectTests(unittest.TestCase):
    @unittest.expectedFailure
    def test_fraction_rounding_up_carries_into_the_integer_part(self):
        self.assertEqual(india_utils.format_inr_full(0.999, decimals=2), "1.00")

    @unittest.expectedFailure
    def test_carry_across_a_grouping_boundary(self):
        self.assertEqual(india_utils.format_inr_full(99_999.999, decimals=2), "1,00,000.00")

    @unittest.expectedFailure
    def test_compact_formatter_inherits_the_same_carry_bug(self):
        self.assertEqual(india_utils.format_inr_compact(0.999), "₹1.00")

    @unittest.expectedFailure
    def test_fraction_never_renders_more_digits_than_requested(self):
        for value in (0.999, 1.999, 99.995):
            with self.subTest(value=value):
                out = india_utils.format_inr_full(value, decimals=2)
                self.assertEqual(len(out.split(".")[1]), 2)


# ── DEFECT 2 — a fill on the last day's daily bar is in no equity row ────────
#
# Severity: medium for daily-bar backtests: the run's final NAV (and every metric computed from equity.csv)
# leaves out the last day's trades, while fills.csv and the trade metrics include them.
#
# A daily bar for day d is knowable only at d + 1 day, 00:00 IST (A4 §1.1); D-44 lets a DAY order wait for it.
# For the last bundle day that moment is after the run's final DAY_END (23:59:59.999999999 IST), so the fill is
# booked after the last equity and positions_eod rows were written. Oracle and engine agree, so this is a gap in
# A4 §1.10, not a parity bug. Found by P-06 (tests/stockify/test_accounting.py), e.g. daily_world(5211):
# a SELL fills at 2026-03-19 00:00 IST on the 2026-03-18 bar, the last day.
#
# Fix sketch (needs an A4 decision first): either a run's last DAY_END comes after its last daily bar's
# knowable time, or the bundle resolver adds the next trading day so that bar lands inside the run.

class LastDailyBarFillDefectTests(unittest.TestCase):
    @unittest.expectedFailure
    def test_every_fill_is_in_some_equity_row(self):
        import tempfile
        from pathlib import Path

        from stockify.oracle import run_bundle
        from tests.stockify.test_accounting import accounting_problems
        from tests.stockify.test_engine_strategies import REFS, _load, _materialise, daily_world
        g, params = daily_world(5211)
        with tempfile.TemporaryDirectory(prefix="defect-") as tmp:
            folder = Path(tmp)
            pack, bundle = _materialise(g, params, folder)
            run_bundle(bundle, [pack], folder / "run", strategy=_load(REFS["sma_cross"]), report=False)
            self.assertEqual(accounting_problems(folder / "run"), [])


if __name__ == "__main__":
    unittest.main()
