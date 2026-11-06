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


if __name__ == "__main__":
    unittest.main()
