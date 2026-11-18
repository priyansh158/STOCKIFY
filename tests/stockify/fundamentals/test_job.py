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
OVERDUE = TUE - timedelta(days=15)      # listed longer ago than the weekly split allows: listed whatever the weekday
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
        for s in ("ALPHA", "BETA", "GAMMA"):            # every symbol listed yesterday: no weekly listing owed
            job.note_listed(s, TUE - timedelta(days=1))

    def patch(self, target, name, value):  # noqa: ANN001
        p = mock.patch.object(target, name, value)
        p.start()
        self.addCleanup(p.stop)

    def fake_list_for(self, symbol, *, client, since):  # noqa: ANN001, ANN202
        self.calls.append(("list", symbol))
        return []

    def fake_download(self, *, client, limit=None, deadline_ns=None, can_fetch=None):  # noqa: ANN001, ANN202
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
        self.assertEqual([n for n in self.names() if n != "list"], ["calendar", "download", "ingest", "order", "save"])
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

    def test_every_symbol_is_listed_once_a_week_spread_over_the_weekdays(self):
        """R12: each company is re-listed on its own weekday (a stable split by symbol), so a week's listings are
        spread over Monday to Friday instead of all on one day."""
        symbols = [f"S{i:03d}" for i in range(60)]
        self.patch(universe, "current", lambda: [{"symbol": s} for s in symbols])
        week = [date(2026, 11, 16) + timedelta(days=i) for i in range(5)]            # Monday to Friday
        for s in symbols:
            job.note_listed(s, week[0] - timedelta(days=3))
        listed: dict[str, list[date]] = {}
        for d in week:
            self.calls.clear()
            job.run(d, client=self.client)
            for c in self.calls:
                if c[0] == "list":
                    listed.setdefault(c[1], []).append(d)
        self.assertEqual(sorted(listed), symbols)                                       # each once ...
        self.assertTrue(all(len(v) == 1 for v in listed.values()), listed)
        per_day = [sum(1 for v in listed.values() if v[0] == d) for d in week]
        self.assertTrue(all(n > 0 for n in per_day), per_day)                          # ... spread over the week
        self.assertEqual({s: job.relist_weekday(s) for s in symbols},
                         {s: listed[s][0].weekday() for s in symbols})                  # stable by symbol

    def test_a_company_missed_on_its_weekday_is_listed_once_overdue(self):
        other = next(s for s in ("ALPHA", "BETA", "GAMMA") if job.relist_weekday(s) != TUE.weekday())
        job.note_listed(other, TUE - timedelta(days=9))
        job.run(TUE, client=self.client)
        self.assertNotIn(("list", other), self.calls)
        job.note_listed(other, TUE - timedelta(days=14))
        job.run(TUE, client=self.client)
        self.assertIn(("list", other), self.calls)

    def test_companies_never_listed_are_left_to_the_backfill(self):
        import logging
        was = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, was)
        with store._lock:
            store._connect().execute("DELETE FROM filings_listed WHERE symbol = 'GAMMA'")
        self.meeting("GAMMA", TUE - timedelta(days=1))
        with self.assertLogs("stockify.fundamentals", "INFO") as logs:
            res = job.run(TUE, client=self.client)
        self.assertNotIn("list", self.names())
        self.assertEqual(res["never_listed"], 1)
        self.assertTrue(any("stockify fundamentals backfill" in line for line in logs.output), logs.output)

    def test_one_symbols_trouble_is_listed_and_the_rest_go_on(self):
        for s in ("ALPHA", "BETA"):
            job.note_listed(s, OVERDUE)

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
        self.assertEqual(job.listed_on("ALPHA"), OVERDUE)


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
            job.note_listed(s, OVERDUE)

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
        self.assertEqual(job.listed_on("GAMMA"), OVERDUE)     # the rest runs on a later pass

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
        job.note_listed("ALPHA", OVERDUE)
        job.note_listed("BETA", OVERDUE)

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

    def test_a_spent_budget_is_marked_and_a_retry_asks_nse_nothing(self):
        """FS 8.3: one company failing while the day's budget is spent: the job is marked (the failure waits for
        tomorrow), and a second pass 30 minutes later on the same day makes no NSE request."""
        from stockify import daemon
        from stockify.fundamentals import nse_api as api
        state = scheduler.State()
        for s in ("ALPHA", "BETA", "GAMMA"):
            job.note_listed(s, OVERDUE)
        def flaky(symbol, *, client, since):  # noqa: ANN001, ANN202
            self.calls.append(("list", symbol))
            for _ in range(4):
                client.http.session.get(f"https://www.nseindia.com/api/x?symbol={symbol}")
            if symbol == "ALPHA":
                raise nse_files.NseError("HTTP 500")
            return []
        self.patch(filings, "list_for", flaky)
        with mock.patch.object(job, "daily_request_budget", return_value=6), \
                mock.patch.object(api.Client, "__init__", lambda c, http=None, cfg=None: _init_client(c, self.session)), \
                self.assertLogs("stockify", "INFO") as logs:
            daemon._run_job("fundamentals", TUE, state, timeutil.ist_ns(TUE, "20:40"))
            self.assertTrue(state.done("fundamentals", TUE))
            self.assertTrue(any("tomorrow" in line for line in logs.output), logs.output)
            sent_first = len(self.session.calls)
            self.assertGreaterEqual(sent_first, 6)
            self.calls.clear()
            daemon._run_job("fundamentals", TUE, state, timeutil.ist_ns(TUE, "21:10"))
        self.assertEqual(len(self.session.calls), sent_first)
        self.assertNotIn("list", self.names())

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

    def test_the_job_stops_before_the_next_session_within_its_budget(self):
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
        self.assertEqual(seen["budget"], 900)

    def test_the_daemon_job_asks_nse_nothing_after_the_deadline(self):
        """End to end through the daemon: at 08:25 IST the next session's preparation is 5 minutes away."""
        from stockify import daemon
        state = scheduler.State()
        wed = TUE + timedelta(days=1)
        with mock.patch.object(nse_files.Http, "get", side_effect=AssertionError("an NSE request")):
            daemon._run_job("fundamentals", TUE, state, timeutil.ist_ns(wed, "08:25"))
        self.assertEqual(self.calls, [])
        self.assertFalse(state.done("fundamentals", TUE))


