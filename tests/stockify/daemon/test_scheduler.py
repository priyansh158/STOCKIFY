"""stockify.daemon.scheduler and run_daemon: the day plan, sleep after close,
self-wake, catch-up (D-32). Nothing really sleeps: fake clock and fake power."""
from __future__ import annotations

import json
import unittest
from datetime import date

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import timeutil
from stockify.daemon import exitcodes, scheduler
from stockify.daemon.clock import ManualClock
from stockify.daemon.keepawake import FakePower
from stockify.data import calendar as cal

MIN = 60 * 1_000_000_000


def ist(d: date, hhmm: str) -> int:
    return timeutil.ist_ns(d, hhmm)


def local(t_ns: int) -> str:
    return timeutil.ns_to_ist(t_ns).astimezone(cal.owner_tz()).strftime("%a %d %b %H:%M %Z")


class PlanTests(unittest.TestCase):
    def setUp(self):
        cal.reload()
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])

    def test_session_day(self):
        mon = date(2026, 11, 9)
        self.assertEqual(scheduler.session_day(ist(mon, "07:00")), mon)
        self.assertEqual(scheduler.session_day(ist(mon, "15:44")), mon)
        self.assertEqual(scheduler.session_day(ist(mon, "15:45")), date(2026, 11, 11))   # 10 Nov holiday
        self.assertEqual(scheduler.session_day(ist(date(2026, 11, 7), "12:00")), mon)    # Saturday

    def test_next_wake_skips_weekends_and_holidays(self):
        fri = date(2026, 11, 6)
        wake = scheduler.next_wake_ns(ist(fri, "16:31"))
        self.assertEqual(wake, ist(date(2026, 11, 9), "06:00"))
        self.assertEqual(local(wake), "Sun 08 Nov 16:30 PST")            # Diwali Sunday, Seattle
        after_monday = scheduler.next_wake_ns(ist(date(2026, 11, 9), "16:31"))
        self.assertEqual(after_monday, ist(date(2026, 11, 11), "06:00"))

    def test_next_wake_across_us_daylight_saving(self):
        wake = scheduler.next_wake_ns(ist(date(2027, 3, 12), "16:31"))    # Friday
        self.assertEqual(wake, ist(date(2027, 3, 15), "06:00"))
        self.assertEqual(local(wake), "Sun 14 Mar 17:30 PDT")

    def test_state_persists_and_reloads(self):
        s = scheduler.State()
        s.mark("compaction", date(2026, 11, 9))
        self.assertTrue(scheduler.State().done("compaction", date(2026, 11, 9)))
        self.assertFalse(scheduler.State().done("compaction", date(2026, 11, 11)))


class SleepDecisionTests(unittest.TestCase):
    def setUp(self):
        cal.reload()
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        self.state = scheduler.State()
        self.day = date(2026, 11, 9)

    def decide(self, hhmm="16:31", *, idle_s=3600.0, stay=False, day="default"):
        d = self.day if day == "default" else day
        return scheduler.should_sleep(ist(self.day, hhmm), d, self.state, idle_s=idle_s, stay_awake=stay)

    def test_blocked_until_compaction_is_done(self):
        self.state.mark("margin_files", self.day)
        self.state.mark("candles", self.day)                                 # CW 1: they hold sleep too
        self.assertEqual(self.decide(), (False, "compaction for 2026-11-09 not done"))
        self.state.mark("compaction", self.day)
        self.assertEqual(self.decide(), (True, "ok"))

    def test_the_margin_files_keep_the_laptop_up_until_done_or_22_00(self):
        """M-3: the day's SPAN and ELM files are fetched the same evening, not once the next morning."""
        self.state.mark("compaction", self.day)
        self.state.mark("candles", self.day)                                 # CW 1: they hold sleep too
        self.assertEqual(self.decide("16:31"), (False, "margin_files for 2026-11-09 not done (window until 22:00 IST)"))
        self.assertEqual(self.decide("21:59")[0], False)
        self.assertEqual(self.decide("22:00"), (True, "ok"))                 # the window is over: sleep anyway
        self.state.mark("margin_files", self.day)
        self.assertEqual(self.decide("16:31"), (True, "ok"))

    def test_not_before_16_00_ist(self):
        self.state.mark("compaction", self.day)
        self.state.mark("margin_files", self.day)
        self.assertFalse(self.decide("15:59")[0])

    def test_owner_activity_postpones(self):
        self.state.mark("compaction", self.day)
        self.state.mark("margin_files", self.day)
        self.assertEqual(self.decide(idle_s=300)[0], False)

    def test_stay_awake_flag_and_disabled_config(self):
        self.state.mark("compaction", self.day)
        self.state.mark("margin_files", self.day)
        self.assertEqual(self.decide(stay=True), (False, "stay_awake flag present"))
        cfg = scheduler.schedule()
        cfg["sleep_after_close"]["enabled"] = False
        self.assertEqual(scheduler.should_sleep(ist(self.day, "17:00"), self.day, self.state, idle_s=3600,
                                                stay_awake=False, cfg=cfg)[0], False)

    def test_no_obligations_at_night_or_weekend(self):
        self.assertEqual(self.decide("02:00", day=None), (True, "ok"))


class CatchUpTests(unittest.TestCase):
    def setUp(self):
        cal.reload()
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])

    def test_missed_evening_fetch_runs_on_wake(self):
        state = scheduler.State()
        missed = scheduler.missed_jobs(ist(date(2026, 11, 11), "06:00"), state)
        self.assertIn(("nse_fetch", date(2026, 11, 9)), missed)
        state.mark("nse_fetch", date(2026, 11, 9))
        self.assertNotIn(("nse_fetch", date(2026, 11, 9)),
                         scheduler.missed_jobs(ist(date(2026, 11, 11), "06:00"), state))


