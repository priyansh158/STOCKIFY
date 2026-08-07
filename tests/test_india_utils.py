"""Unit tests for india_utils — currency formatting and ticker suffix handling.

These are pure functions with no I/O, so everything here is exact-value
assertions plus the degenerate inputs (None, NaN, inf, non-numeric, negative,
empty string) that a UI layer will eventually feed them.
"""
from __future__ import annotations

import unittest

import tests.support  # noqa: F401  — sandboxes cache dir; must precede project imports

from india_utils import (
    CRORE,
    LAKH,
    RUPEE,
    format_inr_compact,
    format_inr_full,
    format_price,
    strip_exchange_suffix,
    to_kite_symbol,
    to_yfinance_ticker,
)


class StripExchangeSuffixTests(unittest.TestCase):
    def test_strips_each_known_suffix(self):
        for raw in ("RELIANCE.NS", "RELIANCE.BO", "RELIANCE.BSE", "RELIANCE.NSE"):
            with self.subTest(raw=raw):
                self.assertEqual(strip_exchange_suffix(raw), "RELIANCE")

    def test_bare_symbol_is_uppercased_untouched(self):
        self.assertEqual(strip_exchange_suffix("reliance"), "RELIANCE")

    def test_case_insensitive_suffix(self):
        self.assertEqual(strip_exchange_suffix("reliance.ns"), "RELIANCE")
        self.assertEqual(strip_exchange_suffix("Reliance.Bo"), "RELIANCE")

    def test_only_trailing_suffix_is_stripped(self):
        # ".NS" in the middle is part of the name, not an exchange tag.
        self.assertEqual(strip_exchange_suffix("A.NS.B"), "A.NS.B")

    def test_unknown_suffix_preserved(self):
        self.assertEqual(strip_exchange_suffix("RELIANCE.L"), "RELIANCE.L")

    def test_symbols_with_punctuation_survive(self):
        # Real NSE symbols contain & and -.
        self.assertEqual(strip_exchange_suffix("M&M.NS"), "M&M")
        self.assertEqual(strip_exchange_suffix("BAJAJ-AUTO.NS"), "BAJAJ-AUTO")

    def test_empty_string(self):
        self.assertEqual(strip_exchange_suffix(""), "")


class ToYfinanceTickerTests(unittest.TestCase):
    def test_bare_symbol_defaults_to_nse(self):
        self.assertEqual(to_yfinance_ticker("RELIANCE"), "RELIANCE.NS")

    def test_bse_exchange_appends_bo(self):
        self.assertEqual(to_yfinance_ticker("RELIANCE", "BSE"), "RELIANCE.BO")

    def test_exchange_arg_is_case_insensitive(self):
        self.assertEqual(to_yfinance_ticker("RELIANCE", "bse"), "RELIANCE.BO")

    def test_unknown_exchange_falls_back_to_nse(self):
        self.assertEqual(to_yfinance_ticker("RELIANCE", "NASDAQ"), "RELIANCE.NS")

    def test_existing_yfinance_suffix_preserved(self):
        self.assertEqual(to_yfinance_ticker("RELIANCE.NS"), "RELIANCE.NS")
        self.assertEqual(to_yfinance_ticker("RELIANCE.BO"), "RELIANCE.BO")

    def test_long_form_suffixes_normalised_to_yfinance_form(self):
        # .BSE/.NSE are exchange names; yfinance wants .BO/.NS.
        self.assertEqual(to_yfinance_ticker("RELIANCE.BSE"), "RELIANCE.BO")
        self.assertEqual(to_yfinance_ticker("RELIANCE.NSE"), "RELIANCE.NS")

    def test_lowercase_input_with_suffix_is_uppercased(self):
        self.assertEqual(to_yfinance_ticker("reliance.bse"), "RELIANCE.BO")

    def test_surrounding_whitespace_stripped(self):
        self.assertEqual(to_yfinance_ticker("  RELIANCE  "), "RELIANCE.NS")

    def test_empty_symbol_returned_as_is(self):
        # Guard clause returns the falsy input untouched rather than ".NS".
        self.assertEqual(to_yfinance_ticker(""), "")

    def test_suffix_is_not_double_applied(self):
        once = to_yfinance_ticker("RELIANCE")
        self.assertEqual(to_yfinance_ticker(once), once)


class ToKiteSymbolTests(unittest.TestCase):
    def test_nse_default(self):
        self.assertEqual(to_kite_symbol("RELIANCE"), "NSE:RELIANCE")

    def test_bse(self):
        self.assertEqual(to_kite_symbol("RELIANCE", "BSE"), "BSE:RELIANCE")

    def test_strips_yfinance_suffix_before_prefixing(self):
        self.assertEqual(to_kite_symbol("RELIANCE.NS"), "NSE:RELIANCE")

    def test_unknown_exchange_coerced_to_nse(self):
        self.assertEqual(to_kite_symbol("RELIANCE", "NYSE"), "NSE:RELIANCE")

    def test_empty_symbol_returns_empty_string(self):
        self.assertEqual(to_kite_symbol(""), "")

    def test_exchange_arg_wins_over_embedded_suffix(self):
        # Documented behaviour: the `exchange` argument is authoritative; a
        # contradicting suffix on the symbol is discarded, not honoured.
        # (Callers must pass exchange explicitly — see search router.)
        self.assertEqual(to_kite_symbol("RELIANCE.BO"), "NSE:RELIANCE")
        self.assertEqual(to_kite_symbol("RELIANCE.NS", "BSE"), "BSE:RELIANCE")


