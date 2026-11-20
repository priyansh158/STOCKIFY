"""CW 2: a Kite index row reaches the search box as an INDEX, so the watchlist's add box can leave it out."""
from __future__ import annotations

import unittest

import tests.support as support  # noqa: F401  — must precede project imports


@support.requires_fastapi
class SearchHitTypeTests(unittest.TestCase):
    def test_index_rows_are_typed_index_and_shares_stay_eq(self):
        from server.routers import search
        hits = search._kite_rows_to_hits([
            {"tradingsymbol": "NIFTY 50", "exchange": "NSE", "segment": "INDICES", "instrument_type": "EQ"},
            {"tradingsymbol": "INFY", "exchange": "NSE", "segment": "NSE", "instrument_type": "EQ"}])
        self.assertEqual([(h.ticker, h.instrument_type) for h in hits], [("NIFTY 50", "INDEX"), ("INFY", "EQ")])


if __name__ == "__main__":
    unittest.main()
