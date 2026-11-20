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
        self.assertEqual(due, [("finalise", MON), ("compaction", MON), ("parity", MON), ("daily_summary", MON),
                               ("desk", MON), ("fundamentals", MON), ("watch_results", MON), ("backup", WED)])
        self.assertLess(ist(WED, "08:00"), scheduler.plan(WED).day_bundles_ns)

    def test_the_whole_evening_is_ordered_per_day(self):
        due = since(scheduler.missed_jobs(ist(WED, "08:00"), self.state), MON)
        mon = [j for j, d in due if d == MON]
        self.assertEqual(mon, ["nse_fetch", "margin_files", "finalise", "compaction", "parity", "daily_summary", "desk",
                               "fundamentals", "watch_results"])
        days = [d for j, d in due if j != "backup"]
        self.assertEqual(days, sorted(days))                                    # oldest day first
        self.assertEqual(due[-1][0], "backup")

    def test_holidays_skipped(self):
        due = since(scheduler.missed_jobs(ist(TUE_HOLIDAY, "23:00"), self.state), MON)
        self.assertNotIn(TUE_HOLIDAY, [d for _, d in due])
        self.assertIn(("finalise", MON), due)
        self.assertIn(("backup", MON), due)                                      # the latest trading day, not the 10th

    def test_backup_always_catch_up_after_wake(self):
        for job in ("nse_fetch", "margin_files", "finalise", "compaction", "parity", "daily_summary", "desk",
                    "fundamentals", "watch_results"):
            self.state.mark(job, MON)
        self.assertEqual(since(scheduler.missed_jobs(ist(WED, "05:44"), self.state), MON), [("backup", MON)])
        self.assertEqual(since(scheduler.missed_jobs(ist(WED, "06:00"), self.state), MON), [("backup", WED)])    # the wake time
        self.assertEqual(scheduler.missed_jobs(ist(WED, "17:00"), self.state)[-1], ("backup", WED))  # still owed
        self.state.mark("backup", WED)
        self.assertNotIn("backup", [j for j, _ in scheduler.missed_jobs(ist(WED, "17:00"), self.state)])

    def test_todays_backup_is_due_only_after_0545(self):
        for job in ("nse_fetch", "margin_files", "finalise", "parity", "daily_summary"):
            self.state.mark(job, MON)
        self.assertNotIn(("backup", WED), scheduler.missed_jobs(ist(WED, "05:44"), self.state))
        self.assertIn(("backup", WED), scheduler.missed_jobs(ist(WED, "05:45"), self.state))

    def test_only_compaction_blocks_sleep(self):
        self.assertEqual(scheduler.BLOCKS_SLEEP, ("compaction",))

    def test_compaction_is_caught_up_for_a_day_the_machine_slept_through_and_waits_for_finalise(self):
        self.assertIn("compaction", scheduler.CATCH_UP)
        self.assertEqual(scheduler.DAY_ORDER.index("compaction"), scheduler.DAY_ORDER.index("finalise") + 1)
        self.assertFalse(scheduler.runnable("compaction", MON, self.state))
        self.state.mark("finalise", MON)
        self.assertTrue(scheduler.runnable("compaction", MON, self.state))
        self.assertIn(("compaction", MON), scheduler.missed_jobs(ist(WED, "08:00"), self.state))
        self.state.mark("compaction", MON)
        self.assertNotIn(("compaction", MON), scheduler.missed_jobs(ist(WED, "08:00"), self.state))
        self.assertNotIn(("compaction", WED), scheduler.missed_jobs(ist(WED, "08:00"), self.state))   # 16:30 has not come

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
        self.assertEqual(store.get_session_day("s1", MON.isoformat())["status"], "finalised_failed")   # C-1: carried on

    def test_a_day_the_dispatcher_failed_is_still_offered_to_finalise(self):
        self.sessions_with_day(MON)
        store.add_timeline("s1", "session_failed", {"error": "x"}, wall_ns=ist(MON, "11:02"), engine_ns=None)
        self.assertEqual(self.run_job("09:00", nse_done=True), [("s1", MON, True, True)])

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
        self.assertEqual(since(ran, MON)[:7], [("finalise", MON), ("compaction", MON), ("parity", MON),
                                               ("daily_summary", MON), ("desk", MON), ("fundamentals", MON),
                                               ("backup", WED)])

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
class LiveBase(pf.PaperDayTest):
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

    def run_live(self, *, stop=None, login_at=None, **kw):
        """One live run on the manual clock; the margin poller's start stops it (nothing else ends a live run)."""
        stop = stop or threading.Event()
        real = market_day._margin_poller

        def factory(dispatcher, session, clock):
            poller = real(dispatcher, session, clock)
            poller.start = lambda: stop.set() if self.stop_at_poller else None
            poller.stop = lambda: None
            return poller

        fresh = (lambda: self.clock.now_ns() >= login_at) if login_at else (lambda: True)
        with mock.patch.object(market_day, "_margin_poller", factory):
            return market_day.run_market_day(ticker_spec="live", session_ids=["s1"], clock=self.clock, stop=stop,
                                             token_fresh=fresh, **kw)

    stop_at_poller = True


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class LateLoginTest(LiveBase):
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
        [row] = [r for r in store.timeline("s1") if r["kind"] == "gap" and r["detail"]["cause"] == "late_login"]
        self.assertEqual(row["detail"]["t_ns"], gaps[0]["t_event"])          # the journaled t, not an intended one

    def test_the_timeline_gives_the_journaled_t_after_a_d68_lift(self):
        """A paused session's journal already holds a `strategy_control` at the start; the gap's journaled t is lifted
        to it (D-68), and the timeline row says that t, not 09:15."""
        store.set_session_state("s1", "ARMED", ["owner"])
        self.run_live(login_at=timeutil.ist_ns(pf.DAY, "10:00"))
        gaps = [r for r in tmd.decode("s1") if r["kind"] == "GAP" and r["reason"] == "token_expired"
                and r["phase"] == 0]
        [row] = [r for r in store.timeline("s1") if r["kind"] == "gap" and r["detail"]["cause"] == "late_login"]
        self.assertEqual(row["detail"]["t_ns"], gaps[0]["t_event"])
        self.assertGreater(gaps[0]["t_event"], timeutil.ist_ns(pf.DAY, "09:15:00"))


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class RecorderFirstTest(LiveBase):
    """I-3, I-4 and the differential review's M1: a session can never cost the recording."""

    def events_of(self, *names):
        return [e for e in self.events if e in names]

    def test_a_long_session_replay_does_not_delay_the_recorder_start(self):
        from stockify.daemon import recorder as recorder_mod, recovery
        from stockify.daemon import journal
        from stockify.data import packs
        self.assertEqual(self.run_live(), exitcodes.OK)                         # day one's journal exists now
        t = timeutil.ist_ns(pf.DAY, "09:15:00")
        w = journal.JournalWriter(paths.journal_file("s1", pf.DAY), fsync_every_mono_ns=10 ** 9, mono=lambda: 0)
        w.append([(t, packs.RANK[packs.CLOCK], packs.enc_clock(t))])                # so a restart has a journal to replay
        w.close(finalise=False)
        self.events.clear()
        real_recover, real_start = recovery.recover, recorder_mod.Recorder.start

        def slow_recover(*a, **kw):
            self.events.append("recover")
            return real_recover(*a, **kw)

        def start(rec):
            self.events.append("recorder.start")
            return real_start(rec)

        with mock.patch.object(recovery, "recover", slow_recover),                 mock.patch.object(recorder_mod.Recorder, "start", start):
            self.assertEqual(self.run_live(), exitcodes.OK)
        order = self.events_of("recorder.start", "ticker.connect", "recover")
        self.assertEqual(order, ["recorder.start", "ticker.connect", "recover"])
        self.assertEqual(store.get_session("s1")["status"], "RUNNING")

    def run_until(self, seconds, **kw):
        stop = threading.Event()
        timer = threading.Timer(seconds, stop.set)
        timer.start()
        self.addCleanup(timer.cancel)
        self.stop_at_poller = False
        return self.run_live(stop=stop, **kw)

    def test_a_dead_engine_is_noticed_once_and_the_recorder_carries_on(self):
        from stockify.daemon import dispatcher as dispatcher_mod
        told = []
        with mock.patch.object(dispatcher_mod.Dispatcher, "run", side_effect=RuntimeError("engine boom")),                 mock.patch("stockify.daemon.notify.session_event", lambda *a, **kw: told.append((a, kw))):
            self.assertEqual(self.run_until(2.5), exitcodes.OK)
        self.assertIn("ticker.connect", self.events)
        rows = [r for r in store.timeline("s1") if r["kind"] == "session_failed"]
        self.assertEqual(len(rows), 1)
        self.assertIn("engine boom", rows[0]["detail"]["error"])
        self.assertEqual([a[0] for a, _ in told], ["session_error"])               # told once, however long it ran
        status = json.loads(paths.daemon_status_file().read_text(encoding="utf-8"))
        self.assertIn("engine_thread_died", status["alarms"])
        self.assertTrue(status["sessions"]["s1"]["failed"])

    def test_a_corrupt_session_row_does_not_stop_the_recorder(self):
        import sqlite3
        conn = store._connect()
        conn.execute("UPDATE sessions SET config_json = '{not json' WHERE session_id = 's1'")
        conn.commit()
        told = []
        with mock.patch("stockify.daemon.notify.session_event", lambda *a, **kw: told.append(a)):
            self.assertEqual(self.run_until(1.5), exitcodes.OK)
        self.assertIn("ticker.connect", self.events)
        self.assertEqual([a[0] for a in told], ["session_error"])

    def test_a_bad_paper_json_does_not_stop_the_recorder(self):
        real = loader.load_json

        def load(name, *a, **kw):
            if name == "paper.json":
                raise loader.ConfigError("paper.json: clock_every_s is missing")
            return real(name, *a, **kw)

        told = []
        with mock.patch.object(loader, "load_json", load),                 mock.patch("stockify.daemon.notify.session_event", lambda *a, **kw: told.append(a)):
            self.assertEqual(self.run_until(1.5), exitcodes.OK)
        self.assertIn("ticker.connect", self.events)
        self.assertEqual([a[0] for a in told], ["session_error"])


