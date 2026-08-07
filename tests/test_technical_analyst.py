"""Unit tests for technical_analyst — candle geometry, statistics, and the
model-response parser.

The parser is the interesting surface: it consumes free-form LLM output, so it
is tested against the ways Claude actually deviates from the requested schema
(prose preamble, markdown fences, trailing commas, missing keys, wrong types).

`TechnicalAnalyst` is constructed via __new__ so no Anthropic client is built
and no API key is required.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime
from unittest.mock import patch

import tests.support  # noqa: F401  — must precede project imports

import technical_analyst
from technical_analyst import CandleStick, TechnicalAnalysisResult, TechnicalAnalyst


def analyst() -> TechnicalAnalyst:
    """An analyst without an Anthropic client — parsing/stats need no network."""
    return TechnicalAnalyst.__new__(TechnicalAnalyst)


def candles(*specs) -> list:
    """specs: (open, high, low, close, volume) tuples."""
    return [
        CandleStick(f"2025-08-{i + 1:02d}", o, h, l, c, v)
        for i, (o, h, l, c, v) in enumerate(specs)
    ]


# ── Candle geometry ──────────────────────────────────────────────────────────

class CandleStickTests(unittest.TestCase):
    def test_bullish_candle(self):
        c = CandleStick("2025-08-01", 100.0, 110.0, 95.0, 105.0, 1000)
        self.assertTrue(c.is_bullish())
        self.assertFalse(c.is_bearish())

    def test_bearish_candle(self):
        c = CandleStick("2025-08-01", 105.0, 110.0, 95.0, 100.0, 1000)
        self.assertTrue(c.is_bearish())
        self.assertFalse(c.is_bullish())

    def test_doji_is_neither_bullish_nor_bearish(self):
        c = CandleStick("2025-08-01", 100.0, 110.0, 95.0, 100.0, 1000)
        self.assertFalse(c.is_bullish())
        self.assertFalse(c.is_bearish())

    def test_body_size_is_absolute(self):
        up = CandleStick("d", 100.0, 110.0, 95.0, 105.0, 1)
        down = CandleStick("d", 105.0, 110.0, 95.0, 100.0, 1)
        self.assertEqual(up.body_size(), 5.0)
        self.assertEqual(down.body_size(), 5.0)

    def test_doji_body_is_zero(self):
        self.assertEqual(CandleStick("d", 100.0, 110.0, 95.0, 100.0, 1).body_size(), 0.0)

    def test_wicks_on_a_bullish_candle(self):
        c = CandleStick("d", 100.0, 110.0, 95.0, 105.0, 1)
        self.assertEqual(c.upper_wick(), 5.0)   # 110 - max(100,105)
        self.assertEqual(c.lower_wick(), 5.0)   # min(100,105) - 95

    def test_wicks_on_a_bearish_candle(self):
        c = CandleStick("d", 105.0, 110.0, 95.0, 100.0, 1)
        self.assertEqual(c.upper_wick(), 5.0)   # 110 - max(105,100)
        self.assertEqual(c.lower_wick(), 5.0)   # min(105,100) - 95

    def test_marubozu_has_no_wicks(self):
        c = CandleStick("d", 100.0, 105.0, 100.0, 105.0, 1)
        self.assertEqual(c.upper_wick(), 0.0)
        self.assertEqual(c.lower_wick(), 0.0)

    def test_wicks_are_never_negative_for_wellformed_candles(self):
        for c in candles((100.0, 110.0, 95.0, 105.0, 1),
                         (105.0, 110.0, 95.0, 100.0, 1),
                         (100.0, 100.0, 100.0, 100.0, 1)):
            with self.subTest(candle=c.date):
                self.assertGreaterEqual(c.upper_wick(), 0.0)
                self.assertGreaterEqual(c.lower_wick(), 0.0)

    def test_to_str_marks_direction_and_formats_volume(self):
        up = CandleStick("2025-08-01", 100.0, 110.0, 95.0, 105.0, 1_234_567)
        s = up.to_str()
        self.assertIn("▲", s)
        self.assertIn("1,234,567", s)
        self.assertIn("2025-08-01", s)

    def test_to_str_marks_bearish(self):
        self.assertIn("▼", CandleStick("d", 105.0, 110.0, 95.0, 100.0, 1).to_str())

    def test_to_str_renders_doji_as_bearish_arrow(self):
        # is_bullish() is strict (>) so an unchanged close falls to the ▼ arm.
        self.assertIn("▼", CandleStick("d", 100.0, 100.0, 100.0, 100.0, 1).to_str())


# ── Statistics ───────────────────────────────────────────────────────────────

class BasicStatsTests(unittest.TestCase):
    def setUp(self):
        self.a = analyst()

    def test_empty_candles_returns_empty_dict(self):
        self.assertEqual(self.a._compute_basic_stats([]), {})

    def test_known_series_produces_exact_values(self):
        cs = candles(
            (10.0, 12.0, 9.0, 10.0, 100),
            (10.0, 22.0, 10.0, 20.0, 200),
            (20.0, 21.0, 14.0, 15.0, 300),
        )
        s = self.a._compute_basic_stats(cs)
        self.assertEqual(s["current_price"], 15.0)
        self.assertEqual(s["period_start_price"], 10.0)
        self.assertEqual(s["price_change"], 5.0)
        self.assertEqual(s["pct_change"], 50.0)
        self.assertEqual(s["period_high"], 22.0)
        self.assertEqual(s["period_low"], 9.0)
        self.assertEqual(s["avg_volume"], 200)
        self.assertEqual(s["recent_volume"], 300)
        self.assertEqual(s["volume_vs_avg_pct"], 50.0)
        self.assertEqual(s["candle_count"], 3)

    def test_volatility_is_stddev_of_returns_as_a_percentage(self):
        # returns = [+1.0, -0.25]; mean 0.375; population stddev 0.625 → 62.5%
        cs = candles(
            (10.0, 10.0, 10.0, 10.0, 1),
            (10.0, 20.0, 10.0, 20.0, 1),
            (20.0, 20.0, 15.0, 15.0, 1),
        )
        self.assertAlmostEqual(self.a._compute_basic_stats(cs)["daily_volatility_pct"], 62.5)

    def test_flat_series_has_zero_volatility_and_zero_change(self):
        cs = candles(*[(10.0, 10.0, 10.0, 10.0, 5)] * 4)
        s = self.a._compute_basic_stats(cs)
        self.assertEqual(s["daily_volatility_pct"], 0.0)
        self.assertEqual(s["pct_change"], 0.0)
        self.assertEqual(s["volume_vs_avg_pct"], 0.0)

    def test_single_candle_has_no_returns_and_no_volatility(self):
        s = self.a._compute_basic_stats(candles((10.0, 11.0, 9.0, 10.5, 100)))
        self.assertEqual(s["candle_count"], 1)
        self.assertEqual(s["daily_volatility_pct"], 0.0)
        self.assertEqual(s["pct_change"], round((10.5 - 10.5) / 10.5 * 100, 2))

    def test_negative_period_performance(self):
        cs = candles((100.0, 100.0, 50.0, 100.0, 1), (100.0, 100.0, 50.0, 50.0, 1))
        s = self.a._compute_basic_stats(cs)
        self.assertEqual(s["price_change"], -50.0)
        self.assertEqual(s["pct_change"], -50.0)

    def test_smas_are_none_until_enough_candles(self):
        s = self.a._compute_basic_stats(candles(*[(10.0, 10.0, 10.0, 10.0, 1)] * 9))
        self.assertIsNone(s["sma_10"])
        self.assertIsNone(s["sma_20"])
        self.assertIsNone(s["sma_50"])

    def test_sma_10_appears_at_exactly_ten_candles(self):
        s = self.a._compute_basic_stats(candles(*[(10.0, 10.0, 10.0, 10.0, 1)] * 10))
        self.assertEqual(s["sma_10"], 10.0)
        self.assertIsNone(s["sma_20"])

    def test_sma_uses_only_the_trailing_window(self):
        # 19 candles at 100 then 10 at 200 → SMA(10) is 200, SMA(20) is not.
        cs = candles(*([(100.0, 100.0, 100.0, 100.0, 1)] * 19
                       + [(200.0, 200.0, 200.0, 200.0, 1)] * 10))
        s = self.a._compute_basic_stats(cs)
        self.assertEqual(s["sma_10"], 200.0)
        self.assertNotEqual(s["sma_20"], 200.0)

    def test_zero_volume_series_divides_by_zero(self):
        # volume_vs_avg_pct has no guard for an all-zero-volume window (a real
        # possibility for illiquid smallcaps or a halted session).
        cs = candles(*[(10.0, 10.0, 10.0, 10.0, 0)] * 3)
        with self.assertRaises(ZeroDivisionError):
            self.a._compute_basic_stats(cs)

    def test_zero_opening_close_divides_by_zero(self):
        cs = candles((0.0, 0.0, 0.0, 0.0, 1), (1.0, 1.0, 1.0, 1.0, 1))
        with self.assertRaises(ZeroDivisionError):
            self.a._compute_basic_stats(cs)


# ── Kite row conversion ──────────────────────────────────────────────────────

class KiteRowConversionTests(unittest.TestCase):
    def convert(self, rows, interval="1d"):
        return TechnicalAnalyst._kite_rows_to_candles(rows, interval)

    def test_empty_rows(self):
        self.assertEqual(self.convert([]), [])

    def test_daily_rows_use_a_date_only_label(self):
        out = self.convert([{
            "date": datetime(2025, 8, 6, 15, 30),
            "open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 1000,
        }])
        self.assertEqual(out[0].date, "2025-08-06")

    def test_intraday_rows_include_the_time(self):
        out = self.convert([{
            "date": datetime(2025, 8, 6, 15, 30),
            "open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 1000,
        }], interval="15m")
        self.assertEqual(out[0].date, "2025-08-06 15:30")

    def test_string_dates_are_truncated_not_fatal(self):
        out = self.convert([{
            "date": "2025-08-06T15:30:00+0530",
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1,
        }])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].date, "2025-08-06T15:30")

    def test_rows_without_a_date_are_skipped(self):
        self.assertEqual(self.convert([{"open": 1.0, "close": 1.0}]), [])

    def test_prices_are_rounded_to_two_decimals(self):
        out = self.convert([{
            "date": datetime(2025, 8, 6), "open": 100.456, "high": 110.999,
            "low": 95.001, "close": 105.555, "volume": 1,
        }])
        self.assertEqual(out[0].open, 100.46)
        self.assertEqual(out[0].high, 111.0)
        self.assertEqual(out[0].low, 95.0)
        self.assertEqual(out[0].close, 105.56)

    def test_missing_ohlc_defaults_to_zero(self):
        out = self.convert([{"date": datetime(2025, 8, 6)}])
        self.assertEqual((out[0].open, out[0].high, out[0].low, out[0].close),
                         (0.0, 0.0, 0.0, 0.0))

    def test_none_volume_becomes_zero(self):
        out = self.convert([{"date": datetime(2025, 8, 6), "close": 1.0, "volume": None}])
        self.assertEqual(out[0].volume, 0)

    def test_float_volume_is_truncated_to_int(self):
        out = self.convert([{"date": datetime(2025, 8, 6), "close": 1.0, "volume": 1234.9}])
        self.assertEqual(out[0].volume, 1234)

    def test_a_malformed_row_does_not_drop_its_neighbours(self):
        rows = [
            {"date": datetime(2025, 8, 6), "open": 1.0, "high": 1.0, "low": 1.0,
             "close": 1.0, "volume": 1},
            {"date": datetime(2025, 8, 7), "open": "abc", "close": 1.0},   # skipped
            {"date": datetime(2025, 8, 8), "open": 2.0, "high": 2.0, "low": 2.0,
             "close": 2.0, "volume": 2},
        ]
        out = self.convert(rows)
        self.assertEqual([c.date for c in out], ["2025-08-06", "2025-08-08"])

    def test_order_is_preserved(self):
        rows = [{"date": datetime(2025, 8, d), "close": float(d), "volume": 1}
                for d in (1, 2, 3)]
        self.assertEqual([c.close for c in self.convert(rows)], [1.0, 2.0, 3.0])


# ── Interval / period mapping tables ─────────────────────────────────────────

class IntervalMappingTests(unittest.TestCase):
    def test_daily_and_intraday_intervals_map_to_kite_names(self):
        self.assertEqual(technical_analyst._INTERVAL_TO_KITE["1d"], "day")
        self.assertEqual(technical_analyst._INTERVAL_TO_KITE["1wk"], "week")
        self.assertEqual(technical_analyst._INTERVAL_TO_KITE["1h"], "60minute")
        self.assertEqual(technical_analyst._INTERVAL_TO_KITE["15m"], "15minute")

    def test_every_mapped_interval_is_a_documented_kite_interval(self):
        valid = {"minute", "3minute", "5minute", "10minute", "15minute",
                 "30minute", "60minute", "day", "week", "month"}
        self.assertTrue(set(technical_analyst._INTERVAL_TO_KITE.values()) <= valid)

    def test_period_days_are_monotonic(self):
        order = ["5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "max"]
        days = [technical_analyst._PERIOD_TO_DAYS[p] for p in order]
        self.assertEqual(days, sorted(days))

    def test_period_days_have_headroom_for_holidays(self):
        # NSE trades ~250 days/year; the windows over-request on purpose.
        self.assertGreater(technical_analyst._PERIOD_TO_DAYS["1y"], 365)
        self.assertGreater(technical_analyst._PERIOD_TO_DAYS["3mo"], 90)


# ── Response parsing ─────────────────────────────────────────────────────────

VALID_PAYLOAD = {
    "recommendation": "SWING_TRADING",
    "confidence": 72,
    "trend": "UPTREND",
    "patterns_found": ["Bullish Engulfing on 2025-08-01"],
    "support_levels": [2800.0, 2750.5],
    "resistance_levels": [2950.0],
    "entry_point": "Buy above ₹2,850",
    "target_price": "₹3,050",
    "stop_loss": "₹2,720",
    "risk_reward_ratio": "1:2.5",
    "volume_analysis": "Volume confirms",
    "momentum": "Building",
    "summary": "Constructive setup.",
    "trading_notes": ["Watch the RBI policy date"],
}


class ParseResponseTests(unittest.TestCase):
    def setUp(self):
        self.a = analyst()

    def parse(self, raw: str) -> TechnicalAnalysisResult:
        return self.a._parse_response(raw, "3 months daily", verbose=False)

    def test_clean_json(self):
        r = self.parse(json.dumps(VALID_PAYLOAD))
        self.assertEqual(r.recommendation, "SWING_TRADING")
        self.assertEqual(r.confidence, 72)
        self.assertEqual(r.trend, "UPTREND")
        self.assertEqual(r.support_levels, [2800.0, 2750.5])
        self.assertEqual(r.timeframe_analyzed, "3 months daily")

    def test_json_wrapped_in_prose(self):
        raw = "Here is my analysis.\n" + json.dumps(VALID_PAYLOAD) + "\nHope that helps!"
        self.assertEqual(self.parse(raw).recommendation, "SWING_TRADING")

    def test_json_inside_a_markdown_fence(self):
        raw = "```json\n" + json.dumps(VALID_PAYLOAD) + "\n```"
        self.assertEqual(self.parse(raw).recommendation, "SWING_TRADING")

    def test_leading_and_trailing_whitespace(self):
        self.assertEqual(self.parse("\n\n  " + json.dumps(VALID_PAYLOAD) + "  \n").confidence, 72)

    def test_nested_objects_do_not_truncate_the_span(self):
        payload = dict(VALID_PAYLOAD, deep={"a": {"b": [1, 2, 3]}})
        self.assertEqual(self.parse(json.dumps(payload)).recommendation, "SWING_TRADING")

    def test_empty_response_is_a_safe_avoid(self):
        r = self.parse("")
        self.assertEqual(r.recommendation, "AVOID")
        self.assertEqual(r.confidence, 0)

    def test_prose_without_json_is_a_safe_avoid(self):
        r = self.parse("I cannot analyse this stock.")
        self.assertEqual(r.recommendation, "AVOID")
        self.assertEqual(r.summary, "Failed to parse analysis response.")
        self.assertEqual(r.raw_response, "I cannot analyse this stock.")

    def test_malformed_json_is_a_safe_avoid(self):
        r = self.parse('{"recommendation": "BUY", oops}')
        self.assertEqual(r.recommendation, "AVOID")
        self.assertEqual(r.summary, "Failed to parse JSON response.")

    def test_failure_paths_preserve_the_raw_text_for_debugging(self):
        raw = '{"recommendation": "BUY", oops}'
        self.assertEqual(self.parse(raw).raw_response, raw)

    def test_timeframe_is_preserved_on_failure(self):
        self.assertEqual(self.parse("garbage").timeframe_analyzed, "3 months daily")

    def test_missing_keys_fall_back_to_defaults(self):
        r = self.parse('{"confidence": 50}')
        self.assertEqual(r.recommendation, "AVOID")
        self.assertEqual(r.trend, "")
        self.assertEqual(r.patterns_found, [])
        self.assertEqual(r.support_levels, [])

    def test_empty_json_object(self):
        r = self.parse("{}")
        self.assertEqual(r.recommendation, "AVOID")
        self.assertEqual(r.confidence, 0)

    def test_numeric_string_confidence_is_coerced(self):
        self.assertEqual(self.parse('{"confidence": "85"}').confidence, 85)

    def test_float_confidence_is_truncated(self):
        self.assertEqual(self.parse('{"confidence": 85.9}').confidence, 85)

    def test_integer_support_levels_become_floats(self):
        r = self.parse('{"support_levels": [2800, 2750]}')
        self.assertEqual(r.support_levels, [2800.0, 2750.0])
        self.assertTrue(all(isinstance(x, float) for x in r.support_levels))

    def test_numeric_string_levels_are_coerced(self):
        self.assertEqual(self.parse('{"support_levels": ["2800.5"]}').support_levels, [2800.5])

    def test_result_serialises_to_the_pipeline_dict(self):
        d = self.parse(json.dumps(VALID_PAYLOAD)).to_dict()
        self.assertEqual(d["stage"], "Technical Analysis")
        self.assertEqual(d["recommendation"], "SWING_TRADING")
        self.assertNotIn("raw_response", d)   # raw text is not shipped to the UI

    def test_to_dict_keys_are_stable(self):
        self.assertEqual(
            set(self.parse(json.dumps(VALID_PAYLOAD)).to_dict()),
            {"stage", "recommendation", "confidence", "timeframe_analyzed",
             "patterns_found", "trend", "support_levels", "resistance_levels",
             "entry_point", "target_price", "stop_loss", "risk_reward_ratio",
             "volume_analysis", "momentum", "summary", "trading_notes"},
        )


# ── Prompt construction ──────────────────────────────────────────────────────

class BuildPromptTests(unittest.TestCase):
    def setUp(self):
        self.a = analyst()
        self.cs = candles(*[(100.0 + i, 110.0 + i, 95.0 + i, 105.0 + i, 1000 + i)
                            for i in range(80)])
        self.stats = self.a._compute_basic_stats(self.cs)

    def prompt(self):
        return self.a._build_prompt("RELIANCE", "Reliance Industries Ltd",
                                    self.cs, self.stats, "3 months daily")

    def test_includes_identity_and_timeframe(self):
        p = self.prompt()
        self.assertIn("RELIANCE", p)
        self.assertIn("Reliance Industries Ltd", p)
        self.assertIn("3 months daily", p)

    def test_prices_are_denominated_in_rupees(self):
        self.assertIn("₹", self.prompt())
        self.assertNotIn("$", self.prompt())

    def test_states_the_indian_listing_context(self):
        self.assertIn("NSE", self.prompt())

    def test_candle_table_is_capped_at_sixty_rows(self):
        p = self.prompt()
        table_rows = [ln for ln in p.splitlines() if "O:" in ln and "Vol:" in ln]
        # 60 in the main table + the 5 most-recent repeat.
        self.assertEqual(len(table_rows), 65)

    def test_shows_the_most_recent_candles_not_the_oldest(self):
        p = self.prompt()
        self.assertIn(self.cs[-1].to_str(), p)
        self.assertNotIn(self.cs[0].to_str(), p)

    def test_short_series_is_not_padded(self):
        short = candles(*[(100.0, 110.0, 95.0, 105.0, 1000)] * 3)
        p = self.a._build_prompt("X", "X Ltd", short,
                                 self.a._compute_basic_stats(short), "1 month daily")
        rows = [ln for ln in p.splitlines() if "O:" in ln and "Vol:" in ln]
        self.assertEqual(len(rows), 6)   # 3 in the table + 3 recent

    def test_requests_the_documented_recommendation_enum(self):
        p = self.prompt()
        for token in ("DAY_TRADING", "SWING_TRADING", "LONG_TERM", "AVOID"):
            self.assertIn(token, p)

    def test_mentions_the_nse_session_window(self):
        self.assertIn("9:15", self.prompt())

    def test_empty_stats_break_prompt_construction(self):
        # `{stats.get('avg_volume','N/A'):,}` cannot format the 'N/A' fallback,
        # so an empty-candle analysis raises here rather than prompting Claude
        # with a meaningless table. Callers must guard on empty candles.
        with self.assertRaises(ValueError):
            self.a._build_prompt("X", "X Ltd", [], {}, "n/a")


# ── Data fetching fallbacks ──────────────────────────────────────────────────

class FetchCandlesTests(unittest.TestCase):
    def setUp(self):
        self.a = analyst()

    def test_raises_when_neither_backend_is_available(self):
        with patch.object(technical_analyst, "KITE_AVAILABLE", False), \
             patch.object(technical_analyst, "YFINANCE_AVAILABLE", False):
            with self.assertRaises(ImportError):
                self.a.fetch_candles("RELIANCE")

    def test_kite_path_is_used_when_authenticated(self):
        expected = candles((100.0, 110.0, 95.0, 105.0, 1000))
        with patch.object(technical_analyst, "KITE_AVAILABLE", True), \
             patch.object(technical_analyst.kite_client, "is_authenticated", return_value=True), \
             patch.object(TechnicalAnalyst, "_fetch_candles_kite", return_value=expected):
            got, timeframe = self.a.fetch_candles("RELIANCE", "3mo", "1d")
        self.assertEqual(got, expected)
        self.assertEqual(timeframe, "3 months daily")

    def test_falls_back_to_yfinance_when_kite_returns_nothing(self):
        with patch.object(technical_analyst, "KITE_AVAILABLE", True), \
             patch.object(technical_analyst.kite_client, "is_authenticated", return_value=True), \
             patch.object(TechnicalAnalyst, "_fetch_candles_kite", return_value=[]), \
             patch.object(technical_analyst, "YFINANCE_AVAILABLE", False):
            with self.assertRaises(ImportError):
                self.a.fetch_candles("RELIANCE")

    def test_kite_exception_falls_through_rather_than_propagating(self):
        with patch.object(technical_analyst, "KITE_AVAILABLE", True), \
             patch.object(technical_analyst.kite_client, "is_authenticated", return_value=True), \
             patch.object(TechnicalAnalyst, "_fetch_candles_kite",
                          side_effect=RuntimeError("kite 500")), \
             patch.object(technical_analyst, "YFINANCE_AVAILABLE", False):
            with self.assertRaises(ImportError):   # the yfinance-missing error, not kite's
                self.a.fetch_candles("RELIANCE")

    def test_timeframe_labels(self):
        expected = candles((1.0, 1.0, 1.0, 1.0, 1))
        cases = [
            ("3mo", "1d", "3 months daily"),
            ("1y", "1wk", "1 year weekly"),
            ("1mo", "15m", "1 month 15-minute"),
            ("5y", "1d", "5y daily"),        # unmapped period passes through
        ]
        for period, interval, label in cases:
            with self.subTest(period=period, interval=interval):
                with patch.object(technical_analyst, "KITE_AVAILABLE", True), \
                     patch.object(technical_analyst.kite_client, "is_authenticated",
                                  return_value=True), \
                     patch.object(TechnicalAnalyst, "_fetch_candles_kite", return_value=expected):
                    self.assertEqual(self.a.fetch_candles("X", period, interval)[1], label)

    def test_kite_path_returns_empty_for_an_unmapped_interval(self):
        with patch.object(technical_analyst.kite_client, "get_instrument_token",
                          side_effect=AssertionError("must not resolve a token")):
            self.assertEqual(self.a._fetch_candles_kite("RELIANCE", "3mo", "7m"), [])

    def test_kite_path_returns_empty_when_the_symbol_cannot_be_resolved(self):
        with patch.object(technical_analyst.kite_client, "get_instrument_token",
                          return_value=None):
            self.assertEqual(self.a._fetch_candles_kite("NOSUCH", "3mo", "1d"), [])

    def test_kite_path_tries_bse_when_nse_misses(self):
        seen = []

        def resolve(symbol, exchange="NSE"):
            seen.append(exchange)
            return 738561 if exchange == "BSE" else None

        with patch.object(technical_analyst.kite_client, "get_instrument_token", resolve), \
             patch.object(technical_analyst.kite_client, "historical", return_value=[]):
            self.a._fetch_candles_kite("RELIANCE", "3mo", "1d")
        self.assertEqual(seen, ["NSE", "BSE"])

    def test_kite_path_strips_a_yfinance_suffix_before_resolving(self):
        with patch.object(technical_analyst.kite_client, "get_instrument_token",
                          return_value=738561) as m, \
             patch.object(technical_analyst.kite_client, "historical", return_value=[]):
            self.a._fetch_candles_kite("RELIANCE.NS", "3mo", "1d")
        self.assertEqual(m.call_args[0][0], "RELIANCE")


class AnalyzeFailureTests(unittest.TestCase):
    def test_streaming_failure_degrades_to_avoid(self):
        a = analyst()

        class _Boom:
            class messages:
                @staticmethod
                def stream(**kw):
                    raise RuntimeError("connection reset")

        a.client = _Boom()
        result = a.analyze("RELIANCE", "Reliance", candles((1.0, 1.0, 1.0, 1.0, 1)),
                           "3 months daily", verbose=False)
        self.assertEqual(result.recommendation, "AVOID")
        self.assertEqual(result.confidence, 0)
        self.assertIn("connection reset", result.summary)


if __name__ == "__main__":
    unittest.main()
