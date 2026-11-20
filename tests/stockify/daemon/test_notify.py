"""stockify.daemon.notify: one channel, per-event switches, a daily summary (RW 5.1; M5.8).

Nothing here can send: every test injects a sender, patches the transport, and `notify` itself refuses real delivery
while `tests.support` is loaded."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest
from tests.stockify.daemon.test_engine_log import enable_logging

from stockify import parity, store
from stockify.config import loader
from stockify.daemon import lifecycle, notify, recovery

DAY = date(2026, 11, 17)
SECRET_TOPIC = "-".join(["marker", "topic", "one"])          # built, not literal: a fixture, never a real secret
SECRET_TOKEN = "-".join(["marker", "bot", "token"])


def settings(channel=None, **events) -> dict:
    ev = {k: True for k in loader.NOTIFY_EVENTS}
    ev.update(events)
    return {"schema": "stockify.notify/1", "channel": channel, "events": ev}


class Recorder:
    """A sender that remembers; `fail` makes it raise as an SMTP outage would."""

    def __init__(self, fail: Exception | None = None):
        self.calls: list[tuple[str, str, str]] = []
        self.fail = fail

    def __call__(self, channel, subject, body):
        self.calls.append((channel, subject, body))
        if self.fail:
            raise self.fail


class NotifyTestBase(unittest.TestCase):
    def setUp(self):
        enable_logging(self)
        notify.reset()
        self.addCleanup(notify.reset)
        notify.outbox_path().unlink(missing_ok=True)             # no test inherits another's undelivered mail
        self.addCleanup(lambda: notify.outbox_path().unlink(missing_ok=True))

    def configured(self, **kw):
        p = mock.patch.object(notify, "settings", return_value=settings(**kw))
        p.start()
        self.addCleanup(p.stop)


class SendTests(NotifyTestBase):
    def test_the_shipped_channel_is_email(self):
        self.assertEqual(loader.load_json("notify.json")["channel"], "email")

    def test_every_shipped_switch_is_on_so_email_flows_once_smtp_is_set(self):
        """D-82 (owner, 2026-11-12): email is on by default; each switch can be turned off."""
        doc = loader.load_json("notify.json")
        self.assertEqual(set(doc["events"].values()), {True})
        self.assertEqual(sorted(doc["events"]), sorted(loader.NOTIFY_EVENTS))

    def test_the_watchlist_events_are_shipped_in_both_places(self):
        """CW 3: the 52-week alerts and the results reminders have their own switches, in notify.json and in
        loader.NOTIFY_EVENTS (the loader refuses a notify.json whose keys differ)."""
        for event in ("watch_high_low", "watch_results"):
            self.assertIn(event, loader.NOTIFY_EVENTS)
            self.assertIn(event, notify.EVENTS)
            self.assertIs(loader.load_json("notify.json")["events"][event], True)

    def test_a_watchlist_event_is_sent_like_any_other(self):
        self.configured(channel="email")
        sender = Recorder()
        self.assertIsNone(notify.send("watch_high_low", "s", "b", sender=sender, key="52w:2026-11-17:1"))
        self.assertIsNone(notify.send("watch_high_low", "s", "b", sender=sender, key="52w:2026-11-17:1"))
        self.assertEqual(len(sender.calls), 1)

    def test_default_is_log_only(self):
        self.configured(channel=None)
        sender = Recorder()
        with self.assertLogs("stockify.daemon.notify", "INFO") as cm:
            self.assertIsNone(notify.send("parity_failure", "subj", "body", sender=sender))
        self.assertEqual(sender.calls, [])
        self.assertIn("parity_failure", "\n".join(cm.output))

    def test_event_switch_off_sends_nothing(self):
        self.configured(channel="email", daily_summary=False)
        sender = Recorder()
        self.assertIsNone(notify.send("daily_summary", "s", "b", sender=sender))
        self.assertEqual(sender.calls, [])
        self.assertIsNone(notify.send("parity_failure", "s", "b", sender=sender))   # another event still goes
        self.assertEqual([c[0] for c in sender.calls], ["email"])

    def test_a_failing_sender_never_raises_and_is_reported(self):
        self.configured(channel="email")
        sender = Recorder(fail=OSError(f"smtp down for {SECRET_TOPIC}"))
        with self.assertLogs("stockify.daemon.notify", "ERROR") as cm:
            error = notify.send("parity_failure", "s", "b", sender=sender)
        self.assertEqual(error, "OSError")
        self.assertNotIn(SECRET_TOPIC, "\n".join(cm.output))                    # only the error's class is logged

    def test_a_keyed_notification_is_sent_once(self):
        self.configured(channel="email")
        sender = Recorder()
        for _ in range(3):
            notify.send("parity_failure", "s", "b", sender=sender, key="s1:2026-11-17")
        notify.send("parity_failure", "s", "b", sender=sender, key="s1:2026-11-18")
        self.assertEqual(len(sender.calls), 2)

    def test_a_failed_keyed_send_may_be_tried_again(self):
        self.configured(channel="email")
        bad = Recorder(fail=OSError())
        with self.assertLogs("stockify.daemon.notify", "ERROR"):
            notify.send("parity_failure", "s", "b", sender=bad, key="k")
        good = Recorder()
        notify.send("parity_failure", "s", "b", sender=good, key="k")
        self.assertEqual(len(good.calls), 1)

    def test_real_delivery_is_blocked_under_test(self):
        self.configured(channel="email")
        with self.assertLogs("stockify.daemon.notify", "ERROR"):
            self.assertIsNotNone(notify.send("disk_low", "s", "b"))              # the default sender refuses

    def test_unknown_event_is_refused(self):
        self.assertIsNotNone(notify.send("nonsense", "s", "b", sender=Recorder()))


class PostTests(NotifyTestBase):
    def test_post_hands_off_to_another_thread(self):
        self.configured(channel="email")
        threads = []
        gate = threading.Event()

        def slow(channel, subject, body):
            threads.append(threading.current_thread())
            gate.wait(2)

        with mock.patch.object(notify, "_default_sender", slow):
            started = time.monotonic()
            notify.post("disk_low", "s", "b")
            self.assertLess(time.monotonic() - started, 1.0)                    # post does not wait for the send
            gate.set()
            self.assertTrue(notify.flush(5))
        self.assertEqual(len(threads), 1)
        self.assertIsNot(threads[0], threading.current_thread())

    def test_post_never_raises_when_the_queue_is_full_or_the_send_fails(self):
        self.configured(channel="email")
        with mock.patch.object(notify, "_default_sender", side_effect=OSError("down")), \
                self.assertLogs("stockify.daemon.notify", "ERROR"):
            notify.post("disk_low", "s", "b")
            self.assertTrue(notify.flush(5))
        with mock.patch.object(notify, "QUEUE_MAX", 0), self.assertLogs("stockify.daemon.notify", "ERROR"):
            self.assertFalse(notify.post("disk_low", "s", "b"))


class OwnerCheckTests(NotifyTestBase):
    def run_cli(self):
        import contextlib
        import io
        from stockify import __main__ as cli
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            return cli.main(["notify", "test"]), err.getvalue()

    def test_notify_test_sends_one_message_whatever_the_switches_say(self):
        self.configured(channel="email", **{e: False for e in loader.NOTIFY_EVENTS})
        sender = Recorder()
        with mock.patch.object(notify, "_default_sender", sender):
            code, _ = self.run_cli()
        self.assertEqual((code, [c[0] for c in sender.calls]), (0, ["email"]))

    def test_notify_test_without_a_channel_says_so(self):
        self.configured(channel=None)
        code, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("no channel", err)

    def test_notify_test_reads_the_settings_file_like_the_daemon(self):
        # The SMTP settings live in the repo's settings file; the daemon loads it before sending, so the owner's
        # check must too, or it reports "not configured" whatever the file holds.
        from stockify import __main__ as cli
        self.configured(channel="email")
        with mock.patch.object(cli, "_load_env") as load, \
                mock.patch.object(notify, "_default_sender", Recorder()):
            self.run_cli()
        load.assert_called_once_with()

    def test_notify_test_failure_names_the_class_only(self):
        self.configured(channel="email")
        with mock.patch.object(notify, "_default_sender", Recorder(fail=OSError(SECRET_TOKEN))),                 self.assertLogs("stockify.daemon.notify", "ERROR") as cm:
            code, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertNotIn(SECRET_TOKEN, err + " ".join(cm.output))


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=str(support.SANDBOX_ROOT)))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def load(self, doc):
        (self.dir / "notify.json").write_text(json.dumps(doc), encoding="utf-8")
        return loader.load_json("notify.json", self.dir)

    def test_old_notify_shape_is_refused_by_loader(self):
        with self.assertRaises(loader.ConfigError):
            self.load({"schema": "stockify.notify/1", "channels": [], "events": {}})

    def test_the_new_shape_and_its_rules(self):
        self.assertEqual(self.load(settings("email"))["channel"], "email")
        self.assertIsNone(self.load(settings(None))["channel"])
        for gone in ("ntfy", "telegram", "sms"):                      # email only (owner, 2026-11-11)
            with self.assertRaises(loader.ConfigError):
                self.load(settings(gone))
        bad = settings()
        bad["events"]["disk_low"] = "yes"
        with self.assertRaises(loader.ConfigError):
            self.load(bad)
        bad = settings()
        del bad["events"]["disk_low"]
        with self.assertRaises(loader.ConfigError):
            self.load(bad)


class SummaryTests(SandboxedDBTest):
    db_modules = (store,)

    def day_end(self, sid, day, nav, positions=(), coverage=None, parity_outcome=None, labels=()):
        store.save_session_day_end(sid, day, orders=[], positions=list(positions),
                                   equity={"nav_paise": nav, "cash_paise": nav, "margin_paise": 0},
                                   eod_state_json="{}", labels=list(labels))
        store.upsert_session_day(sid, day, status="finalised", coverage=coverage, parity=parity_outcome)

    def test_rupees_use_indian_grouping(self):
        self.assertEqual(notify.rupees(10_000_000), "₹1,00,000")
        self.assertEqual(notify.rupees(123_456_789), "₹12,34,567.89")
        self.assertEqual(notify.rupees(-250_050), "-₹2,500.50")
        self.assertEqual(notify.rupees(5), "₹0.05")
        self.assertEqual(notify.rupees(0), "₹0")

    def test_summary_text_in_rupees(self):
        store.create_session("orb", "ORB", {"capital_paise": 10_000_000}, 1)
        store.create_session("fade", "Fade", {"capital_paise": 5_000_000}, 2)
        self.day_end("orb", "2026-11-16", 10_100_000)
        self.day_end("orb", DAY.isoformat(), 10_250_000, coverage=0.9712, parity_outcome="pass",
                     positions=[{"instrument_id": "7", "qty": "65", "unrealised_paise": "100"},
                                {"instrument_id": "8", "qty": "0"}], labels=["settlement_fallback"])
        self.day_end("fade", DAY.isoformat(), 4_900_000, coverage=1.0, parity_outcome="fail")
        subject, body = notify.daily_summary(DAY)
        self.assertIn("17 Nov 2026", subject)
        self.assertIn("ORB", body)
        self.assertIn("made ₹1,500", body)                    # 10,250,000 - 10,100,000 paise
        self.assertIn("₹1,02,500", body)                      # the account's value
        self.assertIn("lost ₹1,000", body)                     # Fade: started the day with ₹50,000
        self.assertIn("97.1%", body)
        self.assertIn("1 open position", body)                # the flat row is not one
        self.assertIn("replay check failed", body.lower())
        self.assertIn("settlement_fallback", body)
        self.assertNotIn("$", body)
        for jargon in ("Sharpe", "drawdown", "NAV", "paise"):
            self.assertNotIn(jargon, body)

    def test_a_day_with_no_finalised_session_has_no_summary(self):
        store.create_session("orb", "ORB", {"capital_paise": 1}, 1)
        self.assertIsNone(notify.daily_summary(DAY))

    def test_send_daily_summary_goes_through_the_daily_summary_switch(self):
        store.create_session("orb", "ORB", {"capital_paise": 10_000_000}, 1)
        self.day_end("orb", DAY.isoformat(), 10_000_000, coverage=1.0, parity_outcome="pass")
        sender = Recorder()
        with mock.patch.object(notify, "settings", return_value=settings("email")):
            self.assertTrue(notify.send_daily_summary(DAY, sender=sender))
            self.assertTrue(notify.send_daily_summary(DAY, sender=sender))     # the same day is told once
        self.assertEqual(len(sender.calls), 1)


class DailySummaryJobTests(SandboxedDBTest):
    db_modules = (store,)

    def test_the_daemon_tells_the_day_once_and_never_fails_on_it(self):
        from stockify import daemon
        from stockify.daemon import scheduler
        store.create_session("orb", "ORB", {"capital_paise": 10_000_000}, 1)
        state = scheduler.State()
        with mock.patch.object(notify, "post_daily_summary", return_value=False) as post:
            daemon._daily_summary(DAY, state)                         # nothing finalised yet: tried again later
            self.assertFalse(state.done("daily_summary", DAY))
            self.assertFalse(state.done("daily_summary_try1", DAY))   # a not-ready day is not a try
        post.assert_not_called()                                      # nothing is ready: nothing is posted
        enable_logging(self)
        with mock.patch.object(notify, "summary_ready", side_effect=RuntimeError("boom")), \
                self.assertLogs("stockify.daemon", "ERROR"):
            daemon._daily_summary(date(2026, 11, 18), state)


class CallSiteTests(SandboxedDBTest):
    db_modules = (store,)

    def posted(self):
        p = mock.patch.object(notify, "post")
        m = p.start()
        self.addCleanup(p.stop)
        return m

    def failing_result(self, line_key="1"):
        first = {"table": "fills", "key": line_key, "stored": {"p": 1}, "regenerated": {"p": 2}}
        return parity.ParityResult("s1", DAY, "journal", "fail", {"first_difference": first, "records": 3})

    def test_determinism_and_parity_failures_notify(self):
        post = self.posted()
        store.create_session("s1", "s", {"capital_paise": 1}, 1)
        lifecycle.arm("s1", wall_ns=2)
        # parity: the first failure tells the owner, the same failure again does not (parity_still_open)
        parity._record(self.failing_result())
        self.assertEqual([c.args[0] for c in post.call_args_list], ["parity_failure"])
        parity._record(self.failing_result())
        self.assertEqual(post.call_count, 1)
        self.assertIn("s1", post.call_args.args[1] + post.call_args.args[2])
        parity._record(self.failing_result("2"))                                 # a different mismatch is news
        self.assertEqual(post.call_count, 2)
        # determinism: first mismatch tells, the same one on a restart does not
        post.reset_mock()
        sd = mock.MagicMock(torn_bytes=0)
        res = SimpleNamespace(session_id="s1", day=DAY, records=5, written=0, tail_lost_rows=0, mismatch="differs: x")
        diff = recovery._Diff(mismatch={"table": "fills", "key": 1, "stored": {}, "regenerated": {}})
        recovery._record(sd, res, diff, 10)
        self.assertEqual([c.args[0] for c in post.call_args_list], ["parity_failure"])
        recovery._record(sd, res, diff, 20)
        self.assertEqual(post.call_count, 1)

    def test_a_subscription_cap_skip_notifies_through_the_notifier(self):
        post = self.posted()
        from stockify.daemon import market_day
        market_day.notify_cap("subscription_cap", {"session_id": "s1", "total": 9, "cap": 3, "session_instruments": 5})
        self.assertEqual(post.call_args.args[0], "session_error")
        self.assertIn("s1", post.call_args.args[2])

    def test_a_finalised_day_with_low_or_missing_coverage_notifies(self):
        post = self.posted()
        from stockify.daemon import finalise
        finalise.notify_timeline("s1", DAY.isoformat(), [(finalise.COVERAGE_LOW, {"day": "d", "covered_s": 1,
                                                                                  "continuous_s": 2})])
        finalise.notify_timeline("s1", DAY.isoformat(), [(finalise.COVERAGE_UNAVAILABLE, {"day": "d", "reason": "x"})])
        self.assertEqual([c.args[0] for c in post.call_args_list], ["session_error", "session_error"])
        post.reset_mock()
        finalise.notify_timeline("s1", DAY.isoformat(), [("sod_state_size", {"day": "d"})])
        post.assert_not_called()

    def test_waiting_for_a_login_notifies_once(self):
        post = self.posted()
        from stockify.daemon import token_watch
        token_watch.begin_wait(5)
        token_watch.begin_wait(6)                                               # already waiting: no second mail
        self.assertEqual([c.args[0] for c in post.call_args_list], ["login_needed"])


class EmailOnlyTests(NotifyTestBase):
    def unconfigured(self):
        self.configured(channel="email")
        for p in (mock.patch.object(notify, "_blocked", return_value=False),
                  mock.patch("geo_monitor.email_notify.email_configured", return_value=False)):
            p.start()
            self.addCleanup(p.stop)

    def test_unconfigured_smtp_is_reported_once_per_process_and_never_raises(self):
        self.unconfigured()
        with self.assertLogs("stockify.daemon.notify", "INFO") as cm:
            first = notify.send("disk_low", "s", "b")
            second = notify.send("parity_failure", "s", "b")
        self.assertEqual((first, second), ("email not configured", "email not configured"))
        warned = [line for line in cm.output if "NotConfigured" in line]
        self.assertEqual(len(warned), 1)
        self.assertTrue(issubclass(notify.NotConfigured, RuntimeError))

    def test_a_long_body_is_cut_for_email(self):
        sender = Recorder()
        self.configured(channel="email")
        notify.send("disk_low", "s", "x" * 100_000, sender=sender)
        sent = sender.calls[0][2]
        self.assertLessEqual(len(sent), notify.MAX_BODY_CHARS)
        self.assertIn("see the log", sent)
        self.assertEqual(notify.fit("short"), "short")

    def test_there_is_no_other_channel_in_the_code(self):
        self.assertFalse(hasattr(notify, "_http_post"))
        self.assertEqual(loader.NOTIFY_CHANNELS, (None, "email"))


class FixRoundTests(SandboxedDBTest):
    """Review of 6b06fa7: the summary is retried, posted and complete; email only."""
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        enable_logging(self)
        notify.reset()
        self.addCleanup(notify.reset)

    def configured(self, **kw):
        p = mock.patch.object(notify, "settings", return_value=settings(**kw))
        p.start()
        self.addCleanup(p.stop)

    def day_end(self, sid, day, nav, finalised=True):
        store.save_session_day_end(sid, day, orders=[], positions=[],
                                   equity={"nav_paise": nav, "cash_paise": nav, "margin_paise": 0},
                                   eod_state_json="{}", labels=[])
        store.upsert_session_day(sid, day, status="finalised" if finalised else "open", coverage=1.0,
                                 parity="pass")

    # I-1: success only when the send succeeded; retries are capped
    def test_send_daily_summary_is_true_only_if_the_send_worked(self):
        store.create_session("orb", "ORB", {"capital_paise": 10_000_000}, 1)
        self.day_end("orb", DAY.isoformat(), 10_000_000)
        self.configured(channel="email")
        self.assertFalse(notify.send_daily_summary(DAY, sender=Recorder(fail=OSError())))
        good = Recorder()
        self.assertTrue(notify.send_daily_summary(DAY, sender=good))            # a failed send was not remembered
        self.assertEqual(len(good.calls), 1)

    def drive(self, state, passes, outcome):
        """Run the daemon's summary step `passes` times, each time letting the worker finish with `outcome`."""
        from stockify import daemon
        for _ in range(passes):
            daemon._daily_summary(DAY, state)
            self.assertTrue(notify.flush(5))

    def test_a_failing_channel_is_retried_at_most_five_times_then_given_up_once(self):
        from stockify.daemon import scheduler
        store.create_session("orb", "ORB", {"capital_paise": 10_000_000}, 1)
        self.day_end("orb", DAY.isoformat(), 10_000_000)
        self.configured(channel="email")
        bad = Recorder(fail=OSError())
        state = scheduler.State()
        with mock.patch.object(notify, "_default_sender", bad), self.assertLogs("stockify.daemon", "ERROR") as cm:
            self.drive(state, 9, None)
        self.assertEqual(len(bad.calls), 5)
        self.assertEqual(sum("giving up" in line for line in cm.output), 1)
        self.assertTrue(state.done("daily_summary", DAY))                       # finished: no sixth try, no repeat

    def test_a_retry_that_works_marks_the_day_done(self):
        from stockify.daemon import scheduler
        store.create_session("orb", "ORB", {"capital_paise": 10_000_000}, 1)
        self.day_end("orb", DAY.isoformat(), 10_000_000)
        self.configured(channel="email")
        state = scheduler.State()
        calls = []

        def flaky(channel, subject, body):
            calls.append(1)
            if len(calls) < 3:
                raise OSError()

        with mock.patch.object(notify, "_default_sender", flaky), self.assertLogs("stockify.daemon", "ERROR"):
            self.drive(state, 6, None)
        self.assertEqual(len(calls), 3)
        self.assertTrue(state.done("daily_summary", DAY))

    # I-2: the loop never waits for the sender
    def test_a_hung_sender_does_not_stall_the_daemon_loop(self):
        from stockify import daemon
        from stockify.daemon import scheduler
        store.create_session("orb", "ORB", {"capital_paise": 10_000_000}, 1)
        self.day_end("orb", DAY.isoformat(), 10_000_000)
        self.configured(channel="email")
        gate = threading.Event()
        self.addCleanup(gate.set)
        state = scheduler.State()
        with mock.patch.object(notify, "_default_sender", lambda c, s, b: gate.wait(5)):
            started = time.monotonic()
            daemon._daily_summary(DAY, state)
            daemon._daily_summary(DAY, state)                                   # one send in flight: not a second
            self.assertLess(time.monotonic() - started, 1.0)
            gate.set()
            self.assertTrue(notify.flush(5))
        daemon._daily_summary(DAY, state)                                       # collects the result
        self.assertTrue(state.done("daily_summary", DAY))

    # I-3
    def test_the_summary_waits_for_every_session_and_includes_all(self):
        store.create_session("orb", "ORB", {"capital_paise": 10_000_000}, 1)
        store.create_session("fade", "Fade", {"capital_paise": 5_000_000}, 2)
        self.day_end("orb", DAY.isoformat(), 10_100_000)
        self.day_end("fade", DAY.isoformat(), 5_000_000, finalised=False)       # still finalising
        self.assertFalse(notify.summary_ready(DAY))
        self.day_end("fade", DAY.isoformat(), 5_050_000)
        self.assertTrue(notify.summary_ready(DAY))
        _, body = notify.daily_summary(DAY)
        self.assertIn("ORB", body)
        self.assertIn("Fade", body)

    def test_a_session_whose_day_failed_does_not_hold_the_summary_back(self):
        store.create_session("orb", "ORB", {"capital_paise": 10_000_000}, 1)
        store.create_session("bad", "Bad", {"capital_paise": 5_000_000}, 2)
        self.day_end("orb", DAY.isoformat(), 10_100_000)
        store.upsert_session_day("bad", DAY.isoformat(), status="open")
        from stockify import timeutil
        store.add_timeline("bad", "session_failed", {"error": "x"}, wall_ns=timeutil.ist_ns(DAY, "11:00"),
                           engine_ns=None)
        self.assertTrue(notify.summary_ready(DAY))
        _, body = notify.daily_summary(DAY)
        self.assertIn("Bad", body)
        self.assertIn("did not finish", body)

    # M-3
    def test_an_unconfigured_email_says_so_by_name(self):
        self.configured(channel="email")
        with mock.patch.dict("sys.modules", {}), mock.patch.object(notify, "_blocked", return_value=False), \
                mock.patch("geo_monitor.email_notify.email_configured", return_value=False), \
                self.assertLogs("stockify.daemon.notify", "WARNING") as cm:
            error = notify.send("disk_low", "s", "b")
        self.assertEqual(error, "email not configured")
        self.assertIn("email not configured", "".join(cm.output))
        self.assertTrue(issubclass(notify.NotConfigured, RuntimeError))

    # (a)


