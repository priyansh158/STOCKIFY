"""Offline unit tests for gnews (no network)."""
from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from gnews.client import GoogleNewsClient, _dedupe
from gnews.decode import extract_article_id
from gnews.models import Article, SearchResult
from gnews.rss import build_search_url, locale_to_params, parse_rss_xml
from gnews.slicing import compose_query, resolve_windows, should_day_slice
from tests.gnews.fixtures import SAMPLE_RSS


class LocaleTests(unittest.TestCase):
    def test_india_defaults(self):
        hl, gl, ceid = locale_to_params("en", "IN")
        self.assertEqual(gl, "IN")
        self.assertEqual(ceid, "IN:en")
        self.assertIn("en", hl)


class RssParseTests(unittest.TestCase):
    def test_parse_and_strip_html(self):
        arts = parse_rss_xml(SAMPLE_RSS, query="RBI")
        self.assertEqual(len(arts), 3)  # includes duplicate before dedupe
        self.assertEqual(arts[0].source, "The Economic Times")
        self.assertTrue(arts[0].published_at.endswith("Z") or "2025" in arts[0].published_at)
        self.assertEqual(arts[0].query, "RBI")
        self.assertNotIn("<", arts[0].description)

    def test_build_search_url(self):
        url = build_search_url("RBI repo", language="en", region="IN")
        self.assertIn("news.google.com/rss/search", url)
        self.assertIn("gl=IN", url)
        self.assertIn("q=RBI", url)

    def test_pipeline_dict(self):
        arts = parse_rss_xml(SAMPLE_RSS)
        d = arts[0].to_pipeline_dict()
        self.assertEqual(set(d.keys()), {"title", "source", "content", "url", "published_at"})
        self.assertEqual(d["url"], arts[0].link)


class SlicingTests(unittest.TestCase):
    def test_when_appended(self):
        qs = resolve_windows("RBI", max_items=50, open_ended="1d")
        self.assertEqual(len(qs), 1)
        self.assertIn("when:1d", qs[0])

    def test_day_slice_for_large_max(self):
        self.assertTrue(
            should_day_slice(max_items=150, open_ended="7d", date_from=None, date_to=None)
        )
        qs = resolve_windows(
            "RBI",
            max_items=150,
            open_ended="3d",
            today=date(2025, 8, 6),
        )
        # 3 calendar days → 3 windows
        self.assertEqual(len(qs), 3)
        self.assertTrue(all("after:" in q and "before:" in q for q in qs))
        self.assertNotIn("when:", qs[0])

    def test_compose_query(self):
        q = compose_query("TCS", after=date(2025, 8, 1), before=date(2025, 8, 2))
        self.assertEqual(q, "TCS after:2025-08-01 before:2025-08-02")


class DecodeHelperTests(unittest.TestCase):
    def test_extract_article_id(self):
        url = "https://news.google.com/rss/articles/CBMiTestGuidAAA?oc=5"
        self.assertEqual(extract_article_id(url), "CBMiTestGuidAAA")
        self.assertIsNone(extract_article_id("https://reuters.com/article/1"))


class ClientDedupeTests(unittest.TestCase):
    def test_dedupe_by_guid(self):
        arts = parse_rss_xml(SAMPLE_RSS)
        unique = _dedupe(arts)
        self.assertEqual(len(unique), 2)

    @patch("gnews.client.fetch_and_parse")
    def test_search_uses_windows(self, mock_fetch):
        mock_fetch.return_value = parse_rss_xml(SAMPLE_RSS, query="RBI")[:2]
        client = GoogleNewsClient(session=MagicMock())
        result = client.search("RBI", max_items=10, open_ended="1d")
        self.assertEqual(result.unique_count, 2)
        self.assertEqual(len(result.to_pipeline_articles()), 2)
        mock_fetch.assert_called()

    def test_search_many_accepts_a_generator_without_exhausting_it(self):
        client = GoogleNewsClient(session=MagicMock())
        article = Article(
            title="RBI update",
            link="https://example.com/a",
            guid="g1",
            source="Reuters",
            published_at="2025-08-06",
        )
        with patch.object(
            client,
            "search",
            return_value=SearchResult(
                articles=[article],
                query="RBI",
                windows_fetched=1,
                raw_count=1,
                unique_count=1,
            ),
        ) as search:
            result = client.search_many((q for q in ("RBI", "crude")))
        self.assertEqual(search.call_count, 2)
        self.assertEqual(result.query, "RBI | crude")
        self.assertEqual(result.unique_count, 1)


if __name__ == "__main__":
    unittest.main()