PREV = "2026-11-16"                                                              # the trading day before pf.DAY


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class OpenSequenceTest(LiveBase):
    """CDM 5.2: what the open needs runs unthrottled before the sessions open; the rest of the catch-up runs only
    after the recorder is up; a blocked session opens as soon as D-1 is finalised."""

    def setUp(self):
        super().setUp()
        from stockify.daemon import finalise
        store.upsert_session_day("s1", PREV, status="open")
        self.d1_done = False

        def next_sod_state(sid, day):                       # a real state is the engine's business, not this test's
            if not self.d1_done:
                raise finalise.FinaliseError(f"{PREV} is not finalised (open)")
            return None

        for patcher in (mock.patch.object(market_day, "IDLE_PASS_S", 0.05),
                        mock.patch("stockify.daemon.finalise.next_sod_state", next_sod_state),
                        mock.patch("stockify.daemon.finalise.next_sod_state_from",
                                   lambda sid, day: (next_sod_state(sid, day), None))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def finalise_prev(self):
        self.d1_done = True

    def test_d1_finalises_late_then_the_session_opens_that_day(self):
        calls = []

        def before_open(day):
            calls.append(day)
            if len(calls) >= 2:                                                  # the retry: now it finalises
                self.finalise_prev()

        self.stop_at_poller = False
        opened = []
        real_open = market_day.open_sessions

        def watching(*a, **kw):
            out = real_open(*a, **kw)
            opened.append([sd.session_id for sd in out])
            return out

        with mock.patch.object(market_day, "open_sessions", watching):
            # the first run: blocked, then the idle pass finalises D-1 and the run ends so that it can start again
            self.assertEqual(self.run_live(before_open=before_open), exitcodes.OK)
            self.assertEqual(opened, [[]])
            self.assertEqual(store.get_session("s1")["status"], "ARMED")
            kinds = [r["kind"] for r in store.timeline("s1")]
            self.assertEqual(kinds.count("blocked_on_finalise"), 1)
            self.stop_at_poller = True
            self.assertEqual(self.run_live(before_open=before_open), exitcodes.OK)      # the daemon's next pass
        self.assertEqual(opened[-1], ["s1"])
        self.assertEqual(store.get_session("s1")["status"], "RUNNING")
        self.assertTrue(paths.journal_file("s1", pf.DAY).exists())

    def test_before_open_runs_unthrottled_in_the_daemon(self):
        from stockify import daemon
        ran = []
        state = scheduler.State()
        state.mark("nse_fetch", MON)
        state.mark("margin_files", MON)

        def fake_market_day(**kw):
            kw["on_idle"]()                                                      # tried: the 15 minute throttle starts
            kw["before_open"](WED)                                               # and this still runs
            return exitcodes.TOKEN_EXPIRED

        def fake_job(job, d, st, now=None):
            ran.append((job, d))

        clock = ManualClock(ist(WED, "08:40"))
        with mock.patch.object(market_day, "run_market_day", fake_market_day), \
                mock.patch.object(daemon, "_run_job", fake_job):
            daemon.run_daemon(power=mock.MagicMock(idle_seconds=lambda: 0), clock=clock,
                              token_fresh=lambda: False, send_reminder=lambda s, b: None)
        self.assertEqual(ran.count(("finalise", MON)), 2)                        # loop top; the idle pass is throttled

    def test_heavy_catch_up_waits_for_the_close(self):
        """M-2: while the session is on, the recording process does not replay an earlier day (parity, finalise) or
        copy the lab (backup); the light jobs still run, and everything runs again at the close."""
        from stockify import daemon
        ran = []
        state = scheduler.State()
        for job in ("nse_fetch", "margin_files"):
            state.mark(job, MON)

        def fake_market_day(**kw):
            kw["on_idle"]()
            return exitcodes.TOKEN_EXPIRED

        def run_at(hhmm):
            ran.clear()
            clock = ManualClock(ist(WED, hhmm))
            with mock.patch.object(market_day, "run_market_day", fake_market_day),                     mock.patch.object(daemon, "_run_job", lambda job, d, st, now=None: ran.append((job, d))):
                daemon.run_daemon(power=mock.MagicMock(idle_seconds=lambda: 0), clock=clock,
                                  token_fresh=lambda: False, send_reminder=lambda s, b: None)
            return {j for j, _ in ran}

        self.assertFalse(run_at("10:00") & {"finalise", "parity", "backup"})
        self.assertIn("nse_fetch", run_at("10:00") | {"nse_fetch"})
        self.assertTrue({"finalise", "backup"} <= run_at("08:40"))
        self.assertTrue(scheduler.session_in_progress(ist(WED, "10:00")))
        self.assertFalse(scheduler.session_in_progress(ist(WED, "08:40")))
        self.assertFalse(scheduler.session_in_progress(ist(WED, "15:45")))
        self.assertFalse(scheduler.session_in_progress(ist(TUE_HOLIDAY, "10:00")))

    def test_a_failing_catch_up_still_starts_the_recorder(self):
        def boom(*a, **kw):
            raise RuntimeError("calendar broke")

        self.finalise_prev()
        with mock.patch("stockify.daemon.scheduler.missed_jobs", boom):
            code = self.run_live(on_idle=lambda: scheduler.missed_jobs(0, None), before_open=boom)
        self.assertEqual(code, exitcodes.OK)
        self.assertIn("ticker.connect", self.events)

    def test_the_rest_of_the_catch_up_runs_after_the_recorder_is_up(self):
        order = []
        self.ticker.connect = lambda threaded=False: order.append("connect")
        stop = threading.Event()
        self.stop_at_poller = False

        def idle():
            order.append("idle")
            stop.set()

        self.finalise_prev()
        self.run_live(stop=stop, on_idle=idle)
        self.assertEqual(order[:2], ["connect", "idle"])


class ParityGiveUpTest(JobsBase):
    def test_skipped_parity_is_tried_8_times_a_day_then_given_up_once(self):
        from stockify import daemon, parity
        self.sessions_with_day(MON, "finalised")

        def skipped(sid, d):
            return parity.ParityResult(sid, d, "recorder", "skipped", {"reason": "MON was not recorded"})

        with mock.patch("stockify.parity.journal_parity",
                        lambda sid, d: parity.ParityResult(sid, d, "journal", "pass")), \
                mock.patch("stockify.parity.recorder_parity", skipped):
            for n in range(1, 8):
                daemon._run_job("parity", MON, self.state, ist(WED, "08:00"))
                self.assertFalse(self.state.done("parity", MON), n)
            self.assertEqual([r for r in store.timeline("s1") if r["kind"] == "parity_gave_up"], [])
            daemon._run_job("parity", MON, self.state, ist(WED, "08:00"))            # the 8th
        self.assertTrue(self.state.done("parity", MON))
        [row] = [r for r in store.timeline("s1") if r["kind"] == "parity_gave_up"]
        self.assertIn("was not recorded", json.dumps(row["detail"]))
        self.assertEqual(row["detail"]["day"], MON.isoformat())


class BackupRetryTest(JobsBase):
    def run_job(self, errors=(), refused=False):
        from stockify import backup, daemon
        real = loader.load_json

        def fake(t, *, day):
            if refused:
                raise backup.BackupRefused("no")
            res = backup.BackupResult()
            res.errors.extend(errors)
            return res

        with mock.patch.object(loader, "load_json", lambda n, *a, **k: {"backup": {"target": "X"}}
                               if n == "storage.json" else real(n, *a, **k)), \
                mock.patch("stockify.backup.run", fake):
            daemon._run_job("backup", WED, self.state, ist(WED, "06:00"))

    def test_copy_errors_are_retried_at_most_3_times_a_day(self):
        for n in (1, 2):
            self.run_job(errors=["copy failed"])
            self.assertFalse(self.state.done("backup", WED), n)
        self.run_job(errors=["copy failed"])
        self.assertTrue(self.state.done("backup", WED))                          # the third try is the last

    def test_a_success_is_done_at_once(self):
        self.run_job()
        self.assertTrue(self.state.done("backup", WED))

    def test_a_refused_target_is_a_logged_skip(self):
        self.run_job(refused=True)
        self.assertTrue(self.state.done("backup", WED))

    def test_backup_is_throttled_between_tries(self):
        self.assertGreater(scheduler.retry_every_ns("backup"), 0)


SDK_TOKEN_ERROR = type("TokenException", (Exception,), {"__module__": "kiteconnect.exceptions"})


class RefdataTokenTest(unittest.TestCase):
    def setUp(self):
        cal.reload()
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])

    def test_the_sdks_token_exception_is_a_token_failure_that_waits_for_the_login(self):
        from stockify import daemon
        from stockify.daemon import token_watch
        clock = ManualClock(ist(WED, "09:00"))
        flags, calls = [], []

        def fresh():
            flags.append(token_watch.read_status().get("waiting_for_login"))
            return clock.now_ns() >= ist(WED, "10:30")

        def fake(day):
            calls.append(clock.now_ns())
            if len(calls) == 1:
                raise SDK_TOKEN_ERROR("Incorrect api_key or access_token")

        with mock.patch.object(daemon, "_ensure_refdata", fake), mock.patch("stockify.daemon.notify.post"):
            stale = daemon.refresh_refdata(WED, clock, threading.Event(), token_fresh=fresh)
        self.assertFalse(stale)                                                  # never the stale label
        self.assertIn(True, flags)                                               # waiting_for_login was set
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(calls[1], ist(WED, "10:30"))                     # retried at once on the login

    def test_the_last_give_up_logs_the_class_and_a_traceback(self):
        import logging
        from stockify import daemon
        was = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, was)
        with mock.patch.object(daemon, "_ensure_refdata", side_effect=TypeError("a bug")):
            with self.assertLogs("stockify.daemon", "WARNING") as logs:
                stale = daemon.refresh_refdata(WED, ManualClock(ist(WED, "10:00")), threading.Event())
        self.assertTrue(stale)
        errors = [r for r in logs.records if r.levelno >= logging.ERROR]
        self.assertTrue(any("TypeError" in r.getMessage() for r in errors))
        self.assertTrue(all(r.exc_info for r in logs.records))