if __name__ == "__main__":
    unittest.main()


class StallAndDiskMessageTests(NotifyTestBase):
    """RM 7.1: `recorder_stall` and `disk_low` have senders now; each is once per key and goes through the queue."""

    def test_a_stall_is_emailed_once_per_episode_with_a_plain_message(self):
        from stockify import timeutil
        sender = Recorder()
        self.configured(channel="email")
        since = timeutil.ist_ns(DAY, "10:15:30")
        with mock.patch.object(notify, "_default_sender", sender):
            self.assertTrue(notify.recorder_stall("CM", "NIFTY 50", since))
            self.assertTrue(notify.recorder_stall("CM", "NIFTY 50", since))          # the same episode again
            self.assertTrue(notify.recorder_stall("CM", "NIFTY 50", since + 600 * 10**9))   # a new episode
            self.assertTrue(notify.flush(5))
        self.assertEqual(len(sender.calls), 2)
        channel, subject, body = sender.calls[0]
        self.assertEqual(channel, "email")
        self.assertIn("NIFTY 50", subject)
        self.assertIn("10:15:30 IST", body)
        self.assertIn("2026-11-17".replace("2026-11-17", "17 Nov 2026"), body)

    def test_a_stall_is_only_logged_when_its_switch_is_off(self):
        sender = Recorder()
        self.configured(channel="email", recorder_stall=False)
        with mock.patch.object(notify, "_default_sender", sender):
            notify.recorder_stall("CM", "NIFTY 50", 1)
            self.assertTrue(notify.flush(5))
        self.assertEqual(sender.calls, [])

    def test_disk_low_is_one_email_a_day_per_drive_naming_every_warning(self):
        sender = Recorder()
        self.configured(channel="email")
        warnings = ["only 3.0 GB free (warning below 20 GB)", "only 1.0% of the disk free"]
        with mock.patch.object(notify, "_default_sender", sender):
            notify.disk_low(DAY, "C:", warnings)
            notify.disk_low(DAY, "C:", warnings)                      # the next loop, the same day
            notify.disk_low(DAY, "D:", warnings)                      # another drive
            notify.disk_low(date(2026, 11, 18), "C:", warnings)       # the next day
            self.assertTrue(notify.flush(5))
        self.assertEqual(len(sender.calls), 3)
        self.assertIn("only 3.0 GB free", sender.calls[0][2])
        self.assertIn("only 1.0% of the disk", sender.calls[0][2])

    def test_a_notice_builder_never_raises(self):
        with mock.patch.object(notify, "post", side_effect=RuntimeError("boom")),                 self.assertLogs("stockify.daemon.notify", "ERROR"):
            self.assertFalse(notify.recorder_stall("CM", "X", 1))
            self.assertFalse(notify.disk_low(DAY, "C:", ["w"]))