class BudgetTests(JobCase):
    """R12: a run's NSE requests (warm-ups and retries included) stop at its budget; what is left waits for the next
    day, and the run still makes the day's list from what it has."""

    def requesting_list_for(self, symbol, *, client, since):  # noqa: ANN001, ANN202
        self.calls.append(("list", symbol))
        for _ in range(4):
            client.http.get(f"https://www.nseindia.com/api/x?symbol={symbol}")
        return []

    def test_the_listing_stops_at_the_budget(self):
        for s in ("ALPHA", "BETA", "GAMMA"):
            job.note_listed(s, OVERDUE)
        self.patch(filings, "list_for", self.requesting_list_for)
        res = job.run(TUE, client=self.client, budget=6)
        self.assertEqual([c[1] for c in self.calls if c[0] == "list"], ["ALPHA", "BETA"])
        self.assertNotIn("download", self.names())
        self.assertEqual(self.names()[-3:], ["ingest", "order", "save"])
        self.assertTrue(res["budget_spent"])
        self.assertTrue(res["complete"])
        self.assertEqual(res["requests"], len(self.session.calls))
        self.assertEqual(job.listed_on("GAMMA"), OVERDUE)

    def test_the_download_asks_the_budget_before_each_request(self):
        """Each file fetch is asked for against the requests really sent (a fetch's retries count)."""
        def download(*, client, limit=None, deadline_ns=None, can_fetch=None):  # noqa: ANN001, ANN202
            n = 0
            while can_fetch():
                for _ in range(3):                                   # a fetch with two retries
                    client.http.get("https://nsearchives.nseindia.com/corporate/xbrl/x.xml")
                n += 1
            return {"downloaded": n, "skipped": 0, "failed": 0, "stopped": "limit"}
        self.patch(filings, "download_pending", download)
        res = job.run(TUE, client=self.client, budget=10)
        self.assertEqual(res["download"]["downloaded"], 3)               # 4 (the warm-up too), 7, 10 sent
        self.assertTrue(res["budget_spent"])
        self.assertTrue(res["complete"])
        self.assertLessEqual(len(self.session.calls), 10 + 3)

    def test_without_a_budget_nothing_is_capped(self):
        seen = {}
        self.patch(filings, "download_pending",
                   lambda *, client, limit=None, deadline_ns=None, can_fetch=None:
                   seen.update(limit=limit, can_fetch=can_fetch) or dict(self.download))
        res = job.run(TUE, client=self.client)
        self.assertIsNone(seen["limit"])
        self.assertIsNone(seen["can_fetch"])
        self.assertFalse(res["budget_spent"])
        self.assertEqual(job.spent_on(TUE), 0)                          # only budgeted runs are counted

    def test_the_budget_is_per_trading_day_across_runs(self):
        for s in ("ALPHA", "BETA", "GAMMA"):
            job.note_listed(s, OVERDUE)
        self.patch(filings, "list_for", self.requesting_list_for)
        first = job.run(TUE, client=self.client, budget=6)
        self.assertEqual(job.spent_on(TUE), len(self.session.calls))
        sent = len(self.session.calls)
        self.calls.clear()
        second = job.run(TUE, client=self.client, budget=6)            # a retry the same evening
        self.assertEqual(len(self.session.calls), sent)                 # asks NSE nothing more
        self.assertNotIn("list", self.names())
        self.assertNotIn("calendar", self.names())
        self.assertTrue(second["budget_spent"])
        self.assertIn("save", self.names())
        self.assertTrue(first["budget_spent"])
        self.assertEqual(job.spent_on(TUE + timedelta(days=1)), 0)      # the next day starts afresh

    def test_the_http_is_left_as_it_was(self):
        before = vars(self.session).get("get")
        job.run(TUE, client=self.client, budget=5)
        self.assertEqual(vars(self.session).get("get"), before)

    def test_the_budget_is_a_checked_config_value(self):
        from stockify.config import loader
        doc = loader.load_json("fundamentals.json")
        self.assertEqual(doc["daily_request_budget"], 900)
        self.assertEqual(job.daily_request_budget(), 900)
        for bad in (0, -1, "900", True):
            with self.subTest(bad=bad), self.assertRaises(loader.ConfigError):
                loader._check_fundamentals({**doc, "daily_request_budget": bad})


