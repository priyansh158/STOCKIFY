"""stockify.daemon.run_recorder, live path: the day's landing file is created only when recording
starts (I-13). A run that returns early must leave no landing file behind: an empty one for a day
whose landing was already deleted after verification is a stray that compaction has to refuse.
Nothing is live here: fake clock, fake ticker, no Kite login, no network."""
from __future__ import annotations

import threading
import unittest
from datetime import date
from unittest.mock import patch

import tests.support as support  # noqa: F401  — must precede project imports

import kite_client
from stockify import daemon, paths, store, timeutil
from stockify.daemon import exitcodes
from stockify.daemon.clock import ManualClock
from stockify.daemon.landing import LandingDB
from stockify.data import calendar as cal
from stockify.data import refdata

DAY = date(2026, 11, 9)                       # a Monday, an NSE trading day


def ist(hhmm: str) -> int:
    return timeutil.ist_ns(DAY, hhmm)


class FakeTicket:
    def __init__(self):
        self.released = 0

    def release(self) -> None:
        self.released += 1


class FakeTicker:
    """Stands in for KiteTicker: holds a connection-budget ticket, never connects."""

    def __init__(self):
        self.budget_ticket = FakeTicket()
        self.connected = self.closed = False

    def connect(self, threaded: bool = False) -> None:
        self.connected = True

    def close(self, *args) -> None:
        self.closed = True

    def subscribe(self, tokens) -> None:
        pass

    def unsubscribe(self, tokens) -> None:
        pass

    def set_mode(self, mode, tokens) -> None:
        pass


class LiveRecorderTests(unittest.TestCase):
    def setUp(self):
        cal.reload()
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        for module in (refdata, store):
            support.reset_sqlite_singleton(module)
            self.addCleanup(support.reset_sqlite_singleton, module)
        self.clock = ManualClock(ist("06:00"))
        self.ticker = FakeTicker()
        for target, name, value in (
                ("stockify.daemon.clock", "WallClock", lambda: self.clock),
                ("stockify.daemon.ticker", "make_live_ticker", lambda *a, **kw: self.ticker),
                ("stockify.daemon.ticker", "live_call_in_ticker", lambda fn, *args: fn(*args)),
                ("stockify.kite_readonly", "client", lambda: None)):
            patcher = patch(f"{target}.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for obj, name, value in (
                (timeutil, "ist_today", lambda: DAY),
                (kite_client, "is_authenticated", lambda: True),
                (kite_client, "load_cached_token", lambda: {"access_token": "not-a-real-token"}),
                (kite_client, "_api_key", lambda: "not-a-real-key"),
                (daemon, "_ensure_refdata", lambda day: None)):
            patcher = patch.object(obj, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_at(self, hhmm: str, stop: threading.Event | None = None) -> int:
        self.clock.set(ist(hhmm))
        return daemon.run_recorder(ticker_spec="live", stop=stop)

    def landing_files(self) -> list[str]:
        return sorted(p.name for p in paths.ticks_landing_dir().iterdir())

    def test_a_run_started_after_the_unsubscribe_time_creates_no_landing_file(self):
        self.assertEqual(self.run_at("15:45"), exitcodes.OK)
        self.assertEqual(self.landing_files(), [])
        self.assertEqual(self.run_at("17:00"), exitcodes.OK)
        self.assertEqual(self.landing_files(), [])
        self.assertFalse(self.ticker.connected)
        self.assertEqual(self.ticker.budget_ticket.released, 2)      # the connection ticket is given back each time

    def test_a_run_stopped_while_waiting_for_the_subscribe_time_creates_no_landing_file(self):
        stop = threading.Event()
        stop.set()
        self.assertEqual(self.run_at("08:50", stop), exitcodes.OK)
        self.assertEqual(self.landing_files(), [])
        self.assertEqual(self.ticker.budget_ticket.released, 1)

    def test_a_run_inside_the_session_opens_the_landing_file_and_records_into_it(self):
        stop = threading.Event()
        stop.set()                                                   # start recording, then stop at once
        self.assertEqual(self.run_at("10:00", stop), exitcodes.OK)
        self.assertEqual(self.landing_files(), [paths.ticks_landing_file(DAY).name])
        self.assertTrue(self.ticker.connected and self.ticker.closed)
        landing = LandingDB(DAY, readonly=True)
        self.addCleanup(landing.close)
        self.assertEqual(landing.meta()["trade_date"], DAY.isoformat())   # the recorder began its run in it

    def test_a_run_that_waited_for_the_subscribe_time_then_records(self):
        stop = threading.Event()
        waited = []
        real_sleep = self.clock.sleep_until

        def sleep_until(t_ns, stop_event=None):
            waited.append(t_ns)
            self.assertEqual(self.landing_files(), [])               # nothing on disk while it only waits
            ok = real_sleep(t_ns, stop_event)
            stop.set()                                               # then record, and stop at once
            return ok

        self.clock.sleep_until = sleep_until
        self.assertEqual(self.run_at("08:50", stop), exitcodes.OK)
        self.assertEqual(waited, [ist("08:58")])
        self.assertEqual(self.landing_files(), [paths.ticks_landing_file(DAY).name])


    def test_a_bad_token_map_fails_before_the_wait_and_creates_no_landing_file(self):
        waited = []
        self.clock.sleep_until = lambda t_ns, stop_event=None: waited.append(t_ns) or True
        with patch.object(daemon, "_token_map", side_effect=RuntimeError("token map is broken")):
            with self.assertRaisesRegex(RuntimeError, "token map is broken"):
                self.run_at("08:50")
        self.assertEqual(waited, [])                                 # it did not wait for 08:58 first
        self.assertEqual(self.landing_files(), [])
        self.assertFalse(self.ticker.connected)
        self.assertEqual(self.ticker.budget_ticket.released, 1)

    def test_a_bad_universe_fails_before_the_wait_and_creates_no_landing_file(self):
        from stockify.config import loader
        waited = []
        self.clock.sleep_until = lambda t_ns, stop_event=None: waited.append(t_ns) or True
        real = loader.load_json

        def load_json(name, *args, **kw):
            if name == "universe.json":
                raise ValueError("universe is broken")
            return real(name, *args, **kw)

        with patch.object(loader, "load_json", load_json):
            with self.assertRaisesRegex(ValueError, "universe is broken"):
                self.run_at("08:50")
        self.assertEqual(waited, [])
        self.assertEqual(self.landing_files(), [])
        self.assertEqual(self.ticker.budget_ticket.released, 1)


if __name__ == "__main__":
    unittest.main()