class AllFailedSummaryTest(JobsBase):
    def test_a_day_where_every_session_failed_is_summarised_and_done(self):
        from stockify import daemon
        from stockify.daemon import notify
        self.sessions_with_day(MON)
        store.add_timeline("s1", "session_failed", {"error": "x"}, wall_ns=ist(MON, "12:00"), engine_ns=None)
        self.assertTrue(notify.summary_ready(MON))
        subject, body = notify.daily_summary(MON)
        self.assertIn("every session failed", body.lower())
        sent = []
        with mock.patch.object(notify, "post_daily_summary", lambda d, cb: (sent.append(d), cb(None), True)[2]):
            daemon._summary_flight.clear()
            daemon._daily_summary(MON, self.state)
            daemon._daily_summary(MON, self.state)
        self.assertEqual(sent, [MON])
        self.assertTrue(self.state.done("daily_summary", MON))


class DeskJobTest(JobsBase):
    def test_desk_job_is_held_during_the_session_and_after_nse_fetch(self):
        """HD 8.1: the nightly desk job grades the holdings after the evening's NSE files, never beside the live
        recorder, and is caught up like the other evening jobs."""
        self.assertIn("desk", scheduler.CATCH_UP)
        self.assertIn("desk", scheduler.HELD_DURING_SESSION)
        self.assertEqual(scheduler.DEPENDS["desk"], "nse_fetch")
        self.assertEqual(scheduler.DAY_ORDER.index("desk"), scheduler.DAY_ORDER.index("daily_summary") + 1)
        p = scheduler.plan(MON)
        self.assertEqual((p.desk_ns, p.desk_until_ns), (ist(MON, "20:10"), ist(MON, "23:30")))
        self.assertEqual(scheduler.window_end_ns("desk", MON), ist(MON, "23:30"))
        self.assertEqual(scheduler.retry_every_ns("desk"), 30 * MIN)
        self.assertNotIn(("desk", MON), scheduler.missed_jobs(ist(MON, "20:09"), self.state))
        self.assertIn(("desk", MON), scheduler.missed_jobs(ist(MON, "20:10"), self.state))
        self.assertFalse(scheduler.runnable("desk", MON, self.state))
        self.state.mark("nse_fetch", MON)
        self.assertTrue(scheduler.runnable("desk", MON, self.state))
        self.state.mark("desk", MON)
        self.assertNotIn(("desk", MON), scheduler.missed_jobs(ist(WED, "08:00"), self.state))
        self.assertNotIn("desk", scheduler.BLOCKS_SLEEP)

    def test_the_desk_job_waits_for_the_close_in_the_daemon(self):
        from stockify import daemon
        ran = []
        state = scheduler.State()
        state.mark("nse_fetch", MON)

        def fake_market_day(**kw):
            kw["on_idle"]()
            return exitcodes.TOKEN_EXPIRED

        def run_at(hhmm):
            ran.clear()
            clock = ManualClock(ist(WED, hhmm))
            with mock.patch.object(market_day, "run_market_day", fake_market_day), \
                    mock.patch.object(daemon, "_run_job", lambda job, d, st, now=None: ran.append((job, d))):
                daemon.run_daemon(power=mock.MagicMock(idle_seconds=lambda: 0), clock=clock,
                                  token_fresh=lambda: False, send_reminder=lambda s, b: None)
            return ran[:]

        self.assertNotIn(("desk", MON), run_at("10:00"))
        self.assertIn(("desk", MON), run_at("08:40"))


