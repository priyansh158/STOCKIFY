"""Unit tests for server.services.prompts — the pure prompt builders.

Prompts are the product here: a silently dropped section (position context,
macro snapshot, the JSON schema block) degrades every downstream analysis
without raising anything. These tests assert the structural invariants each
builder must preserve.

Pure string functions — no imports beyond the module itself.
"""
from __future__ import annotations

import re
import unittest

import tests.support  # noqa: F401  — must precede project imports

from server.services import prompts

_OWNER_WORDS = re.compile(
    r"\b(you|your|yours|i|me|my|mine|we|us|our|ours|user)\b", re.IGNORECASE
)

# The shape build_summary_data() emits — every key present, "" / 0 defaults.
FULL_SUMMARY = {
    "company": {"name": "Reliance Industries Ltd", "ticker": "RELIANCE",
                "exchange": "NSE", "country": "IN", "sector": "Energy"},
    "financial": {"score": 78, "passed": True, "revenue_status": "GROWING",
                  "margin_trend": "STABLE", "cash_flow_status": "POSITIVE",
                  "green_flags": ["Debt reduced"], "red_flags": []},
    "geo": {"risk_score": 30, "risk_level": "LOW", "passed": True,
            "collateral_risks": [], "safe_signals": ["Domestic demand"]},
    "technical": {"recommendation": "LONG_TERM", "confidence": 71,
                  "trend": "UPTREND", "current_price": "₹2,900",
                  "entry_point": "₹2,850", "target_price": "₹3,050",
                  "stop_loss": "₹2,720", "risk_reward_ratio": "1:2.5"},
}


class ProfilePromptTests(unittest.TestCase):
    def test_includes_identity_fields(self):
        p = prompts.build_profile_prompt("RELIANCE", {
            "name": "Reliance Industries Ltd", "sector": "Energy",
            "industry": "Refineries", "country": "India",
            "description": "Oil to chemicals conglomerate.",
        })
        for token in ("RELIANCE", "Reliance Industries Ltd", "Energy",
                      "Refineries", "India", "Oil to chemicals"):
            self.assertIn(token, p)

    def test_missing_metadata_falls_back_to_unknown(self):
        p = prompts.build_profile_prompt("RELIANCE", {})
        self.assertIn("Unknown", p)
        self.assertIn("RELIANCE", p)   # ticker stands in for the name

    def test_description_is_truncated(self):
        p = prompts.build_profile_prompt("X", {"description": "A" * 5000})
        self.assertNotIn("A" * 601, p)
        self.assertIn("A" * 600, p)

    def test_none_description_does_not_render_the_literal_none(self):
        p = prompts.build_profile_prompt("X", {"description": None})
        self.assertIn("Business description: \n", p)

    def test_requests_json_only(self):
        p = prompts.build_profile_prompt("X", {})
        self.assertIn("Return ONLY valid JSON", p)

    def test_schema_keys_the_pipeline_reads_are_requested(self):
        p = prompts.build_profile_prompt("X", {})
        for key in ("company_name", "hq_country", "sector", "business_model",
                    "key_markets", "supply_chain", "key_products",
                    "risk_keywords", "news_search_queries",
                    "geopolitical_exposures", "what_moves_the_stock"):
            with self.subTest(key=key):
                self.assertIn(f'"{key}"', p)

    def test_braces_are_escaped_so_the_schema_survives_the_fstring(self):
        p = prompts.build_profile_prompt("X", {})
        self.assertIn("{", p)
        self.assertIn("}", p)


