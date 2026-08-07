"""Unit tests for analysis_history — the SQLite store of completed analyses.

This store is the artifact of record (it replaced the PDF report), so the
round-trip fidelity tests here matter: anything lost between save_analysis()
and get_analysis() is permanently lost for that analysis.

Runs against a throwaway DB in the tests sandbox.
"""
from __future__ import annotations

import time
import unittest
from datetime import datetime
from unittest.mock import patch

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import analysis_history


def save(**overrides) -> int:
    """save_analysis() with every required kwarg defaulted."""
    payload = dict(
        ticker="RELIANCE",
        company_name="Reliance Industries Ltd",
        sector="Energy",
        verdict="BUY (Long-Term Investment)",
        verdict_reason="All three stages cleared.",
        bottom_line="Strong fundamentals, benign macro.",
        profile={"industry": "Refineries"},
        financials={"score": 78, "passed": True},
        geo={"risk_score": 30, "risk_level": "LOW"},
        technical={"recommendation": "LONG_TERM", "confidence": 71},
        summary={"company": {"ticker": "RELIANCE"}},
    )
    payload.update(overrides)
    return analysis_history.save_analysis(**payload)


class HistoryTest(SandboxedDBTest):
    db_modules = (analysis_history,)


class InitTests(HistoryTest):
    def test_init_db_is_idempotent(self):
        analysis_history.init_db()
        analysis_history.init_db()
        self.assertEqual(analysis_history.count_analyses(), 0)

    def test_db_file_lands_in_sandbox(self):
        support.assert_sandboxed(analysis_history._db_path())
        self.assertEqual(analysis_history._db_path().name, "history.sqlite")


class SaveTests(HistoryTest):
    def test_returns_a_positive_rowid(self):
        self.assertGreater(save(), 0)

    def test_ids_increment(self):
        first, second = save(), save()
        self.assertGreater(second, first)

    def test_ticker_is_normalised_to_upper_and_stripped(self):
        rid = save(ticker="  reliance  ")
        self.assertEqual(analysis_history.get_analysis(rid).ticker, "RELIANCE")

    def test_scores_are_denormalised_into_their_own_columns(self):
        rid = save(financials={"score": 78}, geo={"risk_score": 30})
        rec = analysis_history.get_analysis(rid)
        self.assertEqual(rec.financial_score, 78)
        self.assertEqual(rec.geo_risk_score, 30)

    def test_missing_scores_default_to_zero(self):
        rid = save(financials={}, geo={})
        rec = analysis_history.get_analysis(rid)
        self.assertEqual(rec.financial_score, 0)
        self.assertEqual(rec.geo_risk_score, 0)

    def test_none_score_is_coerced_to_zero(self):
        rid = save(financials={"score": None}, geo={"risk_score": None})
        self.assertEqual(analysis_history.get_analysis(rid).financial_score, 0)

    def test_numeric_string_score_is_accepted(self):
        rid = save(financials={"score": "78"})
        self.assertEqual(analysis_history.get_analysis(rid).financial_score, 78)

    def test_non_numeric_score_propagates_rather_than_being_swallowed(self):
        # The int() coercions sit outside save_analysis's try/except, so a
        # malformed stage result surfaces here instead of silently writing 0.
        # Callers must therefore validate stage output before persisting.
        with self.assertRaises(ValueError):
            save(financials={"score": "high"})

    def test_technical_classification_truncated_to_32_chars(self):
        rid = save(technical={"recommendation": "X" * 100})
        rec = analysis_history.get_analysis(rid)
        self.assertEqual(len(rec.technical_classification), 32)

    def test_created_at_is_populated(self):
        before = int(time.time())
        rid = save()
        self.assertGreaterEqual(analysis_history.get_analysis(rid).created_at, before)

    def test_hostile_ticker_is_stored_literally_not_executed(self):
        # Parameterised SQL — the payload must survive verbatim and the table
        # must still exist afterwards.
        evil = "'; DROP TABLE analyses;--"
        rid = save(ticker=evil)
        self.assertGreater(rid, 0)
        self.assertEqual(analysis_history.get_analysis(rid).ticker, evil.upper())
        self.assertEqual(analysis_history.count_analyses(), 1)

    def test_empty_optional_strings_are_accepted(self):
        rid = save(company_name="", sector="", verdict="", verdict_reason="", bottom_line="")
        rec = analysis_history.get_analysis(rid)
        self.assertEqual(rec.company_name, "")
        self.assertEqual(rec.verdict, "")


