"""CW 2: /api/watchlist (the list with last close and 52-week range, add, remove, alerts). Offline: FastAPI's
TestClient, a sandboxed lab, and a Kite fake that records every call (the endpoints must make none)."""
from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest
from tests.stockify.test_bundle import HEADER

import kite_client
from stockify import kite_readonly, timeutil
from stockify.data import history, instruments, refdata
from stockify.desk import store as desk_store
from stockify.watchlist import store

DAY = date(2026, 11, 17)                                   # a Tuesday
AFTER_CLOSE = timeutil.ist_ns(DAY, "20:00")
TOKENS = {"INFY": 408065, "TCS": 2953217, "NEWCO": 777001}


class FakeKite:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):  # noqa: ANN204
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **k: self.calls.append(name)


def install() -> dict[str, int]:
    rows = "".join(f"{tok},1,{sym},{sym},0,,0,0.05,1,EQ,NSE,NSE\n" for sym, tok in TOKENS.items())
    rows += "9001,1,NIFTY 50,NIFTY 50,0,,0,0.05,1,EQ,INDICES,NSE\n"
    instruments.sync(DAY, {"NSE": f"{HEADER}\n{rows}"}, underlyings={})
    return {s: instruments.find("NSE", s) for s in TOKENS}


def put_bars(iid: int, first: date, n: int, *, close=100_000, spike: tuple[int, int, int] | None = None) -> None:
    """`n` daily bars from `first`, flat at `close` with a 10 % range; `spike` = (index, high, low) of one day."""
    with history._Tx() as conn:
        for i in range(n):
            d = first + timedelta(days=i)
            hi, lo = close + 1000, close - 1000
            if spike and spike[0] == i:
                hi, lo = spike[1], spike[2]
            conn.execute("INSERT INTO bars VALUES (?, 'day', ?, ?, ?, ?, ?, 1000, NULL, '2026-01-01')",
                         (iid, timeutil.ist_ns(d, "00:00"), close, hi, lo, close))


