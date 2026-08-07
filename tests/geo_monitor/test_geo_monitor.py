"""Unit tests for geo monitor helpers (no network / no Gemini)."""
from __future__ import annotations

import unittest

from geo_monitor.gemini_classifier import heuristic_suspect, should_alert
from geo_monitor.queries import build_monitor_queries, holding_queries


class QueryTests(unittest.TestCase):
    def test_macro_included(self):
        qs = build_monitor_queries([], include_macro=True)
        self.assertGreater(len(qs), 5)
        self.assertTrue(any("RBI" in q for q in qs))

    def test_holdings(self):
        qs = holding_queries(
            [{"tradingsymbol": "TCS", "name": "Tata Consultancy Services"}]
        )
        self.assertEqual(len(qs), 1)
        self.assertIn("TCS", qs[0])


class HeuristicTests(unittest.TestCase):
    def test_disruptive_title(self):
        self.assertTrue(heuristic_suspect("RBI surprises with emergency rate hike"))
        self.assertFalse(heuristic_suspect("Company announces routine AGM date"))

    def test_holding_term(self):
        self.assertTrue(
            heuristic_suspect("TCS wins mega deal", holding_terms=["TCS"])
        )

    def test_should_alert(self):
        self.assertTrue(should_alert({"severity": "HIGH", "disruption_score": 40}))
        self.assertTrue(should_alert({"severity": "LOW", "disruption_score": 75}))
        self.assertFalse(should_alert({"severity": "LOW", "disruption_score": 20}))


if __name__ == "__main__":
    unittest.main()