class FormatInrCompactTests(unittest.TestCase):
    def test_docstring_examples(self):
        self.assertEqual(format_inr_compact(1_234_567_890), "₹123.46 Cr")
        self.assertEqual(format_inr_compact(98_765_432), "₹9.88 Cr")
        self.assertEqual(format_inr_compact(250_000), "₹2.50 L")
        self.assertEqual(format_inr_compact(4_500), "₹4,500")

    def test_none_and_nan_and_junk_render_em_dash(self):
        self.assertEqual(format_inr_compact(None), "—")
        self.assertEqual(format_inr_compact(float("nan")), "—")
        self.assertEqual(format_inr_compact("not a number"), "—")
        self.assertEqual(format_inr_compact([1, 2]), "—")

    def test_zero_gets_paise_precision(self):
        # Values below ₹1 take the n<1 branch, so zero renders as "₹0.00"
        # rather than "₹0" — matters for empty-portfolio UI cells.
        self.assertEqual(format_inr_compact(0), f"{RUPEE}0.00")

    def test_negative_sign_precedes_the_rupee_symbol(self):
        # Intentional: "-₹2.50 L", not "₹-2.50 L".
        self.assertEqual(format_inr_compact(-250_000), "-₹2.50 L")
        self.assertEqual(format_inr_compact(-1_234_567_890), "-₹123.46 Cr")

    def test_boundary_exactly_one_lakh_uses_lakh_unit(self):
        self.assertEqual(format_inr_compact(LAKH), "₹1.00 L")

    def test_boundary_just_below_one_lakh_uses_plain_grouping(self):
        self.assertEqual(format_inr_compact(LAKH - 1), "₹99,999")

    def test_boundary_exactly_one_crore_uses_crore_unit(self):
        self.assertEqual(format_inr_compact(CRORE), "₹1.00 Cr")

    def test_boundary_just_below_one_crore_uses_lakh_unit(self):
        self.assertEqual(format_inr_compact(CRORE - 1), "₹100.00 L")

    def test_decimals_argument_respected(self):
        self.assertEqual(format_inr_compact(1_234_567_890, decimals=0), "₹123 Cr")
        self.assertEqual(format_inr_compact(1_234_567_890, decimals=4), "₹123.4568 Cr")

    def test_sub_rupee_values_get_paise_precision(self):
        self.assertEqual(format_inr_compact(0.5), "₹0.50")

    def test_numeric_strings_are_accepted(self):
        self.assertEqual(format_inr_compact("250000"), "₹2.50 L")

    def test_infinity_does_not_raise(self):
        # Degenerate but must not blow up a UI render.
        self.assertIn("Cr", format_inr_compact(float("inf")))


class FormatInrFullTests(unittest.TestCase):
    """Indian digit grouping: last 3 digits, then pairs (1,23,45,678)."""

    def test_indian_grouping_pairs_after_first_three(self):
        self.assertEqual(format_inr_full(12_345_678), "1,23,45,678")
        self.assertEqual(format_inr_full(1_234_567), "12,34,567")
        self.assertEqual(format_inr_full(250_000), "2,50,000")

    def test_three_digits_or_fewer_are_ungrouped(self):
        for n in (0, 1, 99, 999):
            with self.subTest(n=n):
                self.assertEqual(format_inr_full(n), str(n))

    def test_four_digits_groups_once(self):
        self.assertEqual(format_inr_full(1_000), "1,000")

    def test_five_digits(self):
        self.assertEqual(format_inr_full(99_999), "99,999")

    def test_six_digits_starts_the_pair_grouping(self):
        self.assertEqual(format_inr_full(100_000), "1,00,000")

    def test_negative(self):
        self.assertEqual(format_inr_full(-1_234_567), "-12,34,567")

    def test_no_rupee_symbol_emitted(self):
        self.assertNotIn(RUPEE, format_inr_full(12_345_678))

    def test_decimals_appended(self):
        self.assertEqual(format_inr_full(1_234.5, decimals=2), "1,234.50")

    def test_none_and_nan(self):
        self.assertEqual(format_inr_full(None), "—")
        self.assertEqual(format_inr_full(float("nan")), "—")
        self.assertEqual(format_inr_full("junk"), "—")

    def test_fraction_truncated_when_decimals_zero(self):
        self.assertEqual(format_inr_full(1_234.99, decimals=0), "1,234")


class FormatPriceTests(unittest.TestCase):
    def test_two_decimals_by_default(self):
        self.assertEqual(format_price(1234.5), "₹1,234.50")

    def test_rounds_to_decimals(self):
        self.assertEqual(format_price(2848.456), "₹2,848.46")
        self.assertEqual(format_price(2848.456, decimals=1), "₹2,848.5")
        self.assertEqual(format_price(2848.456, decimals=0), "₹2,848")

    def test_none_and_nan_and_junk(self):
        self.assertEqual(format_price(None), "—")
        self.assertEqual(format_price(float("nan")), "—")
        self.assertEqual(format_price("junk"), "—")

    def test_negative(self):
        self.assertEqual(format_price(-12.3), "₹-12.30")

    def test_uses_western_grouping_not_indian(self):
        # format_price deliberately uses Python's , format spec (thousands
        # grouping), unlike format_inr_full's lakh/crore pairing. Locking this
        # in so a future "consistency" change is a conscious decision.
        self.assertEqual(format_price(1_234_567), "₹1,234,567.00")


if __name__ == "__main__":
    unittest.main()
