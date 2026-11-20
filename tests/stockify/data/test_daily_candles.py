"""CW 1.3: the nightly daily candles (stockify.data.daily_candles). Offline: a fake Kite client counts every request;
the clock is a ManualClock, so the bucket's waits and the 429 backoff move it instead of sleeping."""
from __future__ import annotations

import unittest
from datetime import date, datetime
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.data.test_history import HEADER, FakeKite

from stockify import timeutil
from stockify.daemon.clock import ManualClock
from stockify.data import daily_candles, history, instruments, refdata
from stockify.fundamentals import store as fstore

DAY = date(2026, 11, 16)                        # a Monday
BEFORE = date(2026, 11, 2)
SEC = 1_000_000_000
STOCKS = (("RELIANCE", 738561), ("TCS", 2953217), ("INFY", 408065))


class RateLimited(Exception):
    def __init__(self):
        super().__init__("Too many requests")
        self.code = 429


class LoggedOut(Exception):
    def __init__(self):
        super().__init__("Incorrect `api_key` or `access_token`.")
        self.code = 403


class Kite(FakeKite):
    """FakeKite plus scripted failures: `fail[token]` is a list of exceptions raised by that token's next calls."""

    def __init__(self, clock, **kw):  # noqa: ANN001
        super().__init__(clock, **kw)
        self.fail: dict[int, list[Exception]] = {}

    def historical_data(self, token, from_date, to_date, interval, continuous=False, oi=False):  # noqa: ANN001
        queue = self.fail.get(token)
        if queue:
            self.calls.append({"token": token, "from": from_date, "to": to_date, "failed": True,
                               "mono": self.clock.mono_ns()})
            raise queue.pop(0)
        return super().historical_data(token, from_date, to_date, interval, continuous, oi)


def dump(extra: str = "") -> dict:
    rows = "".join(f"{tok},{i},{sym},{sym} LTD,0,,0,0.05,1,EQ,NSE,NSE\n" for i, (sym, tok) in enumerate(STOCKS, 1))
    return {"NSE": f"{HEADER}\n256265,99,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n{rows}{extra}",
            "NFO": f"{HEADER}\n"}


class CandlesTest(unittest.TestCase):
    def setUp(self):
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        for mod in (refdata, history, fstore):
            support.reset_sqlite_singleton(mod)
            self.addCleanup(support.reset_sqlite_singleton, mod)
        instruments.sync(BEFORE, dump("111,50,GONECO,GONE CO,0,,0,0.05,1,EQ,NSE,NSE\n"), underlyings={})
        instruments.sync(DAY, dump(), underlyings={})          # GONECO left the dump: no token on DAY
        self.ids = {sym: instruments.find("NSE", sym) for sym in ("RELIANCE", "TCS", "INFY", "GONECO", "NIFTY 50")}
        self.clock = ManualClock(timeutil.ist_ns(DAY, "16:40"))
        self.universe(["INFY", "RELIANCE", "TCS"])

    def universe(self, symbols, ids=None):  # noqa: ANN001
        ids = ids or {}
        fstore.put_universe(DAY.isoformat(), [
            {"symbol": s, "instrument_id": ids.get(s, self.ids.get(s)), "isin": f"INE{i:09d}", "company": s,
             "median_value_paise": 10 ** 12, "liquid": True, "list_industry": "X"} for i, s in enumerate(symbols)])

    def run_job(self, kite, day=DAY, **kw):  # noqa: ANN001
        kw.setdefault("logged_in", lambda: True)
        return daily_candles.run(day, client=kite, clock=self.clock, **kw)


class TargetTests(CandlesTest):
    def test_targets_are_the_universe_then_the_watchlist_once_each(self):
        self.assertEqual(daily_candles.targets(DAY), [(self.ids["INFY"], "INFY"), (self.ids["RELIANCE"], "RELIANCE"),
                                                      (self.ids["TCS"], "TCS")])
        with mock.patch.object(daily_candles, "_watchlist_ids", return_value=[self.ids["TCS"], self.ids["GONECO"]]):
            got = daily_candles.targets(DAY)
        self.assertEqual([s for _, s in got], ["INFY", "RELIANCE", "TCS", "GONECO"])

    def test_the_watchlist_hook_is_empty_until_phase_2(self):
        self.assertEqual(daily_candles._watchlist_ids(), [])

    def test_a_universe_row_without_an_id_is_resolved_by_its_nse_symbol(self):
        self.universe(["INFY", "MISSING"], ids={"INFY": None})
        self.assertEqual(daily_candles.targets(DAY), [(self.ids["INFY"], "INFY"), (None, "MISSING")])

    def test_no_universe_yet_means_no_targets(self):
        with mock.patch.object(fstore, "latest_as_of", return_value=None):
            self.assertEqual(daily_candles.targets(DAY), [])


