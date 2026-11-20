"""CW 3: results coming up for the watched stocks (`stockify.watchlist.reminders`): the window over weekends and holidays,
past meetings, the once-only record per meeting date, a moved date, a failed email tried again."""
from __future__ import annotations

from datetime import date

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.watchlist.test_store import StoreCase

from stockify import dbconn, timeutil
from stockify.fundamentals import calendar as fcal
from stockify.fundamentals import store as fstore
from stockify.watchlist import reminders, store

FRI = date(2026, 11, 20)            # the Tuesday after (2026-11-24, Prakash Gurpurb) is an NSE holiday


class Send:
    def __init__(self, error=None):
        self.calls: list[tuple] = []
        self.error = error

    def __call__(self, event, subject, body, *, key):
        self.calls.append((event, subject, body, key))
        return self.error


class ReminderCase(StoreCase):
    db_modules = StoreCase.db_modules + (fstore,)

    def setUp(self):
        super().setUp()
        store.add("RELIANCE")
        store.add("INFY")
        self.send = Send()

    def meetings(self, *rows: tuple[str, str]) -> None:
        conn = fstore._connect()
        with dbconn.locked_write(fstore._lock, conn):
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM results_calendar")
            conn.executemany("INSERT INTO results_calendar (symbol, meeting_date, purpose, period_hint, read_on) "
                             "VALUES (?, ?, 'Financial Results', ?, ?)",
                             [(s, d, fcal.period_hint(date.fromisoformat(d)), FRI.isoformat()) for s, d in rows])
            conn.execute("COMMIT")

    def remind(self, day: date = FRI, at: str = "21:00", on: date | None = None) -> dict:
        return reminders.run(day, now_ns=timeutil.ist_ns(on or day, at), send=self.send)


class WindowTests(ReminderCase):
    def test_a_friday_covers_the_weekend_and_two_trading_days_past_a_holiday(self):
        self.assertEqual(reminders.horizon(FRI, 2), date(2026, 11, 25))
        self.meetings(("RELIANCE", "2026-11-20"), ("RELIANCE", "2026-11-21"), ("INFY", "2026-11-24"),
                      ("INFY", "2026-11-25"), ("RELIANCE", "2026-11-26"), ("TCS", "2026-11-23"))
        res = self.remind()
        self.assertEqual(res["told"], [("RELIANCE", "2026-11-21"), ("INFY", "2026-11-24"), ("INFY", "2026-11-25")])
        [(event, subject, body, key)] = self.send.calls
        self.assertEqual((event, key), ("watch_results", "results:2026-11-20"))
        self.assertEqual(subject, "[STOCKIFY] Results coming up: RELIANCE, INFY")
        self.assertIn("RELIANCE: board meeting on Sat 21 Nov 2026 for results (Jul–Sep quarter)", body)
        self.assertNotIn("TCS", body)                                    # not watched

    def test_a_catch_up_the_next_morning_skips_meetings_already_past(self):
        self.meetings(("RELIANCE", "2026-11-21"), ("INFY", "2026-11-23"))
        res = self.remind(at="08:00", on=date(2026, 11, 23))                # Friday's run, done on Monday morning
        self.assertEqual(res["told"], [("INFY", "2026-11-23")])

    def test_nothing_due_sends_nothing(self):
        self.meetings(("RELIANCE", "2026-12-15"))
        self.assertEqual(self.remind()["told"], [])
        self.assertEqual(self.send.calls, [])


class OnceTests(ReminderCase):
    def test_each_meeting_is_told_once_and_a_moved_date_is_told_again(self):
        self.meetings(("RELIANCE", "2026-11-23"))
        self.remind()
        self.remind()                                                       # a second run, or a restart
        self.assertEqual(len(self.send.calls), 1)
        self.meetings(("RELIANCE", "2026-11-25"))                        # NSE moved it
        self.assertEqual(self.remind()["told"], [("RELIANCE", "2026-11-25")])
        self.assertEqual(len(self.send.calls), 2)
        self.assertEqual({a["ref"]: a["delivery"] for a in store.alerts() if a["kind"] == "results"},
                         {"2026-11-23": "sent", "2026-11-25": "sent"})

    def test_a_failed_email_is_tried_again(self):
        self.meetings(("RELIANCE", "2026-11-23"))
        self.send.error = "SMTPServerDisconnected"
        self.assertEqual(self.remind()["error"], "SMTPServerDisconnected")
        self.assertEqual(store.alerts()[0]["delivery"], "failed")
        self.send.error = None
        res = self.remind(at="21:30")
        self.assertEqual((res["told"], res["error"]), ([("RELIANCE", "2026-11-23")], None))
        self.assertEqual(store.alerts()[0]["delivery"], "sent")

    def test_an_empty_watchlist_reads_nothing(self):
        store.remove("RELIANCE")
        store.remove("INFY")
        self.meetings(("RELIANCE", "2026-11-23"))
        self.assertEqual(self.remind(), {"meetings": 0, "told": [], "error": None})


class CalendarSymbolsTests(ReminderCase):
    def test_the_calendar_refresh_reads_universe_and_watched_stocks(self):
        """CW 3: fundamentals/calendar.refresh keeps the meetings of universe ∪ watchlist (no extra NSE request)."""
        from unittest import mock
        doc = [{"symbol": "INFY", "purpose": "Financial Results", "date": "23-Nov-2026"},
               {"symbol": "ABB", "purpose": "Financial Results", "date": "23-Nov-2026"},
               {"symbol": "TCS", "purpose": "Financial Results", "date": "23-Nov-2026"}]
        client = mock.MagicMock()
        client.json.return_value = doc
        with mock.patch.object(fcal.universe, "current", return_value=[{"symbol": "ABB"}]):
            self.assertEqual(fcal.refresh(FRI, client=client), 2)
        self.assertEqual({r["symbol"] for r in fcal.upcoming(FRI)}, {"ABB", "INFY"})
        self.assertEqual(client.json.call_count, 1)

    def test_a_broken_watchlist_leaves_the_universe_meetings(self):
        from unittest import mock
        client = mock.MagicMock()
        client.json.return_value = [{"symbol": "ABB", "purpose": "Financial Results", "date": "23-Nov-2026"}]
        with mock.patch.object(fcal.universe, "current", return_value=[{"symbol": "ABB"}]), \
                mock.patch.object(store, "symbols", side_effect=RuntimeError("corrupt")):
            self.assertEqual(fcal.refresh(FRI, client=client), 1)