class JsonRoundTripTests(HistoryTest):
    def test_nested_structures_survive(self):
        profile = {
            "key_products": ["Jio", "Retail"],
            "nested": {"a": [1, 2, {"b": True}]},
            "none_value": None,
        }
        rid = save(profile=profile)
        self.assertEqual(analysis_history.get_analysis(rid).profile, profile)

    def test_unicode_and_rupee_symbols_survive(self):
        summary = {"bottom_line": "Target ₹3,050 — भारतीय बाज़ार", "emoji": "📈"}
        rid = save(summary=summary)
        self.assertEqual(analysis_history.get_analysis(rid).summary, summary)

    def test_non_serialisable_values_are_stringified_not_fatal(self):
        # json.dumps(..., default=str) is the escape hatch: a stage returning a
        # datetime (or any exotic object) must not abort the save.
        rid = save(profile={"when": datetime(2025, 8, 6, 10, 30)})
        rec = analysis_history.get_analysis(rid)
        self.assertIsInstance(rec.profile["when"], str)
        self.assertIn("2025-08-06", rec.profile["when"])

    def test_empty_dicts_round_trip_as_empty_dicts(self):
        rid = save(profile={}, financials={"score": 1}, geo={}, technical={}, summary={})
        rec = analysis_history.get_analysis(rid)
        self.assertEqual(rec.profile, {})
        self.assertEqual(rec.summary, {})

    def test_macro_snapshot_round_trips_when_present(self):
        macro = {"indices": {"NIFTY_50": {"ltp": 24500.0, "ts": 1}}}
        rid = save(macro_snapshot=macro)
        self.assertEqual(analysis_history.get_analysis(rid).macro_snapshot, macro)

    def test_macro_snapshot_defaults_to_none(self):
        self.assertIsNone(analysis_history.get_analysis(save()).macro_snapshot)

    def test_empty_macro_snapshot_normalises_to_none(self):
        # {} is falsy on write (stored NULL) and `_load(...) or None` on read,
        # so an empty snapshot is indistinguishable from "no snapshot".
        rid = save(macro_snapshot={})
        self.assertIsNone(analysis_history.get_analysis(rid).macro_snapshot)

    def test_position_context_round_trips(self):
        pos = {"quantity": 100, "average_price": 2800.0}
        rid = save(position_context=pos)
        self.assertEqual(analysis_history.get_analysis(rid).position_context, pos)

    def test_corrupt_json_column_degrades_to_empty_dict(self):
        rid = save()
        conn = analysis_history._connect()
        conn.execute("UPDATE analyses SET profile_json = ? WHERE id = ?", ("{not json", rid))
        conn.commit()
        self.assertEqual(analysis_history.get_analysis(rid).profile, {})


class GetTests(HistoryTest):
    def test_unknown_id_returns_none(self):
        self.assertIsNone(analysis_history.get_analysis(999_999))

    def test_zero_and_negative_ids_return_none(self):
        self.assertIsNone(analysis_history.get_analysis(0))
        self.assertIsNone(analysis_history.get_analysis(-1))

    def test_string_id_is_coerced(self):
        rid = save()
        self.assertIsNotNone(analysis_history.get_analysis(str(rid)))