class RunTests(CandlesTest):
    def test_the_first_fill_then_a_top_up_then_nothing_when_up_to_date(self):
        kite = Kite(self.clock)
        res = self.run_job(kite)
        self.assertTrue(res["complete"])
        self.assertEqual((res["targets"], res["fetched"], res["up_to_date"]), (3, 3, 0))
        self.assertEqual(res["requests"], len(kite.calls))
        self.assertEqual(res["requests"], 6)                                     # six years: two chunks a stock
        self.assertEqual({c["from"] for c in kite.calls}, {datetime(2020, 11, 16), datetime(2026, 5, 9)})
        for sym in ("RELIANCE", "TCS", "INFY"):
            self.assertTrue(daily_candles.up_to_date(self.ids[sym], DAY), sym)
        again = Kite(self.clock)
        res = self.run_job(again)                                                # the same evening: nothing asked
        self.assertEqual((res["up_to_date"], res["requests"], again.calls), (3, 0, []))
        self.assertTrue(res["complete"])
        nxt = date(2026, 11, 17)
        self.clock.set(timeutil.ist_ns(nxt, "16:40"))
        later = Kite(self.clock)
        res = self.run_job(later, nxt)                                           # next night: a check + a top-up
        self.assertEqual((res["fetched"], res["requests"]), (3, 6))
        self.assertEqual({c["from"].date() for c in later.calls}, {date(2026, 11, 10), DAY})   # last 5 bars; last

    def test_a_fetch_before_the_close_is_not_up_to_date(self):
        self.clock.set(timeutil.ist_ns(DAY, "12:00"))                            # an intraday fetch: provisional
        self.run_job(Kite(self.clock))
        self.assertFalse(daily_candles.up_to_date(self.ids["INFY"], DAY))
        self.clock.set(timeutil.ist_ns(DAY, "16:40"))
        self.assertFalse(daily_candles.up_to_date(self.ids["INFY"], DAY))
        res = self.run_job(Kite(self.clock))
        self.assertEqual(res["fetched"], 3)
        self.assertTrue(daily_candles.up_to_date(self.ids["INFY"], DAY))

    def test_skips_have_plain_reasons(self):
        self.universe(["INFY", "MISSING", "NIFTY 50"])
        with mock.patch.object(daily_candles, "_watchlist_ids", return_value=[self.ids["GONECO"]]):
            kite = Kite(self.clock)
            res = self.run_job(kite)
        self.assertEqual(sorted(res["skipped"], key=lambda s: s["symbol"]), [
            {"symbol": "GONECO", "reason": "no Kite token (delisted or suspended)"},
            {"symbol": "MISSING", "reason": "not in instruments"},
            {"symbol": "NIFTY 50", "reason": "not an NSE stock"},
        ])
        self.assertEqual(res["fetched"], 1)
        self.assertTrue(res["complete"])                                         # a skip is not a failure
        self.assertEqual({c["token"] for c in kite.calls}, {408065})

    def test_one_stock_failing_never_stops_the_rest(self):
        kite = Kite(self.clock)
        kite.fail[738561] = [RuntimeError("Kite said no")]
        res = self.run_job(kite)
        self.assertEqual(res["failed"], [{"symbol": "RELIANCE", "error": "RuntimeError: Kite said no"}])
        self.assertEqual(res["fetched"], 2)
        self.assertFalse(res["complete"])
        self.assertTrue(res["logged_in"])
        self.assertIsNone(res["stopped"])
        self.assertFalse(daily_candles.up_to_date(self.ids["RELIANCE"], DAY))

    def test_not_logged_in_asks_nothing(self):
        kite = Kite(self.clock)
        res = self.run_job(kite, logged_in=lambda: False)
        self.assertEqual((res["logged_in"], res["complete"], res["requests"], kite.calls), (False, False, 0, []))

    def test_a_login_that_expires_mid_run_stops_it(self):
        kite = Kite(self.clock)
        kite.fail[738561] = [LoggedOut()]
        res = self.run_job(kite)
        self.assertEqual((res["logged_in"], res["complete"], res["stopped"]), (False, False, "login"))
        self.assertEqual(res["fetched"], 1)                                      # INFY first, then the stop
        self.assertNotIn(2953217, {c["token"] for c in kite.calls})              # TCS never asked
        self.assertEqual(res["failed"], [])

    def test_a_429_backs_off_then_retries(self):
        kite = Kite(self.clock)
        kite.fail[408065] = [RateLimited(), RateLimited()]
        start = self.clock.now_ns()
        res = self.run_job(kite)
        self.assertTrue(res["complete"])
        self.assertEqual(res["fetched"], 3)
        self.assertGreaterEqual(self.clock.now_ns() - start, 3 * SEC)            # waited 1 s, then 2 s
        failed = [c for c in kite.calls if c.get("failed")]
        self.assertEqual(len(failed), 2)

    def test_a_429_that_outlasts_the_backoff_stops_the_run(self):
        kite = Kite(self.clock)
        kite.fail[408065] = [RateLimited() for _ in range(4)]
        start = self.clock.now_ns()
        res = self.run_job(kite)
        self.assertEqual((res["stopped"], res["complete"], res["fetched"]), ("rate_limited", False, 0))
        self.assertTrue(res["logged_in"])
        self.assertEqual(len(kite.calls), 4)                                     # the try and three retries
        self.assertGreaterEqual(self.clock.now_ns() - start, 7 * SEC)            # 1 + 2 + 4 s
        self.assertNotIn(738561, {c["token"] for c in kite.calls})

    def test_the_deadline_is_checked_before_each_stock(self):
        kite = Kite(self.clock)
        res = self.run_job(kite, deadline_ns=self.clock.now_ns())
        self.assertEqual((res["stopped"], res["complete"], kite.calls), ("deadline", False, []))
        self.assertTrue(res["logged_in"])

    def test_the_deadline_is_checked_between_chunks(self):
        kite = Kite(self.clock)
        res = self.run_job(kite, deadline_ns=self.clock.now_ns() + SEC // 4)    # 2 a second: after one chunk
        self.assertEqual((res["stopped"], res["complete"], res["requests"]), ("deadline", False, 1))
        self.assertFalse(daily_candles.up_to_date(self.ids["INFY"], DAY))
        self.clock.advance(3600 * SEC)
        res = self.run_job(Kite(self.clock))                                     # the rest, later
        self.assertTrue(res["complete"])
        days = [timeutil.ns_to_ist(b["ts_ns"]).date() for b in history.bars(self.ids["INFY"], "day")]
        self.assertEqual((days[0], days[-1], len(days), len(set(days))), (date(2020, 11, 16), DAY, len(days),
                                                                         len(days)))

    def test_requests_are_paced_at_the_configured_rate(self):
        kite = Kite(self.clock)
        self.run_job(kite)
        monos = [c["mono"] for c in kite.calls]
        for a, b in zip(monos, monos[1:]):
            self.assertGreaterEqual(b - a, SEC // 2 - 1_000)                     # candles.json rps "2"

    def test_an_adjustment_wipe_refills_the_years(self):
        self.run_job(Kite(self.clock))
        nxt = date(2026, 11, 17)
        self.clock.set(timeutil.ist_ns(nxt, "16:40"))
        split = Kite(self.clock, factor=0.5)
        res = self.run_job(split, nxt)
        self.assertTrue(res["complete"])
        self.assertEqual(sorted(res["refetched"]), ["INFY", "RELIANCE", "TCS"])
        bars = history.bars(self.ids["INFY"], "day")
        self.assertEqual(timeutil.ns_to_ist(bars[0]["ts_ns"]).date(), date(2020, 11, 17))
        self.assertTrue(all(b["fetched_on"] == nxt.isoformat() for b in bars))


if __name__ == "__main__":
    unittest.main()
