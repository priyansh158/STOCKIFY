"""CW 6: the nightly desk's second pass over the watchlist's stocks that are not held.

Offline, like test_nightly: a fake holdings snapshot, `history.fetch` recorded, the grading backtests faked. The
watchlist is a real store in the sandbox."""
from __future__ import annotations

import unittest
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports

from stockify.data import daily_candles, history, instruments, refdata
from stockify.desk import nightly
from stockify.desk import store as desk_store
from stockify.watchlist import store as watch_store
from tests.stockify.desk.test_nightly import DAY, NightlyBase, holding


class WatchBase(NightlyBase):
    db_modules = (refdata, history, desk_store, watch_store)

    def default_holdings(self):
        return [holding("LONGCO", 500001, isin="INE000A01011")]

    def watch(self, *symbols):
        for s in symbols:
            watch_store.add(s, now_ns=1)

    def viewed(self):
        return {r["symbol"] for r in desk_store.latest_views()}


class WatchPassTests(WatchBase):
    def test_watched_stocks_are_graded_after_the_holdings(self):
        self.watch("MOVEDCO", "ALSOCO")
        calls = []
        res = self.run_job(calls=calls)
        self.assertEqual([c[1] for c in calls], ["LONGCO", "MOVEDCO", "ALSOCO"])       # holdings first
        self.assertEqual(self.viewed(), {"LONGCO", "MOVEDCO", "ALSOCO"})
        self.assertEqual((res["analysed"], res["watch"]["analysed"], res["watch"]["watched"]), (1, 2, 2))
        self.assertTrue(res["complete"])
        row = desk_store.latest_view(self.ids["MOVEDCO"])
        self.assertEqual(row["trade_date"], DAY.isoformat())
        self.assertFalse({"average_price", "pnl", "quantity", "cost"} & set(row["view"]))
        self.assertEqual(row["view"]["source"], "watch")                  # the logged-out holdings list leaves it out
        self.assertNotIn("source", desk_store.latest_view(self.ids["LONGCO"])["view"])

    def test_a_held_stock_on_the_watchlist_is_done_once_as_a_holding(self):
        self.watch("LONGCO", "MOVEDCO")
        calls = []
        res = self.run_job(calls=calls)
        self.assertEqual([c[1] for c in calls], ["LONGCO", "MOVEDCO"])
        self.assertEqual((res["analysed"], res["watch"]["watched"], res["watch"]["analysed"]), (1, 1, 1))

    def test_a_holding_bought_on_bse_is_still_held(self):
        """ALSOCO is held through BSE and watched on NSE: the holding pass owns it."""
        self.watch("ALSOCO")
        calls = []
        res = self.run_job([holding("ALSOCO", 800005, exchange="BSE", isin="INE000D01011")], calls=calls)
        self.assertEqual(len(calls), 1)
        self.assertEqual(res["watch"]["watched"], 0)

    def test_the_cap_spreads_first_gradings_over_nights(self):
        self.watch("MOVEDCO", "ALSOCO")
        with mock.patch.object(nightly, "_watch_settings", return_value=1):
            first = self.run_job()
            self.assertEqual((first["watch"]["analysed"], first["watch"]["deferred"]), (1, ["ALSOCO"]))
            self.assertEqual(self.viewed(), {"LONGCO", "MOVEDCO"})
            self.assertTrue(first["complete"])                    # waiting for another night is not unfinished
            second = self.run_job()
        self.assertEqual(second["watch"]["deferred"], [])
        self.assertEqual(second["watch"]["analysed"], 2)           # the first is brought up to date, uncounted
        self.assertEqual(self.viewed(), {"LONGCO", "MOVEDCO", "ALSOCO"})

    def test_the_cap_does_not_hold_back_a_stock_already_graded(self):
        self.watch("MOVEDCO", "ALSOCO")
        with mock.patch.object(nightly, "_watch_settings", return_value=1):
            self.run_job(); self.run_job()                        # one graded a night: both graded by the second
            res = self.run_job()
        self.assertEqual((res["watch"]["analysed"], res["watch"]["deferred"]), (2, []))

    def test_a_cap_of_zero_turns_the_pass_off(self):
        self.watch("MOVEDCO")
        with mock.patch.object(nightly, "_watch_settings", return_value=0):
            res = self.run_job()
        self.assertEqual((res["watch"]["watched"], res["watch"]["analysed"]), (0, 0))
        self.assertEqual(self.viewed(), {"LONGCO"})
        self.assertNotIn(self.ids["MOVEDCO"], [f[0] for f in self.fetches])
        self.assertTrue(res["complete"])

    def test_a_deferred_stock_asks_nothing_of_kite(self):
        self.watch("MOVEDCO", "ALSOCO")
        with mock.patch.object(nightly, "_watch_settings", return_value=1):
            self.run_job()
        self.assertNotIn(self.ids["ALSOCO"], [f[0] for f in self.fetches])

    def test_a_login_failure_stops_the_watch_pass_instead_of_failing_each_stock(self):
        import kite_client
        self.watch("MOVEDCO", "ALSOCO")

        def fetch(iid, interval, from_day, to_day, **kw):  # noqa: ANN001, ANN003, ANN202
            if iid in (self.ids["MOVEDCO"], self.ids["ALSOCO"]):
                raise kite_client.KiteAuthError("token expired")
            return history.FetchStats(iid, interval)
        with mock.patch.object(history, "fetch", side_effect=fetch):
            res = self.run_job()
        self.assertEqual((res["watch"]["failed"], res["watch"]["stopped"]), ([], "login"))
        self.assertFalse(res["complete"])                         # runs again after the next login
        self.assertEqual(res["analysed"], 1)

    def test_the_candles_and_the_watch_pass_take_the_same_kinds(self):
        self.assertIs(daily_candles.STOCK_KINDS, instruments.STOCK_KINDS)

    def test_the_default_cap_is_ten(self):
        self.assertEqual(nightly._watch_settings(), 10)

    def test_the_deadline_stops_the_watch_pass_and_leaves_it_for_later(self):
        self.watch("MOVEDCO", "ALSOCO")
        def now():
            return 5 if desk_store.latest_view(self.ids["MOVEDCO"]) else 0   # past the deadline once the first is stored

        res = self.run_job(deadline_ns=3, now=now)
        self.assertEqual(res["analysed"], 1)
        self.assertEqual(res["watch"]["analysed"], 1)
        self.assertFalse(res["complete"])
        self.assertEqual(self.viewed(), {"LONGCO", "MOVEDCO"})

    def test_a_deadline_inside_the_bar_top_up_stops_the_watch_pass_without_a_failure(self):
        self.watch("MOVEDCO")

        def fetch(iid, interval, from_day, to_day, **kw):  # noqa: ANN001, ANN003, ANN202
            st = history.FetchStats(iid, interval)
            if iid == self.ids["MOVEDCO"]:
                st.outcome = "stopped"
            return st
        with mock.patch.object(history, "fetch", side_effect=fetch):
            res = self.run_job(deadline_ns=10 ** 30)
        self.assertEqual((res["watch"]["failed"], res["analysed"]), ([], 1))
        self.assertFalse(res["complete"])
        self.assertNotIn("MOVEDCO", self.viewed())

    def test_the_holdings_pass_stopping_at_the_deadline_skips_the_watch_pass(self):
        self.watch("MOVEDCO")
        res = self.run_job(deadline_ns=1, now=lambda: 2)
        self.assertIsNone(res["watch"])
        self.assertEqual(self.viewed(), set())

    def test_a_stock_with_candles_up_to_date_is_not_fetched(self):
        self.watch("MOVEDCO", "ALSOCO")
        with mock.patch.object(daily_candles, "up_to_date", side_effect=lambda iid, day: iid == self.ids["MOVEDCO"]):
            self.run_job()
        fetched = {f[0] for f in self.fetches}
        self.assertNotIn(self.ids["MOVEDCO"], fetched)
        self.assertIn(self.ids["ALSOCO"], fetched)
        self.assertIn("MOVEDCO", self.viewed())

    def test_a_short_history_is_skipped_with_its_reason(self):
        self.watch("SHORTCO")
        res = self.run_job()
        self.assertEqual(res["watch"]["skipped"], [{"symbol": "SHORTCO", "reason": nightly.SHORT_HISTORY}])
        self.assertNotIn("SHORTCO", self.viewed())

    def test_one_watched_stock_failing_does_not_stop_the_others(self):
        self.watch("MOVEDCO", "ALSOCO")
        real = nightly.closing_view

        def view(iid, symbol, bars, rows):  # noqa: ANN001, ANN202
            if symbol == "MOVEDCO":
                raise RuntimeError("boom")
            return real(iid, symbol, bars, rows)
        with mock.patch.object(nightly, "closing_view", side_effect=view):
            res = self.run_job()
        self.assertEqual([f["symbol"] for f in res["watch"]["failed"]], ["MOVEDCO"])
        self.assertEqual(res["watch"]["analysed"], 1)
        self.assertTrue(res["complete"])                          # fail-soft: the holdings' run is not failed by it
        self.assertEqual(res["failed"], [])