class ListTests(HistoryTest):
    def _save_at(self, ts: int, **kw) -> int:
        with patch.object(analysis_history.time, "time", return_value=ts):
            return save(**kw)

    def test_empty_store_returns_empty_list(self):
        self.assertEqual(analysis_history.list_analyses(), [])

    def test_newest_first(self):
        self._save_at(1000, ticker="AAA")
        self._save_at(3000, ticker="CCC")
        self._save_at(2000, ticker="BBB")
        self.assertEqual([r.ticker for r in analysis_history.list_analyses()],
                         ["CCC", "BBB", "AAA"])

    def test_filter_by_ticker_is_exact_and_case_insensitive(self):
        self._save_at(1000, ticker="RELIANCE")
        self._save_at(2000, ticker="TCS")
        got = analysis_history.list_analyses(ticker="reliance")
        self.assertEqual([r.ticker for r in got], ["RELIANCE"])

    def test_ticker_filter_does_not_substring_match(self):
        self._save_at(1000, ticker="TCS")
        self.assertEqual(analysis_history.list_analyses(ticker="TC"), [])

    def test_filter_by_verdict_substring(self):
        self._save_at(1000, ticker="AAA", verdict="BUY (Swing Trading)")
        self._save_at(2000, ticker="BBB", verdict="AVOID")
        got = analysis_history.list_analyses(verdict_contains="buy")
        self.assertEqual([r.ticker for r in got], ["AAA"])

    def test_filter_by_since_ts_is_inclusive(self):
        self._save_at(1000, ticker="OLD")
        self._save_at(2000, ticker="NEW")
        got = analysis_history.list_analyses(since_ts=2000)
        self.assertEqual([r.ticker for r in got], ["NEW"])

    def test_filters_combine_with_and(self):
        self._save_at(1000, ticker="AAA", verdict="AVOID")
        self._save_at(2000, ticker="AAA", verdict="BUY (Long-Term Investment)")
        self._save_at(3000, ticker="BBB", verdict="BUY (Long-Term Investment)")
        got = analysis_history.list_analyses(ticker="AAA", verdict_contains="BUY")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].created_at, 2000)

    def test_limit(self):
        for i in range(5):
            self._save_at(1000 + i)
        self.assertEqual(len(analysis_history.list_analyses(limit=2)), 2)

    def test_offset_paginates_without_overlap(self):
        for i in range(5):
            self._save_at(1000 + i, ticker=f"T{i}")
        page1 = analysis_history.list_analyses(limit=2, offset=0)
        page2 = analysis_history.list_analyses(limit=2, offset=2)
        self.assertEqual([r.ticker for r in page1], ["T4", "T3"])
        self.assertEqual([r.ticker for r in page2], ["T2", "T1"])

    def test_offset_past_the_end_returns_empty(self):
        self._save_at(1000)
        self.assertEqual(analysis_history.list_analyses(offset=100), [])

    def test_limit_zero_returns_nothing(self):
        self._save_at(1000)
        self.assertEqual(analysis_history.list_analyses(limit=0), [])

    def test_no_match_returns_empty_list(self):
        self._save_at(1000, ticker="RELIANCE")
        self.assertEqual(analysis_history.list_analyses(ticker="NOSUCH"), [])

    def test_hostile_filter_values_are_parameterised(self):
        self._save_at(1000, ticker="RELIANCE")
        self.assertEqual(analysis_history.list_analyses(ticker="'; DROP TABLE analyses;--"), [])
        self.assertEqual(analysis_history.count_analyses(), 1)

    def test_verdict_filter_wildcards_are_not_escaped(self):
        # The filter interpolates the term into a LIKE pattern, so a caller
        # passing "%" matches everything. Locking in the current behaviour so a
        # future escaping change is deliberate.
        self._save_at(1000, verdict="AVOID")
        self.assertEqual(len(analysis_history.list_analyses(verdict_contains="%")), 1)

    def test_records_are_fully_hydrated(self):
        self._save_at(1000, profile={"industry": "Refineries"})
        rec = analysis_history.list_analyses()[0]
        self.assertEqual(rec.profile, {"industry": "Refineries"})
        self.assertEqual(rec.company_name, "Reliance Industries Ltd")


class DeleteTests(HistoryTest):
    def test_delete_returns_true_and_removes(self):
        rid = save()
        self.assertTrue(analysis_history.delete_analysis(rid))
        self.assertIsNone(analysis_history.get_analysis(rid))
        self.assertEqual(analysis_history.count_analyses(), 0)

    def test_delete_unknown_id_returns_false(self):
        self.assertFalse(analysis_history.delete_analysis(999_999))

    def test_double_delete_returns_false_the_second_time(self):
        rid = save()
        self.assertTrue(analysis_history.delete_analysis(rid))
        self.assertFalse(analysis_history.delete_analysis(rid))

    def test_delete_leaves_siblings_intact(self):
        keep, drop = save(ticker="KEEP"), save(ticker="DROP")
        analysis_history.delete_analysis(drop)
        self.assertIsNotNone(analysis_history.get_analysis(keep))
        self.assertEqual(analysis_history.count_analyses(), 1)


class CountTests(HistoryTest):
    def test_zero_on_empty(self):
        self.assertEqual(analysis_history.count_analyses(), 0)

    def test_counts_all(self):
        save(); save(); save()
        self.assertEqual(analysis_history.count_analyses(), 3)

    def test_counts_by_ticker(self):
        save(ticker="RELIANCE"); save(ticker="RELIANCE"); save(ticker="TCS")
        self.assertEqual(analysis_history.count_analyses("RELIANCE"), 2)
        self.assertEqual(analysis_history.count_analyses("TCS"), 1)

    def test_ticker_arg_is_normalised(self):
        save(ticker="RELIANCE")
        self.assertEqual(analysis_history.count_analyses("  reliance "), 1)

    def test_unknown_ticker_counts_zero(self):
        save(ticker="RELIANCE")
        self.assertEqual(analysis_history.count_analyses("NOSUCH"), 0)

    def test_count_agrees_with_list_length(self):
        for _ in range(4):
            save()
        self.assertEqual(analysis_history.count_analyses(),
                         len(analysis_history.list_analyses(limit=1000)))


if __name__ == "__main__":
    unittest.main()
