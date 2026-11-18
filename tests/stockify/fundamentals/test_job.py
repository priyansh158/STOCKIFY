"""FS 8: the fundamentals daily job (`job.run`), its daemon wiring, the backfill and the CLI commands.

Offline: NSE is a FakeSession behind the project's own Http (a ManualClock moves the time), and the steps the job
calls (universe, calendar, filings, xbrl, screen) are recorded fakes unless a test needs the real one."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.data.test_nse_files import FakeSession
from tests.support import SandboxedDBTest

from stockify import timeutil
from stockify.daemon import scheduler
from stockify.daemon.clock import ManualClock
from stockify.data import nse_files
from stockify.fundamentals import calendar as fcal
from stockify.fundamentals import filings, job, nse_api, screen, store, universe, xbrl

TUE = date(2026, 11, 17)
FRI = date(2026, 11, 20)
MIN = 60 * 1_000_000_000
UNIVERSE = [{"symbol": s} for s in ("ALPHA", "BETA", "GAMMA")]
ORDER = [{"symbol": "BETA"}, {"symbol": "ALPHA"}]


def read_universe(day: date) -> None:
    """A universe read on `day` (one stock is enough for its date)."""
    store.put_universe(day.isoformat(), [{"symbol": "ALPHA", "instrument_id": None, "isin": "INE000A01011", "company": "Alpha",
                                          "median_value_paise": 0, "liquid": 1, "list_industry": "Textiles"}])


class JobCase(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        self.calls: list[tuple] = []
        self.clock = ManualClock(timeutil.ist_ns(TUE, "20:40"))
        self.session = FakeSession({}, self.clock)
        self.client = nse_api.Client(nse_files.Http(session=self.session, clock=self.clock))
        self.download = {"downloaded": 0, "skipped": 0, "failed": 0, "stopped": None}
        self.patch(universe, "current", lambda: list(UNIVERSE))
        self.patch(universe, "refresh", lambda day, *, client: self.calls.append(("universe", day)) or [])
        self.patch(fcal, "refresh", lambda day, *, client: self.calls.append(("calendar", day)) or 0)
        self.patch(filings, "list_for", self.fake_list_for)
        self.patch(filings, "download_pending", self.fake_download)
        self.patch(xbrl, "ingest_pending", lambda **kw: self.calls.append(("ingest",)) or {"parsed": 0, "held": 0})
        self.patch(screen, "order", lambda day: self.calls.append(("order", day)) or list(ORDER))
        self.patch(screen, "save", lambda day, rows: self.calls.append(("save", day, rows)))
        read_universe(TUE)          # a universe read today: no refresh owed on a Tuesday
        for s in ("ALPHA", "BETA", "GAMMA"):            # every symbol listed lately: no weekly listing owed
            job.note_listed(s, TUE - timedelta(days=2))

    def patch(self, target, name, value):  # noqa: ANN001
        p = mock.patch.object(target, name, value)
        p.start()
        self.addCleanup(p.stop)

    def fake_list_for(self, symbol, *, client, since):  # noqa: ANN001, ANN202
        self.calls.append(("list", symbol))
        return []

    def fake_download(self, *, client, limit=None, deadline_ns=None):  # noqa: ANN001, ANN202
        self.calls.append(("download", deadline_ns))
        return dict(self.download)

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def meeting(self, symbol: str, day: date) -> None:
        conn = store._connect()
        conn.execute("INSERT INTO results_calendar (symbol, meeting_date, purpose, period_hint, read_on) "
                     "VALUES (?, ?, 'Financial Results', 'Jul–Sep quarter', ?)",
                     (symbol, day.isoformat(), day.isoformat()))

    def filing(self, symbol: str, broadcast: date) -> None:
        conn = store._connect()
        fid = f"{symbol}-{broadcast}"
        conn.execute("INSERT INTO filings (filing_id, symbol, period_end, period_kind, basis, broadcast_ns, source,"
                     " status) VALUES (?, ?, '2026-09-30', 'Q', 'consolidated', ?, 'integrated', 'parsed')",
                     (fid, symbol, timeutil.ist_ns(broadcast, "18:00")))


class DailyRunTests(JobCase):
    def test_job_saves_todays_order(self):
        res = job.run(TUE, client=self.client)
        self.assertEqual(self.names(), ["calendar", "download", "ingest", "order", "save"])
        self.assertEqual(self.calls[-1], ("save", TUE, ORDER))
        self.assertTrue(res["complete"])
        self.assertIsNone(res["stopped"])
        self.assertEqual(res["saved"], 2)

    def test_universe_refreshed_on_fridays_and_when_a_week_old(self):
        job.run(FRI, client=self.client)                              # read on Tuesday: Friday reads it again
        self.assertEqual(self.calls[0], ("universe", FRI))
        read_universe(FRI)
        self.calls.clear()
        job.run(FRI, client=self.client)                              # read already today: not again
        self.assertNotIn("universe", self.names())
        with store._lock:
            store._connect().execute("DELETE FROM universe")
        read_universe(TUE - timedelta(days=8))
        self.calls.clear()
        job.run(TUE, client=self.client)
        self.assertEqual(self.calls[0], ("universe", TUE))

    def test_a_universe_that_cannot_be_read_keeps_the_last_one(self):
        def refuse(day, *, client):  # noqa: ANN001, ANN202
            raise universe.NotEnoughHistory("cm_eod holds 10 days")
        self.patch(universe, "refresh", refuse)
        res = job.run(FRI, client=self.client)
        self.assertEqual(self.names(), ["calendar", "download", "ingest", "order", "save"])
        self.assertTrue(res["complete"])

    def test_symbols_whose_results_date_passed_since_their_last_filing_are_listed(self):
        self.meeting("ALPHA", TUE - timedelta(days=1))             # met yesterday, filed before: list it
        self.filing("ALPHA", TUE - timedelta(days=90))
        self.meeting("BETA", TUE - timedelta(days=1))              # met yesterday and filed: nothing new
        self.filing("BETA", TUE - timedelta(days=1))
        self.meeting("GAMMA", TUE + timedelta(days=3))             # not met yet
        res = job.run(TUE, client=self.client)
        self.assertEqual([c[1] for c in self.calls if c[0] == "list"], ["ALPHA"])
        self.assertEqual(res["listed"], 1)
        self.assertEqual(job.listed_on("ALPHA"), TUE)
        self.calls.clear()
        job.run(TUE, client=self.client)                            # listed today: a retry does not ask again
        self.assertNotIn("list", self.names())

    def test_every_symbol_is_listed_once_a_week(self):
        job.note_listed("GAMMA", TUE - timedelta(days=7))
        job.run(TUE, client=self.client)
        self.assertEqual([c[1] for c in self.calls if c[0] == "list"], ["GAMMA"])

    def test_one_symbols_trouble_is_listed_and_the_rest_go_on(self):
        for s in ("ALPHA", "BETA"):
            job.note_listed(s, TUE - timedelta(days=8))

        def flaky(symbol, *, client, since):  # noqa: ANN001, ANN202
            self.calls.append(("list", symbol))
            if symbol == "ALPHA":
                raise nse_files.NseError("HTTP 500")
            return []
        self.patch(filings, "list_for", flaky)
        res = job.run(TUE, client=self.client)
        self.assertEqual([c[1] for c in self.calls if c[0] == "list"], ["ALPHA", "BETA"])
        self.assertEqual(res["failed"], [{"symbol": "ALPHA", "error": "NseError: HTTP 500"}])
        self.assertFalse(res["complete"])
        self.assertIn("save", self.names())                         # the list is still made from what is there
        self.assertEqual(job.listed_on("ALPHA"), TUE - timedelta(days=8))


class DeadlineTests(JobCase):
    def test_job_skips_nse_calls_after_deadline(self):
        """Past the deadline: no NSE request at all, nothing else done, and the run is not complete."""
        read_universe(FRI)
        res = job.run(FRI, client=self.client, deadline_ns=self.clock.now_ns())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.session.calls, [])
        self.assertEqual(res["stopped"], "deadline")
        self.assertFalse(res["complete"])

    def test_the_deadline_stops_the_listing_before_the_next_request(self):
        for s in ("ALPHA", "BETA", "GAMMA"):
            job.note_listed(s, TUE - timedelta(days=8))

        def slow(symbol, *, client, since):  # noqa: ANN001, ANN202
            self.calls.append(("list", symbol))
            self.clock.advance(10 * MIN)
            return []
        self.patch(filings, "list_for", slow)
        deadline = self.clock.now_ns() + 15 * MIN
        res = job.run(TUE, client=self.client, deadline_ns=deadline)
        self.assertEqual([c[1] for c in self.calls if c[0] == "list"], ["ALPHA", "BETA"])
        self.assertNotIn("download", self.names())
        self.assertNotIn("save", self.names())
        self.assertEqual(res["stopped"], "deadline")
        self.assertFalse(res["complete"])
        self.assertEqual(job.listed_on("GAMMA"), TUE - timedelta(days=8))     # the rest runs on a later pass

    def test_the_deadline_is_handed_to_the_download(self):
        deadline = self.clock.now_ns() + 60 * MIN
        job.run(TUE, client=self.client, deadline_ns=deadline)
        self.assertIn(("download", deadline), self.calls)

    def test_a_download_stopped_at_the_deadline_is_not_complete(self):
        self.download["stopped"] = "deadline"
        res = job.run(TUE, client=self.client, deadline_ns=self.clock.now_ns() + MIN)
        self.assertEqual(res["stopped"], "deadline")
        self.assertNotIn("save", self.names())
        self.assertFalse(res["complete"])


class BlockedTests(JobCase):
    def test_blocked_listing_stops_the_run(self):
        job.note_listed("ALPHA", TUE - timedelta(days=8))
        job.note_listed("BETA", TUE - timedelta(days=8))

        def refuse(symbol, *, client, since):  # noqa: ANN001, ANN202
            self.calls.append(("list", symbol))
            raise nse_api.Blocked("HTTP 403")
        self.patch(filings, "list_for", refuse)
        res = job.run(TUE, client=self.client)
        self.assertEqual([c[1] for c in self.calls if c[0] == "list"], ["ALPHA"])
        self.assertNotIn("download", self.names())
        self.assertEqual(res["stopped"], "blocked")
        self.assertFalse(res["complete"])

    def test_blocked_download_stops_the_run(self):
        self.download["stopped"] = "blocked"
        res = job.run(TUE, client=self.client)
        self.assertEqual(res["stopped"], "blocked")
        self.assertNotIn("ingest", self.names())
        self.assertNotIn("save", self.names())

    def test_blocked_calendar_stops_the_run(self):
        def refuse(day, *, client):  # noqa: ANN001, ANN202
            raise nse_api.Blocked("an HTML page")
        self.patch(fcal, "refresh", refuse)
        res = job.run(TUE, client=self.client)
        self.assertEqual(res["stopped"], "blocked")
        self.assertEqual(self.calls, [])


class DaemonWiringTests(JobCase):
    def setUp(self):
        super().setUp()
        import logging
        was = logging.root.manager.disable                                       # the runner switches logging off
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, was)

    def result(self, **kw) -> dict:
        return {"complete": False, "stopped": None, "failed": [], **kw}

    def test_blocked_is_retried_not_marked(self):
        from stockify import daemon
        state = scheduler.State()
        with mock.patch.object(job, "run", return_value=self.result(stopped="blocked")), \
                self.assertLogs("stockify", "INFO"):
            daemon._run_job("fundamentals", TUE, state, timeutil.ist_ns(TUE, "23:45"))
        self.assertFalse(state.done("fundamentals", TUE))

    def test_a_deadline_stop_is_retried_not_marked(self):
        from stockify import daemon
        state = scheduler.State()
        with mock.patch.object(job, "run", return_value=self.result(stopped="deadline")):
            daemon._run_job("fundamentals", TUE, state, timeutil.ist_ns(TUE, "23:45"))
        self.assertFalse(state.done("fundamentals", TUE))

    def test_a_complete_run_is_marked(self):
        from stockify import daemon
        state = scheduler.State()
        with mock.patch.object(job, "run", return_value=self.result(complete=True)):
            daemon._run_job("fundamentals", TUE, state, timeutil.ist_ns(TUE, "20:40"))
        self.assertTrue(state.done("fundamentals", TUE))

    def test_failures_are_retried_until_the_window_ends_then_given_up(self):
        from stockify import daemon
        state = scheduler.State()
        part = self.result(failed=[{"symbol": "ALPHA", "error": "NseError: HTTP 500"}])
        with mock.patch.object(job, "run", return_value=part):
            daemon._run_job("fundamentals", TUE, state, timeutil.ist_ns(TUE, "21:00"))
            self.assertFalse(state.done("fundamentals", TUE))
            with self.assertLogs("stockify.daemon", "WARNING") as logs:
                daemon._run_job("fundamentals", TUE, state, timeutil.ist_ns(TUE, "23:30"))
        self.assertTrue(state.done("fundamentals", TUE))
        self.assertIn("giving up", logs.output[-1])

    def test_a_raised_error_never_escapes_and_is_retried(self):
        from stockify import daemon
        state = scheduler.State()
        with mock.patch.object(job, "run", side_effect=RuntimeError("boom")), \
                self.assertLogs("stockify.daemon", "ERROR"):
            daemon._run_job("fundamentals", TUE, state, timeutil.ist_ns(TUE, "21:00"))
        self.assertFalse(state.done("fundamentals", TUE))

    def test_the_job_stops_before_the_next_session_on_the_daemon_clock(self):
        from stockify import daemon
        seen = {}

        def run(day, **kw):  # noqa: ANN001, ANN003, ANN202
            seen.update(kw)
            return self.result(complete=True)
        now = timeutil.ist_ns(TUE, "20:40")
        with mock.patch.object(job, "run", side_effect=run):
            daemon._run_job("fundamentals", TUE, scheduler.State(), now)
        nxt = scheduler.plan(scheduler.session_day(now))
        self.assertEqual(seen["deadline_ns"], min(nxt.refdata_ns, nxt.day_bundles_ns) - daemon.DESK_MARGIN_NS)
        http_now = seen["client"].http.clock.now_ns()                  # the Http's clock runs on the daemon's time
        self.assertGreaterEqual(http_now, now)
        self.assertLess(http_now, now + MIN)

    def test_the_daemon_job_asks_nse_nothing_after_the_deadline(self):
        """End to end through the daemon: at 08:25 IST the next session's preparation is 5 minutes away."""
        from stockify import daemon
        state = scheduler.State()
        wed = TUE + timedelta(days=1)
        with mock.patch.object(nse_files.Http, "get", side_effect=AssertionError("an NSE request")):
            daemon._run_job("fundamentals", TUE, state, timeutil.ist_ns(wed, "08:25"))
        self.assertEqual(self.calls, [])
        self.assertFalse(state.done("fundamentals", TUE))