class LockTests(JobCase):
    def test_the_lock_is_kept_beside_the_daemon_lock_not_in_the_backed_up_raw_files(self):
        from stockify import paths
        self.assertEqual(job._lock_path(), paths.fundamentals_lock_file())
        self.assertEqual(paths.fundamentals_lock_file().parent, paths.lab_dir())
        with job.hold():
            pass
        self.assertFalse((paths.nse_raw_dir("fundamentals") / "run.lock").exists())

    def test_a_second_run_is_refused_while_one_holds_the_lock(self):
        with job.hold():
            with self.assertRaises(job.Busy):
                job.run(TUE, client=self.client)
            with self.assertRaises(job.Busy), redirect_stdout(io.StringIO()):
                job.backfill(today=TUE, client=self.client, cm_missing=lambda a, b: [])
        self.assertEqual(self.calls, [])
        job.run(TUE, client=self.client)                                 # released: runs again
        self.assertIn("save", self.names())

    def test_the_daemon_job_leaves_a_held_run_unmarked(self):
        import logging
        from stockify import daemon
        was = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, was)
        state = scheduler.State()
        with job.hold(), self.assertLogs("stockify.daemon", "INFO") as logs:
            daemon._run_job("fundamentals", TUE, state, timeutil.ist_ns(TUE, "20:40"))
        self.assertFalse(state.done("fundamentals", TUE))
        self.assertTrue(any("another fundamentals run" in line for line in logs.output), logs.output)
        self.assertFalse(any(line.startswith("ERROR") for line in logs.output), logs.output)
        self.assertEqual(self.calls, [])


def _init_client(c, session) -> None:  # noqa: ANN001
    """A real nse_api.Client whose Http answers from the test's FakeSession on a fresh ManualClock."""
    c.http = nse_files.Http(session=session, clock=ManualClock(timeutil.ist_ns(TUE, "20:40")))
    c.cfg = nse_api.config()
    c.base = c.cfg["api"]["base"].rstrip("/")
    c._warmed = set()
    c.last_sha256 = None


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


