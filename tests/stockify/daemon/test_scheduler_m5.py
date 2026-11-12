"""CDM 5.1 (M5.9): the paper day's jobs, caught up in order after every wake.

Nothing sleeps, nothing is really finalised or backed up: the jobs under test are called with fakes, and the live
login test uses a manual clock and a fake ticker (the paper fixture's landed day, no Kite, no network)."""
from __future__ import annotations

import json
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest
from tests.stockify.daemon import paper_fixture as pf
from tests.stockify.daemon import test_market_day as tmd

import kite_client
from stockify import engine, paths, store, timeutil
from stockify.config import loader
from stockify.daemon import exitcodes, lifecycle, market_day, scheduler
from stockify.daemon.clock import ManualClock
from stockify.daemon.finalise import FinaliseResult
from stockify.data import calendar as cal
from stockify.data import history, refdata

MIN = 60 * 1_000_000_000
MON, TUE_HOLIDAY, WED, THU = date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11), date(2026, 11, 12)


def ist(d: date, hhmm: str) -> int:
    return timeutil.ist_ns(d, hhmm)


def since(due, day: date):
    """The jobs owed for `day` or later (the week before it is another test's business)."""
    return [(j, d) for j, d in due if d >= day]


class JobsBase(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        cal.reload()
        self.state = scheduler.State()

    def sessions_with_day(self, day: date, status: str = "open", sid: str = "s1") -> None:
        store.create_session(sid, sid, {"x": 1}, 1)
        store.upsert_session_day(sid, day.isoformat(), status=status)


class CatchUpOrderTest(JobsBase):
    def test_finalise_runs_as_catch_up_before_day_bundles(self):
        """Woken at 08:00 on Wednesday: Monday's evening work is owed, oldest day first, in dependency order, and
        the backup comes last; all of it is due before Wednesday's day bundles (08:30)."""
        for job in ("nse_fetch", "margin_files"):
            self.state.mark(job, MON)
        due = since(scheduler.missed_jobs(ist(WED, "08:00"), self.state), MON)
        self.assertEqual(due, [("finalise", MON), ("parity", MON), ("daily_summary", MON), ("backup", WED)])
        self.assertLess(ist(WED, "08:00"), scheduler.plan(WED).day_bundles_ns)

    def test_the_whole_evening_is_ordered_per_day(self):
        due = since(scheduler.missed_jobs(ist(WED, "08:00"), self.state), MON)
        mon = [j for j, d in due if d == MON]
        self.assertEqual(mon, ["nse_fetch", "margin_files", "finalise", "parity", "daily_summary"])
        days = [d for j, d in due if j != "backup"]
        self.assertEqual(days, sorted(days))                                    # oldest day first
        self.assertEqual(due[-1][0], "backup")

    def test_holidays_skipped(self):
        due = since(scheduler.missed_jobs(ist(TUE_HOLIDAY, "23:00"), self.state), MON)
        self.assertNotIn(TUE_HOLIDAY, [d for _, d in due])
        self.assertIn(("finalise", MON), due)
        self.assertIn(("backup", MON), due)                                      # the latest trading day, not the 10th

    def test_backup_always_catch_up_after_wake(self):
        for job in ("nse_fetch", "margin_files", "finalise", "parity", "daily_summary"):
            self.state.mark(job, MON)
        self.assertEqual(since(scheduler.missed_jobs(ist(WED, "05:44"), self.state), MON), [("backup", MON)])
        self.assertEqual(since(scheduler.missed_jobs(ist(WED, "06:00"), self.state), MON), [("backup", WED)])    # the wake time
        self.assertEqual(scheduler.missed_jobs(ist(WED, "17:00"), self.state)[-1], ("backup", WED))  # still owed
        self.state.mark("backup", WED)
        self.assertNotIn("backup", [j for j, _ in scheduler.missed_jobs(ist(WED, "17:00"), self.state)])

    def test_only_compaction_blocks_sleep(self):
        self.assertEqual(scheduler.BLOCKS_SLEEP, ("compaction",))

    def test_dependent_jobs_wait_for_finalise(self):
        self.assertFalse(scheduler.runnable("parity", MON, self.state))
        self.assertFalse(scheduler.runnable("daily_summary", MON, self.state))
        self.assertTrue(scheduler.runnable("finalise", MON, self.state))
        self.state.mark("finalise", MON)
        self.assertTrue(scheduler.runnable("parity", MON, self.state))


class FinaliseJobTest(JobsBase):
    def run_job(self, now: str, result="finalised", day: date = MON, nse_done: bool = False):
        from stockify import daemon
        if nse_done:
            self.state.mark("nse_fetch", day)
        calls = []

        def fake(sid, d, *, files_complete, cutoff_passed, live=None):
            calls.append((sid, d, files_complete, cutoff_passed))
            if isinstance(result, BaseException):
                raise result
            return FinaliseResult(sid, d, result if files_complete or cutoff_passed else "waiting")

        with mock.patch("stockify.daemon.finalise.finalise_day", fake):
            daemon._run_job("finalise", day, self.state, ist(WED, now))
        return calls

    def test_cutoff_is_next_day_bundle_time(self):
        self.sessions_with_day(MON)
        cutoff = scheduler.plan(WED).day_bundles_ns                              # Tuesday is a holiday: Wednesday
        self.assertEqual(cutoff, ist(WED, "08:30"))
        self.assertEqual(self.run_job("08:29"), [("s1", MON, False, False)])
        self.assertFalse(self.state.done("finalise", MON))                       # waiting: retried later
        self.assertEqual(self.run_job("08:30"), [("s1", MON, False, True)])
        self.assertTrue(self.state.done("finalise", MON))

    def test_files_complete_when_nse_fetch_is_done(self):
        self.sessions_with_day(MON)
        self.assertEqual(self.run_job("08:00", nse_done=True), [("s1", MON, True, False)])

    def test_busy_or_a_raised_error_is_retried_and_never_stops_others(self):
        self.sessions_with_day(MON)
        store.create_session("s2", "s2", {"x": 1}, 1)
        store.upsert_session_day("s2", MON.isoformat(), status="open")
        calls = self.run_job("09:00", result="busy", nse_done=True)
        self.assertEqual([c[0] for c in calls], ["s1", "s2"])
        self.assertFalse(self.state.done("finalise", MON))
        calls = self.run_job("09:00", result=OSError("disk"), nse_done=True)
        self.assertEqual([c[0] for c in calls], ["s1", "s2"])                    # s2 was still tried
        self.assertFalse(self.state.done("finalise", MON))

    def test_a_failed_day_is_recorded_and_the_job_is_done(self):
        self.sessions_with_day(MON)
        self.run_job("09:00", result="failed", nse_done=True)
        self.assertTrue(self.state.done("finalise", MON))
        kinds = [r["kind"] for r in store.timeline("s1")]
        self.assertIn("session_failed", kinds)

    def test_a_day_with_no_sessions_is_done(self):
        self.assertEqual(self.run_job("09:00", nse_done=True), [])
        self.assertTrue(self.state.done("finalise", MON))


class ParityJobTest(JobsBase):
    def run_job(self, journal, recorder):
        from stockify import daemon, parity

        def res(kind, outcome):
            return lambda sid, d: parity.ParityResult(sid, d, kind, outcome)

        with mock.patch("stockify.parity.journal_parity", res("journal", journal)), \
                mock.patch("stockify.parity.recorder_parity", res("recorder", recorder)):
            daemon._run_job("parity", MON, self.state, ist(WED, "08:00"))

    def test_skipped_results_are_retried(self):
        self.sessions_with_day(MON, "finalised")
        self.run_job("pass", "skipped")
        self.assertFalse(self.state.done("parity", MON))
        self.run_job("skipped", "pass")
        self.assertFalse(self.state.done("parity", MON))
        self.run_job("pass", "fail")                                             # a failure is a result, not a skip
        self.assertTrue(self.state.done("parity", MON))

    def test_an_error_does_not_escape(self):
        from stockify import daemon
        self.sessions_with_day(MON, "finalised")
        with mock.patch("stockify.parity.journal_parity", side_effect=RuntimeError("boom")):
            daemon._run_job("parity", MON, self.state, ist(WED, "08:00"))
        self.assertFalse(self.state.done("parity", MON))


class BackupJobTest(JobsBase):
    def run_job(self, target):
        from stockify import daemon
        real = loader.load_json
        calls = []
        with mock.patch.object(loader, "load_json",
                               lambda n, *a, **k: {"backup": {"target": target, "include_span": False}}
                               if n == "storage.json" else real(n, *a, **k)), \
                mock.patch("stockify.backup.run", lambda t, *, day: calls.append((t, day))):
            daemon._run_job("backup", WED, self.state, ist(WED, "06:00"))
        return calls

    def test_a_null_target_is_a_clear_skip_never_an_error(self):
        import logging
        was = logging.root.manager.disable                                       # the runner switches logging off
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, was)
        with self.assertLogs("stockify.daemon", "INFO") as logs:
            self.assertEqual(self.run_job(None), [])
        self.assertTrue(any("no target" in line for line in logs.output))
        self.assertTrue(self.state.done("backup", WED))

    def test_a_target_is_backed_up(self):
        self.assertEqual(self.run_job("D:\\backups"), [(Path("D:\\backups"), WED)])
        self.assertTrue(self.state.done("backup", WED))

    def test_a_refused_target_does_not_escape(self):
        from stockify import backup, daemon
        with mock.patch("stockify.backup.run", side_effect=backup.BackupRefused("no")), \
                mock.patch.object(loader, "load_json", lambda n, *a, **k: {"backup": {"target": "X"}}):
            daemon._run_job("backup", WED, self.state, ist(WED, "06:00"))