class OffsetClockTests(unittest.TestCase):
    def test_offset_clock_follows_the_daemon_time(self):
        from stockify.daemon.clock import OffsetClock
        base = ManualClock(1_000)
        c = OffsetClock(base, 5_000)
        self.assertEqual(c.now_ns(), 5_000)
        base.advance(10)
        self.assertEqual(c.now_ns(), 5_010)
        c.sleep_until(5_100)
        self.assertEqual(base.now_ns(), 1_100)
        self.assertEqual(c.mono_ns(), base.mono_ns())


class BackfillTests(JobCase):
    def setUp(self):
        super().setUp()
        with store._lock:
            store._connect().execute("DELETE FROM filings_listed")
        self.cm_calls: list = []

    def run_backfill(self, **kw):  # noqa: ANN003, ANN202
        out = io.StringIO()
        with redirect_stdout(out):
            res = job.backfill(today=TUE, client=self.client, cm_missing=lambda start, end: [],
                               cm_backfill=lambda start, end: self.cm_calls.append((start, end)), **kw)
        return res, out.getvalue()

    def test_backfill_lists_every_symbol_then_downloads_ingests_and_saves(self):
        res, out = self.run_backfill()
        self.assertEqual([c[1] for c in self.calls if c[0] == "list"], ["ALPHA", "BETA", "GAMMA"])
        self.assertEqual(self.names()[-4:], ["download", "ingest", "order", "save"])
        self.assertIn("[3/3] GAMMA", out)
        self.assertEqual(res["listed"], 3)
        self.assertIsNone(res["stopped"])
        self.assertEqual(self.cm_calls, [])

    def test_backfill_resumes_after_the_symbols_already_listed(self):
        job.note_listed("ALPHA", TUE)
        self.run_backfill()
        self.assertEqual([c[1] for c in self.calls if c[0] == "list"], ["BETA", "GAMMA"])

    def test_backfill_limit_caps_the_companies_listed(self):
        res, _ = self.run_backfill(limit=2)
        self.assertEqual([c[1] for c in self.calls if c[0] == "list"], ["ALPHA", "BETA"])
        self.assertEqual(res["listed"], 2)

    def test_backfill_runs_the_cm_backfill_when_cm_eod_lacks_days(self):
        out = io.StringIO()
        with redirect_stdout(out):
            job.backfill(today=TUE, client=self.client, since=date(2021, 4, 1),
                         cm_missing=lambda start, end: [date(2021, 4, 1)],
                         cm_backfill=lambda start, end: self.cm_calls.append((start, end)))
        self.assertEqual(self.cm_calls, [(date(2021, 4, 1), TUE - timedelta(days=1))])

    def test_backfill_without_a_universe_stops_with_plain_words(self):
        self.patch(universe, "current", lambda: [])

        def refuse(day, *, client):  # noqa: ANN001, ANN202
            raise universe.NotEnoughHistory("cm_eod holds 10 trading days, 63 needed")
        self.patch(universe, "refresh", refuse)
        res, out = self.run_backfill()
        self.assertEqual(res["stopped"], "no universe")
        self.assertIn("63 needed", out)
        self.assertEqual(self.calls, [])

    def test_backfill_stops_cleanly_when_blocked(self):
        def refuse(symbol, *, client, since):  # noqa: ANN001, ANN202
            raise nse_api.Blocked("HTTP 403")
        self.patch(filings, "list_for", refuse)
        res, out = self.run_backfill()
        self.assertEqual(res["stopped"], "blocked")
        self.assertNotIn("download", self.names())
        self.assertIn("NSE refused", out)

    def test_cm_missing_counts_the_cm_trading_days_without_rows(self):
        from stockify.data import calendar as dcal
        from stockify.data import refdata
        support.reset_sqlite_singleton(refdata)
        self.addCleanup(support.reset_sqlite_singleton, refdata)
        days = dcal.trading_days("CM", date(2026, 11, 2), date(2026, 11, 6))
        conn = refdata._connect()
        conn.execute("INSERT INTO cm_eod (trade_date, instrument_id, series, close_paise) VALUES (?, 1, 'EQ', 100)",
                     (days[0].isoformat(),))
        self.assertEqual(job.cm_missing(date(2026, 11, 2), date(2026, 11, 6)), days[1:])