class WatchLogTests(WatchBase):
    def test_the_desk_job_logs_one_summary_line_for_the_watch_pass(self):
        import logging
        from stockify import daemon, timeutil
        from stockify.daemon import scheduler
        was = logging.root.manager.disable                       # the runner switches logging off
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, was)
        done = {"holdings": 1, "analysed": 1, "skipped": [], "failed": [], "complete": True, "logged_in": True,
                "watch": {"watched": 4, "analysed": 2, "deferred": ["A", "B"], "skipped": [], "failed": [],
                          "stopped": None}}
        with mock.patch.object(nightly, "run", return_value=done), self.assertLogs("stockify", "INFO") as logs:
            daemon._run_job("desk", DAY, scheduler.State(), timeutil.ist_ns(DAY, "20:10"))
        lines = [m for m in logs.output if "watchlist:" in m]
        self.assertEqual(len(lines), 1, logs.output)
        self.assertIn("4 watched, 2 analysed, 2 deferred, 0 skipped, 0 failed", lines[0])


class WatchFailSoftTests(WatchBase):
    def test_an_unreadable_watchlist_leaves_the_holdings_result_whole(self):
        self.watch("MOVEDCO")
        with mock.patch.object(watch_store, "rows", side_effect=OSError("locked")):
            res = self.run_job()
        self.assertEqual((res["analysed"], res["failed"], res["complete"]), (1, [], True))
        self.assertEqual(res["watch"]["watched"], 0)
        self.assertEqual(self.viewed(), {"LONGCO"})

    def test_a_bad_config_value_is_also_soft(self):
        self.watch("MOVEDCO")
        with mock.patch.object(nightly, "_watch_settings", side_effect=ValueError("bad")):
            res = self.run_job()
        self.assertEqual((res["analysed"], res["complete"]), (1, True))