class CompactionJobDiskTests(NotifyTestBase):
    """RM 7.1: the daemon's compaction job is the one place disk warnings are raised to the owner."""

    def run_job(self, warnings):
        from stockify import daemon
        from stockify.daemon import scheduler
        from stockify.data import compact
        state = scheduler.State()
        res = SimpleNamespace(status="verified", error=None)
        with mock.patch.object(compact, "compact", return_value=res), \
                mock.patch.object(compact, "disk_warnings", return_value=warnings), \
                mock.patch.object(compact, "apply_retention", return_value=[]), \
                mock.patch.object(notify, "post") as post:
            daemon._run_job("compaction", DAY, state)
            daemon._run_job("compaction", DAY, state)                       # the next pass the same day
        self.assertTrue(state.done("compaction", DAY))
        return post

    def test_warnings_post_disk_low_with_a_per_day_key(self):
        post = self.run_job(["only 3.0 GB free (warning below 20 GB)"])
        self.assertEqual(post.call_count, 2)                                # `post` is told; its key makes it once
        events = {c.args[0] for c in post.call_args_list}
        keys = {c.kwargs["key"] for c in post.call_args_list}
        self.assertEqual(events, {"disk_low"})
        self.assertEqual(len(keys), 1)
        self.assertTrue(next(iter(keys)).startswith(DAY.isoformat()))

    def test_no_warning_posts_nothing(self):
        self.assertEqual(self.run_job([]).call_count, 0)

    def test_a_notice_failure_does_not_fail_the_job(self):
        from stockify import daemon
        from stockify.daemon import scheduler
        from stockify.data import compact
        state = scheduler.State()
        with mock.patch.object(compact, "compact", return_value=SimpleNamespace(status="verified", error=None)), \
                mock.patch.object(compact, "disk_warnings", return_value=["w"]), \
                mock.patch.object(compact, "apply_retention", return_value=[]), \
                mock.patch.object(notify, "disk_low", side_effect=RuntimeError("x")), \
                self.assertLogs("stockify.daemon", "ERROR"):
            daemon._run_job("compaction", DAY, state)
        self.assertTrue(state.done("compaction", DAY))


