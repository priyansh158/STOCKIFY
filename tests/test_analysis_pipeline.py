"""Unit tests for server.services.analysis_pipeline — the pure decision layer.

`compute_verdict` is the screen that turns three stage results into a BUY /
AVOID / REVIEW call, so its threshold boundaries are pinned from both sides:
an off-by-one here silently flips real recommendations.

`build_summary_data` is the contract the frontend and the history store both
read, so its key set and defaulting are asserted exhaustively.

The Claude-driven stage generators are not exercised here — those belong to
test_stream_claude.py, which owns the streaming contract.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import tests.support  # noqa: F401  — must precede project imports

from server.services import analysis_pipeline as pipeline
from server.services.analysis_pipeline import (
    AnalysisState,
    STAGE_ORDER,
    _exposure_text,
    _fallback_bottom_line,
    _safe_subfield,
    build_summary_data,
    compute_verdict,
)

PASS_FIN = {"score": 80, "passed": True}
PASS_GEO = {"risk_score": 20, "risk_level": "LOW", "passed": True}


class StageOrderTests(unittest.TestCase):
    def test_five_stages_in_dependency_order(self):
        self.assertEqual(STAGE_ORDER,
                         ("overview", "financials", "geo", "technical", "summary"))

    def test_thresholds_are_the_documented_defaults(self):
        self.assertEqual(pipeline.FIN_PASS_THRESHOLD_DEFAULT, 60)
        self.assertEqual(pipeline.GEO_FAIL_THRESHOLD_DEFAULT, 55)


class AnalysisStateTests(unittest.TestCase):
    def test_every_stage_starts_pending(self):
        state = AnalysisState(analysis_id="x", ticker="RELIANCE")
        self.assertEqual(set(state.stage_status), set(STAGE_ORDER))
        self.assertTrue(all(v == "pending" for v in state.stage_status.values()))

    def test_stage_results_start_none(self):
        state = AnalysisState(analysis_id="x", ticker="RELIANCE")
        for stage in ("overview", "financials", "geo", "technical", "summary"):
            self.assertIsNone(getattr(state, stage))

    def test_mutable_defaults_are_not_shared_between_states(self):
        a = AnalysisState(analysis_id="a", ticker="A")
        b = AnalysisState(analysis_id="b", ticker="B")
        a.stage_status["overview"] = "ready"
        a.stage_errors["overview"] = "boom"
        a.yf_info["k"] = "v"
        self.assertEqual(b.stage_status["overview"], "pending")
        self.assertEqual(b.stage_errors, {})
        self.assertEqual(b.yf_info, {})


class CreateAnalysisTests(unittest.TestCase):
    def create(self, ticker, info=None, holding=None):
        info = {"name": "Reliance Industries Ltd", "sector": "Energy"} if info is None else info
        with patch.object(pipeline.data, "get_yf_info", return_value=info), \
             patch.object(pipeline.portfolio_sync, "holding_for", return_value=holding):
            return pipeline.create_analysis(ticker)

    def test_ticker_is_normalised(self):
        self.assertEqual(self.create("  reliance ").ticker, "RELIANCE")

    def test_empty_ticker_is_rejected(self):
        with self.assertRaises(ValueError):
            self.create("   ")

    def test_metadata_is_hydrated_from_yfinance(self):
        state = self.create("RELIANCE")
        self.assertEqual(state.company_name, "Reliance Industries Ltd")
        self.assertEqual(state.sector, "Energy")

    def test_company_name_falls_back_to_the_ticker(self):
        self.assertEqual(self.create("RELIANCE", info={}).company_name, "RELIANCE")

    def test_analysis_id_is_unique_and_registered(self):
        a, b = self.create("RELIANCE"), self.create("TCS")
        self.assertNotEqual(a.analysis_id, b.analysis_id)
        self.assertIs(pipeline.get_state(a.analysis_id), a)
        self.assertIs(pipeline.get_state(b.analysis_id), b)

    def test_unknown_analysis_id_returns_none(self):
        self.assertIsNone(pipeline.get_state("no-such-id"))

    def test_position_context_is_none_without_a_holding(self):
        self.assertIsNone(self.create("RELIANCE").position_context)

    def test_position_context_is_populated_from_a_holding(self):
        from portfolio_sync import Holding
        h = Holding(tradingsymbol="RELIANCE", exchange="NSE", quantity=100,
                    average_price=2800.0, last_price=2900.0, pnl=10_000.0,
                    day_change_pct=1.0, instrument_token=738561)
        state = self.create("RELIANCE", holding=h)
        self.assertEqual(state.position_context["quantity"], 100)

    def test_portfolio_failure_does_not_abort_the_analysis(self):
        with patch.object(pipeline.data, "get_yf_info", return_value={}), \
             patch.object(pipeline.portfolio_sync, "holding_for",
                          side_effect=RuntimeError("kite down")):
            state = pipeline.create_analysis("RELIANCE")
        self.assertIsNone(state.position_context)
        self.assertEqual(state.ticker, "RELIANCE")


class ComputeVerdictTests(unittest.TestCase):
    # ── Financial screen ─────────────────────────────────────────────────────

    def test_below_financial_threshold_is_avoid(self):
        verdict, reason = compute_verdict({"score": 59}, PASS_GEO,
                                          {"recommendation": "LONG_TERM"})
        self.assertEqual(verdict, "AVOID")
        self.assertIn("financial screen", reason)
        self.assertIn("59/100", reason)

    def test_exactly_at_the_financial_threshold_passes(self):
        verdict, _ = compute_verdict({"score": 60}, PASS_GEO,
                                     {"recommendation": "LONG_TERM"})
        self.assertEqual(verdict, "BUY (Long-Term Investment)")

    def test_custom_financial_threshold_is_honoured(self):
        verdict, _ = compute_verdict({"score": 59}, PASS_GEO,
                                     {"recommendation": "LONG_TERM"}, fin_threshold=50)
        self.assertEqual(verdict, "BUY (Long-Term Investment)")

    def test_missing_financial_score_is_treated_as_zero(self):
        verdict, _ = compute_verdict({"passed": True}, PASS_GEO,
                                     {"recommendation": "LONG_TERM"})
        self.assertEqual(verdict, "AVOID")

    def test_none_financial_score_is_treated_as_zero(self):
        verdict, _ = compute_verdict({"score": None}, PASS_GEO,
                                     {"recommendation": "LONG_TERM"})
        self.assertEqual(verdict, "AVOID")

    # ── Geopolitical screen ──────────────────────────────────────────────────

    def test_above_geo_threshold_is_avoid(self):
        verdict, reason = compute_verdict(PASS_FIN, {"risk_score": 56, "risk_level": "HIGH"},
                                          {"recommendation": "LONG_TERM"})
        self.assertEqual(verdict, "AVOID")
        self.assertIn("geopolitical screen", reason)
        self.assertIn("HIGH", reason)

    def test_exactly_at_the_geo_threshold_passes(self):
        verdict, _ = compute_verdict(PASS_FIN, {"risk_score": 55},
                                     {"recommendation": "LONG_TERM"})
        self.assertEqual(verdict, "BUY (Long-Term Investment)")

    def test_custom_geo_threshold_is_honoured(self):
        verdict, _ = compute_verdict(PASS_FIN, {"risk_score": 56},
                                     {"recommendation": "LONG_TERM"}, geo_threshold=70)
        self.assertEqual(verdict, "BUY (Long-Term Investment)")

    def test_geo_risk_level_defaults_to_high_in_the_reason(self):
        _, reason = compute_verdict(PASS_FIN, {"risk_score": 90},
                                    {"recommendation": "LONG_TERM"})
        self.assertIn("HIGH", reason)

    def test_financial_screen_is_evaluated_before_geopolitical(self):
        # Both fail; the reported reason must be the financial one.
        _, reason = compute_verdict({"score": 10}, {"risk_score": 99},
                                    {"recommendation": "LONG_TERM"})
        self.assertIn("financial screen", reason)

    # ── Technical screen ─────────────────────────────────────────────────────

    def test_technical_avoid_overrides_passing_screens(self):
        verdict, reason = compute_verdict(PASS_FIN, PASS_GEO, {"recommendation": "AVOID"})
        self.assertEqual(verdict, "AVOID")
        self.assertIn("no safe entry", reason)

    def test_recommendation_is_case_insensitive(self):
        verdict, _ = compute_verdict(PASS_FIN, PASS_GEO, {"recommendation": "avoid"})
        self.assertEqual(verdict, "AVOID")

    def test_recommendation_is_whitespace_tolerant(self):
        verdict, _ = compute_verdict(PASS_FIN, PASS_GEO, {"recommendation": "  long_term  "})
        self.assertEqual(verdict, "BUY (Long-Term Investment)")

    def test_each_recommendation_maps_to_its_buy_label(self):
        cases = {
            "DAY_TRADING": "BUY (Day Trading)",
            "SWING_TRADING": "BUY (Swing Trading)",
            "LONG_TERM": "BUY (Long-Term Investment)",
        }
        for rec, expected in cases.items():
            with self.subTest(rec=rec):
                verdict, reason = compute_verdict(PASS_FIN, PASS_GEO, {"recommendation": rec})
                self.assertEqual(verdict, expected)
                self.assertEqual(reason, "All three stages cleared.")

    def test_unknown_recommendation_becomes_review(self):
        verdict, reason = compute_verdict(PASS_FIN, PASS_GEO, {"recommendation": "MAYBE"})
        self.assertEqual(verdict, "REVIEW")
        self.assertIn("unclear", reason)

    def test_missing_recommendation_becomes_review(self):
        self.assertEqual(compute_verdict(PASS_FIN, PASS_GEO, {})[0], "REVIEW")

    def test_none_recommendation_becomes_review(self):
        self.assertEqual(compute_verdict(PASS_FIN, PASS_GEO, {"recommendation": None})[0],
                         "REVIEW")

    # ── Degenerate inputs ────────────────────────────────────────────────────

    def test_empty_stage_dicts_skip_their_screens(self):
        # `if fin and ...` — an absent stage is not treated as a failing stage,
        # so a technical-only run can still yield a BUY.
        verdict, _ = compute_verdict({}, {}, {"recommendation": "LONG_TERM"})
        self.assertEqual(verdict, "BUY (Long-Term Investment)")

    def test_all_empty_yields_review(self):
        self.assertEqual(compute_verdict({}, {}, {})[0], "REVIEW")

    def test_always_returns_a_two_tuple_of_non_empty_strings(self):
        cases = [
            ({}, {}, {}),
            ({"score": 0}, {"risk_score": 100}, {"recommendation": "AVOID"}),
            (PASS_FIN, PASS_GEO, {"recommendation": "SWING_TRADING"}),
        ]
        for fin, geo, tech in cases:
            with self.subTest(fin=fin, geo=geo, tech=tech):
                verdict, reason = compute_verdict(fin, geo, tech)
                self.assertTrue(verdict.strip())
                self.assertTrue(reason.strip())

    def test_verdict_is_always_one_of_the_known_labels(self):
        known = {"AVOID", "REVIEW", "BUY (Day Trading)", "BUY (Swing Trading)",
                 "BUY (Long-Term Investment)"}
        for score in (0, 59, 60, 100):
            for risk in (0, 55, 56, 100):
                for rec in ("DAY_TRADING", "SWING_TRADING", "LONG_TERM", "AVOID", "?"):
                    verdict, _ = compute_verdict({"score": score}, {"risk_score": risk},
                                                 {"recommendation": rec})
                    self.assertIn(verdict, known)


class SafeSubfieldTests(unittest.TestCase):
    def test_reads_a_nested_child(self):
        self.assertEqual(_safe_subfield({"revenue_trend": {"status": "GROWING"}},
                                        "revenue_trend", "status"), "GROWING")

    def test_missing_parent_returns_empty_string(self):
        self.assertEqual(_safe_subfield({}, "revenue_trend", "status"), "")

    def test_none_parent_returns_empty_string(self):
        self.assertEqual(_safe_subfield({"revenue_trend": None}, "revenue_trend", "status"), "")

    def test_missing_child_returns_empty_string(self):
        self.assertEqual(_safe_subfield({"revenue_trend": {}}, "revenue_trend", "status"), "")

    def test_empty_child_returns_empty_string(self):
        self.assertEqual(_safe_subfield({"revenue_trend": {"status": ""}},
                                        "revenue_trend", "status"), "")

    def test_string_parent_is_returned_directly(self):
        # The model sometimes flattens the object into a bare string.
        self.assertEqual(_safe_subfield({"revenue_trend": "GROWING"},
                                        "revenue_trend", "status"), "GROWING")

    def test_scalar_parent_is_stringified(self):
        self.assertEqual(_safe_subfield({"revenue_trend": 42}, "revenue_trend", "status"), "42")

    def test_non_string_child_is_stringified(self):
        self.assertEqual(_safe_subfield({"p": {"c": 7}}, "p", "c"), "7")

    def test_always_returns_a_string(self):
        for parent in ({}, {"p": None}, {"p": "s"}, {"p": {"c": 1}}, {"p": 5}, {"p": []}):
            with self.subTest(parent=parent):
                self.assertIsInstance(_safe_subfield(parent, "p", "c"), str)


class ExposureTextTests(unittest.TestCase):
    def test_first_present_key_wins(self):
        self.assertEqual(_exposure_text({"sector_exposure": "A", "sector_risk": "B"},
                                        "sector_exposure", "sector_risk"), "A")

    def test_falls_through_to_the_alternate_key(self):
        self.assertEqual(_exposure_text({"sector_risk": "B"},
                                        "sector_exposure", "sector_risk"), "B")

    def test_no_key_present_returns_empty_string(self):
        self.assertEqual(_exposure_text({}, "sector_exposure", "sector_risk"), "")

    def test_none_and_empty_values_are_skipped(self):
        self.assertEqual(_exposure_text({"sector_exposure": None, "sector_risk": "B"},
                                        "sector_exposure", "sector_risk"), "B")
        self.assertEqual(_exposure_text({"sector_exposure": "", "sector_risk": "B"},
                                        "sector_exposure", "sector_risk"), "B")

    def test_dict_value_prefers_the_note_field(self):
        self.assertEqual(
            _exposure_text({"sector_exposure": {"note": "N", "level": "HIGH"}},
                           "sector_exposure"), "N")

    def test_dict_value_field_precedence(self):
        for field in ("note", "description", "summary", "level"):
            with self.subTest(field=field):
                self.assertEqual(
                    _exposure_text({"k": {field: "V"}}, "k"), "V")

    def test_dict_without_known_fields_is_joined(self):
        got = _exposure_text({"k": {"alpha": "1", "beta": "2"}}, "k")
        self.assertIn("alpha: 1", got)
        self.assertIn("beta: 2", got)

    def test_falsy_entries_are_dropped_from_the_join(self):
        got = _exposure_text({"k": {"alpha": "1", "beta": ""}}, "k")
        self.assertIn("alpha: 1", got)
        self.assertNotIn("beta", got)

    def test_always_returns_a_string(self):
        for payload in ({}, {"k": None}, {"k": ""}, {"k": "s"}, {"k": {"note": "n"}},
                        {"k": {"x": "y"}}):
            with self.subTest(payload=payload):
                self.assertIsInstance(_exposure_text(payload, "k"), str)


class BuildSummaryDataTests(unittest.TestCase):
    def build(self, profile=None, fin=None, geo=None, tech=None, **kw):
        args = dict(ticker="RELIANCE", company_name="Reliance Industries Ltd")
        args.update(kw)
        return build_summary_data(profile or {}, fin or {}, geo or {}, tech or {}, **args)

    def test_top_level_sections(self):
        self.assertEqual(set(self.build()),
                         {"company", "profile", "financial", "geo", "technical"})

    def test_company_section_keys(self):
        self.assertEqual(set(self.build()["company"]),
                         {"name", "ticker", "exchange", "country", "sector"})

    def test_company_fields_are_carried_through(self):
        s = self.build(exchange="NSE", country="IN", sector="Energy")
        self.assertEqual(s["company"]["ticker"], "RELIANCE")
        self.assertEqual(s["company"]["name"], "Reliance Industries Ltd")
        self.assertEqual(s["company"]["exchange"], "NSE")
        self.assertEqual(s["company"]["country"], "IN")
        self.assertEqual(s["company"]["sector"], "Energy")

    def test_company_name_falls_back_to_the_profile(self):
        s = self.build(profile={"company_name": "From Profile"}, company_name="")
        self.assertEqual(s["company"]["name"], "From Profile")

    def test_sector_falls_back_to_the_profile(self):
        s = self.build(profile={"sector": "Energy"}, sector="")
        self.assertEqual(s["company"]["sector"], "Energy")

    def test_empty_inputs_produce_a_fully_shaped_result(self):
        s = self.build()
        self.assertEqual(s["financial"]["score"], 0)
        self.assertFalse(s["financial"]["passed"])
        self.assertEqual(s["geo"]["risk_score"], 0)
        self.assertEqual(s["technical"]["confidence"], 0)

    def test_list_fields_default_to_empty_lists_never_none(self):
        s = self.build(profile={"key_products": None}, geo={"collateral_risks": None},
                       tech={"patterns_found": None})
        self.assertEqual(s["profile"]["key_products"], [])
        self.assertEqual(s["geo"]["collateral_risks"], [])
        self.assertEqual(s["technical"]["patterns_found"], [])

    def test_dict_fields_default_to_empty_dicts_never_none(self):
        s = self.build(fin={"key_metrics": None, "deep_analysis": None})
        self.assertEqual(s["financial"]["key_metrics"], {})
        self.assertEqual(s["financial"]["deep_analysis"], {})

    def test_scores_are_coerced_to_int(self):
        s = self.build(fin={"score": "78"}, geo={"risk_score": "30"},
                       tech={"confidence": "71"})
        self.assertEqual(s["financial"]["score"], 78)
        self.assertEqual(s["geo"]["risk_score"], 30)
        self.assertEqual(s["technical"]["confidence"], 71)

    def test_passed_flags_are_coerced_to_bool(self):
        s = self.build(fin={"passed": 1}, geo={"passed": 0})
        self.assertIs(s["financial"]["passed"], True)
        self.assertIs(s["geo"]["passed"], False)

    def test_financial_substatuses_are_flattened(self):
        s = self.build(fin={
            "revenue_trend": {"status": "GROWING"},
            "cash_flow": {"status": "POSITIVE"},
            "profitability": {"trend": "EXPANDING"},
            "balance_sheet": {"status": "STRONG"},
        })
        self.assertEqual(s["financial"]["revenue_status"], "GROWING")
        self.assertEqual(s["financial"]["cash_flow_status"], "POSITIVE")
        self.assertEqual(s["financial"]["margin_trend"], "EXPANDING")
        self.assertEqual(s["financial"]["balance_sheet_status"], "STRONG")

    def test_geo_exposures_have_a_stable_key_set(self):
        self.assertEqual(set(self.build()["geo"]["exposures"]),
                         {"sector", "geographic", "supply_chain", "regulatory",
                          "consumer_sentiment"})

    def test_geo_exposures_accept_either_naming_convention(self):
        s = self.build(geo={"sector_exposure": "A", "geographic_risk": "B"})
        self.assertEqual(s["geo"]["exposures"]["sector"], "A")
        self.assertEqual(s["geo"]["exposures"]["geographic"], "B")

    def test_technical_section_keys(self):
        self.assertEqual(
            set(self.build()["technical"]),
            {"recommendation", "confidence", "trend", "timeframe", "current_price",
             "period_change", "entry_point", "target_price", "stop_loss",
             "risk_reward_ratio", "patterns_found", "support_levels",
             "resistance_levels", "volume_analysis", "momentum", "trading_notes",
             "summary"},
        )

    def test_result_is_json_serialisable(self):
        import json
        json.dumps(self.build(fin={"score": 78}, tech={"confidence": 71}))

    def test_output_feeds_the_bottom_line_prompt_without_none_leaks(self):
        from server.services import prompts
        s = self.build(exchange="NSE", country="IN", sector="Energy",
                       fin={"score": 78}, geo={"risk_score": 30},
                       tech={"recommendation": "LONG_TERM", "confidence": 71})
        self.assertNotIn("None", prompts.build_bottom_line_prompt(s, "BUY"))


class FallbackBottomLineTests(unittest.TestCase):
    def test_mentions_the_verdict_and_all_three_scores(self):
        summary = {
            "financial": {"score": 78, "passed": True},
            "geo": {"risk_score": 30, "risk_level": "LOW"},
            "technical": {"recommendation": "LONG_TERM", "current_price": "₹2,900"},
        }
        line = _fallback_bottom_line(summary, "BUY (Long-Term Investment)")
        self.assertIn("BUY (Long-Term Investment)", line)
        self.assertIn("78/100", line)
        self.assertIn("30/100", line)
        self.assertIn("LONG_TERM", line)
        self.assertIn("pass", line)

    def test_failing_financials_render_fail(self):
        summary = {"financial": {"score": 10, "passed": False}, "geo": {}, "technical": {}}
        self.assertIn("fail", _fallback_bottom_line(summary, "AVOID"))

    def test_empty_summary_uses_placeholders_without_raising(self):
        line = _fallback_bottom_line({}, "REVIEW")
        self.assertIn("REVIEW", line)
        self.assertIn("0/100", line)
        self.assertIn("—", line)

    def test_always_returns_a_non_empty_string(self):
        for summary in ({}, {"financial": {}}, {"technical": {"recommendation": "AVOID"}}):
            with self.subTest(summary=summary):
                self.assertTrue(_fallback_bottom_line(summary, "AVOID").strip())


if __name__ == "__main__":
    unittest.main()