class FinancialsPromptTests(unittest.TestCase):
    YF = {
        "income_statement": {
            "2025-03-31": {"Total Revenue": 1_000_000.0, "Net Income": 100_000.0},
        },
    }

    def test_includes_company_and_scraped_text(self):
        p = prompts.build_financials_prompt("Reliance Industries Ltd",
                                            "Revenue grew 12% YoY", self.YF)
        self.assertIn("Reliance Industries Ltd", p)
        self.assertIn("Revenue grew 12% YoY", p)

    def test_renders_yfinance_statements(self):
        p = prompts.build_financials_prompt("X", "", self.YF)
        self.assertIn("Annual Income Statement", p)
        self.assertIn("2025-03-31", p)
        self.assertIn("Total Revenue", p)

    def test_absent_statements_say_not_available(self):
        p = prompts.build_financials_prompt("X", "", {})
        self.assertIn("Not available.", p)

    def test_numeric_values_are_thousands_grouped(self):
        p = prompts.build_financials_prompt("X", "", self.YF)
        self.assertIn("1,000,000", p)

    def test_non_numeric_values_pass_through_unformatted(self):
        p = prompts.build_financials_prompt(
            "X", "", {"income_statement": {"2025": {"Note": "restated"}}}
        )
        self.assertIn("Note: restated", p)

    def test_none_metrics_are_omitted(self):
        p = prompts.build_financials_prompt(
            "X", "", {"income_statement": {"2025": {"Missing": None, "Present": 5}}}
        )
        self.assertNotIn("Missing", p)
        self.assertIn("Present", p)

    def test_position_block_is_injected_verbatim(self):
        block = "## OBSERVED POSITION FOR CONTEXT\n- Quantity: 100 shares\n"
        p = prompts.build_financials_prompt("X", "", {}, position_block=block)
        self.assertIn(block, p)

    def test_position_block_is_optional(self):
        p = prompts.build_financials_prompt("X", "", {})
        self.assertNotIn("OBSERVED POSITION", p)

    def test_prompt_stays_owner_neutral_when_a_position_is_attached(self):
        # The anonymisation guarantee has to survive prompt assembly, not just
        # blurb generation.
        block = ("## OBSERVED POSITION FOR CONTEXT\n"
                 "(Anonymized context for analysis — do NOT assume the analysis "
                 "recipient owns this.)\n- Quantity: 100 shares\n")
        p = prompts.build_financials_prompt("X", "", {}, position_block=block)
        self.assertEqual(_OWNER_WORDS.findall(block), [])
        # The static prompt body may address the model directly ("You are..."),
        # but the injected position block must not attribute ownership.
        self.assertNotIn("you hold", p.lower())
        self.assertNotIn("your position", p.lower())


class MarketContextBlockTests(unittest.TestCase):
    def test_none_and_empty_render_nothing(self):
        self.assertEqual(prompts.format_market_context_block(None), "")
        self.assertEqual(prompts.format_market_context_block({}), "")

    def test_context_without_instruments_renders_nothing(self):
        self.assertEqual(
            prompts.format_market_context_block({"indices": {}, "currency": {}}), ""
        )

    def test_renders_indices_with_friendly_labels(self):
        block = prompts.format_market_context_block(
            {"indices": {"NIFTY_50": {"ltp": 24500.25}}}
        )
        self.assertIn("NIFTY 50", block)
        self.assertNotIn("NIFTY_50", block)
        self.assertIn("24,500.25", block)

    def test_renders_currency(self):
        block = prompts.format_market_context_block(
            {"currency": {"USDINR": {"ltp": 83.5}}}
        )
        self.assertIn("USD/USD" if False else "USD/INR", block)
        self.assertIn("83.50", block)

    def test_unknown_labels_pass_through_unmapped(self):
        block = prompts.format_market_context_block(
            {"indices": {"NIFTY_CUSTOM": {"ltp": 100.0}}}
        )
        self.assertIn("NIFTY_CUSTOM", block)

    def test_positive_change_uses_an_up_arrow(self):
        block = prompts.format_market_context_block(
            {"indices": {"NIFTY_50": {"ltp": 24500.0, "change_pct": 1.25}}}
        )
        self.assertIn("▲", block)
        self.assertIn("1.25%", block)

    def test_negative_change_uses_a_down_arrow_and_absolute_value(self):
        block = prompts.format_market_context_block(
            {"indices": {"NIFTY_50": {"ltp": 24500.0, "change_pct": -1.25}}}
        )
        self.assertIn("▼", block)
        self.assertIn("1.25%", block)
        self.assertNotIn("-1.25", block)

    def test_flat_change_uses_a_neutral_marker(self):
        block = prompts.format_market_context_block(
            {"indices": {"NIFTY_50": {"ltp": 24500.0, "change_pct": 0.0}}}
        )
        self.assertIn("·", block)

    def test_absent_change_pct_omits_the_parenthetical(self):
        block = prompts.format_market_context_block(
            {"indices": {"NIFTY_50": {"ltp": 24500.0}}}
        )
        row = [ln for ln in block.splitlines() if "NIFTY 50" in ln][0]
        self.assertNotIn("(", row)
        self.assertNotIn("%", row)

    def test_missing_ltp_defaults_to_zero(self):
        block = prompts.format_market_context_block({"indices": {"NIFTY_50": {}}})
        self.assertIn("0.00", block)

    def test_market_state_is_surfaced_when_present(self):
        block = prompts.format_market_context_block(
            {"indices": {"NIFTY_50": {"ltp": 1.0}}, "market_state": "OPEN"}
        )
        self.assertIn("Market state: OPEN", block)

    def test_market_state_omitted_when_absent(self):
        block = prompts.format_market_context_block({"indices": {"NIFTY_50": {"ltp": 1.0}}})
        self.assertNotIn("Market state", block)

    def test_indices_and_currency_both_render(self):
        block = prompts.format_market_context_block({
            "indices": {"NIFTY_50": {"ltp": 24500.0}},
            "currency": {"USDINR": {"ltp": 83.5}},
        })
        self.assertIn("NIFTY 50", block)
        self.assertIn("USD/INR", block)

    def test_block_is_labelled_as_live_indian_market_data(self):
        block = prompts.format_market_context_block({"indices": {"NIFTY_50": {"ltp": 1.0}}})
        self.assertIn("CURRENT INDIAN MARKET SNAPSHOT", block)