class OutboxTests(NotifyTestBase):
    """RM 7.2: a notification that could not be delivered because the network is down is kept on disk and retried;
    one that cannot be fixed by waiting (auth, config) is not."""

    def setUp(self):
        super().setUp()
        self.configured(channel="email")
        self.addCleanup(lambda: notify.outbox_path().unlink(missing_ok=True))
        notify.outbox_path().unlink(missing_ok=True)

    def lines(self):
        p = notify.outbox_path()
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def test_a_dns_failure_is_kept_not_lost(self):
        import socket
        down = Recorder(fail=socket.gaierror(11001, "getaddrinfo failed"))
        err = notify.send("recorder_stall", "[STOCKIFY] stalled", "body", sender=down, key="k1")
        self.assertEqual(err, "gaierror")
        (rec,) = self.lines()
        self.assertEqual((rec["event"], rec["subject"], rec["body"], rec["key"]),
                         ("recorder_stall", "[STOCKIFY] stalled", "body", "k1"))

    def test_every_network_class_error_is_kept(self):
        import smtplib
        for exc in (OSError("x"), ConnectionError("x"), TimeoutError("x"), smtplib.SMTPConnectError(421, "x"),
                    smtplib.SMTPServerDisconnected("x")):
            with self.subTest(exc=type(exc).__name__):
                notify.outbox_path().unlink(missing_ok=True)
                notify.send("disk_low", "s", "b", sender=Recorder(fail=exc))
                self.assertEqual(len(self.lines()), 1)

    def test_auth_and_config_errors_are_not_kept(self):
        import smtplib
        for exc in (notify.NotConfigured("email not configured"), smtplib.SMTPAuthenticationError(535, b"bad"),
                    RuntimeError("boom"), smtplib.SMTPRecipientsRefused({})):
            with self.subTest(exc=type(exc).__name__):
                notify.send("disk_low", "s", "b", sender=Recorder(fail=exc))
                self.assertEqual(self.lines(), [])

    def test_retry_delivers_and_removes_with_the_original_time(self):
        notify.send("recorder_stall", "s", "b", sender=Recorder(fail=OSError("down")), key="k")
        written = self.lines()[0]["written_ns"]
        up = Recorder()
        self.assertEqual(notify.retry_outbox(sender=up), 1)
        self.assertEqual(self.lines(), [])
        (channel, subject, body), = up.calls
        self.assertEqual((channel, subject), ("email", "s"))
        self.assertTrue(body.startswith("b"))
        self.assertIn("sent late", body)
        from stockify import timeutil
        self.assertIn(f"{timeutil.ns_to_ist(written):%H:%M} IST", body)

    def test_retry_while_still_down_keeps_everything_and_stops_at_the_first_failure(self):
        notify.send("disk_low", "a", "b", sender=Recorder(fail=OSError()), key="1")
        notify.send("disk_low", "c", "d", sender=Recorder(fail=OSError()), key="2")
        down = Recorder(fail=OSError())
        self.assertEqual(notify.retry_outbox(sender=down), 0)
        self.assertEqual(len(down.calls), 1)
        self.assertEqual(len(self.lines()), 2)

    def test_the_outbox_is_bounded_and_the_oldest_is_dropped_with_a_log_line(self):
        with self.assertLogs("stockify.daemon.notify", level="WARNING") as ctx:
            for i in range(notify.OUTBOX_MAX + 3):
                notify.send("disk_low", f"s{i}", "b", sender=Recorder(fail=OSError()), key=str(i))
        subjects = [r["subject"] for r in self.lines()]
        self.assertEqual(len(subjects), notify.OUTBOX_MAX)
        self.assertEqual(subjects[0], "s3")
        self.assertTrue(any("dropped" in line for line in ctx.output))

    def test_a_key_is_once_only_in_the_outbox_and_after_delivery(self):
        notify.send("recorder_stall", "s", "b", sender=Recorder(fail=OSError()), key="k")
        notify.send("recorder_stall", "s", "b", sender=Recorder(fail=OSError()), key="k")   # the caller tried again
        self.assertEqual(len(self.lines()), 1)                    # still one waiting copy
        notify.retry_outbox(sender=Recorder())
        again = Recorder()
        self.assertIsNone(notify.send("recorder_stall", "s", "b", sender=again, key="k"))
        self.assertEqual(again.calls, [])                         # delivered late: never sent a second time

    def test_queued_says_whether_a_keyed_message_waits_in_the_outbox(self):
        """CW 3: a caller can tell 'kept for later' from 'lost', so it does not send the same reminder again."""
        self.assertFalse(notify.queued("watch_results", "results:2026-11-20"))
        notify.send("watch_results", "s", "b", sender=Recorder(fail=OSError()), key="results:2026-11-20")
        self.assertTrue(notify.queued("watch_results", "results:2026-11-20"))
        self.assertFalse(notify.queued("watch_results", "results:2026-11-21"))
        self.assertFalse(notify.queued("watch_high_low", "results:2026-11-20"))
        notify.retry_outbox(sender=Recorder())
        self.assertFalse(notify.queued("watch_results", "results:2026-11-20"))

    def test_a_callers_retry_that_works_removes_the_waiting_copy(self):
        notify.send("recorder_stall", "s", "b", sender=Recorder(fail=OSError()), key="k")
        notify.send("recorder_stall", "s", "b", sender=Recorder(), key="k")
        self.assertEqual(self.lines(), [])

    def test_a_good_send_drains_the_outbox(self):
        notify.send("disk_low", "old", "b", sender=Recorder(fail=OSError()), key="1")
        ok = Recorder()
        with mock.patch.object(notify, "_default_sender", ok):
            notify.send("disk_low", "new", "b", sender=ok, key="2")
        self.assertEqual([c[1] for c in ok.calls], ["new", "old"])
        self.assertEqual(self.lines(), [])

    def test_a_damaged_line_does_not_stop_the_rest(self):
        notify.send("disk_low", "s", "b", sender=Recorder(fail=OSError()), key="1")
        with notify.outbox_path().open("a", encoding="utf-8") as f:
            f.write("{not json\n")
        self.assertEqual(notify.retry_outbox(sender=Recorder()), 1)


