"""GK 5.1 (M5.1): one ticker feeds the recorder and the paper sessions; a stop drains, syncs and journals the
process stop, and never finalises a day.

The day is the paper fixture's NIFTY expiry day, landed once and replayed through the real daemon code
(`replay:<day>:max`): nothing is live, no Kite login, no network, nothing outside the test sandbox."""
from __future__ import annotations

import json
import signal
import threading
import time
import unittest
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.daemon import paper_fixture as pf

from stockify import __main__ as cli
from stockify import engine, paths, store
from stockify.config import loader
from stockify.daemon import exitcodes, journal, lifecycle, market_day, session_day
from stockify.daemon.dispatcher import Dispatcher
from stockify.daemon.landing import LandingDB
from stockify.daemon.recorder import Recorder
from stockify.daemon.replay_ticker import ReplayTicker
from stockify.data import packs

DAY = pf.DAY
SPEC = f"replay:{DAY.isoformat()}:max"


def decode(sid: str) -> list[dict]:
    _, body, _ = journal.read(paths.journal_file(sid, DAY))
    out, off = [], 0
    while off < len(body):
        rec, off = packs.decode(body, off)
        out.append(rec)
    return out


def process_stops(sid: str) -> list[dict]:
    return [r for r in decode(sid) if r["kind"] == "GAP" and r["reason"] == "process_stop"]


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class MarketDayTest(pf.PaperDayTest):
    def setUp(self):
        super().setUp()
        self.record(seconds=120)
        self.patch_universe()
        self.cfg = self.session_cfg(strategy={"kind": "script", "actions": []})
        self.add_session("s1")

    def patch_universe(self):
        real = loader.load_json
        patcher = mock.patch.object(
            loader, "load_json", lambda name, *a, **kw: pf.UNIVERSE if name == "universe.json" else real(name, *a, **kw))
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def add_session(self, sid, *, arm=True):
        store.create_session(sid, sid, self.cfg, 1)
        if arm:
            lifecycle.arm(sid, wall_ns=2)

    def run_day(self, **kw):
        kw.setdefault("ticker_spec", SPEC)
        kw.setdefault("session_ids", ["s1"])
        return market_day.run_market_day(**kw)

    def status(self) -> dict:
        return json.loads(paths.daemon_status_file().read_text(encoding="utf-8"))

    def replay_landing(self) -> LandingDB:
        db = LandingDB(DAY, paths.ticks_replay_file(DAY), readonly=True)
        self.addCleanup(db.close)
        return db

    def stop_after(self, events: int, stop: threading.Event):
        """Ctrl+C partway through the replay: after `events` frames or heartbeats the replay thread sets `stop`
        and waits, so the main loop sees the stop before the replay can finish by itself."""
        real_wait, seen = ReplayTicker._wait, []

        def wait(ticker, prev, t_ns):
            real_wait(ticker, prev, t_ns)
            seen.append(t_ns)
            if len(seen) == events:
                stop.set()
                time.sleep(0.4)

        patcher = mock.patch.object(ReplayTicker, "_wait", wait)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    # ── E-01 part 1 ──────────────────────────────────────────────────────────
    def test_replay_day_with_one_session_end_to_end(self):
        self.assertEqual(self.run_day(), exitcodes.OK)
        records = decode("s1")
        self.assertGreater(len(records), 50)
        self.assertTrue(any(r["kind"] == "CLOCK" for r in records))
        self.assertFalse(journal.is_finalised(paths.journal_file("s1", DAY)))          # a day is never finalised here
        self.assertEqual(process_stops("s1"), [])                                        # the replay ended by itself
        row = store.get_session_day("s1", pf.EXPIRY)
        self.assertEqual(row["status"], "open")
        self.assertGreater(row["last_frame_no"], 0)
        self.assertTrue(paths.day_bundle_file("s1", DAY).exists())
        self.assertEqual(store.get_session("s1")["status"], "RUNNING")                   # ARMED -> RUNNING at the open
        landing = self.replay_landing()
        self.assertGreater(landing.max_frame_no(), 0)                                    # the recorder still recorded
        self.assertEqual(row["last_frame_no"], landing.max_frame_no())                   # and every frame was drained
        status = self.status()
        self.assertEqual((status["last_frame_no"], status["exit_code"]), (landing.max_frame_no(), 0))
        self.assertGreater(status["last_frame_recv_ns"], 0)
        names = {t.name for t in threading.enumerate()}
        self.assertFalse({"engine", "recorder-writer"} & names, names)                   # both threads are gone

    def test_all_means_every_armed_or_running_session_and_none_means_none(self):
        self.add_session("s2")
        self.add_session("s3", arm=False)                                               # CREATED: not armed, not run
        self.assertEqual(self.run_day(session_ids=None), exitcodes.OK)
        self.assertTrue(paths.journal_file("s1", DAY).exists() and paths.journal_file("s2", DAY).exists())
        self.assertFalse(paths.journal_file("s3", DAY).exists())
        self.assertEqual([store.get_session(s)["status"] for s in ("s1", "s2", "s3")], ["RUNNING", "RUNNING", "CREATED"])

    def test_the_cli_spells_the_session_choice(self):
        self.assertIsNone(cli._parse_sessions("all"))
        self.assertEqual(cli._parse_sessions("none"), [])
        self.assertEqual(cli._parse_sessions("a, b,,c"), ["a", "b", "c"])

    # ── shutdown ─────────────────────────────────────────────────────────────
    def test_ctrl_break_drains_and_journals_process_stop(self):
        stop = threading.Event()
        self.stop_after(40, stop)
        order = []

        def spy(owner, name, tag):
            real = getattr(owner, name)

            def wrapped(*a, **kw):
                order.append((tag, kw.get("finalise")))
                return real(*a, **kw)

            patcher = mock.patch.object(owner, name, wrapped)
            patcher.start()
            self.addCleanup(patcher.stop)

        spy(ReplayTicker, "close", "ticker_close")
        spy(Recorder, "stop", "recorder_stop")
        spy(Dispatcher, "drain", "dispatcher_drain")
        spy(session_day.SessionDay, "fsync", "journal_fsync")
        spy(session_day.SessionDay, "close", "journal_close")
        spy(market_day, "write_status", "status")
        self.assertEqual(self.run_day(stop=stop), exitcodes.OK)
        names = [n for n, _ in order]
        first = {n: names.index(n) for n in set(names)}
        self.assertLess(first["ticker_close"], first["recorder_stop"])                  # 1 stop the ticker
        self.assertLess(first["recorder_stop"], first["dispatcher_drain"])             # 2 drain both queues
        self.assertLess(first["dispatcher_drain"], first["journal_fsync"])             # 3 fsync the journals
        self.assertLess(first["journal_fsync"], first["journal_close"])                # 5 close
        self.assertLess(first["journal_close"], first["status"])                       # 6 status file
        self.assertEqual({fin for n, fin in order if n == "journal_close"}, {False})   # never finalised early
        stops = process_stops("s1")                                                      # 4 the process stop
        self.assertEqual(len(stops), 1)
        self.assertEqual((stops[0]["phase"], stops[0]["scope"]), (0, 0))                 # a START, scope all
        self.assertEqual(decode("s1")[-1]["kind"], "GAP")                                # and it is the journal's tail
        self.assertFalse(journal.is_finalised(paths.journal_file("s1", DAY)))
        _, _, torn = journal.read(paths.journal_file("s1", DAY))
        self.assertEqual(torn, 0)
        landing = self.replay_landing()                                                  # everything delivered is kept
        row = store.get_session_day("s1", pf.EXPIRY)
        self.assertEqual(row["last_frame_no"], landing.max_frame_no())
        self.assertEqual(self.status()["last_frame_no"], landing.max_frame_no())
        self.assertLess(landing.max_frame_no(), 120)                                     # it really stopped partway

    def test_no_process_stop_gap_after_close(self):
        stop = threading.Event()
        self.stop_after(40, stop)
        with mock.patch.object(market_day, "inside_market_hours", return_value=False):
            self.assertEqual(self.run_day(stop=stop), exitcodes.OK)
        self.assertEqual(process_stops("s1"), [])
        self.assertFalse(journal.is_finalised(paths.journal_file("s1", DAY)))
        self.assertGreater(len(decode("s1")), 0)

    def test_inside_market_hours_follows_the_exchange_phases(self):
        for hhmmss, inside in (("08:00:00", False), ("09:05:00", True), ("10:00:00", True), ("15:39:59", True),
                               ("15:40:00", False), ("18:00:00", False)):
            with self.subTest(hhmmss):
                self.assertEqual(market_day.inside_market_hours(self.t(hhmmss)), inside)
        self.assertFalse(market_day.inside_market_hours(self.t("10:00:00") + 4 * 86_400 * 10**9))     # a Saturday

    def test_ctrl_c_and_ctrl_break_both_set_the_stop_event(self):
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGBREAK)}
        for sig in (signal.SIGINT, signal.SIGBREAK):
            with self.subTest(sig.name):
                stop = threading.Event()
                restore = market_day.install_signal_handlers(stop)
                try:
                    signal.raise_signal(sig)
                    self.assertTrue(stop.is_set())
                finally:
                    restore()
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)      # the old handlers are back

    # ── recorder only ────────────────────────────────────────────────────────
    def test_recorder_only_still_works(self):
        from stockify import daemon
        for call in (lambda: self.run_day(session_ids=[]), lambda: daemon.run_recorder(ticker_spec=SPEC)):
            self.assertEqual(call(), exitcodes.OK)
            landing = self.replay_landing()
            self.assertGreater(landing.max_frame_no(), 0)
            self.assertFalse(paths.journal_file("s1", DAY).exists())                    # no session was opened
            self.assertEqual(store.get_session("s1")["status"], "ARMED")
            self.assertEqual(self.status()["last_frame_no"], landing.max_frame_no())
            landing.close()
            paths.ticks_replay_file(DAY).unlink()

    def test_a_session_that_cannot_open_never_costs_the_recording(self):
        self.add_session("s2")
        real = session_day.SessionDay.open

        def open_(sd, *a, **kw):
            if sd.session_id == "s1":
                raise session_day.SessionDayError("no bundle today")
            return real(sd, *a, **kw)

        with mock.patch.object(session_day.SessionDay, "open", open_):
            self.assertEqual(self.run_day(session_ids=["s1", "s2", "nope"]), exitcodes.OK)
        landing = self.replay_landing()
        self.assertGreater(landing.max_frame_no(), 0)
        self.assertTrue(paths.journal_file("s2", DAY).exists())                         # the other session ran
        self.assertGreater(store.get_session_day("s2", pf.EXPIRY)["last_frame_no"], 0)
        self.assertIn("session_failed", [r["kind"] for r in store.timeline("s1")])
        self.assertEqual(store.get_session("s1")["status"], "ARMED")                    # not started, not stopped

    def test_a_restart_resumes_the_journal_and_a_finalised_day_is_left_alone(self):
        stop = threading.Event()
        patcher = self.stop_after(40, stop)
        self.assertEqual(self.run_day(stop=stop), exitcodes.OK)
        patcher.stop()
        before = len(decode("s1"))
        paths.ticks_replay_file(DAY).unlink()
        self.assertEqual(self.run_day(), exitcodes.OK)                                  # same journal, resumed
        self.assertGreater(len(decode("s1")), before)
        jp = paths.journal_file("s1", DAY)
        journal.JournalWriter(jp, fsync_every_mono_ns=1, mono=time.monotonic_ns).close(finalise=True)
        size = jp.stat().st_size
        paths.ticks_replay_file(DAY).unlink()
        self.assertEqual(self.run_day(), exitcodes.OK)
        self.assertEqual(jp.stat().st_size, size)                                       # a finalised day takes no input


if __name__ == "__main__":
    unittest.main()
