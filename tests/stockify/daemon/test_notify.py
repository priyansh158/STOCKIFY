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

    def configured(self, **kw):
        p = mock.patch.object(notify, "settings", return_value=settings(**kw))
        p.start()
        self.addCleanup(p.stop)


class SendTests(NotifyTestBase):
    def test_default_is_log_only(self):
        self.assertIsNone(loader.load_json("notify.json")["channel"])           # the shipped file
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

    def test_ntfy_posts_without_logging_topic(self):
        self.configured(channel="ntfy")
        seen = {}

        def post(url, data, headers):
            seen.update(url=url, data=data, headers=headers)

        with support.isolated_env({"STOCKIFY_NTFY_TOPIC": SECRET_TOPIC}), \
                mock.patch.object(notify, "_http_post", post), \
                self.assertLogs("stockify.daemon.notify", "DEBUG") as cm:
            self.assertIsNone(notify.send("login_needed", "Kite login", "please log in"))
        self.assertTrue(seen["url"].startswith("https://"))
        self.assertTrue(seen["url"].endswith(SECRET_TOPIC))
        self.assertEqual(seen["data"], b"please log in")
        self.assertNotIn(SECRET_TOPIC, "\n".join(cm.output))
        # a transport error that quotes the URL must not leak it either
        with support.isolated_env({"STOCKIFY_NTFY_TOPIC": SECRET_TOPIC}), \
                mock.patch.object(notify, "_http_post", side_effect=OSError(f"cannot reach {seen['url']}")), \
                self.assertLogs("stockify.daemon.notify", "DEBUG") as cm:
            error = notify.send("login_needed", "Kite login", "please log in")
        self.assertNotIn(SECRET_TOPIC, f"{error}\n" + "\n".join(cm.output))

    def test_telegram_posts_without_logging_token(self):
        self.configured(channel="telegram")
        seen = {}
        env = {"STOCKIFY_TELEGRAM_TOKEN": SECRET_TOKEN, "STOCKIFY_TELEGRAM_CHAT": "4242"}
        with support.isolated_env(env), \
                mock.patch.object(notify, "_http_post", lambda url, data, headers: seen.update(url=url, data=data)), \
                self.assertLogs("stockify.daemon.notify", "DEBUG") as cm:
            self.assertIsNone(notify.send("disk_low", "disk", "low"))
        self.assertIn(SECRET_TOKEN, seen["url"])
        self.assertEqual(json.loads(seen["data"])["chat_id"], "4242")
        self.assertNotIn(SECRET_TOKEN, "\n".join(cm.output))

    def test_a_missing_secret_is_an_error_not_a_crash(self):
        self.configured(channel="ntfy")
        with support.isolated_env({}), self.assertLogs("stockify.daemon.notify", "ERROR"):
            self.assertIsNotNone(notify.send("disk_low", "s", "b"))

    def test_real_delivery_is_blocked_under_test(self):
        self.configured(channel="email")
        with self.assertLogs("stockify.daemon.notify", "ERROR"):
            self.assertIsNotNone(notify.send("disk_low", "s", "b"))              # the default sender refuses

    def test_the_login_reminder_still_emails_while_the_channel_is_unset(self):
        sender = Recorder()
        self.assertIsNone(notify.send("login_needed", "s", "b", sender=sender, legacy_email=True))
        self.assertEqual([c[0] for c in sender.calls], ["email"])

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
        self.configured(channel="ntfy", **{e: False for e in loader.NOTIFY_EVENTS})
        sender = Recorder()
        with mock.patch.object(notify, "_default_sender", sender):
            code, _ = self.run_cli()
        self.assertEqual((code, [c[0] for c in sender.calls]), (0, ["ntfy"]))

    def test_notify_test_without_a_channel_says_so(self):
        self.configured(channel=None)
        code, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("no channel", err)

    def test_notify_test_failure_names_the_class_only(self):
        self.configured(channel="telegram")
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
        self.assertEqual(self.load(settings("ntfy"))["channel"], "ntfy")
        with self.assertRaises(loader.ConfigError):
            self.load(settings("sms"))
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
        with mock.patch.object(notify, "send_daily_summary", return_value=False) as send:
            daemon._daily_summary(DAY, state)                         # nothing finalised yet: tried again later
            self.assertFalse(state.done("daily_summary", DAY))
            send.return_value = True
            daemon._daily_summary(DAY, state)
            daemon._daily_summary(DAY, state)
        self.assertEqual(send.call_count, 2)
        self.assertTrue(state.done("daily_summary", DAY))
        enable_logging(self)
        with mock.patch.object(notify, "send_daily_summary", side_effect=RuntimeError("boom")),                 self.assertLogs("stockify.daemon", "ERROR"):
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


if __name__ == "__main__":
    unittest.main()
