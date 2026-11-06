"""stockify.numerics: exact rate conversion, integer helpers, canonical output
(A4_engine_semantics.md §0, §9.2)."""
from __future__ import annotations

import json
import random
import unittest

import tests.support  # noqa: F401  — must precede project imports

from stockify import numerics as n


class RateConversionTests(unittest.TestCase):
    def test_documented_examples(self):
        # A4 §0.1 table.
        self.assertEqual(n.to_ppb("0.0015"), 1_500_000)
        self.assertEqual(n.to_ppb("0.0000183"), 18_300)
        self.assertEqual(n.to_ppb("0.000001"), 1_000)
        self.assertEqual(n.to_ppb("0.18"), 180_000_000)
        self.assertEqual(n.to_ppm("0.1"), 100_000)
        self.assertEqual(n.to_bp("0.02"), 200)
        self.assertEqual(n.to_bp("0.005"), 50)

    def test_paise(self):
        self.assertEqual(n.to_paise("1234.50"), 123_450)
        self.assertEqual(n.to_paise("-0.01"), -1)

    def test_non_integer_result_is_an_error_not_a_rounding(self):
        with self.assertRaises(n.NumericError):
            n.to_ppb("0.0000000001")
        with self.assertRaises(n.NumericError):
            n.to_paise("1.005")

    def test_rejects_floats_and_garbage(self):
        for bad in (0.1, "abc", "NaN", "Infinity", ""):
            with self.subTest(bad=bad), self.assertRaises(n.NumericError):
                n.to_ppb(bad)

    def test_exact_beyond_the_decimal_context_precision(self):
        # 29 significant digits: Decimal's 28-digit multiplication would round this to exactly 1e9 ppb.
        with self.assertRaises(n.NumericError):
            n.to_ppb("1.0000000000000000000000000001")
        self.assertEqual(n.to_ppb("1.000000000000000000000000000"), 1_000_000_000)
        self.assertEqual(n.to_paise("12345678901234567.89"), 1_234_567_890_123_456_789)
        self.assertEqual(n.to_paise("0e999999999"), 0)
        with self.assertRaises(OverflowError):
            n.to_paise("1e999999999")  # fails fast instead of building a billion-digit integer
        with self.assertRaises(n.NumericError):
            n.to_paise("1e-999999999")


class IntegerHelperTests(unittest.TestCase):
    def test_floor_and_ceil_follow_mathematics_for_negatives(self):
        self.assertEqual(n.floor_div(-7, 2), -4)
        self.assertEqual(n.ceil_div(-7, 2), -3)
        self.assertEqual(n.ceil_div(7, 2), 4)
        self.assertEqual(n.floor_to(-150, 100), -200)
        self.assertEqual(n.ceil_to(101, 100), 200)

    def test_round_half_up(self):
        self.assertEqual(n.round_half_up_div(5, 10), 1)
        self.assertEqual(n.round_half_up_div(4, 10), 0)
        self.assertEqual(n.round_half_up_div(15, 10), 2)

    def test_round_half_up_rejects_negative_and_odd_divisors(self):
        for a, d in ((-1, 10), (1, 3), (1, 0)):
            with self.subTest(a=a, d=d), self.assertRaises(n.NumericError):
                n.round_half_up_div(a, d)

    def test_mul_div_overflow_is_an_error(self):
        with self.assertRaises(OverflowError):
            n.mul_div_floor(n.INT64_MAX, 2, 1)
        self.assertEqual(n.mul_div_floor(n.INT64_MAX, 2, 2), n.INT64_MAX)

    def test_margin_rounds_up_to_whole_rupees(self):
        self.assertEqual(n.money_from_float_margin(12345.01), 12400)
        self.assertEqual(n.money_from_float_margin(12300.0), 12300)


class CanonicalOutputTests(unittest.TestCase):
    def test_float_format_round_trips(self):
        rng = random.Random(7)
        for _ in range(2000):
            x = rng.uniform(-1e20, 1e20) * rng.choice((1e-12, 1e-3, 1.0))
            self.assertEqual(float(n.format_float(x)), x)

    def test_float_layout(self):
        self.assertEqual(n.format_float(2.0), "2.0")
        self.assertEqual(n.format_float(1e16), "1e+16")
        self.assertEqual(n.format_float(0.0001), "0.0001")
        self.assertEqual(n.format_float(0.00001), "1e-05")

    def test_non_finite_floats_are_rejected(self):
        with self.assertRaises(n.NumericError):
            n.format_float(float("nan"))

    def test_canonical_json_sorts_keys_and_has_no_spaces(self):
        out = n.dumps_canonical({"b": 1, "a": [1.5, "x\n", None, True]})
        self.assertEqual(out, '{"a":[1.5,"x\\n",null,true],"b":1}')
        self.assertEqual(json.loads(out), {"a": [1.5, "x\n", None, True], "b": 1})

    def test_canonical_json_keeps_non_ascii_text(self):
        self.assertEqual(n.dumps_canonical("₹"), '"₹"')


if __name__ == "__main__":
    unittest.main()