class CatchUpClockTests(JobCase):
    """C1: every job of a catch-up pass is given the daemon clock's time when it starts, not the pass's start, so a
    job that runs after a long one judges the next session's deadline on the real time."""

    def setUp(self):
        from stockify import store as lab_store
        self.db_modules = (store, lab_store)
        super().setUp()
        import logging
        was = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, was)

    def test_a_job_after_a_long_one_asks_nse_and_kite_nothing_past_the_deadline(self):
        import portfolio_sync
        from stockify import daemon
        from stockify.daemon import exitcodes, market_day
        wed = TUE + timedelta(days=1)
        clock = ManualClock(timeutil.ist_ns(wed, "07:00"))     # woken at 07:00: Tuesday's evening is owed
        deadline = timeutil.ist_ns(wed, "08:20")
        real = daemon._run_job
        given: dict[str, int] = {}

        def run_job(job_name, d, state, now=None):  # noqa: ANN001, ANN202
            if job_name in ("desk", "fundamentals"):
                given[job_name] = now
                return real(job_name, d, state, now)
            if job_name == "finalise" and d == TUE:
                clock.advance(90 * MIN)                          # a long replay: 08:30 when it is done
            state.mark(job_name, d)
            return None

        def fake_market_day(**kw):  # noqa: ANN003, ANN202
            kw["on_idle"]()
            return exitcodes.TOKEN_EXPIRED

        state = scheduler.State()
        for back in range(1, 9):                                 # the week before is done
            for name in scheduler.CATCH_UP:
                state.mark(name, TUE - timedelta(days=back))
        with mock.patch.object(market_day, "run_market_day", fake_market_day), \
                mock.patch.object(daemon, "_run_job", run_job), \
                mock.patch.object(nse_files.Http, "get", side_effect=AssertionError("an NSE request")), \
                mock.patch.object(portfolio_sync, "fetch_snapshot", side_effect=AssertionError("a Kite call")):
            daemon.run_daemon(power=mock.MagicMock(idle_seconds=lambda: 0), clock=clock,
                              token_fresh=lambda: False, send_reminder=lambda s, b: None)
        self.assertGreaterEqual(given["desk"], deadline)
        self.assertGreaterEqual(given["fundamentals"], deadline)
        self.assertFalse(state.done("desk", TUE))
        self.assertFalse(state.done("fundamentals", TUE))
        self.assertEqual(self.calls, [])


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

    def test_backfill_stops_when_a_batch_makes_no_progress(self):
        batches = []

        def failing(*, client, limit=None, deadline_ns=None):  # noqa: ANN001, ANN202
            batches.append(limit)
            return {"downloaded": 0, "skipped": 0, "failed": limit, "stopped": "limit"}
        self.patch(filings, "download_pending", failing)
        res, out = self.run_backfill()
        self.assertEqual(len(batches), 1)
        self.assertEqual(res["stopped"], "no progress")
        self.assertIn("no file", out)
        self.assertNotIn("save", self.names())

    def test_backfill_goes_on_while_batches_make_progress(self):
        answers = [{"downloaded": 60, "skipped": 40, "failed": 0, "stopped": "limit"},
                   {"downloaded": 3, "skipped": 0, "failed": 2, "stopped": None}]
        self.patch(filings, "download_pending", lambda **kw: answers.pop(0))
        res, _ = self.run_backfill()
        self.assertEqual(res["download"]["downloaded"], 63)
        self.assertIsNone(res["stopped"])

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
    in_session = False
    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        from stockify import __main__ as cli
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch.object(cli, "_load_env", lambda: None), \
                mock.patch.object(scheduler, "session_in_progress", lambda now, cfg=None: self.in_session):
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
        seen, kws = [], []
        with mock.patch.object(job, "run", lambda day, **kw: seen.append(day) or kws.append(kw) or
                               {"complete": True, "stopped": None, "failed": []}):
            code, out, _ = self.run_cli(["fundamentals", "refresh"])
        self.assertEqual(code, 0)
        self.assertEqual(seen, [timeutil.ist_today()])
        self.assertEqual(kws[0].get("budget"), job.daily_request_budget())   # counted against the day's budget (R12)
        self.assertIn('"complete": true', out)

    def test_the_commands_refuse_to_start_during_the_session(self):
        self.in_session = True
        with mock.patch.object(job, "run") as run, mock.patch.object(job, "backfill") as bf:
            for cmd in ("backfill", "refresh", "test-record"):
                with self.subTest(cmd=cmd):
                    code, _, err = self.run_cli(["fundamentals", cmd])
                    self.assertEqual(code, 1)
                    self.assertIn("session", err)
        run.assert_not_called()
        bf.assert_not_called()

    def test_a_run_in_progress_is_reported_plainly(self):
        for cmd, target in (("backfill", "backfill"), ("refresh", "run")):
            with self.subTest(cmd=cmd), mock.patch.object(job, target, side_effect=job.Busy("held")):
                code, _, err = self.run_cli(["fundamentals", cmd])
            self.assertEqual(code, 1)
            self.assertIn("Another fundamentals run is in progress", err)

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