class CatchUpWiringTest(JobsBase):
    def test_run_daemon_catch_up_runs_in_order_and_skips_jobs_whose_finalise_is_not_done(self):
        from stockify import daemon
        ran = []
        clock = ManualClock(ist(WED, "08:00"))

        def fake_market_day(**kw):
            kw["on_idle"]()
            return exitcodes.TOKEN_EXPIRED

        def fake_job(job, d, state, now=None):
            ran.append((job, d))
            if job == "finalise":
                state.mark("finalise", d)

        self.state.mark("nse_fetch", MON)
        self.state.mark("margin_files", MON)
        with mock.patch.object(market_day, "run_market_day", fake_market_day), \
                mock.patch.object(daemon, "_run_job", fake_job):
            daemon.run_daemon(power=mock.MagicMock(idle_seconds=lambda: 0), clock=clock,
                              token_fresh=lambda: False, send_reminder=lambda s, b: None)
        self.assertEqual(since(ran, MON)[:4], [("finalise", MON), ("parity", MON), ("daily_summary", MON), ("backup", WED)])

    def test_a_failing_job_does_not_stop_the_others(self):
        from stockify import daemon
        ran = []
        clock = ManualClock(ist(WED, "08:00"))

        def fake_market_day(**kw):
            kw["on_idle"]()
            return exitcodes.TOKEN_EXPIRED

        def fake_job(job, d, state, now=None):
            ran.append((job, d))
            if job == "finalise":
                raise RuntimeError("boom")

        self.state.mark("nse_fetch", MON)
        self.state.mark("margin_files", MON)
        with mock.patch.object(market_day, "run_market_day", fake_market_day), \
                mock.patch.object(daemon, "_run_job", fake_job):
            daemon.run_daemon(power=mock.MagicMock(idle_seconds=lambda: 0), clock=clock,
                              token_fresh=lambda: False, send_reminder=lambda s, b: None)
        self.assertIn(("backup", WED), ran)


