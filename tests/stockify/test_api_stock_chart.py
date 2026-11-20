"""CW 4: GET /api/stocks/{symbol}/chart (a year of daily candles with the 52-week high and low lines), its cache and
its guards. Offline: FastAPI's TestClient, a sandboxed lab, and a Kite fake that records every call (there must be none)."""
from __future__ import annotations

import unittest
from datetime import timedelta
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest
from tests.stockify.watchlist.test_api import AFTER_CLOSE, DAY, FakeKite, install, put_bars

import kite_client
from stockify import kite_readonly
from stockify.data import history, refdata


@support.requires_fastapi
class StockChartTests(SandboxedDBTest):
    db_modules = (refdata, history)

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        from server.routers import stocks as router
        self.router = router
        router.reset()
        self.addCleanup(router.reset)
        self.client = TestClient(app, base_url=support.API_BASE_URL)
        self.ids = install()
        self.kite = FakeKite()
        for p in (mock.patch.object(router, "now_ns", lambda: AFTER_CLOSE),
                  mock.patch.object(kite_client, "is_authenticated", lambda: True),
                  mock.patch.object(kite_readonly, "client", lambda: self.kite)):
            p.start()
            self.addCleanup(p.stop)

    def get(self, sym="INFY", status=200):
        r = self.client.get(f"/api/stocks/{sym}/chart")
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def test_a_year_of_candles_with_the_52_week_lines(self):
        put_bars(self.ids["INFY"], DAY - timedelta(days=420), 420, spike=(300, 190_000, 60_000))
        body = self.get("infy")
        from server import schemas
        schemas.RunChartDTO.model_validate(body)
        self.assertEqual((body["data_state"], body["interval_s"], body["markers"]), ("ok", 86_400, []))
        self.assertEqual(body["instruments"][0]["symbol"], "INFY")
        self.assertEqual(body["bars"][-1]["d"], "2026-11-16")
        self.assertEqual(len(body["bars"]), 420)             # under 3 years of history: the chart shows all of it
        plots = {p["label"]: p for p in body["plots"]}
        self.assertEqual(set(plots), {"52-week high", "52-week low"})
        self.assertEqual({pt["value"] for pt in plots["52-week high"]["points"]}, {"1900.00"})
        self.assertEqual({pt["value"] for pt in plots["52-week low"]["points"]}, {"600.00"})
        self.assertEqual(len(plots["52-week high"]["points"]), len(body["bars"]))
        self.assertEqual(self.kite.calls, [])

    def test_three_years_of_candles_with_the_lines_still_the_last_52_weeks(self):
        put_bars(self.ids["INFY"], DAY - timedelta(days=1400), 1400, spike=(1280, 190_000, 60_000))   # ~4 months back
        # and a bigger spike ~2.5 years back: on the chart, but outside the 52-week range
        from stockify import timeutil
        from stockify.data import history as _h
        with _h._Tx() as conn:
            conn.execute("UPDATE bars SET high_paise = 500000 WHERE instrument_id = ? AND ts_ns = ?",
                         (self.ids["INFY"], timeutil.ist_ns(DAY - timedelta(days=900), "00:00")))
        body = self.get("INFY")
        first = body["bars"][0]["d"]
        self.assertGreaterEqual(first, (DAY - timedelta(days=1095)).isoformat())     # chart_days: about 3 years shown
        self.assertLessEqual(first, (DAY - timedelta(days=1080)).isoformat())
        self.assertGreater(len(body["bars"]), 1000)
        plots = {p["label"]: p for p in body["plots"]}
        self.assertEqual({pt["value"] for pt in plots["52-week high"]["points"]}, {"1900.00"})   # the range is still 365 days
        self.assertEqual(max(float(b["high"]) for b in body["bars"]), 5000.0)       # the older, higher spike is on the chart
        self.assertEqual(len(plots["52-week high"]["points"]), len(body["bars"]))

    def test_the_lines_are_the_watchlists_range(self):
        from stockify.watchlist import ranges
        put_bars(self.ids["TCS"], DAY - timedelta(days=420), 420, spike=(100, 300_000, 10_000))
        want = ranges.prior_range(history.bars(self.ids["TCS"], "day"), DAY, 365, 365)
        body = self.get("TCS")
        by = {p["name"]: p["points"][0]["value"] for p in body["plots"]}
        self.assertEqual(by["high52"], f"{want['high_paise'] / 100:.2f}")
        self.assertEqual(by["low52"], f"{want['low_paise'] / 100:.2f}")

    def test_short_history_has_candles_and_a_note_but_no_lines(self):
        put_bars(self.ids["NEWCO"], DAY - timedelta(days=100), 100)
        body = self.get("NEWCO")
        self.assertEqual((body["data_state"], body["plots"]), ("ok", []))
        self.assertTrue(body["bars"])
        self.assertEqual(body["notice"], "Less than a year of daily prices, so no 52-week range yet")

    def test_a_stock_whose_last_candle_is_over_a_year_old_says_so(self):
        put_bars(self.ids["TCS"], DAY - timedelta(days=900), 420)                 # the last candle is about 480 days back
        body = self.get("TCS")
        self.assertEqual((body["data_state"], body["plots"]), ("ok", []))
        self.assertEqual(body["notice"], "No daily prices in the last year")

    def test_no_bars_is_no_data_with_the_plain_notice(self):
        body = self.get("INFY")
        self.assertEqual((body["data_state"], body["bars"], body["plots"]), ("no_data", [], []))
        self.assertEqual(body["notice"], "No daily prices yet: they are fetched each evening for the screener's stocks "
                                         "and your watchlist.")

    def test_unknown_or_malformed_symbols_are_404(self):
        self.get("NOSUCH", 404)
        self.get("bad%20name!", 404)

    def test_cached_until_the_candles_change(self):
        iid = self.ids["INFY"]
        put_bars(iid, DAY - timedelta(days=420), 420, spike=(300, 190_000, 60_000))
        with mock.patch.object(self.router, "_build", wraps=self.router._build) as build:
            first = self.get()
            self.assertEqual(self.get(), first)
            self.assertEqual(build.call_count, 1)                                   # the second is cached
            # a split or bonus refetch rewrites the older candles (adjusted): the first close moves
            with history._Tx() as conn:
                conn.execute("UPDATE bars SET close_paise=close_paise/2 WHERE instrument_id=? AND ts_ns=("
                             "SELECT MIN(ts_ns) FROM bars WHERE instrument_id=?)", (iid, iid))
            self.get()
            self.assertEqual(build.call_count, 2)
            put_bars(iid, DAY, 1, close=105_000)                                    # one more daily candle
            self.assertEqual(self.get()["bars"][-1]["d"], DAY.isoformat())
            self.assertEqual(build.call_count, 3)

    def test_a_fetch_after_no_data_replaces_the_notice(self):
        self.assertEqual(self.get()["data_state"], "no_data")
        put_bars(self.ids["INFY"], DAY - timedelta(days=420), 420)
        self.assertEqual(self.get()["data_state"], "ok")

    def test_local_only_guards_apply(self):
        """The router is behind the app's guards: a foreign Host or a forwarded foreign host is refused, a write
        method is not served (a GET-only route), and a read from the web origin is."""
        r = self.client.get("/api/stocks/INFY/chart", headers={"Host": "evil.example"})
        self.assertIn(r.status_code, (400, 403))
        r = self.client.get("/api/stocks/INFY/chart", headers={"X-Forwarded-Host": "evil.example"})
        self.assertIn(r.status_code, (400, 403))
        r = self.client.post("/api/stocks/INFY/chart", headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.get("/api/stocks/INFY/chart").status_code, 200)


if __name__ == "__main__":
    unittest.main()
