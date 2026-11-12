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
                        mock.patch("stockify.daemon.finalise.next_sod_state", next_sod_state)):
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


if __name__ == "__main__":
    unittest.main()
