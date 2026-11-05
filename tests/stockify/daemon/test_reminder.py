"""stockify.daemon.reminder: the daily Kite login email (D-32)."""
from __future__ import annotations

import unittest
from datetime import date

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import timeutil
from stockify.daemon import exitcodes, reminder, scheduler
from stockify.daemon.clock import ManualClock
from stockify.daemon.keepawake import FakePower
from stockify.data import calendar as cal

DAY = date(2026, 11, 9)


def ist(hhmm: str) -> int:
    return timeutil.ist_ns(DAY, hhmm)


class ReminderTests(unittest.TestCase):
    def setUp(self):
        cal.reload()
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        self.cfg = scheduler.schedule()
        self.state = scheduler.State()
        self.sent: list[tuple[str, str, str]] = []

    def wait(self, start: str, *, fresh=lambda: False):
        clock = ManualClock(ist(start))

        def send(subject, body):
            self.sent.append((timeutil.ns_to_ist(clock.now_ns()).strftime("%H:%M"), subject, body))
            return None

        ok = reminder.wait_with_reminders(clock, None, ist("08:45"), DAY, self.state, self.cfg,
                                          token_fresh=fresh, send=send)
        return ok, clock

    def test_two_reminders_while_the_token_stays_stale(self):
        ok, clock = self.wait("06:00")
        self.assertTrue(ok)
        self.assertEqual([s[0] for s in self.sent], ["06:05", "08:00"])
        self.assertEqual(clock.now_ns(), ist("08:45"))

    def test_no_reminder_once_logged_in(self):
        self.wait("06:00", fresh=lambda: True)
        self.assertEqual(self.sent, [])

    def test_each_reminder_is_sent_once_per_day(self):
        self.wait("06:00")
        self.wait("06:00")
        self.assertEqual(len(self.sent), 2)

    def test_message_names_the_session_and_the_local_deadline(self):
        subject, body = reminder.message(DAY, self.cfg)
        self.assertIn("Mon 09 Nov", subject)
        self.assertIn("19:30 PST", body)                    # 09:00 IST on Mon 9 Nov = Sun 8 Nov 19:30 PST
        self.assertIn(reminder.LOGIN_URL, body)

    def test_seattle_times_of_the_reminders(self):
        local = [timeutil.ns_to_ist(t).astimezone(cal.owner_tz()).strftime("%a %H:%M")
                 for _, t in reminder.reminder_times(DAY, self.cfg)]
        self.assertEqual(local, ["Sun 16:35", "Sun 18:30"])

    def test_the_daemon_sends_reminders_before_the_session(self):
        from stockify import daemon
        clock = ManualClock(ist("06:01"))
        sent = []
        code = daemon.run_daemon(power=FakePower(), clock=clock,
                                 recorder=lambda: exitcodes.TOKEN_EXPIRED,
                                 token_fresh=lambda: False,
                                 send_reminder=lambda s, b: sent.append(s) or None)
        self.assertEqual((code, len(sent)), (exitcodes.TOKEN_EXPIRED, 2))


if __name__ == "__main__":
    unittest.main()
