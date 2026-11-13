"""LCB 5.2 (M5.7; E-03; D-65): a Kite token expiry pauses the sessions, the process exits 76, and the next process waits
for a login newer than the token that was rejected. Offline: no token value is ever written, read or needed; the token
file's modification time is all the daemon looks at."""
from __future__ import annotations

import contextlib
import io
import json
import os
import threading
import unittest
from datetime import date
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.daemon import paper_fixture as pf
from tests.stockify.daemon.test_engine_log import enable_logging

import kite_client
from stockify import daemon, engine, paths, store, timeutil
from stockify.config import loader
from stockify.daemon import exitcodes, journal, lifecycle, market_day, token_watch
from stockify.daemon.clock import ManualClock
from stockify.daemon.dispatcher import Dispatcher
from stockify.data import calendar as cal
from stockify.data import packs

DAY = pf.DAY
SPEC = f"replay:{DAY.isoformat()}:max"
SEC = 1_000_000_000
MONDAY = date(2026, 11, 9)


def touch_token(mtime_ns: int) -> None:
    """A stand-in token file: empty JSON (no token value, ever) with the given modification time."""
    path = kite_client._token_file()
    path.write_text("{}", encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))


class StatusFileTest(unittest.TestCase):
    def setUp(self):
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])

    def test_no_file_reads_as_empty(self):
        self.assertEqual(token_watch.read_status(), {})

    def test_write_merges_and_keeps_the_other_writers_fields(self):
        paths.daemon_status_file().write_text(json.dumps({"day": "2026-11-17", "exit_code": 76}), encoding="utf-8")
        token_watch.write_status(waiting_for_login=True, since_ns=5)
        token_watch.write_status(rejected_token_mtime_ns=7)
        got = token_watch.read_status()
        self.assertEqual((got["day"], got["exit_code"], got["waiting_for_login"], got["since_ns"],
                          got["rejected_token_mtime_ns"]), ("2026-11-17", 76, True, 5, 7))

    def test_a_corrupt_file_reads_as_empty_and_is_replaced(self):
        paths.daemon_status_file().write_text("{not json", encoding="utf-8")
        self.assertEqual(token_watch.read_status(), {})
        token_watch.write_status(waiting_for_login=False)
        self.assertFalse(token_watch.read_status()["waiting_for_login"])

    def test_a_field_that_could_hold_a_token_is_refused(self):
        for name in ("access_token", "api_secret", "token", "request_token"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                token_watch.write_status(**{name: "x"})

    def test_mtime_is_none_without_a_file_and_never_opens_it(self):
        self.assertIsNone(token_watch.token_mtime_ns())
        touch_token(1_700_000_000 * SEC)
        with mock.patch("builtins.open", side_effect=AssertionError("opened")), \
                mock.patch("pathlib.Path.open", side_effect=AssertionError("opened")), \
                mock.patch("pathlib.Path.read_text", side_effect=AssertionError("read")):
            self.assertEqual(token_watch.token_mtime_ns(), 1_700_000_000 * SEC)


class StatusValueTest(unittest.TestCase):
    """LCB 5.24 (M-1): the status file checks values as well as names."""

    def setUp(self):
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])

    def test_a_long_opaque_string_is_refused_in_any_field(self):
        opaque = "A1b2C3d4" * 6
        for value in (opaque, [opaque], {"x": {"y": opaque}}):
            with self.subTest(value=str(value)[:12]), self.assertRaises(ValueError):
                token_watch.write_status(note=value)

    def test_ordinary_text_and_numbers_pass(self):
        token_watch.write_status(day="2026-11-17", alarms=["on_battery"], note="a sentence with spaces, long enough to "
                                 "pass the opaque rule because it is plainly prose and not one run")
        self.assertEqual(token_watch.read_status()["day"], "2026-11-17")

    def test_the_rejected_mtime_must_be_an_int_or_none(self):
        for bad in ("12345", 1.5, True, "x" * 60):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                token_watch.write_status(rejected_token_mtime_ns=bad)
        token_watch.write_status(rejected_token_mtime_ns=None)
        token_watch.write_status(rejected_token_mtime_ns=7)

    def test_more_secret_shaped_names_are_refused(self):
        for name in ("api_key", "password", "bearer", "authorization"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                token_watch.write_status(**{name: "x"})


class WaitForLoginTest(unittest.TestCase):
    def setUp(self):
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        self.clock = ManualClock(1_000 * SEC)
        self.idle = 0

    def on_idle(self):
        self.idle += 1

    def wait(self, fresh, *, until=None, stop=None):
        return token_watch.wait_for_login(self.clock, stop or threading.Event(), poll_s=30, token_fresh=fresh,
                                          on_idle=self.on_idle, until_ns=until)

    def test_the_poll_interval_is_paper_jsons_token_poll_s(self):
        """The one value of the poll (15 s) lives in paper.json; nothing else hard-codes it."""
        from unittest import mock
        from stockify.config import loader
        self.assertEqual(token_watch.poll_s(), 15.0)
        real = loader.load_json
        with mock.patch.object(loader, "load_json",
                               lambda name, *a, **kw: {**real(name, *a, **kw), "token_poll_s": 7}
                               if name == "paper.json" else real(name, *a, **kw)):
            self.assertEqual(token_watch.poll_s(), 7.0)
            polls = []
            self.assertFalse(token_watch.wait_for_login(self.clock, threading.Event(), token_fresh=lambda: False,
                                                        on_idle=lambda: polls.append(self.clock.now_ns()),
                                                        until_ns=1_000 * SEC + 30 * SEC))
        self.assertEqual([(b - a) // SEC for a, b in zip(polls, polls[1:])], [7, 7, 7, 7, 2])

    def test_a_fresh_token_and_no_rejected_mtime_returns_at_once(self):
        self.assertTrue(self.wait(lambda: True))
        self.assertEqual(self.idle, 0)

    def test_a_stale_token_waits_in_process_and_idles_each_poll(self):
        self.assertTrue(self.wait(lambda: self.clock.now_ns() >= 1_100 * SEC))
        self.assertGreaterEqual(self.idle, 3)
        self.assertTrue(token_watch.read_status()["waiting_for_login"])

    def test_the_wait_ends_at_until_ns(self):
        self.assertFalse(self.wait(lambda: False, until=1_100 * SEC))
        self.assertEqual(self.clock.now_ns(), 1_100 * SEC)

    def test_a_stop_ends_the_wait(self):
        stop = threading.Event()
        stop.set()
        self.assertFalse(self.wait(lambda: False, stop=stop))

    def test_a_token_fresh_by_the_06_00_rule_but_not_newer_than_the_rejected_one_keeps_waiting(self):
        touch_token(500 * SEC)
        token_watch.write_status(rejected_token_mtime_ns=500 * SEC)
        self.assertFalse(self.wait(lambda: True, until=1_100 * SEC))        # same file: rejected
        self.assertGreater(self.idle, 0)
        touch_token(501 * SEC)                                              # a new login rewrites the file
        self.assertTrue(self.wait(lambda: True, until=1_200 * SEC))

    def test_a_rejected_mtime_with_no_token_file_keeps_waiting(self):
        token_watch.write_status(rejected_token_mtime_ns=500 * SEC)
        self.assertFalse(self.wait(lambda: True, until=1_100 * SEC))


class ApiStatusTest(unittest.TestCase):
    """`/api/kite/status` says when the daemon is waiting for a login, and stops saying it once one is made."""

    def setUp(self):
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])

    def status(self, authenticated: bool) -> dict:
        import asyncio
        from server.routers import kite as router
        with mock.patch.object(kite_client, "is_authenticated", return_value=authenticated),                 mock.patch.object(kite_client, "is_enabled", return_value=True):
            return asyncio.run(router.status())

    def test_not_waiting_by_default(self):
        self.assertEqual(self.status(True), {"configured": True, "authenticated": True,
                                             "daemon_waiting_for_login": False})

    def test_waiting_while_the_rejected_token_is_the_only_one(self):
        touch_token(500 * SEC)
        token_watch.write_status(waiting_for_login=True, rejected_token_mtime_ns=500 * SEC)
        got = self.status(True)                                    # fresh by the 06:00 rule, but the rejected file
        self.assertTrue(got["daemon_waiting_for_login"])
        touch_token(501 * SEC)
        self.assertFalse(self.status(True)["daemon_waiting_for_login"])

    def test_the_answer_never_carries_a_token(self):
        token_watch.write_status(waiting_for_login=True)
        self.assertEqual(set(self.status(False)), {"configured", "authenticated", "daemon_waiting_for_login"})


def decode(sid: str) -> list[dict]:
    _, body, _ = journal.read(paths.journal_file(sid, DAY))
    out, off = [], 0
    while off < len(body):
        rec, off = packs.decode(body, off)
        out.append(rec)
    return out


def gap_reasons(records: list[dict]) -> list[str]:
    return [r["reason"] for r in records if r["kind"] == "GAP" and r["phase"] == 0]


def controls(records: list[dict]) -> list[tuple[bool, list[str]]]:
    return [(r["payload"]["enabled"], r["payload"]["reasons"]) for r in records
            if r["kind"] == "REFDATA" and r.get("subtype") == "strategy_control"]


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class ExpiryTest(pf.PaperDayTest):
    def setUp(self):
        super().setUp()
        enable_logging(self)
        self.record(seconds=600)
        real = loader.load_json
        patcher = mock.patch.object(
            loader, "load_json", lambda name, *a, **kw: pf.UNIVERSE if name == "universe.json" else real(name, *a, **kw))
        patcher.start()
        self.addCleanup(patcher.stop)
        store.create_session("s1", "s1", self.session_cfg(strategy={"kind": "script", "actions": []}), 1)
        lifecycle.arm("s1", wall_ns=2)
        self.fault = ["token_expiry@09:20:00+120s"]

    def run_day(self, **kw):
        return market_day.run_market_day(ticker_spec=SPEC, session_ids=["s1"], **kw)

    def test_token_expiry_mid_day_pauses_exits_76_and_resumes_after_login(self):
        self.assertEqual(self.run_day(faults=self.fault), exitcodes.TOKEN_EXPIRED)
        self.assertIn("token_expired", store.get_session("s1")["pause_reasons"])
        self.assertEqual(store.get_session("s1")["status"], "PAUSED")
        status = token_watch.read_status()
        self.assertTrue(status["waiting_for_login"])
        self.assertEqual(status["sessions_paused"], ["s1"])
        self.assertGreater(status["since_ns"], 0)
        self.assertGreater(status["last_frame_no"], 0)
        self.assertGreater(status["last_frame_recv_ns"], 0)
        first = decode("s1")
        self.assertEqual(gap_reasons(first), ["token_expired"])
        self.assertEqual(controls(first)[-1], (False, ["token_expired"]))
        self.assertEqual(self.run_day(token_fresh=lambda: True), exitcodes.OK)
        after = decode("s1")
        self.assertEqual(gap_reasons(after), ["token_expired"])
        ends = [i for i, r in enumerate(after) if r["kind"] == "GAP" and r["phase"] == 1]
        self.assertEqual(len(ends), 1)
        self.assertEqual(controls(after)[-1], (True, []))
        got = store.get_session("s1")
        self.assertEqual((got["status"], got["pause_reasons"]), ("RUNNING", []))
        self.assertTrue([r for r in store.timeline("s1") if r["kind"] == "login"])
        self.assertFalse(token_watch.read_status()["waiting_for_login"])

    def test_the_owners_pause_survives_the_expiry_and_the_login(self):
        lifecycle.pause("s1", "owner", wall_ns=3)
        self.assertEqual(self.run_day(faults=self.fault), exitcodes.TOKEN_EXPIRED)
        self.assertEqual(store.get_session("s1")["pause_reasons"], ["owner", "token_expired"])
        self.assertEqual(self.run_day(token_fresh=lambda: True), exitcodes.OK)
        self.assertEqual(store.get_session("s1")["pause_reasons"], ["owner"])
        self.assertEqual(controls(decode("s1"))[-1], (False, ["owner"]))

    def test_a_replayed_login_that_is_not_newer_than_the_rejected_token_is_refused(self):
        touch_token(500 * SEC)
        self.assertEqual(self.run_day(faults=self.fault), exitcodes.TOKEN_EXPIRED)
        self.assertEqual(token_watch.read_status()["rejected_token_mtime_ns"], 500 * SEC)
        self.assertEqual(self.run_day(token_fresh=lambda: True), exitcodes.TOKEN_EXPIRED)   # same file
        self.assertIn("token_expired", store.get_session("s1")["pause_reasons"])
        touch_token(501 * SEC)
        self.assertEqual(self.run_day(token_fresh=lambda: True), exitcodes.OK)
        self.assertEqual(store.get_session("s1")["pause_reasons"], [])

    def test_a_login_that_triggers_the_403_counts_as_fresh(self):                          # LCB 5.24, I-2
        """The rejected token is the one the ticker was built with, read when it was loaded: a re-login that
        invalidated it (a newer file) is a good login, whatever the file's mtime is at shutdown."""
        touch_token(500 * SEC)
        relogged = []
        real_put = Dispatcher.put_frame

        def spy(disp, item):
            if not relogged:
                relogged.append(1)
                touch_token(600 * SEC)                       # the owner logs in again: this is what 403s the old token
            return real_put(disp, item)

        with mock.patch.object(Dispatcher, "put_frame", spy), mock.patch.object(Dispatcher, "put_nowait", spy):
            self.assertEqual(self.run_day(faults=self.fault), exitcodes.TOKEN_EXPIRED)
        self.assertEqual(token_watch.read_status()["rejected_token_mtime_ns"], 500 * SEC)
        self.assertEqual(self.run_day(token_fresh=lambda: True), exitcodes.OK)               # 600 > 500: a login

    def test_a_token_expired_reason_does_not_outlive_the_day(self):                         # LCB 5.24, I-1
        """A 403 at 11:00, no login before the close, a login that night: the next day opens RUNNING (the owner's
        own pause stays), journal first, then the store, with a `login` row."""
        lifecycle.pause("s1", "token_expired", wall_ns=3)                  # what the expiry left behind (ARMED: stays so)
        self.assertEqual(store.get_session("s1")["status"], "ARMED")
        rows = market_day.choose_sessions(["s1"])
        clock = ManualClock(pf.t("09:14:00"))
        opened = market_day.open_sessions(rows, DAY, clock, clear_token_expired=True)
        self.addCleanup(lambda: [sd.close() for sd in opened])
        got = store.get_session("s1")
        self.assertEqual((got["status"], got["pause_reasons"]), ("RUNNING", []))
        login = [r for r in store.timeline("s1") if r["kind"] == "login"]
        self.assertEqual(len(login), 1)

    def test_the_owners_pause_survives_the_day_change_and_is_journaled(self):
        lifecycle.pause("s1", "owner", wall_ns=3)
        lifecycle.pause("s1", "token_expired", wall_ns=4)
        opened = market_day.open_sessions(market_day.choose_sessions(["s1"]), DAY, ManualClock(pf.t("09:14:00")),
                                          clear_token_expired=True)
        self.addCleanup(lambda: [sd.close() for sd in opened])
        got = store.get_session("s1")
        self.assertEqual(got["pause_reasons"], ["owner"])
        self.assertNotEqual(got["status"], "RUNNING")
        for sd in opened:
            sd.fsync()
        self.assertEqual(controls(decode("s1")), [(False, ["owner"])])      # the journal never held token_expired

    def test_without_a_login_the_reason_stays(self):
        lifecycle.pause("s1", "token_expired", wall_ns=3)
        opened = market_day.open_sessions(market_day.choose_sessions(["s1"]), DAY, ManualClock(pf.t("09:14:00")))
        self.addCleanup(lambda: [sd.close() for sd in opened])
        self.assertEqual(store.get_session("s1")["pause_reasons"], ["token_expired"])

    def test_resume_at_is_cleared_once_the_restart_has_used_it(self):                    # Task 18 review, minor 4
        self.assertEqual(self.run_day(faults=self.fault), exitcodes.TOKEN_EXPIRED)
        self.assertEqual(token_watch.read_status()["resume_at_ns"], pf.t("09:22:00"))
        seen = []
        real_put = Dispatcher.put_frame

        def spy(disp, item):
            if not seen:
                seen.append(token_watch.read_status()["resume_at_ns"])
            return real_put(disp, item)

        with mock.patch.object(Dispatcher, "put_frame", spy), mock.patch.object(Dispatcher, "put_nowait", spy):
            self.assertEqual(self.run_day(), exitcodes.OK)
        self.assertEqual(seen, [None])

    def test_a_finished_replay_run_again_logs_one_nothing_to_do_line(self):              # Task 18 review, minor 2
        self.assertEqual(self.run_day(), exitcodes.OK)
        with self.assertLogs("stockify.daemon", level="INFO") as logs:
            self.assertEqual(self.run_day(), exitcodes.OK)
        self.assertEqual(len([m for m in logs.output if "nothing to do" in m]), 1, logs.output)

    def test_a_first_run_says_nothing_of_the_kind(self):
        with self.assertLogs("stockify.daemon", level="INFO") as logs:
            self.assertEqual(self.run_day(), exitcodes.OK)
        self.assertEqual([m for m in logs.output if "nothing to do" in m], [])

    def test_inject_on_a_live_ticker_is_refused_not_ignored(self):                       # Task 18 review, minor 3
        with self.assertRaises(market_day.LiveFaultsRefused) as ctx:
            market_day.run_market_day(ticker_spec="live", faults=["kill@09:20:00"], session_ids=[])
        self.assertIn("--inject", str(ctx.exception))

    def test_the_cli_refuses_inject_on_a_live_ticker_with_exit_2(self):
        from stockify import __main__ as cli
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["daemon", "--recorder-only", "--inject", "kill@09:20:00"])
        self.assertEqual(code, 2)
        self.assertIn("--inject", err.getvalue())