class GeoPromptTests(unittest.TestCase):
    def base(self, **kw):
        args = dict(
            company="Reliance Industries Ltd", ticker="RELIANCE", sector="Energy",
            description="Oil to chemicals.", keywords=["crude", "tariffs"],
            articles_text="Some article text.",
        )
        args.update(kw)
        return prompts.build_geo_prompt(**args)

    def test_includes_identity_keywords_and_articles(self):
        p = self.base()
        self.assertIn("Reliance Industries Ltd", p)
        self.assertIn("RELIANCE", p)
        self.assertIn("crude, tariffs", p)
        self.assertIn("Some article text.", p)

    def test_empty_keywords_get_a_fallback_hint(self):
        self.assertIn("auto-detected", self.base(keywords=[]))

    def test_threshold_is_stated_in_the_recommendation_instruction(self):
        self.assertIn("risk_score <= 55", self.base())
        self.assertIn("risk_score <= 40", self.base(threshold=40))

    def test_profile_context_is_rendered_when_supplied(self):
        p = self.base(profile={
            "business_model": "Refining and retail",
            "key_markets": ["India", "China"],
            "supply_chain": ["Crude imports"],
            "geopolitical_exposures": ["Strait of Hormuz"],
            "what_moves_the_stock": ["Crude spreads"],
        })
        self.assertIn("COMPANY INTELLIGENCE PROFILE", p)
        self.assertIn("Refining and retail", p)
        self.assertIn("India, China", p)
        self.assertIn("Strait of Hormuz", p)

    def test_profile_context_omitted_when_absent(self):
        self.assertNotIn("COMPANY INTELLIGENCE PROFILE", self.base())

    def test_partial_profile_does_not_raise(self):
        self.assertIn("COMPANY INTELLIGENCE PROFILE",
                      self.base(profile={"business_model": "X"}))

    def test_macro_snapshot_is_injected(self):
        p = self.base(market_context={"indices": {"NIFTY_50": {"ltp": 24500.0}}})
        self.assertIn("CURRENT INDIAN MARKET SNAPSHOT", p)
        self.assertIn("24,500.00", p)

    def test_macro_snapshot_omitted_when_absent(self):
        self.assertNotIn("CURRENT INDIAN MARKET SNAPSHOT", self.base())

    def test_trader_concerns_are_appended(self):
        p = self.base(concerns="Monsoon failure")
        self.assertIn("SPECIFIC RISKS", p)
        self.assertIn("Monsoon failure", p)

    def test_blank_concerns_are_ignored(self):
        self.assertNotIn("SPECIFIC RISKS", self.base(concerns="   "))

    def test_requests_the_schema_keys_the_pipeline_reads(self):
        p = self.base()
        for key in ("risk_score", "risk_level", "collateral_risks", "safe_signals",
                    "sector_exposure", "geographic_exposure", "supply_chain_risk",
                    "regulatory_risk", "consumer_sentiment_risk",
                    "relevant_articles", "summary", "recommendation"):
            with self.subTest(key=key):
                self.assertIn(f'"{key}"', p)

    def test_asks_for_collateral_risk_not_just_direct_mentions(self):
        self.assertIn("COLLATERAL RISK", self.base())