class RunDaemonTests(unittest.TestCase):
    """The whole day loop with a fake clock, fake power and a fake recorder."""

    def setUp(self):
        cal.reload()
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        from stockify import daemon
        self.daemon = daemon
        self.power = FakePower(idle_s=3600)
        from unittest.mock import patch
        from stockify.data import nse_margin_files
        patcher = patch.object(nse_margin_files, "fetch_day", side_effect=RuntimeError("offline"))
        patcher.start()
        self.addCleanup(patcher.stop)                                # catch-up must never reach the network
        from stockify.data import nse_files
        patcher = patch.object(nse_files, "fetch", side_effect=RuntimeError("offline"))
        patcher.start()
        self.addCleanup(patcher.stop)                                # nse_fetch is a job boundary too
        from stockify import backup
        patcher = patch.object(backup, "run", side_effect=AssertionError("a test must never back up to the real home"))
        patcher.start()
        self.addCleanup(patcher.stop)           # catch-up runs the nightly backup: storage.json's target is ~/STOCKIFY-backup

    def run_from(self, start_ns: int, record_code: int = exitcodes.OK):
        clock = ManualClock(start_ns)
        calls = []

        def record():
            calls.append(timeutil.ns_to_ist(clock.now_ns()).strftime("%Y-%m-%d %H:%M"))
            self.assertTrue(self.power.held)                       # awake during the session
            clock.set(ist(scheduler.session_day(clock.now_ns()), "15:45"))
            return record_code

        code = self.daemon.run_daemon(power=self.power, clock=clock, recorder=record)
        return code, calls, clock

    def test_a_trading_day_records_then_hibernates_with_a_wake_file(self):
        code, calls, clock = self.run_from(ist(date(2026, 11, 9), "06:05"))
        self.assertEqual(code, exitcodes.SLEEP)
        self.assertEqual(calls, ["2026-11-09 08:45"])
        self.assertFalse(self.power.held)
        info = json.loads(scheduler.next_wake_file().read_text(encoding="utf-8"))
        self.assertEqual(info["wake_ns"], ist(date(2026, 11, 11), "06:00"))
        self.assertTrue(info["wake_owner_local"].startswith("2026-11-10T16:30"))
        self.assertTrue(scheduler.State().done("compaction", date(2026, 11, 9)))

    def test_started_on_a_weekend_it_sleeps_until_the_next_wake(self):
        code, calls, _ = self.run_from(ist(date(2026, 11, 7), "12:00"))
        self.assertEqual((code, calls), (exitcodes.SLEEP, []))
        info = json.loads(scheduler.next_wake_file().read_text(encoding="utf-8"))
        self.assertEqual(info["wake_ns"], ist(date(2026, 11, 9), "06:00"))

    def test_restart_after_the_close_finishes_the_day_then_sleeps(self):
        code, calls, _ = self.run_from(ist(date(2026, 11, 9), "17:00"))
        self.assertEqual((code, calls), (exitcodes.SLEEP, []))
        self.assertTrue(scheduler.State().done("compaction", date(2026, 11, 9)))

    def test_the_margin_files_are_fetched_the_same_evening_every_30_min_until_22_00(self):
        from unittest.mock import patch
        from stockify.data import nse_margin_files as M
        day = date(2026, 11, 9)
        state = scheduler.State()
        for d in cal.trading_days("FO", date(2026, 11, 1), date(2026, 11, 6)):
            state.mark("margin_files", d)                            # earlier days done: only 9 Nov is due
        clock = ManualClock(ist(day, "17:00"))
        calls = []

        def fetch_day(d):
            calls.append((d, clock.now_ns()))
            return M.DayResult(d, stored=[M.span_name(d, "i1")], missing=[M.span_name(d, "s")])

        with patch.object(M, "fetch_day", side_effect=fetch_day):
            code = self.daemon.run_daemon(power=self.power, clock=clock, recorder=lambda: exitcodes.OK)
        self.assertEqual(code, exitcodes.SLEEP)
        self.assertEqual({d for d, _ in calls}, {day})
        calls = [t for _, t in calls]
        self.assertTrue(all(timeutil.ns_to_ist(t).date() == day for t in calls))     # the same evening
        self.assertLessEqual(calls[0], ist(day, "17:35"))
        self.assertTrue(all(b - a >= 30 * MIN for a, b in zip(calls, calls[1:])), [local(c) for c in calls])
        self.assertGreaterEqual(len(calls), 8, [local(c) for c in calls])
        self.assertGreaterEqual(calls[-1], ist(day, "22:00"))                          # the last try gives up
        self.assertTrue(scheduler.State().done("margin_files", day))

    def test_token_problem_is_passed_to_the_launcher(self):
        code, calls, _ = self.run_from(ist(date(2026, 11, 9), "06:05"), record_code=exitcodes.TOKEN_EXPIRED)
        self.assertEqual(code, exitcodes.TOKEN_EXPIRED)
        self.assertFalse(self.power.held)

    def test_stay_awake_flag_skips_one_night(self):
        scheduler.stay_awake_flag().write_text("", encoding="utf-8")
        clock = ManualClock(ist(date(2026, 11, 9), "17:00"))
        stop_after = []

        def record():
            stop_after.append(1)
            return exitcodes.TOKEN_EXPIRED                          # end the test at the next session

        code = self.daemon.run_daemon(power=self.power, clock=clock, recorder=record)
        self.assertFalse(scheduler.stay_awake_flag().exists())      # used once, then removed
        self.assertEqual((code, len(stop_after)), (exitcodes.TOKEN_EXPIRED, 1))


if __name__ == "__main__":
    unittest.main()
