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
from tests.support import SandboxedDBTest

import india_utils
import tick_buffer
from geo_monitor.gemini_classifier import heuristic_suspect
from server.services import prompts
from technical_analyst import TechnicalAnalyst


# ── DEFECT 1 — tick_buffer.candles_since() can never return candles ──────────
#
# Severity: high. This is the only path that turns buffered ticks into the live
# intraday chart, and it is dead.
#
# The query correlates two scalar subqueries against the outer GROUP BY using
# `ts = MIN(t.ts)` / `ts = MAX(t.ts)`. SQLite rejects an aggregate used that way:
#
#     sqlite3.OperationalError: misuse of aggregate function MIN()
#
# `candles_since` wraps the execute in a bare `except Exception: return []`, so
# the failure is swallowed and the caller sees "no data" rather than an error.
#
# Reproduce:
#     >>> tick_buffer.write_ticks([...three ticks in one bucket...])
#     >>> tick_buffer.candles_since(1, 0, 60)
#     []                      # expected: one OHLC candle
#
# Fix sketch: compute open/close with window functions
# (`FIRST_VALUE(ltp) OVER (PARTITION BY bucket ORDER BY ts)`) or with a
# correlated join against min/max ts per bucket, and let the exception surface
# (or log it) instead of returning [].

class CandlesSinceDefectTests(SandboxedDBTest):
    db_modules = (tick_buffer,)

    def seed(self):
        # Three ticks inside one 60s bucket: 10 → 20 → 15.
        tick_buffer.write_ticks([
            {"instrument_token": 1, "exchange_timestamp": 1000,
             "last_price": 10.0, "volume_traded": 100},
            {"instrument_token": 1, "exchange_timestamp": 1010,
             "last_price": 20.0, "volume_traded": 200},
            {"instrument_token": 1, "exchange_timestamp": 1030,
             "last_price": 15.0, "volume_traded": 300},
        ])

    @unittest.expectedFailure
    def test_returns_a_candle_for_buffered_ticks(self):
        self.seed()
        self.assertEqual(len(tick_buffer.candles_since(1, 0, 60)), 1)

    @unittest.expectedFailure
    def test_candle_ohlc_matches_the_ticks(self):
        self.seed()
        candle = tick_buffer.candles_since(1, 0, 60)[0]
        self.assertEqual(candle["open"], 10.0)    # first tick in the bucket
        self.assertEqual(candle["high"], 20.0)
        self.assertEqual(candle["low"], 10.0)
        self.assertEqual(candle["close"], 15.0)   # last tick in the bucket

    @unittest.expectedFailure
    def test_ticks_are_bucketed_by_interval(self):
        tick_buffer.write_ticks([
            {"instrument_token": 1, "exchange_timestamp": 1000, "last_price": 10.0},
            {"instrument_token": 1, "exchange_timestamp": 1200, "last_price": 20.0},
        ])
        self.assertEqual(len(tick_buffer.candles_since(1, 0, 60)), 2)

    # This one passes today and is NOT expected-failure: the guard clause works.
    def test_zero_token_returns_empty_without_touching_sql(self):
        self.assertEqual(tick_buffer.candles_since(0, 0, 60), [])


# ── DEFECT 2 — format_inr_full loses the carry when rounding up ──────────────
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


# ── DEFECT 3 — _parse_response raises on schema-shaped but mistyped output ───
#
# Severity: medium. Every other failure mode in `_parse_response` degrades to a
# safe AVOID result; these three raise out of the function instead, so the
# stage dies with a traceback rather than a recoverable result.
#
# The coercions are unguarded:
#
#     confidence=int(data.get("confidence", 0)),
#     support_levels=[float(x) for x in data.get("support_levels", [])],
#
# This is not hypothetical: the prompt's own examples show rupee-formatted
# prices ("Buy on break above ₹2,850"), so a model putting "₹2,850" into
# support_levels is a natural failure, as is a qualitative "high" confidence.
#
# Reproduce:
#     >>> a._parse_response('{"support_levels": ["₹2,850"]}', "tf", False)
#     ValueError: could not convert string to float: '₹2,850'
#     >>> a._parse_response('{"confidence": "high"}', "tf", False)
#     ValueError: invalid literal for int() with base 10: 'high'
#     >>> a._parse_response('{"support_levels": [null]}', "tf", False)
#     TypeError: float() argument must be a string or a number, not 'NoneType'
#
# Fix sketch: coerce each value defensively (strip ₹ and separators, drop
# values that will not parse) and fall back to the AVOID result on failure,
# matching the behaviour of the other error paths.

class ParseResponseCoercionDefectTests(unittest.TestCase):
    def setUp(self):
        self.a = TechnicalAnalyst.__new__(TechnicalAnalyst)

    def parse(self, raw):
        return self.a._parse_response(raw, "3 months daily", verbose=False)

    @unittest.expectedFailure
    def test_rupee_formatted_support_level_does_not_raise(self):
        self.parse('{"recommendation": "SWING_TRADING", "support_levels": ["₹2,850"]}')

    @unittest.expectedFailure
    def test_qualitative_confidence_does_not_raise(self):
        self.parse('{"recommendation": "SWING_TRADING", "confidence": "high"}')

    @unittest.expectedFailure
    def test_null_level_does_not_raise(self):
        self.parse('{"recommendation": "SWING_TRADING", "support_levels": [null]}')

    @unittest.expectedFailure
    def test_mistyped_output_degrades_to_a_safe_result(self):
        # The documented contract for unparseable output.
        result = self.parse('{"confidence": "high"}')
        self.assertEqual(result.recommendation, "AVOID")