class HoldingsUnchangedTests(WatchBase):
    def default_holdings(self):
        return [holding("LONGCO", 500001, isin="INE000A01011"), holding("SHORTCO", 500002, isin="INE000B01011"),
                holding("BSEONLY", 900001, exchange="BSE", isin="INE999Z01011")]

    def test_the_holdings_results_are_the_same_with_and_without_a_watchlist(self):
        bare = self.run_job()
        before = {r["symbol"]: (r["verdict"], r["view"]) for r in desk_store.latest_views()}
        self.watch("MOVEDCO", "ALSOCO")
        again = self.run_job()
        for key in ("holdings", "analysed", "skipped", "failed", "refetched", "complete", "logged_in"):
            self.assertEqual(again[key], bare[key], key)
        after = {r["symbol"]: (r["verdict"], r["view"]) for r in desk_store.latest_views()}
        self.assertEqual({s: after[s] for s in before}, before)

    def test_no_login_means_no_watch_pass_and_nothing_marked(self):
        self.watch("MOVEDCO")
        res = self.run_job(error="Kite not authenticated")
        self.assertEqual((res["logged_in"], res["complete"], res["watch"]), (False, False, None))
        self.assertEqual(self.viewed(), set())
        self.assertEqual(self.fetches, [])


if __name__ == "__main__":
    unittest.main()