class CliTests(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        from stockify import __main__ as cli
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch.object(cli, "_load_env", lambda: None):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_backfill_arguments(self):
        seen = {}
        with mock.patch.object(job, "backfill", lambda **kw: seen.update(kw) or {"stopped": None}):
            code, _, _ = self.run_cli(["fundamentals", "backfill", "--from", "2022-01-01", "--limit", "5"])
        self.assertEqual(code, 0)
        self.assertEqual((seen["since"], seen["limit"]), (date(2022, 1, 1), 5))
        with mock.patch.object(job, "backfill", lambda **kw: seen.update(kw) or {"stopped": None}):
            self.run_cli(["fundamentals", "backfill"])
        self.assertEqual((seen["since"], seen["limit"]), (date(2021, 4, 1), None))

    def test_backfill_rejects_a_bad_limit_and_a_bad_date(self):
        with mock.patch.object(job, "backfill") as bf:
            with self.assertRaises(SystemExit):
                self.run_cli(["fundamentals", "backfill", "--limit", "0"])
            with self.assertRaises(SystemExit):
                self.run_cli(["fundamentals", "backfill", "--from", "April"])
        bf.assert_not_called()

    def test_a_blocked_backfill_exits_1(self):
        with mock.patch.object(job, "backfill", lambda **kw: {"stopped": "blocked"}):
            code, _, _ = self.run_cli(["fundamentals", "backfill"])
        self.assertEqual(code, 1)

    def test_refresh_runs_one_daily_run_now(self):
        seen = []
        with mock.patch.object(job, "run", lambda day, **kw: seen.append(day) or {"complete": True, "stopped": None,
                                                                                  "failed": []}):
            code, out, _ = self.run_cli(["fundamentals", "refresh"])
        self.assertEqual(code, 0)
        self.assertEqual(seen, [timeutil.ist_today()])
        self.assertIn('"complete": true', out)

    def test_test_record_fails_fast_without_a_kite_login(self):
        import kite_client
        from stockify.fundamentals import test_record
        with mock.patch.object(kite_client, "is_authenticated", return_value=False), \
                mock.patch.object(test_record, "run") as run:
            code, _, err = self.run_cli(["fundamentals", "test-record"])
        self.assertEqual(code, 1)
        self.assertIn("Kite", err)
        run.assert_not_called()

    def test_test_record_prints_progress(self):
        import kite_client
        from stockify.fundamentals import test_record

        def run(*, fetch, **kw):  # noqa: ANN001, ANN003, ANN202
            fetch(7, "day", date(2022, 1, 1), date(2026, 7, 31), client=None)
            return {"job_id": "wf1", "family_id": "walkforward/wf1", "start": "2022-01-01", "end": "2026-07-31",
                    "symbols": ["ALPHA"], "missing": [], "benchmark": {"label": "price index"}}
        stats = mock.MagicMock(outcome="ok", rows=10)
        with mock.patch.object(kite_client, "is_authenticated", return_value=True), \
                mock.patch.object(test_record, "run", run), \
                mock.patch.object(test_record.bars_history, "fetch", return_value=stats):
            code, out, _ = self.run_cli(["fundamentals", "test-record"])
        self.assertEqual(code, 0)
        self.assertIn("bars 1", out)
        self.assertIn("wf1", out)


if __name__ == "__main__":
    unittest.main()
