"""Tests for Google-News-first stage-2 fetch helpers (mocked)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from server.services import data


class FetchStage2Tests(unittest.TestCase):
    @patch("server.services.data.fetch_google_news_articles")
    def test_gnews_primary_skips_newsapi_when_enough(self, mock_gnews):
        arts = [
            {"title": f"Story {i}", "source": "X", "content": "c", "url": "u", "published_at": ""}
            for i in range(6)
        ]
        mock_gnews.return_value = (arts, [])
        with patch.object(data, "_resolve_newsapi_key", return_value="fake"), patch.object(
            data, "_fetch_newsapi_articles"
        ) as mock_newsapi:
            out, errs = data.fetch_stage2_articles(["RBI"])
            self.assertEqual(len(out), 6)
            self.assertEqual(errs, [])
            mock_newsapi.assert_not_called()

    def test_recent_alerts_empty_without_db_rows(self):
        with patch("geo_alerts.list_alerts", return_value=[]):
            with patch("geo_alerts.init_db"):
                self.assertEqual(data.recent_geo_alerts_for_ticker("TCS"), [])


if __name__ == "__main__":
    unittest.main()
