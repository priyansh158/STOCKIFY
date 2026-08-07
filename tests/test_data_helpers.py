"""Unit tests for server.services.data — the pure helpers and source-fallback logic.

Everything that touches the network (Screener.in, NSE PDFs, NewsAPI, WSJ,
Google News) is patched out. What is tested is the logic *around* those calls:
HTML cleaning, DataFrame normalisation, the article dedupe key, and the
fallback ladder that decides when a secondary news source is consulted at all.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import tests.support  # noqa: F401  — must precede project imports
from tests.support import isolated_env

from server.services import data


# ── Env key resolution ───────────────────────────────────────────────────────

class KeyResolverTests(unittest.TestCase):
    def test_newsapi_key_from_env(self):
        with patch.dict(os.environ, {"NEWSAPI_KEY": "abc"}):
            self.assertEqual(data._resolve_newsapi_key(), "abc")

    def test_rapidapi_key_from_env(self):
        with patch.dict(os.environ, {"RAPIDAPI_KEY": "xyz"}):
            self.assertEqual(data._resolve_rapidapi_key(), "xyz")

    def test_missing_keys_resolve_to_empty_string(self):
        with isolated_env():
            self.assertEqual(data._resolve_newsapi_key(), "")
            self.assertEqual(data._resolve_rapidapi_key(), "")

    def test_keys_are_whitespace_stripped(self):
        with patch.dict(os.environ, {"NEWSAPI_KEY": "  abc  "}):
            self.assertEqual(data._resolve_newsapi_key(), "abc")

    def test_whitespace_only_key_is_falsy(self):
        with patch.dict(os.environ, {"NEWSAPI_KEY": "   "}):
            self.assertFalse(data._resolve_newsapi_key())


# ── HTML cleaning ────────────────────────────────────────────────────────────

class CleanHtmlTests(unittest.TestCase):
    def test_tags_are_stripped(self):
        self.assertEqual(data._clean_html("<p>Hello <b>world</b></p>"), "Hello world")

    def test_whitespace_is_collapsed(self):
        self.assertEqual(data._clean_html("a\n\n\t  b"), "a b")

    def test_result_is_trimmed(self):
        self.assertEqual(data._clean_html("   <p>  x  </p>   "), "x")

    def test_named_entities_are_decoded(self):
        self.assertEqual(data._clean_html("Tata&nbsp;&amp;&nbsp;Sons"), "Tata & Sons")

    def test_angle_bracket_entities_are_decoded(self):
        self.assertEqual(data._clean_html("a &lt;b&gt; c"), "a <b> c")

    def test_numeric_entities_are_decoded_or_dropped(self):
        self.assertEqual(data._clean_html("a&#160;b"), "a b")
        self.assertEqual(data._clean_html("2024&#8211;25"), "2024–25")

    def test_unmapped_numeric_entities_are_removed(self):
        self.assertEqual(data._clean_html("a&#9731;b"), "ab")

    def test_empty_input(self):
        self.assertEqual(data._clean_html(""), "")

    def test_plain_text_passes_through(self):
        self.assertEqual(data._clean_html("Revenue grew 12%"), "Revenue grew 12%")

    def test_attributes_do_not_leak(self):
        cleaned = data._clean_html('<a href="https://x.com" class="y">Link</a>')
        self.assertEqual(cleaned, "Link")

    def test_script_and_style_bodies_survive_as_text(self):
        # The cleaner is tag-based, not a parser: inner text of <script> is kept.
        # Locking this in so a future switch to a real parser is deliberate.
        self.assertIn("var x", data._clean_html("<script>var x = 1;</script>"))

    def test_self_closing_tags(self):
        self.assertEqual(data._clean_html("a<br/>b"), "a b")

    def test_rupee_and_unicode_survive(self):
        self.assertEqual(data._clean_html("<p>₹3,050 — भारतीय</p>"), "₹3,050 — भारतीय")

    def test_multiline_table_markup(self):
        html = "<table>\n<tr><td>Revenue</td><td>1,000</td></tr>\n</table>"
        self.assertEqual(data._clean_html(html), "Revenue 1,000")


# ── yfinance DataFrame normalisation ─────────────────────────────────────────

class _FakeSeries:
    def __init__(self, mapping):
        self._mapping = mapping

    def items(self):
        return self._mapping.items()


class _FakeDataFrame:
    """The narrow slice of the pandas API that _yf_df_to_dict touches."""

    def __init__(self, columns):
        self._columns = columns
        self.empty = not columns

    @property
    def columns(self):
        return list(self._columns)

    def __getitem__(self, key):
        return _FakeSeries(self._columns[key])


class YfDataFrameTests(unittest.TestCase):
    def test_none_returns_empty_dict(self):
        self.assertEqual(data._yf_df_to_dict(None), {})

    def test_empty_frame_returns_empty_dict(self):
        self.assertEqual(data._yf_df_to_dict(_FakeDataFrame({})), {})

    def test_object_without_an_empty_attribute_is_treated_as_empty(self):
        self.assertEqual(data._yf_df_to_dict(object()), {})

    def test_numeric_values_become_floats(self):
        df = _FakeDataFrame({"2025-03-31": {"Total Revenue": 1000, "Net Income": 250.5}})
        got = data._yf_df_to_dict(df)
        self.assertEqual(got["2025-03-31"]["Total Revenue"], 1000.0)
        self.assertEqual(got["2025-03-31"]["Net Income"], 250.5)

    def test_nan_becomes_none(self):
        df = _FakeDataFrame({"2025": {"Missing": float("nan")}})
        self.assertIsNone(data._yf_df_to_dict(df)["2025"]["Missing"])

    def test_none_becomes_none(self):
        df = _FakeDataFrame({"2025": {"Missing": None}})
        self.assertIsNone(data._yf_df_to_dict(df)["2025"]["Missing"])

    def test_non_numeric_values_are_stringified(self):
        df = _FakeDataFrame({"2025": {"Note": "restated"}})
        self.assertEqual(data._yf_df_to_dict(df)["2025"]["Note"], "restated")

    def test_keys_are_stringified(self):
        import datetime
        df = _FakeDataFrame({datetime.date(2025, 3, 31): {"Revenue": 1}})
        self.assertIn("2025-03-31", data._yf_df_to_dict(df))

    def test_result_is_json_serialisable(self):
        import json
        df = _FakeDataFrame({"2025": {"a": 1, "b": float("nan"), "c": "text"}})
        json.dumps(data._yf_df_to_dict(df))

    def test_multiple_periods_are_preserved(self):
        df = _FakeDataFrame({"2025": {"Revenue": 1}, "2024": {"Revenue": 2}})
        self.assertEqual(set(data._yf_df_to_dict(df)), {"2025", "2024"})


class YfInfoGuardTests(unittest.TestCase):
    def test_returns_empty_dict_when_yfinance_is_absent(self):
        with patch.object(data, "YFINANCE_AVAILABLE", False):
            self.assertEqual(data.get_yf_info("RELIANCE.NS"), {})

    def test_financials_return_empty_dict_when_yfinance_is_absent(self):
        with patch.object(data, "YFINANCE_AVAILABLE", False):
            self.assertEqual(data.get_yf_financials("RELIANCE.NS"), {})


class PdfGuardTests(unittest.TestCase):
    def test_returns_empty_string_when_pypdf_is_absent(self):
        with patch.object(data, "PYPDF_AVAILABLE", False):
            self.assertEqual(data._extract_pdf_bytes(b"%PDF-1.4"), "")

    def test_corrupt_pdf_bytes_return_empty_string(self):
        self.assertEqual(data._extract_pdf_bytes(b"not a pdf at all"), "")


# ── Google News primary source ───────────────────────────────────────────────

def article(title, source="Reuters"):
    return {"title": title, "source": source, "content": "body",
            "url": "https://example.com", "published_at": "2025-08-06"}


class _FakeSearchResult:
    def __init__(self, articles, errors=None):
        self._articles = articles
        self.errors = errors or []

    def to_pipeline_articles(self):
        return self._articles


class GoogleNewsFetchTests(unittest.TestCase):
    def test_empty_queries_short_circuit(self):
        arts, errors = data.fetch_google_news_articles([])
        self.assertEqual(arts, [])
        self.assertEqual(errors, ["no queries"])

    def test_blank_queries_are_filtered_out(self):
        arts, errors = data.fetch_google_news_articles(["  ", "", None])
        self.assertEqual(errors, ["no queries"])

    def test_client_failure_is_reported_not_raised(self):
        with patch("gnews.GoogleNewsClient", side_effect=RuntimeError("boom")):
            arts, errors = data.fetch_google_news_articles(["crude"])
        self.assertEqual(arts, [])
        self.assertTrue(errors)

    def test_articles_and_errors_are_both_returned(self):
        result = _FakeSearchResult([article("A")], errors=["one query failed"])
        with patch("gnews.GoogleNewsClient") as client:
            client.return_value.search_many.return_value = result
            arts, errors = data.fetch_google_news_articles(["crude"])
        self.assertEqual(len(arts), 1)
        self.assertIn("one query failed", errors)

    def test_client_is_configured_for_india(self):
        with patch("gnews.GoogleNewsClient") as client:
            client.return_value.search_many.return_value = _FakeSearchResult([])
            data.fetch_google_news_articles(["crude"])
        self.assertEqual(client.call_args.kwargs, {"language": "en", "region": "IN"})


# ── The news fallback ladder ─────────────────────────────────────────────────

class Stage2FallbackTests(unittest.TestCase):
    def setUp(self):
        self._env = isolated_env()
        self._env.start()
        self.addCleanup(self._env.stop)

    def run_fetch(self, gnews=None, gnews_errors=None, newsapi=None, wsj=None,
                  env=None):
        gnews_patch = patch.object(
            data, "fetch_google_news_articles",
            return_value=(gnews or [], gnews_errors or []))
        newsapi_patch = patch.object(
            data, "_fetch_newsapi_articles",
            side_effect=AssertionError("NewsAPI must not be called")
            if newsapi is None else None,
            return_value=newsapi)
        wsj_patch = patch.object(
            data, "_fetch_wsj_articles",
            side_effect=AssertionError("WSJ must not be called")
            if wsj is None else None,
            return_value=wsj)
        with patch.dict(os.environ, env or {}), gnews_patch, \
                newsapi_patch as m_news, wsj_patch as m_wsj:
            arts, errors = data.fetch_stage2_articles(["crude", "tariffs"])
        return arts, errors, m_news, m_wsj

    def test_enough_google_news_articles_skips_every_fallback(self):
        gnews = [article(f"T{i}") for i in range(6)]
        arts, errors, m_news, m_wsj = self.run_fetch(
            gnews=gnews, env={"NEWSAPI_KEY": "k", "RAPIDAPI_KEY": "k"})
        self.assertEqual(len(arts), 6)
        m_news.assert_not_called()
        m_wsj.assert_not_called()

    def test_thin_google_news_falls_back_to_newsapi(self):
        arts, _, m_news, _ = self.run_fetch(
            gnews=[article("T0")],
            newsapi=[article("N1"), article("N2")],
            wsj=[],
            env={"NEWSAPI_KEY": "k"})
        titles = {a["title"] for a in arts}
        self.assertEqual(titles, {"T0", "N1", "N2"})
        self.assertTrue(m_news.called)

    def test_newsapi_is_skipped_without_a_key(self):
        arts, _, m_news, _ = self.run_fetch(gnews=[article("T0")], env={})
        self.assertEqual(len(arts), 1)
        m_news.assert_not_called()

    def test_wsj_is_consulted_when_still_thin(self):
        arts, _, _, m_wsj = self.run_fetch(
            gnews=[article("T0")], newsapi=[], wsj=[article("W1")],
            env={"NEWSAPI_KEY": "k", "RAPIDAPI_KEY": "k"})
        self.assertIn("W1", {a["title"] for a in arts})
        self.assertTrue(m_wsj.called)

    def test_wsj_is_skipped_once_the_minimum_is_met(self):
        arts, _, _, m_wsj = self.run_fetch(
            gnews=[article("T0")],
            newsapi=[article(f"N{i}") for i in range(5)],
            env={"NEWSAPI_KEY": "k", "RAPIDAPI_KEY": "k"})
        self.assertEqual(len(arts), 6)
        m_wsj.assert_not_called()

    def test_duplicate_titles_are_collapsed_case_insensitively(self):
        arts, _, _, _ = self.run_fetch(
            gnews=[article("Red Sea shipping disrupted"),
                   article("RED SEA SHIPPING DISRUPTED"),
                   article("  red sea shipping disrupted  ")])
        self.assertEqual(len(arts), 1)

    def test_first_occurrence_of_a_duplicate_wins(self):
        arts, _, _, _ = self.run_fetch(
            gnews=[article("Same", source="Reuters"), article("SAME", source="PTI")])
        self.assertEqual(arts[0]["source"], "Reuters")

    def test_untitled_google_news_articles_are_dropped(self):
        arts, _, _, _ = self.run_fetch(gnews=[article(""), article("Real")])
        self.assertEqual([a["title"] for a in arts], ["Real"])

    def test_google_news_errors_are_propagated(self):
        _, errors, _, _ = self.run_fetch(gnews=[article("T")],
                                         gnews_errors=["rss timeout"])
        self.assertIn("rss timeout", errors)

    def test_a_failing_fallback_does_not_abort_the_fetch(self):
        with patch.object(data, "fetch_google_news_articles",
                          return_value=([article("T0")], [])), \
             patch.object(data, "_fetch_newsapi_articles",
                          side_effect=RuntimeError("newsapi 500")), \
             patch.dict(os.environ, {"NEWSAPI_KEY": "k"}):
            arts, errors = data.fetch_stage2_articles(["crude"])
        self.assertEqual(len(arts), 1)
        self.assertTrue(any("newsapi 500" in e for e in errors))

    def test_all_sources_empty_returns_empty_without_raising(self):
        arts, errors, _, _ = self.run_fetch(gnews=[], newsapi=[], wsj=[],
                                            env={"NEWSAPI_KEY": "k", "RAPIDAPI_KEY": "k"})
        self.assertEqual(arts, [])

    def test_minimum_article_threshold_is_five(self):
        self.assertEqual(data._MIN_ARTICLES, 5)


class GeoAlertIntegrationTests(unittest.TestCase):
    def test_missing_geo_alerts_module_degrades_to_empty(self):
        with patch.dict("sys.modules", {"geo_alerts": None}):
            self.assertEqual(data.recent_geo_alerts_for_ticker("RELIANCE"), [])


if __name__ == "__main__":
    unittest.main()