class ToastTests(NotifyTestBase):
    """RM 7.3: a Windows toast says a notification could not be emailed; the subject only, never injectable."""

    def setUp(self):
        super().setUp()
        self.configured(channel="email")
        self.addCleanup(lambda: notify.outbox_path().unlink(missing_ok=True))

    def test_blocked_under_tests(self):
        with mock.patch("subprocess.Popen") as popen, mock.patch("subprocess.run") as run:
            notify.toast("[STOCKIFY] x")
            notify.flush_toasts(2)
        popen.assert_not_called()
        run.assert_not_called()

    def test_the_command_cannot_be_broken_out_of(self):
        evil = "a' ; Remove-Item C:\\ -Recurse ; '\u2018\u2019\"`$(calc)\nnext"
        cmd = notify.toast_command(evil)
        self.assertEqual(cmd[:5], ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command"])
        self.assertEqual(len(cmd), 6)
        script = cmd[5]
        self.assertNotIn("\n", script)
        # Every quote character of the subject sits doubled inside the single-quoted literal.
        self.assertIn("a'' ; Remove-Item", script)
        self.assertIn("\u2018\u2018\u2019\u2019", script)

    def test_a_failed_email_shows_a_toast_but_a_retry_does_not(self):
        with mock.patch.object(notify, "toast") as toast:
            notify.send("disk_low", "s1", "b", sender=Recorder(fail=OSError()), key="1")
            self.assertEqual(toast.call_args_list, [mock.call("s1")])
            notify.retry_outbox(sender=Recorder(fail=OSError()))
            self.assertEqual(toast.call_count, 1)