class TechPromptTests(unittest.TestCase):
    STATS = {"current_price": 2900.0, "avg_volume": 5_400_000, "pct_change": 3.2}

    def test_includes_identity_table_and_timeframe(self):
        p = prompts.build_tech_prompt("RELIANCE", "Reliance Industries Ltd",
                                      "2025-08-01 ▲ O:1 H:2 L:0 C:1 Vol:10",
                                      self.STATS, "3 months daily")
        self.assertIn("RELIANCE", p)
        self.assertIn("Reliance Industries Ltd", p)
        self.assertIn("3 months daily", p)
        self.assertIn("Vol:10", p)

    def test_states_the_nse_session_and_rupee_denomination(self):
        p = prompts.build_tech_prompt("X", "X", "", self.STATS, "1d")
        self.assertIn("09:15", p)
        self.assertIn("15:30", p)
        self.assertIn("IST", p)
        self.assertIn("₹", p)

    def test_average_volume_is_grouped(self):
        p = prompts.build_tech_prompt("X", "X", "", self.STATS, "1d")
        self.assertIn("5,400,000", p)

    def test_empty_stats_render_without_raising(self):
        # Unlike technical_analyst._build_prompt, this builder type-guards the
        # avg_volume format, so a stats-less call degrades to "N/A".
        p = prompts.build_tech_prompt("X", "X", "", {}, "1d")
        self.assertIn("N/A", p)

    def test_default_style_adds_no_hint(self):
        p = prompts.build_tech_prompt("X", "X", "", self.STATS, "1d")
        self.assertNotIn("The trader prefers", p)

    def test_explicit_style_adds_a_hint(self):
        p = prompts.build_tech_prompt("X", "X", "", self.STATS, "1d",
                                      trading_style="Swing Trading")
        self.assertIn("The trader prefers Swing Trading", p)


class BottomLinePromptTests(unittest.TestCase):
    SUMMARY = FULL_SUMMARY

    def test_includes_the_verdict(self):
        p = prompts.build_bottom_line_prompt(self.SUMMARY, "BUY (Long-Term Investment)")
        self.assertIn("BUY (Long-Term Investment)", p)

    def test_includes_summary_content(self):
        p = prompts.build_bottom_line_prompt(self.SUMMARY, "BUY")
        self.assertIn("RELIANCE", p)

    def test_empty_summary_does_not_raise(self):
        self.assertIsInstance(prompts.build_bottom_line_prompt({}, "REVIEW"), str)

    def test_a_wellformed_summary_never_renders_the_literal_none(self):
        # build_summary_data() defaults every field to "" / 0, so the real
        # pipeline path is clean. This is the invariant that matters.
        p = prompts.build_bottom_line_prompt(self.SUMMARY, "BUY")
        self.assertNotIn("None", p)

    def test_a_partial_summary_leaks_the_literal_none(self):
        # `.get('name')` has no default, so any caller passing a summary that
        # skipped build_summary_data() ships "None" into the prompt. Documented
        # rather than asserted-away — see tests/test_known_defects.py.
        p = prompts.build_bottom_line_prompt({"company": {}}, "REVIEW")
        self.assertIn("None", p)


class PromptHygieneTests(unittest.TestCase):
    """Cross-cutting invariants every builder must satisfy."""

    def all_prompts(self):
        return {
            "profile": prompts.build_profile_prompt("RELIANCE", {"name": "R Ltd"}),
            "financials": prompts.build_financials_prompt("R Ltd", "text", {}),
            "geo": prompts.build_geo_prompt("R Ltd", "RELIANCE", "Energy", "desc",
                                            ["crude"], "articles"),
            "tech": prompts.build_tech_prompt("RELIANCE", "R Ltd", "table",
                                              {"current_price": 1.0}, "1d"),
            # A realistic summary — i.e. the shape build_summary_data() emits.
            "bottom_line": prompts.build_bottom_line_prompt(
                FULL_SUMMARY, "BUY (Long-Term Investment)"),
        }

    def test_all_builders_return_non_empty_strings(self):
        for name, p in self.all_prompts().items():
            with self.subTest(prompt=name):
                self.assertIsInstance(p, str)
                self.assertGreater(len(p.strip()), 100)

    def test_no_unrendered_format_placeholders(self):
        for name, p in self.all_prompts().items():
            with self.subTest(prompt=name):
                self.assertNotIn("{ticker}", p)
                self.assertNotIn("{company}", p)
                self.assertNotIn("{self.", p)

    def test_no_literal_none_leaks_into_prompt_text(self):
        for name, p in self.all_prompts().items():
            with self.subTest(prompt=name):
                self.assertNotIn(": None", p)

    def test_no_dollar_denominated_prices(self):
        # The app is NSE/BSE only; a $ in a price prompt means a stale US prompt
        # survived the Indianisation pass.
        for name in ("geo", "tech"):
            with self.subTest(prompt=name):
                self.assertNotIn("$", self.all_prompts()[name])


if __name__ == "__main__":
    unittest.main()