class FundamentalsJobTest(JobsBase):
    def test_fundamentals_job_held_during_session_after_nse_fetch(self):
        """FS 8: the fundamentals screener's daily job reads NSE after the evening's files (market cap needs the
        day's raw closes), never beside the live recorder, after the desk, and is caught up like the others."""
        self.assertIn("fundamentals", scheduler.CATCH_UP)
        self.assertIn("fundamentals", scheduler.HELD_DURING_SESSION)
        self.assertEqual(scheduler.DEPENDS["fundamentals"], "nse_fetch")
        self.assertEqual(scheduler.DAY_ORDER.index("fundamentals"), scheduler.DAY_ORDER.index("desk") + 1)
        p = scheduler.plan(MON)
        self.assertEqual((p.fundamentals_ns, p.fundamentals_until_ns), (ist(MON, "20:40"), ist(MON, "23:30")))
        self.assertEqual(scheduler.window_end_ns("fundamentals", MON), ist(MON, "23:30"))
        self.assertEqual(scheduler.retry_every_ns("fundamentals"), 30 * MIN)
        self.assertNotIn(("fundamentals", MON), scheduler.missed_jobs(ist(MON, "20:39"), self.state))
        self.assertIn(("fundamentals", MON), scheduler.missed_jobs(ist(MON, "20:40"), self.state))
        self.assertFalse(scheduler.runnable("fundamentals", MON, self.state))
        self.state.mark("nse_fetch", MON)
        self.assertTrue(scheduler.runnable("fundamentals", MON, self.state))
        self.state.mark("fundamentals", MON)
        self.assertNotIn(("fundamentals", MON), scheduler.missed_jobs(ist(WED, "08:00"), self.state))
        self.assertNotIn("fundamentals", scheduler.BLOCKS_SLEEP)
        self.assertNotIn("fundamentals", scheduler.HOLDS_SLEEP_UNTIL_WINDOW_ENDS)

    def test_the_fundamentals_job_waits_for_the_close_in_the_daemon(self):
        from stockify import daemon
        ran = []
        state = scheduler.State()
        state.mark("nse_fetch", MON)

        def fake_market_day(**kw):
            kw["on_idle"]()
            return exitcodes.TOKEN_EXPIRED

        def run_at(hhmm):
            ran.clear()
            clock = ManualClock(ist(WED, hhmm))
            with mock.patch.object(market_day, "run_market_day", fake_market_day), \
                    mock.patch.object(daemon, "_run_job", lambda job, d, st, now=None: ran.append((job, d))):
                daemon.run_daemon(power=mock.MagicMock(idle_seconds=lambda: 0), clock=clock,
                                  token_fresh=lambda: False, send_reminder=lambda s, b: None)
            return ran[:]

        self.assertNotIn(("fundamentals", MON), run_at("10:00"))
        self.assertIn(("fundamentals", MON), run_at("08:40"))