class RefdataTest(unittest.TestCase):
    """`day_bundles` for D starts only once refdata is in (or has been given up on as stale)."""

    def setUp(self):
        cal.reload()
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])

    def refresh(self, start: str, outcomes, **kw):
        from stockify import daemon
        clock = ManualClock(ist(WED, start))
        attempts = []

        def fake(day):
            attempts.append(timeutil.ns_to_ist(clock.now_ns()).strftime("%H:%M"))
            out = outcomes[min(len(attempts), len(outcomes)) - 1]
            if out is not None:
                raise out

        with mock.patch.object(daemon, "_ensure_refdata", fake):
            stale = daemon.refresh_refdata(WED, clock, threading.Event(), **kw)
        return stale, attempts, clock

    def test_day_bundles_waits_for_refdata(self):
        stale, attempts, _ = self.refresh("08:30", [OSError("net"), OSError("net"), None])
        self.assertEqual(attempts, ["08:30", "08:35", "08:40"])                 # retried every 5 minutes
        self.assertFalse(stale)

    def test_refdata_failure_uses_stale_label(self):
        stale, attempts, clock = self.refresh("08:58", [OSError("net")])
        self.assertTrue(stale)
        self.assertEqual(attempts[-1], "09:10")                                  # tried until 09:10, then gave up
        self.assertEqual(attempts, ["08:58", "09:03", "09:08", "09:10"])

    def test_a_token_failure_is_not_stale_it_waits_for_the_login(self):
        stale, attempts, clock = self.refresh("09:05", [kite_client.KiteTokenExpired("x"), None])
        self.assertFalse(stale)
        self.assertEqual(attempts, ["09:05", "09:10"])

    def test_after_0910_a_later_login_runs_at_once(self):
        stale, attempts, _ = self.refresh("10:00", [None])
        self.assertFalse(stale)
        self.assertEqual(attempts, ["10:00"])

    def test_after_0910_a_non_token_failure_is_stale_at_once(self):
        stale, attempts, _ = self.refresh("10:00", [OSError("net")])
        self.assertTrue(stale)
        self.assertEqual(attempts, ["10:00"])

    def test_a_stop_ends_the_wait(self):
        from stockify import daemon
        stop = threading.Event()
        stop.set()
        with mock.patch.object(daemon, "_ensure_refdata", side_effect=OSError("net")):
            self.assertIsNone(daemon.refresh_refdata(WED, ManualClock(ist(WED, "08:30")), stop))


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class SessionDayWiringTest(pf.PaperDayTest):
    def setUp(self):
        super().setUp()
        self.record(seconds=120)
        self.cfg = self.session_cfg(strategy={"kind": "script", "actions": []})

    def add_armed(self, sid="s1"):
        store.create_session(sid, sid, self.cfg, 1)
        lifecycle.arm(sid, wall_ns=2)

    def test_refdata_failure_uses_stale_label_in_the_bundle(self):
        self.add_armed()
        clock = ManualClock(pf.t("08:50:00"))
        sds = market_day.open_sessions(market_day.choose_sessions(["s1"]), pf.DAY, clock, refdata_stale=True)
        self.assertEqual(len(sds), 1)
        sds[0].close()
        bundle = json.loads(paths.day_bundle_file("s1", pf.DAY).read_text(encoding="utf-8"))
        self.assertIn("refdata_stale", bundle["labels"])

    def test_session_blocked_when_yesterday_not_finalised(self):
        self.add_armed()
        store.upsert_session_day("s1", "2026-11-16", status="open")             # the trading day before DAY
        clock = ManualClock(pf.t("08:50:00"))
        for _ in range(2):                                                       # asked twice: one row only
            self.assertEqual(market_day.open_sessions(market_day.choose_sessions(["s1"]), pf.DAY, clock), [])
        self.assertEqual(store.get_session("s1")["status"], "ARMED")
        kinds = [r["kind"] for r in store.timeline("s1")]
        self.assertEqual(kinds.count("blocked_on_finalise"), 1)
        self.assertNotIn("session_failed", kinds)


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class LateLoginTest(pf.PaperDayTest):
    def setUp(self):
        super().setUp()
        self.record(seconds=120)
        real = loader.load_json
        for patcher in (
                mock.patch.object(loader, "load_json", lambda name, *a, **kw: pf.UNIVERSE if name == "universe.json"
                                  else real(name, *a, **kw)),
                mock.patch("stockify.daemon.ticker.make_live_ticker", lambda *a, **kw: self.ticker),
                mock.patch("stockify.daemon.ticker.live_call_in_ticker", lambda fn, *args: fn(*args)),
                mock.patch("stockify.kite_readonly.client", lambda: None),
                mock.patch.object(kite_client, "load_cached_token", lambda: {"access_token": "not-a-real-token"}),
                mock.patch.object(kite_client, "_api_key", lambda: "not-a-real-key"),
                mock.patch("stockify.daemon._ensure_refdata", lambda day: None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.events: list[str] = []
        self.ticker = tmd.FakeLiveTicker(self.events)
        self.clock = ManualClock(timeutil.ist_ns(pf.DAY, "06:30"))
        store.create_session("s1", "s1", self.session_cfg(strategy={"kind": "script", "actions": []}), 1)
        lifecycle.arm("s1", wall_ns=2)

    def test_late_login_after_0910_still_runs_session(self):
        """Login at 10:00 IST: refdata, the bundle, then RUNNING, with a token gap from the 09:15 open."""
        login_at = timeutil.ist_ns(pf.DAY, "10:00")
        stop = threading.Event()
        real = market_day._margin_poller

        def factory(dispatcher, session, clock):
            poller = real(dispatcher, session, clock)
            poller.start = lambda: stop.set()
            poller.stop = lambda: None
            return poller

        with mock.patch.object(market_day, "_margin_poller", factory):
            code = market_day.run_market_day(ticker_spec="live", session_ids=["s1"], clock=self.clock, stop=stop,
                                             token_fresh=lambda: self.clock.now_ns() >= login_at)
        self.assertEqual(code, exitcodes.OK)
        self.assertEqual(store.get_session("s1")["status"], "RUNNING")
        gaps = [r for r in tmd.decode("s1") if r["kind"] == "GAP" and r["reason"] == "token_expired"
                and r["phase"] == 0]
        self.assertEqual([g["t_event"] for g in gaps], [timeutil.ist_ns(pf.DAY, "09:15:00")])


if __name__ == "__main__":
    unittest.main()