@support.requires_fastapi
class WatchlistApiTests(SandboxedDBTest):
    db_modules = (refdata, history, store, desk_store)

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        from server.routers import watchlist as router
        self.client = TestClient(app, base_url=support.API_BASE_URL)
        self.ids = install()
        self.kite = FakeKite()
        self.patches = [mock.patch.object(router, "now_ns", lambda: AFTER_CLOSE),
                        mock.patch.object(kite_client, "is_authenticated", lambda: True),
                        mock.patch.object(kite_readonly, "client", lambda: self.kite)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def add(self, symbol):
        return self.client.post("/api/watchlist", json={"symbol": symbol})

    def test_empty_list(self):
        r = self.client.get("/api/watchlist")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"items": [], "max_stocks": 50})

    def test_add_then_list_with_last_close_and_range(self):
        put_bars(self.ids["INFY"], DAY - timedelta(days=420), 420, spike=(300, 190_000, 60_000))
        r = self.add("infy")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["symbol"], "INFY")
        body = self.client.get("/api/watchlist").json()
        (row,) = body["items"]
        self.assertEqual(row["symbol"], "INFY")
        self.assertEqual(row["token"], TOKENS["INFY"])
        self.assertEqual((row["last_close_paise"], row["last_close_date"]), (100_000, "2026-11-16"))
        self.assertEqual((row["high_paise"], row["low_paise"]), (190_000, 60_000))
        self.assertEqual(row["high_date"], (DAY - timedelta(days=420) + timedelta(days=300)).isoformat())
        self.assertIsNone(row["note"])

    def test_a_graded_stock_carries_the_desks_verdict_and_an_ungraded_one_does_not(self):
        """CW 6: the row has the desk's verdict label at the last close; none while the desk has not graded it."""
        self.add("INFY")
        self.add("TCS")
        desk_store.put_view("2026-11-16", self.ids["INFY"], "INFY", "Hold",
                            {"verdict": "Hold", "close_date": "2026-11-16"}, AFTER_CLOSE)
        rows = {r["symbol"]: r for r in self.client.get("/api/watchlist").json()["items"]}
        self.assertEqual((rows["INFY"]["desk_verdict"], rows["INFY"]["desk_close_date"]), ("Hold", "2026-11-16"))
        self.assertIsNone(rows["TCS"]["desk_verdict"])
        self.assertIsNone(rows["TCS"]["desk_close_date"])
        self.assertFalse({"pnl", "average_price", "quantity"} & set(rows["INFY"]))

    def test_the_newest_verdict_is_the_one_shown(self):
        self.add("INFY")
        for day, label in (("2026-11-13", "Hold"), ("2026-11-16", "Consider exiting")):
            desk_store.put_view(day, self.ids["INFY"], "INFY", label, {"verdict": label, "close_date": day}, 1)
        (row,) = self.client.get("/api/watchlist").json()["items"]
        self.assertEqual((row["desk_verdict"], row["desk_close_date"]), ("Consider exiting", "2026-11-16"))

    def test_an_unreadable_desk_store_leaves_the_list_whole(self):
        self.add("INFY")
        with mock.patch.object(desk_store, "latest_views", side_effect=OSError("locked")):
            r = self.client.get("/api/watchlist")
        self.assertEqual(r.status_code, 200)
        (row,) = r.json()["items"]
        self.assertEqual(row["symbol"], "INFY")
        self.assertIsNone(row["desk_verdict"])

    def test_a_bar_dated_today_is_in_the_range(self):
        first = DAY - timedelta(days=420)
        put_bars(self.ids["INFY"], first, 421, spike=(420, 250_000, 40_000))        # the last bar is on DAY
        self.add("INFY")
        (row,) = self.client.get("/api/watchlist").json()["items"]
        self.assertEqual((row["high_paise"], row["low_paise"]), (250_000, 40_000))
        self.assertEqual((row["high_date"], row["last_close_date"]), (DAY.isoformat(), DAY.isoformat()))

    def test_without_a_bar_today_it_is_the_evaluators_prior_range(self):
        from stockify.watchlist import ranges
        put_bars(self.ids["INFY"], DAY - timedelta(days=420), 420, spike=(30, 300_000, 10_000))   # 390 days back: out
        put_bars(self.ids["TCS"], DAY - timedelta(days=420), 420, spike=(100, 300_000, 10_000))
        self.add("INFY")
        self.add("TCS")
        rows = {r["symbol"]: r for r in self.client.get("/api/watchlist").json()["items"]}
        for sym in ("INFY", "TCS"):
            want = ranges.prior_range(history.bars(self.ids[sym], "day"), DAY, 365, 365)
            self.assertEqual((rows[sym]["high_paise"], rows[sym]["low_paise"]), (want["high_paise"], want["low_paise"]))
        self.assertEqual(rows["INFY"]["high_paise"], 101_000)             # the spike is older than 365 days
        self.assertEqual(rows["TCS"]["high_paise"], 300_000)

    def test_the_365_day_minimum_history_edge(self):
        put_bars(self.ids["INFY"], DAY - timedelta(days=365), 365)         # first bar exactly 365 days back, none today
        put_bars(self.ids["TCS"], DAY - timedelta(days=364), 364)          # one day short
        self.add("INFY")
        self.add("TCS")
        rows = {r["symbol"]: r for r in self.client.get("/api/watchlist").json()["items"]}
        self.assertIsNotNone(rows["INFY"]["high_paise"])
        self.assertIsNone(rows["TCS"]["high_paise"])
        self.assertIn("no 52-week range", rows["TCS"]["note"])

    def test_only_old_candles_get_their_own_note(self):
        put_bars(self.ids["INFY"], DAY - timedelta(days=900), 300)
        self.add("INFY")
        (row,) = self.client.get("/api/watchlist").json()["items"]
        self.assertEqual(row["note"], "No daily prices in the last year")
        self.assertIsNone(row["high_paise"])
        self.assertIsNotNone(row["last_close_paise"])

    def test_stock_without_candles_says_so_in_plain_words(self):
        self.assertEqual(self.add("TCS").status_code, 201)
        (row,) = self.client.get("/api/watchlist").json()["items"]
        self.assertIsNone(row["last_close_paise"])
        self.assertIsNone(row["high_paise"])
        self.assertEqual(row["note"], "Daily prices arrive after tonight's fetch")

    def test_short_history_keeps_the_last_close_but_no_range(self):
        put_bars(self.ids["NEWCO"], DAY - timedelta(days=60), 60)
        self.add("NEWCO")
        (row,) = self.client.get("/api/watchlist").json()["items"]
        self.assertEqual(row["last_close_paise"], 100_000)
        self.assertIsNone(row["high_paise"])
        self.assertIn("no 52-week range", row["note"])

    def test_the_list_keeps_the_order_added(self):
        for s in ("TCS", "INFY", "NEWCO"):
            self.add(s)
        self.assertEqual([i["symbol"] for i in self.client.get("/api/watchlist").json()["items"]],
                         ["TCS", "INFY", "NEWCO"])

    def test_unknown_symbol_is_404(self):
        r = self.add("NOSUCH")
        self.assertEqual(r.status_code, 404)
        self.assertIn("NOSUCH", r.json()["detail"])

    def test_an_index_is_422(self):
        self.assertEqual(self.add("NIFTY 50").status_code, 422)
        self.assertEqual(store.rows(), [])

    def test_a_malformed_body_is_422(self):
        for body in ({}, {"symbol": ""}, {"symbol": "A" * 41}, {"symbol": "X;DROP"}, {"symbol": 5}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/watchlist", json=body).status_code, 422)

    def test_duplicate_is_409(self):
        self.assertEqual(self.add("INFY").status_code, 201)
        r = self.add("infy")
        self.assertEqual(r.status_code, 409)
        self.assertIn("already", r.json()["detail"])

    def test_full_list_is_409(self):
        with mock.patch.object(store, "max_stocks", lambda: 1):
            self.assertEqual(self.add("INFY").status_code, 201)
            r = self.add("TCS")
        self.assertEqual(r.status_code, 409)
        self.assertIn("at most 1", r.json()["detail"])

    def test_two_adds_at_the_cap_give_one_409(self):
        import threading
        codes = []

        def go(sym):
            codes.append(self.add(sym).status_code)

        with mock.patch.object(store, "max_stocks", lambda: 2):
            self.add("INFY")
            threads = [threading.Thread(target=go, args=(s,)) for s in ("TCS", "NEWCO")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(sorted(codes), [201, 409])
        self.assertEqual(len(store.rows()), 2)

    def test_remove(self):
        self.add("INFY")
        self.assertEqual(self.client.delete("/api/watchlist/infy").status_code, 204)
        self.assertEqual(self.client.get("/api/watchlist").json()["items"], [])
        self.assertEqual(self.client.delete("/api/watchlist/INFY").status_code, 404)
        self.assertEqual(self.client.delete("/api/watchlist/NOSUCH").status_code, 404)
        self.assertEqual(self.client.delete("/api/watchlist/a;b").status_code, 404)

    def test_alerts_within_the_window_newest_first(self):
        iid = self.ids["INFY"]
        old = AFTER_CLOSE - 40 * 86_400 * 10**9
        store.record_alert(iid, "INFY", "high52", "2026-10-09", level_paise=2, prior_paise=1, prior_date="2026-01-01",
                           at_ns=old)
        store.record_alert(iid, "INFY", "high52", "2026-11-16", level_paise=3, prior_paise=2, prior_date="2026-10-09",
                           at_ns=AFTER_CLOSE - 10**9)
        store.record_alert(iid, "INFY", "results", "2026-11-19", at_ns=AFTER_CLOSE - 2 * 10**9)
        got = self.client.get("/api/watchlist/alerts").json()
        self.assertEqual([(a["kind"], a["ref"]) for a in got], [("high52", "2026-11-16"), ("results", "2026-11-19")])
        self.assertEqual(got[0]["level_paise"], 3)
        wide = self.client.get("/api/watchlist/alerts?days=60").json()
        self.assertEqual(len(wide), 3)
        for bad in ("0", "400", "x"):
            self.assertEqual(self.client.get(f"/api/watchlist/alerts?days={bad}").status_code, 422)

    def test_no_kite_call_and_no_money_beyond_the_prices(self):
        put_bars(self.ids["INFY"], DAY - timedelta(days=420), 420)
        self.add("INFY")
        self.client.get("/api/watchlist")
        self.client.get("/api/watchlist/alerts")
        self.client.delete("/api/watchlist/INFY")
        self.assertEqual(self.kite.calls, [])

    def test_writes_from_another_origin_are_refused(self):
        r = self.client.post("/api/watchlist", json={"symbol": "INFY"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)
        r = self.client.delete("/api/watchlist/INFY", headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(store.rows(), [])

    def test_a_foreign_host_is_refused(self):
        from fastapi.testclient import TestClient
        from server.main import app
        r = TestClient(app, base_url="http://evil.example").get("/api/watchlist")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