class WatchResultsJobTest(JobsBase):
    def test_the_results_reminder_runs_at_2100_after_fundamentals_and_is_not_held(self):
        """CW 3: the watchlist's results reminder reads the calendar the fundamentals job refreshed."""
        self.assertIn("watch_results", scheduler.CATCH_UP)
        self.assertNotIn("watch_results", scheduler.HELD_DURING_SESSION)
        self.assertNotIn("watch_results", scheduler.BLOCKS_SLEEP)
        self.assertNotIn("watch_results", scheduler.HOLDS_SLEEP_UNTIL_WINDOW_ENDS)
        self.assertEqual(scheduler.DEPENDS["watch_results"], "fundamentals")
        self.assertEqual(scheduler.DAY_ORDER.index("watch_results"), scheduler.DAY_ORDER.index("fundamentals") + 1)
        self.assertEqual(scheduler.plan(MON).watch_results_ns, ist(MON, "21:00"))
        self.assertEqual(scheduler.retry_every_ns("watch_results"), 30 * MIN)
        self.assertNotIn(("watch_results", MON), scheduler.missed_jobs(ist(MON, "20:59"), self.state))
        self.assertIn(("watch_results", MON), scheduler.missed_jobs(ist(MON, "21:00"), self.state))
        self.assertFalse(scheduler.runnable("watch_results", MON, self.state))
        self.state.mark("fundamentals", MON)
        self.assertTrue(scheduler.runnable("watch_results", MON, self.state))

    def test_the_job_marks_the_day_when_sent_and_gives_up_after_three_failed_sends(self):
        from stockify import daemon
        from stockify.watchlist import reminders
        with mock.patch.object(reminders, "run", return_value={"meetings": 1, "told": [], "error": None}):
            daemon._run_job("watch_results", MON, self.state, ist(MON, "21:00"))
        self.assertTrue(self.state.done("watch_results", MON))
        with mock.patch.object(reminders, "run", return_value={"meetings": 1, "told": [("X", "d")], "error": "E"}):
            for n in range(daemon.WATCH_RESULTS_TRIES):
                self.assertFalse(self.state.done("watch_results", WED), n)
                daemon._run_job("watch_results", WED, self.state, ist(WED, "21:00"))
        self.assertTrue(self.state.done("watch_results", WED))
        with mock.patch.object(reminders, "run", side_effect=RuntimeError("broken")):
            daemon._run_job("watch_results", THU, self.state, ist(THU, "21:00"))      # raised: tried again later
        self.assertFalse(self.state.done("watch_results", THU))


if __name__ == "__main__":
    unittest.main()
