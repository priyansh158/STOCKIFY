"""FS 9: /api/fundamentals (the ordered list with the results coming up, one stock's details, the test record).

Offline: FastAPI's TestClient over a sandboxed fundamentals store and refdata, filled as the screen tests fill them
(screen_support), the list saved as the daily job saves it."""
from __future__ import annotations

import json
import statistics
import unittest
from datetime import date, timedelta
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.fundamentals.screen_support import CR, DAY, ScreenCase

from stockify import timeutil
from stockify.fundamentals import filings, pit, screen, store
from stockify.data import calendar as nse_calendar
from stockify.data import refdata

FORBIDDEN = ("score", "percentile", "composite", "rank", "place")
NOTES = ["Covers today's listed companies only.", "Sectors are today's NSE industry groups."]
REVENUES = {"AAA": 900, "BBB": 1000, "CCC": 1100, "DDD": 1200, "EEE": 1300}


def _keys(obj, out=None):  # noqa: ANN001
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            _keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _keys(v, out)
    return out


@support.requires_fastapi
class FundamentalsApiBase(ScreenCase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        from server.routers import fundamentals as router
        self.client = TestClient(app, base_url=support.API_BASE_URL)
        p = mock.patch.object(router, "now_ns", side_effect=lambda: timeutil.ist_ns(DAY, "20:00"))
        p.start()
        self.addCleanup(p.stop)
        for k, (sym, revenue) in enumerate(REVENUES.items()):
            self.company(sym, "Capital Goods", revenue=revenue, op=300 + 10 * k, profit=250 + 20 * k,
                         debt=1500 - 200 * k)
        self.company("TEX", "Textiles", profit=300)
        self.prices_on_usable_days()
        # On the list, but no results read: its one filing is held.
        self.universe.append({"symbol": "HELD", "instrument_id": None, "isin": "INE000HELD1", "company": "Held Ltd.",
                              "median_value_paise": 9 * CR, "liquid": 1, "list_industry": "Capital Goods"})
        # On the Nifty 500 list but not liquid: outside the universe.
        self.universe.append({"symbol": "THIN", "instrument_id": None, "isin": "INE000THIN1", "company": "Thin Ltd.",
                              "median_value_paise": CR, "liquid": 0, "list_industry": "Capital Goods"})
        store.put_universe("2026-07-31", self.universe)
        fid = self.put("HELD", "2025-09-30", {})
        filings.mark(fid, "held", "the figures do not add up: total income differs from its parts")
        screen.save(DAY, screen.order(DAY))
        conn = store._connect()
        conn.executemany(
            "INSERT INTO results_calendar (symbol, meeting_date, purpose, period_hint, read_on) VALUES (?, ?, ?, ?, ?)",
            [("BBB", "2025-12-05", "Financial Results", "Oct–Dec quarter", "2025-12-01"),
             ("AAA", "2025-12-03", "Financial Results", "Oct–Dec quarter", "2025-12-01"),
             ("CCC", "2025-11-20", "Financial Results", "Jul–Sep quarter", "2025-11-18"),     # past
             ("DDD", "2026-02-10", "Financial Results", "Oct–Dec quarter", "2025-12-01")])    # beyond 28 days

    def prices_on_usable_days(self):
        """A close at each period end and on each filing's usable day, so the P/E and P/B of each quarter can be
        drawn (the share count needs a close near the period end to rule out a split)."""
        conn = refdata._connect()
        for r in store._connect().execute("SELECT symbol, period_end, broadcast_ns FROM filings").fetchall():
            iid = pit._instrument(r["symbol"])
            if iid is None:
                continue
            end = date.fromisoformat(r["period_end"]) + timedelta(days=1)
            for d in (nse_calendar.previous_trading_day("CM", end), pit.usable_day(r["broadcast_ns"])):
                conn.execute("INSERT OR IGNORE INTO cm_eod (trade_date, instrument_id, series, close_paise, "
                             "prev_close_paise, volume, turnover_paise) VALUES (?, ?, 'EQ', 50000, 0, 1, 1)",
                             (d.isoformat(), iid))

    def get(self, path: str, status: int = 200) -> dict:
        r = self.client.get(path)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()


class ListTests(FundamentalsApiBase):

    def test_list_shape_and_order(self):
        body = self.get("/api/fundamentals")
        _, rows = screen.latest()
        self.assertEqual(body["as_of"], DAY.isoformat())
        self.assertEqual([i["symbol"] for i in body["items"]], [r["symbol"] for r in rows])
        self.assertEqual([i["position"] for i in body["items"]], list(range(1, len(rows) + 1)))
        first = body["items"][0]
        self.assertEqual(set(first), {"position", "symbol", "company", "sector", "short_reason"})
        self.assertEqual(first["company"], f"{first['symbol']} Ltd.")
        self.assertEqual(first["short_reason"], rows[0]["short_reason"])
        self.assertEqual({i["symbol"]: i["sector"] for i in body["items"]}["TEX"], "Textiles")
        # Upcoming: today through 28 days, by date; past and later meetings left out.
        self.assertEqual([(u["symbol"], u["date"]) for u in body["upcoming"]],
                         [("AAA", "2025-12-03"), ("BBB", "2025-12-05")])
        self.assertEqual(body["upcoming"][0], {"symbol": "AAA", "company": "AAA Ltd.", "date": "2025-12-03",
                                               "period_hint": "Oct–Dec quarter"})
        self.assertEqual(body["held_count"], 1)                 # HELD: in the universe, not on the list
        self.assertEqual(body["not_listed_count"], 0)           # every placed stock is listed
        self.assertIsNone(body["test_record_id"])
        self.assertEqual(body["notes"], NOTES)

    def test_before_the_first_screen_the_list_is_empty_not_an_error(self):
        store._connect().execute("DELETE FROM screen_runs")
        body = self.get("/api/fundamentals")
        self.assertIsNone(body["as_of"])
        self.assertEqual(body["items"], [])
        self.assertEqual(body["held_count"], 7)
        self.assertEqual(body["not_listed_count"], 0)
        self.assertEqual(len(body["upcoming"]), 2)

    def test_the_test_record_id_comes_from_the_store(self):
        with mock.patch.object(store, "latest_test_record", return_value={"job_id": "wf_abc"}):
            self.assertEqual(self.get("/api/fundamentals")["test_record_id"], "wf_abc")


class ShortListTests(FundamentalsApiBase):
    """Ruling R15: the page lists at most the best `top_kept` placed stocks and none with a red-flag warning
    (loss-making, negative equity); those stay reachable by their details page."""

    def test_only_the_best_top_kept_are_listed(self):
        from server.routers import fundamentals as router
        _, rows = screen.latest()
        with mock.patch.object(router, "_top_kept", return_value=3):
            body = self.get("/api/fundamentals")
        self.assertEqual([i["symbol"] for i in body["items"]], [r["symbol"] for r in rows[:3]])
        self.assertEqual([i["position"] for i in body["items"]], [1, 2, 3])
        self.assertEqual(body["not_listed_count"], len(rows) - 3)
        self.assertEqual(body["held_count"], 1)
        last = rows[-1]["symbol"]                                # below the cut: still has its details page
        detail = self.get(f"/api/fundamentals/{last}")
        self.assertIsNone(detail["left_out_reason"])
        self.assertTrue(detail["reasons"])

    def test_red_flags_are_dropped_before_the_cut_so_the_list_fills_with_clean_names(self):
        # Ruling R15a: drop the red-flag rows first, then keep the best top_kept, so red flags near the top
        # never shorten the list while enough clean stocks exist.
        from server.routers import fundamentals as router
        conn = store._connect()
        _, rows = screen.latest()
        self.assertGreaterEqual(len(rows), 5)
        for r in rows[:2]:                                       # the two best-placed carry a red flag
            r["red_flags"] = ["loss-making"]
            conn.execute("UPDATE screen_runs SET json = ? WHERE day = ? AND symbol = ?",
                         (json.dumps(r, sort_keys=True, ensure_ascii=False), DAY.isoformat(), r["symbol"]))
        with mock.patch.object(router, "_top_kept", return_value=3):
            body = self.get("/api/fundamentals")
        self.assertEqual([i["symbol"] for i in body["items"]], [r["symbol"] for r in rows[2:5]])
        self.assertEqual([i["position"] for i in body["items"]], [1, 2, 3])
        self.assertEqual(body["not_listed_count"], len(rows) - 3)

    def test_red_flagged_stocks_are_left_off_the_list_but_keep_their_details(self):
        self.company("LOSS", "Capital Goods", profit=-50, prior=-40, debt=0)
        self.company("NEGEQ", "Capital Goods", equity=-500, equity_prev=-400)
        self.prices_on_usable_days()
        screen.save(DAY, screen.order(DAY))
        _, rows = screen.latest()
        flagged = {r["symbol"] for r in rows if r["red_flags"]}
        self.assertEqual(flagged, {"LOSS", "NEGEQ"})
        body = self.get("/api/fundamentals")
        self.assertEqual([i["symbol"] for i in body["items"]],
                         [r["symbol"] for r in rows if r["symbol"] not in flagged])
        self.assertEqual([i["position"] for i in body["items"]], list(range(1, len(rows) - 1)))
        self.assertEqual(body["not_listed_count"], 2)
        loss = self.get("/api/fundamentals/LOSS")
        self.assertIsNone(loss["left_out_reason"])
        self.assertTrue(loss["reasons"][0].startswith("Loss-making"), loss["reasons"])
        self.assertTrue(loss["ratios"])
        neg = self.get("/api/fundamentals/NEGEQ")
        self.assertIn("Negative equity", neg["reasons"][0])
        self.assertNotIn("count against", neg["reasons"][0])     # the fact, not the method


class DetailTests(FundamentalsApiBase):

    def test_detail_series_and_filings(self):
        body = self.get("/api/fundamentals/bbb")
        _, rows = screen.latest()
        row = next(r for r in rows if r["symbol"] == "BBB")
        self.assertEqual((body["symbol"], body["company"], body["sector"]), ("BBB", "BBB Ltd.", "Capital Goods"))
        self.assertEqual(body["peer_group"], "Industry: Capital Goods")
        self.assertIsNone(body["left_out_reason"])
        self.assertEqual(body["reasons"], row["reasons"])
        self.assertEqual(body["as_of"], DAY.isoformat())
        ratios = {r["key"]: r for r in body["ratios"]}
        self.assertEqual(list(ratios), ["pe", "pb", "roe", "debt_equity", "op_margin", "profit_growth"])
        self.assertAlmostEqual(ratios["pe"]["value"], row["ratios"]["pe"])
        self.assertAlmostEqual(ratios["pe"]["sector_median"], row["sector_medians"]["pe"])
        self.assertEqual((ratios["pe"]["label"], ratios["pe"]["unit"]), ("P/E", "times"))
        self.assertEqual(ratios["roe"]["unit"], "fraction")

        series = {s["key"]: s for s in body["series"]}
        self.assertEqual(list(series), ["revenue", "net_profit", "op_margin", "roe", "debt_equity", "pe", "pb"])
        rev = series["revenue"]
        self.assertEqual(rev["unit"], "paise")
        ends = [pit.shift_quarter(date(2025, 9, 30), -k).isoformat() for k in range(4, -1, -1)]
        self.assertEqual([p["period_end"] for p in rev["points"]], ends)          # oldest first
        self.assertTrue(all(p["value"] == 1000 * CR for p in rev["points"]))
        self.assertTrue(all(p["sector_median"] == 1100 * CR for p in rev["points"]))   # 900 .. 1300: median 1100
        # TTM figures need four consecutive quarters: the newest two quarter ends only.
        self.assertEqual([p["period_end"] for p in series["pe"]["points"]], ends[3:])
        pe_now = series["pe"]["points"][-1]
        self.assertAlmostEqual(pe_now["value"], 50000 * 1_000_000_000 / ((250 + 20) * 4 * CR))
        medians = [50000 * 1_000_000_000 / ((250 + 20 * k) * 4 * CR) for k in range(5)]
        self.assertAlmostEqual(pe_now["sector_median"], statistics.median(medians))
        # Debt to equity on the balance-sheet quarters (Sep and Mar) only.
        self.assertEqual([p["period_end"] for p in series["debt_equity"]["points"]],
                         [e for e in ends if e[5:7] in ("03", "09")])

        self.assertEqual(len(body["filings"]), 5)
        f = body["filings"][0]                                                     # newest first
        self.assertEqual(set(f), {"period_end", "period_kind", "basis", "broadcast_ist", "xbrl_url", "status",
                                  "reason"})
        self.assertEqual((f["period_end"], f["period_kind"], f["basis"], f["status"]),
                         ("2025-09-30", "Q", "consolidated", "parsed"))
        self.assertEqual(f["broadcast_ist"], "2025-11-09 20:00")
        self.assertTrue(f["xbrl_url"].startswith("https://nsearchives.nseindia.in/"))

    def test_a_held_stock_shows_its_filings_and_why_it_is_not_listed(self):
        body = self.get("/api/fundamentals/HELD")
        self.assertEqual(body["left_out_reason"], "its results could not be read (held back: the figures do not add "
                                                  "up: total income differs from its parts)")
        self.assertIsNone(body["peer_group"])
        self.assertEqual(body["reasons"], [])                    # the reason is said once, in left_out_reason
        self.assertEqual(body["series"], [])
        self.assertEqual([(f["status"], f["reason"]) for f in body["filings"]],
                         [("held", "the figures do not add up: total income differs from its parts")])

    def test_a_listed_stock_in_a_small_industry_is_compared_with_all(self):
        body = self.get("/api/fundamentals/TEX")
        self.assertEqual(body["peer_group"], "All stocks (Textiles has too few)")
        rev = next(s for s in body["series"] if s["key"] == "revenue")
        # 900, 1000, 1000, 1100, 1200, 1300: the middle two's mean, in whole paise
        self.assertEqual(rev["points"][-1]["sector_median"], 1050 * CR)

    def test_a_listed_stocks_detail_reads_the_saved_series(self):
        expected = self.get("/api/fundamentals/BBB")["series"]
        calls = []
        real_known, real_mcap = pit.known_on, pit.market_cap
        with mock.patch.object(pit, "known_on", side_effect=lambda *a, **k: calls.append("known_on")
                               or real_known(*a, **k)),                 mock.patch.object(pit, "market_cap", side_effect=lambda *a, **k: calls.append("market_cap")
                                  or real_mcap(*a, **k)):
            body = self.get("/api/fundamentals/BBB")
        self.assertEqual(calls, [])                             # no per-peer point-in-time reads on a click
        self.assertEqual(body["series"], expected)
        saved = store._connect().execute("SELECT symbol FROM screen_series WHERE day = ? ORDER BY symbol",
                                         (DAY.isoformat(),)).fetchall()
        self.assertEqual([r["symbol"] for r in saved], sorted(r["symbol"] for r in screen.latest()[1]))

    def test_a_newer_universe_does_not_drop_a_listed_stock(self):
        newer = [u for u in self.universe if u["symbol"] != "BBB"]
        store.put_universe("2026-08-07", newer)
        body = self.get("/api/fundamentals/BBB")
        self.assertEqual(body["company"], "BBB Ltd.")
        listing = self.get("/api/fundamentals")
        self.assertEqual({i["symbol"]: i["company"] for i in listing["items"]}["BBB"], "BBB Ltd.")
        self.assertEqual(listing["held_count"], 1)              # counted against the universe the screen used

    def test_unknown_symbol_404(self):
        for sym in ("NOPE", "THIN", "bad%20sym", "A" * 41):
            self.get(f"/api/fundamentals/{sym}", status=404)

    def test_a_non_nse_link_is_never_passed_on(self):
        store._connect().execute("UPDATE filings SET xbrl_url = 'javascript:alert(1)' WHERE symbol = 'BBB'")
        body = self.get("/api/fundamentals/BBB")
        self.assertTrue(all(f["xbrl_url"] is None for f in body["filings"]))


class TestRecordTests(FundamentalsApiBase):

    def test_no_test_record_yet(self):
        body = self.get("/api/fundamentals/test-record")
        self.assertFalse(body["available"])
        self.assertIsNone(body["summary"])
        self.assertEqual(body["message"], "The test has not been run yet.")
        self.assertEqual(body["notes"], NOTES)

    def test_a_finished_test_record_gives_the_walk_forward_summary(self):
        from stockify.research import wf_report
        summary = {"windows": 12, "is_windows": 12, "oos_days": 1000, "oos_days_needed": 250, "is_sharpe": 1.1,
                   "oos_sharpe": 0.8, "ratio": 0.73, "chosen_modal_share": 0.5, "n_trials": 9, "var_sharpe": 0.1,
                   "dsr": None}
        detail = {"summary": summary, "plain": {"ratio": "out-of-sample kept 73 % of in-sample"}}
        with mock.patch.object(store, "latest_test_record",
                               return_value={"job_id": "wf_abc", "benchmark": "price index, without dividends"}), \
                mock.patch.object(wf_report, "detail", return_value=detail):
            body = self.get("/api/fundamentals/test-record")
        self.assertTrue(body["available"])
        self.assertIsNone(body["message"])
        self.assertEqual(body["job_id"], "wf_abc")
        self.assertEqual(body["summary"]["oos_sharpe"], 0.8)
        self.assertEqual(body["benchmark"], "price index, without dividends")
        self.assertEqual(body["plain"], detail["plain"])
        self.assertEqual(body["notes"], NOTES)

    def test_an_unfinished_test_record_is_not_available(self):
        from stockify.research import wf_report
        err = wf_report.WfReportError("the walk-forward job is not finished (it is running)", kind="not_done")
        with mock.patch.object(store, "latest_test_record", return_value={"job_id": "wf_abc"}), \
                mock.patch.object(wf_report, "detail", side_effect=err):
            body = self.get("/api/fundamentals/test-record")
        self.assertFalse(body["available"])
        self.assertEqual(body["job_id"], "wf_abc")
        self.assertEqual(body["message"], "The test is still running.")
        self.assertIsNone(body["summary"])
        self.assertEqual(body["notes"], NOTES)


class SaveTests(FundamentalsApiBase):

    def test_a_failing_precompute_never_loses_the_days_order(self):
        from stockify.fundamentals import series
        rows = screen.order(DAY)
        with mock.patch.object(series, "precompute", side_effect=RuntimeError("boom")),                 mock.patch.object(screen, "log") as log:          # a mock: other tests may reconfigure logging
            screen.save(DAY, rows)
        log.warning.assert_called_once()
        self.assertEqual([r["symbol"] for r in screen.latest()[1]], [r["symbol"] for r in rows])
        # The day's series from the earlier save are dropped, not shown beside a list they may not match.
        self.assertEqual(store._connect().execute("SELECT COUNT(*) FROM screen_series WHERE day = ?",
                                                  (DAY.isoformat(),)).fetchone()[0], 0)
        listing = self.get("/api/fundamentals")
        self.assertEqual([i["symbol"] for i in listing["items"]], [r["symbol"] for r in rows])
        body = self.get("/api/fundamentals/BBB")                 # degrades to its own series, no medians
        self.assertTrue(body["series"])
        self.assertTrue(all(p["sector_median"] is None for s in body["series"] for p in s["points"]))


class PruneTests(FundamentalsApiBase):

    def test_series_older_than_30_days_are_pruned_on_each_save(self):
        # Minor 4d: screen_series is derived (rebuilt by any save), and only the newest day is read.
        conn = store._connect()
        for back in (31, 30, 5):
            old = (DAY - timedelta(days=back)).isoformat()
            conn.execute("INSERT INTO screen_series (day, symbol, json) VALUES (?, 'BBB', '[]')", (old,))
        screen.save(DAY, screen.order(DAY))
        days = {r["day"] for r in conn.execute("SELECT DISTINCT day FROM screen_series")}
        self.assertEqual(days, {(DAY - timedelta(days=30)).isoformat(), (DAY - timedelta(days=5)).isoformat(),
                                DAY.isoformat()})


class NoScoreTests(FundamentalsApiBase):

    def test_no_score_fields_anywhere(self):
        bodies = [self.get("/api/fundamentals"), self.get("/api/fundamentals/BBB"),
                  self.get("/api/fundamentals/HELD"), self.get("/api/fundamentals/test-record")]
        keys = set().union(*(_keys(b) for b in bodies))
        schemas = self.client.app.openapi()["components"]["schemas"]
        for name in ("FundamentalsListDTO", "ResultsDateDTO", "CandidateDTO", "CandidateDetailDTO", "RatioDTO",
                     "SeriesDTO", "SeriesPointDTO", "FilingDTO", "FundamentalsTestRecordDTO"):
            keys |= set(schemas[name]["properties"])
        self.assertEqual([k for k in keys for bad in FORBIDDEN if bad in k.lower()], [])
        # The stored rows and series carry no score either: the list's order is the judgement.
        saved = [json.loads(r["json"]) for r in store._connect().execute("SELECT json FROM screen_series")]
        self.assertTrue(saved)
        self.assertEqual([k for k in _keys([screen.latest()[1], saved]) for bad in FORBIDDEN if bad in k.lower()],
                         [])


if __name__ == "__main__":
    unittest.main()