# ── DEFECT 4 — build_bottom_line_prompt renders the literal "None" ───────────
#
# Severity: low, prompt-quality. `company.get('name')` and friends have no
# default, so any caller that passes a summary not produced by
# build_summary_data() ships "Company: None (None, None, None)" to the model.
#
# The happy path is clean (build_summary_data defaults everything to "" / 0),
# so this only bites partial/legacy callers and the fallback path.
#
# Reproduce:
#     >>> prompts.build_bottom_line_prompt({"company": {}}, "REVIEW")
#     '...Company: None (None, None, None)\nSector:  None...'
#
# Fix sketch: give every .get() an "" default, or route all callers through
# build_summary_data().

class BottomLineNoneLeakDefectTests(unittest.TestCase):
    @unittest.expectedFailure
    def test_partial_summary_does_not_render_the_literal_none(self):
        self.assertNotIn("None", prompts.build_bottom_line_prompt({"company": {}}, "REVIEW"))

    @unittest.expectedFailure
    def test_empty_summary_does_not_render_the_literal_none(self):
        self.assertNotIn("None", prompts.build_bottom_line_prompt({}, "REVIEW"))


# ── DEFECT 5 — _compute_basic_stats divides by zero on degenerate series ─────
#
# Severity: low-medium. Two unguarded divisions:
#
#     volume_vs_avg_pct : volumes[-1] / (sum(volumes)/len(volumes))
#     pct_change        : price_change / closes[0]
#
# An all-zero-volume window (illiquid smallcap, halted session, or a Kite
# response where volume is absent and coerced to 0) raises ZeroDivisionError
# out of the stats step, killing the technical stage before it ever prompts.
#
# Fix sketch: guard both denominators and emit 0.0 / None.

class BasicStatsZeroDivisionDefectTests(unittest.TestCase):
    def setUp(self):
        self.a = TechnicalAnalyst.__new__(TechnicalAnalyst)

    def candles(self, *specs):
        from technical_analyst import CandleStick
        return [CandleStick(f"2025-08-{i + 1:02d}", o, h, l, c, v)
                for i, (o, h, l, c, v) in enumerate(specs)]

    @unittest.expectedFailure
    def test_all_zero_volume_series_does_not_raise(self):
        self.a._compute_basic_stats(self.candles(*[(10.0, 10.0, 10.0, 10.0, 0)] * 3))

    @unittest.expectedFailure
    def test_zero_opening_price_does_not_raise(self):
        self.a._compute_basic_stats(
            self.candles((0.0, 0.0, 0.0, 0.0, 1), (1.0, 1.0, 1.0, 1.0, 1)))


# ── DEFECT 6 — holding tickers match as substrings in the news prefilter ─────
#
# Severity: medium — it costs money on every poll cycle.
#
# `geo_monitor.gemini_classifier.heuristic_suspect` decides whether an article
# is worth a paid Gemini call. Its own cue patterns are all word-anchored
# (`\bwar\b`, `\bban\b`, ...), but holding terms are matched with a bare
# `re.escape()` and no boundaries:
#
#     if len(t) >= 2 and re.search(re.escape(t), text, re.IGNORECASE):
#
# So a short ticker fires inside unrelated words, and since the minimum length
# is 2, two-letter terms are permitted. Every false positive is an article
# pushed into a Gemini batch.
#
# Reproduce:
#     >>> heuristic_suspect("Growth in digital services", holding_terms=["IT"])
#     True                    # "IT" matched inside "digital"
#     >>> heuristic_suspect("Monsoon hits the region", holding_terms=["ON"])
#     True                    # "ON" matched inside "Monsoon"
#
# Fix sketch: wrap the escaped term in \b anchors, matching the cue patterns —
#     re.search(rf"\b{re.escape(t)}\b", text, re.IGNORECASE)
# NSE symbols containing & or - still work, since \b sits at the outer edges.

class HoldingTermBoundaryDefectTests(unittest.TestCase):
    @unittest.expectedFailure
    def test_short_ticker_does_not_match_inside_a_word(self):
        self.assertFalse(
            heuristic_suspect("Growth in digital services", holding_terms=["IT"]))

    @unittest.expectedFailure
    def test_ticker_does_not_match_across_a_word_interior(self):
        self.assertFalse(
            heuristic_suspect("Monsoon hits the region", holding_terms=["ON"]))

    @unittest.expectedFailure
    def test_ticker_still_matches_as_a_whole_word(self):
        # The fix must not break the real case.
        self.assertTrue(
            heuristic_suspect("RELIANCE posts results", holding_terms=["RELIANCE"]))
        self.assertFalse(
            heuristic_suspect("RELIANCEPP posts results", holding_terms=["RELIANCE"]))


if __name__ == "__main__":
    unittest.main()