class LiveLoginWaitTest(unittest.TestCase):
    """The live path, with a fake clock and no Kite: the wait happens in the process."""

    class Reached(BaseException):
        pass

    def setUp(self):
        cal.reload()
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        self.clock = ManualClock(timeutil.ist_ns(MONDAY, "06:30"))

    def run_live(self, fresh, on_idle):
        with mock.patch.object(daemon, "_ensure_refdata", side_effect=self.Reached):
            return market_day.run_market_day(ticker_spec="live", session_ids=[], clock=self.clock,
                                             token_fresh=fresh, on_idle=on_idle)

    def test_stale_token_before_first_connect_waits_in_process(self):
        idle = []
        login_at = timeutil.ist_ns(MONDAY, "07:00")
        with self.assertRaises(self.Reached):                    # the wait ended and the run went on to connect
            self.run_live(lambda: self.clock.now_ns() >= login_at, lambda: idle.append(self.clock.now_ns()))
        self.assertGreaterEqual(len(idle), 2)
        self.assertGreaterEqual(self.clock.now_ns(), login_at)
        self.assertFalse(token_watch.read_status()["waiting_for_login"])

    def test_a_login_that_never_comes_ends_at_the_unsubscribe_time_with_76(self):
        idle = []
        self.assertEqual(self.run_live(lambda: False, lambda: idle.append(1)), exitcodes.TOKEN_EXPIRED)
        self.assertGreater(len(idle), 10)
        self.assertEqual(self.clock.now_ns(), timeutil.ist_ns(MONDAY, "15:45"))
        self.assertTrue(token_watch.read_status()["waiting_for_login"])

    def test_login_wait_still_runs_catch_up(self):
        """`run_daemon` hands the wait an `on_idle` that runs the scheduler's missed jobs, so the evening's work for
        the day before is not held up by a missing login."""
        prev = date(2026, 11, 6)
        ran = []

        def fake_market_day(**kw):
            kw["on_idle"]()
            return exitcodes.TOKEN_EXPIRED

        with mock.patch.object(market_day, "run_market_day", fake_market_day), \
                mock.patch("stockify.daemon.scheduler.missed_jobs",
                           return_value=[("nse_fetch", prev), ("quality", prev)]), \
                mock.patch.object(daemon, "_run_job", lambda job, d, state, now=None: ran.append((job, d))):
            code = daemon.run_daemon(power=mock.MagicMock(idle_seconds=lambda: 0), clock=self.clock,
                                     token_fresh=lambda: False, send_reminder=lambda s, b: None)
        self.assertEqual(code, exitcodes.TOKEN_EXPIRED)
        self.assertEqual(ran, [("nse_fetch", prev), ("quality", prev)] * 2)       # once at the loop top, once idle


if __name__ == "__main__":
    unittest.main()
