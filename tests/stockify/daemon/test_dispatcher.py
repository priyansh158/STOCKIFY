"""DM 5.7 (D-72): one queue in, one engine thread; time comes from the frames, never from the wall clock."""
from __future__ import annotations

import itertools
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.daemon import packets_fixture as pkt, paper_fixture as pf

from stockify import engine, paths, store
from stockify.config import loader
from stockify.daemon import journal, kite_packets as kp, lifecycle, session_day
from stockify.daemon.clock import ManualClock
from stockify.daemon.dispatcher import Dispatcher
from stockify.daemon.encoder import FrameEncoder
from stockify.daemon.landing import LandingDB
from stockify.daemon.recorder import Recorder
from stockify.daemon.replay_ticker import MODE_FULL, ReplayClock, ReplayTicker, parse_fault
from stockify.daemon.subscriptions import SubscriptionManager
from stockify.daemon.ticker import RecorderSession
from stockify.data import packs, refdata

DAY = pf.DAY
SEC = pf.SEC


def decode(sid: str) -> list[dict]:
    _, body, _ = journal.read(paths.journal_file(sid, DAY))
    out, off = [], 0
    while off < len(body):
        rec, off = packs.decode(body, off)
        out.append(rec)
    return out


class Crash(BaseException):
    """Stands in for the process dying between two writes: nothing in the dispatcher may catch it."""


def throughput_frame() -> bytes:
    return kp.build_frame([pkt.index_full(pkt.NIFTY_INDEX, 2_400_000, 2_401_000, 2_399_000, 2_400_000, 2_390_000,
                                          1000, 1)])


class SlowEngine:
    """Delegates to the real engine but takes its time on every push."""

    def __init__(self, real):
        self._real = real

    def push(self, batch):
        time.sleep(0.002)
        return self._real.push(batch)

    def __getattr__(self, name):
        return getattr(self._real, name)


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class DispatcherTest(pf.PaperDayTest):
    def setUp(self):
        super().setUp()
        self.record(seconds=120)
        self.token_map = {pf.pf.NIFTY_INDEX: self.idx, pf.FUT_TOKEN: self.fut,
                          **{pf.CE_TOKEN + i: iid for i, iid in enumerate(self.ces)}}
        self.paper = dict(loader.load_json("paper.json"))
        self.ticks = itertools.count(0, 10)

    # ── helpers ──────────────────────────────────────────────────────────────
    def cfg(self, **over):
        return self.session_cfg(strategy={"kind": "script", "actions": []}, **over)

    def make_day(self, sid, cfg=None):
        store.create_session(sid, sid, {}, 1)
        sd = session_day.SessionDay(sid, DAY, cfg or self.cfg(), mono=lambda: next(self.ticks))
        self.addCleanup(sd.close)
        sd.open(None, [], self.t("09:14:00"))
        return sd

    def dispatcher(self, sessions, *, replay=True, **paper):
        clock = ManualClock(self.t("09:14:59"))
        return Dispatcher(sessions, FrameEncoder(self.token_map), clock=clock, cfg={**self.paper, **paper},
                          replay=replay)

    def play(self, disp, faults=()):
        clock = ReplayClock(self.t("09:14:59"))
        src = LandingDB(DAY, readonly=True)
        self.addCleanup(src.close)
        ticker = ReplayTicker(src, clock, faults=[parse_fault(f, DAY) for f in faults])
        tokens = list(self.token_map)
        ticker.subscribe(tokens)
        ticker.set_mode(MODE_FULL, tokens)
        n = itertools.count(1)

        def on_message(ws, payload, is_binary):
            data = len(payload) > 1
            disp.put_frame((next(n) if data else 0, clock.now_ns(), clock.mono_ns(), payload, True))

        ticker.on_message = on_message
        ticker.connect(threaded=False)
        return ticker

    def journal_bytes(self, sid):
        return paths.journal_file(sid, DAY).read_bytes()

    # ── determinism ──────────────────────────────────────────────────────────
    def test_two_replays_give_identical_journals(self):
        first, second = self.make_day("s1"), self.make_day("s2")
        for sd in (first, second):
            disp = self.dispatcher([sd])
            self.play(disp)
            disp.drain()
        a, b = self.journal_bytes("s1"), self.journal_bytes("s2")
        self.assertGreater(len(a), 10_000)
        self.assertEqual(a, b)
        self.assertEqual(store.get_session_day("s1", pf.EXPIRY)["last_frame_no"],
                         store.get_session_day("s2", pf.EXPIRY)["last_frame_no"])

    def test_clock_from_stamps_not_wall(self):
        fast, slow = self.make_day("s1"), self.make_day("s2")
        slow.engine = SlowEngine(slow.engine)
        d1 = self.dispatcher([fast])
        self.play(d1)
        d1.drain()
        d2 = self.dispatcher([slow])
        stop = threading.Event()
        thread = threading.Thread(target=d2.run, args=(stop,))
        thread.start()
        self.play(d2)
        stop.set()
        thread.join(60)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.journal_bytes("s1"), self.journal_bytes("s2"))
        clocks = [r["t_event"] for r in decode("s1") if r["kind"] == "CLOCK"]
        self.assertGreater(len(clocks), 100)
        self.assertTrue(all(c % SEC == 0 for c in clocks))
        self.assertEqual(clocks, sorted(set(clocks)))

    def test_clock_cadence_comes_from_config(self):
        sd = self.make_day("s1")
        disp = self.dispatcher([sd], clock_every_s=10)
        self.play(disp)
        disp.drain()
        clocks = [r["t_event"] for r in decode("s1") if r["kind"] == "CLOCK"]
        self.assertTrue(clocks and all(c % (10 * SEC) == 0 for c in clocks))
        self.assertLessEqual(len(clocks), 13)

    # ── gaps ─────────────────────────────────────────────────────────────────
    def test_injected_disconnect_gives_a_journal_gap_pair(self):
        sd = self.make_day("s1")
        disp = self.dispatcher([sd])
        clock = ReplayClock(self.t("09:14:59"))
        src = LandingDB(DAY, readonly=True)
        self.addCleanup(src.close)
        ticker = ReplayTicker(src, clock, faults=[parse_fault("disconnect@09:15:30+30s", DAY)])
        target = LandingDB(DAY, paths.ticks_replay_file(DAY))
        self.addCleanup(target.close)
        tokens = {r[0]: r[1] for r in refdata._connect().execute(
            "SELECT instrument_token, instrument_id FROM instrument_tokens")}
        rec = Recorder(target, tokens.get, clock)
        rec.begin()
        manager = SubscriptionManager(DAY, pf.UNIVERSE, clock, log_rows=target.log_subscriptions)
        session = RecorderSession(ticker, rec, manager, clock)
        session.gap_taps.append(disp.put_gap)
        rec.add_tap(disp)                          # the dispatcher is its own tap
        session.start()

        def on_message(ws, payload, is_binary):
            rec.on_message(ws, payload, is_binary)
            rec.process(rec.queue.get_nowait())
            session.tick()

        ticker.on_message = on_message
        ticker.connect(threaded=False)
        disp.drain()
        gaps = [(r["phase"], r["reason"], r["instrument_id"], r["t_event"]) for r in decode("s1")
                if r["kind"] == "GAP"]
        # On a whole second the CLOCK for it comes first and outranks a GAP, so D-68 lifts the GAP by 1 ns.
        self.assertEqual(gaps, [(0, "disconnect", 0, self.t("09:15:30") + 1),
                                (1, "disconnect", 0, self.t("09:16:00") + 1)])
        self.assertEqual(session.gap_taps, [disp.put_gap])

    def test_a_gap_tap_that_raises_never_reaches_the_ticker(self):
        clock = ReplayClock(self.t("09:14:59"))
        src = LandingDB(DAY, readonly=True)
        self.addCleanup(src.close)
        ticker = ReplayTicker(src, clock)
        target = LandingDB(DAY, paths.ticks_replay_file(DAY))
        self.addCleanup(target.close)
        rec = Recorder(target, {}.get, clock)
        manager = SubscriptionManager(DAY, pf.UNIVERSE, clock, log_rows=target.log_subscriptions)
        session = RecorderSession(ticker, rec, manager, clock)
        session.gap_taps.append(lambda *a: 1 / 0)
        session._on_close(None, 1006, "boom")            # opens a disconnect gap; the tap fails
        self.assertEqual(len(target.gaps()), 1)

    def test_stall_gap_journals_live_but_is_a_timeline_row_in_replay(self):
        live, replayed = self.make_day("s1"), self.make_day("s2")
        for sd, replay in ((live, False), (replayed, True)):
            disp = self.dispatcher([sd], replay=replay)
            disp.put_gap("start", "stall", self.t("09:16:00"), self.idx)
            disp.put_gap("end", "stall", self.t("09:16:30"), self.idx)
            disp.drain()
        gaps = [r for r in decode("s1") if r["kind"] == "GAP"]
        self.assertEqual([(g["phase"], g["reason"], g["instrument_id"]) for g in gaps],
                         [(0, "stall", self.idx), (1, "stall", self.idx)])
        self.assertEqual([r for r in decode("s2") if r["kind"] == "GAP"], [])
        rows = [r for r in store.timeline("s2") if r["kind"] == "stall_replay_only"]
        self.assertEqual([r["detail"]["phase"] for r in rows], ["start", "end"])
        self.assertEqual(rows[0]["detail"]["instrument_id"], self.idx)
        self.assertEqual([r for r in store.timeline("s1") if r["kind"] == "stall_replay_only"], [])

    def test_a_disconnect_is_journaled_in_replay_too(self):
        sd = self.make_day("s1")
        disp = self.dispatcher([sd], replay=True)
        disp.put_gap("start", "disconnect", self.t("09:16:00"), None)
        disp.put_gap("end", "disconnect", self.t("09:16:05"), None)
        disp.drain()
        self.assertEqual(len([r for r in decode("s1") if r["kind"] == "GAP"]), 2)

    # ── routing ──────────────────────────────────────────────────────────────
    def test_only_session_instruments_reach_its_journal(self):
        wide = self.make_day("s1")
        narrow = self.make_day("s2", self.cfg(universe=["NSE:NIFTY 50"]))
        self.assertNotIn(self.ces[4], narrow.ids)
        disp = self.dispatcher([wide, narrow])
        self.play(disp)
        disp.drain()
        for sid, sd in (("s1", wide), ("s2", narrow)):
            ticks = [r for r in decode(sid) if r["kind"] in ("TICK_QUOTE", "TICK_FULL", "INDEX")]
            self.assertTrue(ticks)
            self.assertTrue({r["instrument_id"] for r in ticks} <= sd.ids, sid)
            self.assertGreater(len([r for r in decode(sid) if r["kind"] == "CLOCK"]), 50)
        self.assertIn(self.ces[4], {r["instrument_id"] for r in decode("s1") if "ltp" in r})
        self.assertNotIn(self.ces[4], {r["instrument_id"] for r in decode("s2")})

    def test_inject_goes_to_its_session_only(self):
        a, b = self.make_day("s1"), self.make_day("s2")
        disp = self.dispatcher([a, b])
        rec = packs.enc_refdata(self.ces[4], self.t("09:16:00"), "price_band",
                                {"low_paise": 1, "high_paise": 2})
        disp.put_inject(self.t("09:16:00"), "s2", packs.RANK[packs.REFDATA_K], rec)
        disp.drain()
        self.assertEqual([r["subtype"] for r in decode("s2") if r["kind"] == "REFDATA"], ["price_band"])
        self.assertEqual([r for r in decode("s1") if r["kind"] == "REFDATA"], [])

    def test_text_frames_are_filtered_before_encode(self):
        sd = self.make_day("s1")
        disp = self.dispatcher([sd])
        disp.put_frame((0, self.t("09:16:00"), 5, '{"type": "order"}', False))
        disp.drain()
        self.assertEqual(disp.stats.items, 1)
        self.assertEqual(disp.encoder.stats.bad_frames, 0)

    # ── commands ─────────────────────────────────────────────────────────────
    def test_stop_all_command_journals_strategy_control(self):
        a, b = self.make_day("s1"), self.make_day("s2")
        cmd = store.add_command(None, "stop_all", None, 1)
        disp = self.dispatcher([a, b])
        self.play(disp)
        disp.drain()
        for sid in ("s1", "s2"):
            controls = [r for r in decode(sid) if r["kind"] == "REFDATA" and r["subtype"] == "strategy_control"]
            self.assertEqual([c["payload"] for c in controls], [{"enabled": False, "reasons": ["stop_all"]}])
            self.assertEqual(store.get_session(sid)["pause_reasons"], ["stop_all"])
        self.assertEqual(store.pending_commands(), [])
        self.assertEqual(store.get_session("s1")["pause_reasons"], ["stop_all"])
        self.assertTrue(cmd)

    def test_commands_apply_in_fifo_order_and_are_polled_on_stamp_time(self):
        sd = self.make_day("s1")
        store.add_command("s1", "pause", {"reason": "owner"}, 1)
        store.add_command("s1", "resume", None, 2)
        store.add_command("s1", "pause", None, 3)
        disp = self.dispatcher([sd], commands_every_s=60)
        disp.put_gap("start", "disconnect", self.t("09:20:00"), None)
        disp.drain()
        payloads = [r["payload"] for r in decode("s1") if r["kind"] == "REFDATA"]
        self.assertEqual(payloads, [{"enabled": False, "reasons": ["owner"]}, {"enabled": True, "reasons": []},
                                    {"enabled": False, "reasons": ["owner"]}])
        later = store.add_command("s1", "resume", None, 4)
        disp.put_gap("end", "disconnect", self.t("09:20:30"), None)      # inside the 60 s window: not polled
        disp.drain()
        self.assertEqual([c["cmd_id"] for c in store.pending_commands()], [later])
        disp.put_gap("start", "disconnect", self.t("09:21:10"), None)    # past it
        disp.drain()
        self.assertEqual(store.pending_commands(), [])
        self.assertEqual(store.get_session("s1")["pause_reasons"], [])

    def test_a_refused_command_is_marked_and_does_not_stop_the_queue(self):
        sd = self.make_day("s1")
        store.add_command("s1", "pause", {"reason": "stopped"}, 1)     # lifecycle refuses it
        store.add_command("s1", "pause", {"reason": "owner"}, 2)
        disp = self.dispatcher([sd])
        disp.put_gap("start", "disconnect", self.t("09:20:00"), None)
        disp.drain()
        self.assertEqual(store.pending_commands(), [])
        rows = store._connect().execute("SELECT result FROM session_commands ORDER BY cmd_id").fetchall()
        self.assertTrue(rows[0]["result"].startswith("error"))
        self.assertEqual(rows[1]["result"], "ok")
        self.assertEqual(store.get_session("s1")["pause_reasons"], ["owner"])

    def test_stop_journals_stopped_and_refuses_later_commands(self):
        sd = self.make_day("s1")
        store.add_command("s1", "stop", None, 1)
        store.add_command("s1", "resume", None, 2)
        disp = self.dispatcher([sd])
        disp.put_gap("start", "disconnect", self.t("09:20:00"), None)
        disp.drain()
        payloads = [r["payload"] for r in decode("s1") if r["kind"] == "REFDATA"]
        self.assertEqual(payloads, [{"enabled": False, "reasons": ["stopped"]}])
        self.assertEqual([r["detail"] for r in store.timeline("s1") if r["kind"] == "stop_requested"][0]["cmd_id"], 1)
        self.assertEqual(store.pending_commands(), [])

    # ── overflow ─────────────────────────────────────────────────────────────
    def test_overflow_is_a_gap_not_silence(self):
        sd = self.make_day("s1")
        disp = self.dispatcher([sd], dispatcher_queue_max=3)
        stamps = [self.t("09:16:00") + i * SEC for i in range(8)]
        for s in stamps:
            disp.put_frame((0, s, 1, b"\x00", True))
        self.assertEqual(disp.stats.dropped, 5)
        self.assertEqual(disp.stats.alarms, ["dispatcher_queue_full"])
        disp.drain()
        gaps = [(r["phase"], r["reason"], r["instrument_id"], r["t_event"]) for r in decode("s1")
                if r["kind"] == "GAP"]
        self.assertEqual(gaps, [(0, "stall", 0, stamps[3] + 1), (1, "stall", 0, stamps[7] + 1)])
        rows = [r for r in store.timeline("s1") if r["kind"] == "gap"]
        self.assertTrue(rows and all(r["detail"]["cause"] == "dispatcher_overflow" for r in rows))
        self.assertEqual(disp.stats.alarms, ["dispatcher_queue_full"])       # raised once

    def test_a_process_stop_is_journaled_as_one_even_with_the_queue_full(self):
        """GK 5.2 (M6): the shutdown's process_stop never goes through the queue, so it can never be dropped and
        reported as a `stall`."""
        sd = self.make_day("s1")
        disp = self.dispatcher([sd], dispatcher_queue_max=2)
        for i in range(2):
            disp.put_frame((0, self.t("09:16:00") + i * SEC, 1, b"\x00", True))
        self.assertTrue(disp.queue.full())
        disp.drain()                                  # the engine thread is done; the queue is then emptied ...
        for i in range(2):
            disp.put_frame((0, self.t("09:16:10") + i * SEC, 1, b"\x00", True))
        self.assertTrue(disp.queue.full())             # ... and a late producer fills it again
        disp.journal_process_stop(self.t("09:16:30"))
        disp.drain()
        gaps = [(r["phase"], r["reason"], r["instrument_id"]) for r in decode("s1") if r["kind"] == "GAP"]
        self.assertEqual(gaps, [(0, "process_stop", 0)])
        self.assertEqual(disp.stats.dropped, 0)
        self.assertNotIn("dispatcher_queue_full", disp.stats.alarms)

    def test_a_failed_day_does_not_stop_the_others(self):
        bad, good = self.make_day("s1"), self.make_day("s2")
        disp = self.dispatcher([bad, good])
        bad.engine = None                                                    # its next feed raises
        self.play(disp)
        disp.drain()
        self.assertIn("s1", disp.stats.failed)
        self.assertNotIn("s2", disp.stats.failed)
        self.assertGreater(len([r for r in decode("s2") if r["kind"] == "CLOCK"]), 50)

    # ── fix round 1 (review of 8770dc1) ──────────────────────────────────────
    def wait_until(self, check, seconds=10.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if check():
                return True
            time.sleep(0.02)
        return check()

    def controls(self, sid):
        return [r["payload"] for r in decode(sid) if r["kind"] == "REFDATA" and r["subtype"] == "strategy_control"]

    def test_commands_apply_on_a_silent_feed(self):
        sd = self.make_day("s1")
        disp = self.dispatcher([sd])
        stop = threading.Event()
        thread = threading.Thread(target=disp.run, args=(stop,))
        thread.start()
        try:
            store.add_command("s1", "pause", None, 1)
            disp.clock.advance(3 * SEC)                          # the feed sends nothing; only the clock moves
            self.assertTrue(self.wait_until(lambda: store.pending_commands() == []), "the command was never applied")
        finally:
            stop.set()
            thread.join(30)
        self.assertEqual(self.controls("s1"), [{"enabled": False, "reasons": ["owner"]}])
        record = [r for r in decode("s1") if r["kind"] == "REFDATA"][0]
        self.assertGreaterEqual(record["t_event"], self.t("09:14:59"))      # stamped by the dispatcher's clock

    def test_stop_is_stopped_at_command_time_and_survives_a_restart(self):
        sd = self.make_day("s1")
        store.add_command("s1", "stop", None, 1)
        disp = self.dispatcher([sd])
        self.assertFalse(hasattr(disp, "_stopping"))
        disp.put_gap("start", "disconnect", self.t("09:20:00"), None)
        disp.drain()
        self.assertEqual(self.controls("s1"), [{"enabled": False, "reasons": ["stopped"]}])
        got = store.get_session("s1")
        self.assertEqual((got["status"], got["pause_reasons"]), ("STOPPED", ["stopped"]))
        first_stop = got["stopped_ns"]
        # a restarted daemon: a new dispatcher, the store is the truth
        store.add_command("s1", "resume", None, 2)
        store.add_command("s1", "stop", None, 3)
        again = self.dispatcher([sd])
        again.put_gap("end", "disconnect", self.t("09:20:30"), None)
        again.drain()
        self.assertEqual(self.controls("s1"), [{"enabled": False, "reasons": ["stopped"]}])   # nothing journaled
        rows = store._connect().execute("SELECT result FROM session_commands ORDER BY cmd_id").fetchall()
        self.assertTrue(rows[1]["result"].startswith("error"))
        self.assertEqual(rows[2]["result"], "ok")
        self.assertEqual(store.get_session("s1")["status"], "STOPPED")
        self.assertEqual(store.get_session("s1")["stopped_ns"], first_stop)

    def test_journal_comes_before_the_store_for_every_command(self):
        for name in ("pause", "stop_all", "stop"):
            with self.subTest(command=name):
                sid = f"j-{name}"
                sd = self.make_day(sid)
                store.add_command(sid, name, None, 1)
                target = "stop" if name == "stop" else "pause"
                real = getattr(lifecycle, target)
                seen = []

                def spy(*a, real=real, sid=sid, **k):
                    seen.append(len(self.controls(sid)))
                    return real(*a, **k)

                with mock.patch.object(lifecycle, target, side_effect=spy):
                    disp = self.dispatcher([sd])
                    disp.put_gap("start", "disconnect", self.t("09:20:00"), None)
                    disp.drain()
                self.assertEqual(seen, [1], "the journal record must exist before the store write")

    def test_a_crash_between_journal_and_store_is_repaired_by_reapplying(self):
        sd = self.make_day("s1")
        cmd = store.add_command("s1", "pause", None, 1)
        disp = self.dispatcher([sd])
        disp.put_gap("start", "disconnect", self.t("09:20:00"), None)
        with mock.patch.object(lifecycle, "pause", side_effect=Crash()):
            with self.assertRaises(Crash):
                disp.drain()
        self.assertEqual(self.controls("s1"), [{"enabled": False, "reasons": ["owner"]}])   # journaled
        self.assertEqual(store.get_session("s1")["pause_reasons"], [])                       # not stored
        self.assertEqual([c["cmd_id"] for c in store.pending_commands()], [cmd])             # still pending
        restarted = self.dispatcher([sd])                                                    # the restart
        restarted.put_gap("end", "disconnect", self.t("09:20:05"), None)
        restarted.drain()
        self.assertEqual(store.get_session("s1")["pause_reasons"], ["owner"])
        self.assertEqual(store.pending_commands(), [])
        self.assertEqual(self.controls("s1")[-1], {"enabled": False, "reasons": ["owner"]})

    def test_frames_without_records_for_a_session_make_no_store_commit(self):
        narrow = self.make_day("s1", self.cfg(universe=["NSE:NIFTY 50"]))
        self.assertNotIn(self.ces[4], narrow.ids)
        disp = self.dispatcher([narrow], dispatch={"batch_max_items": 1, "batch_max_ms": 50})
        frame = kp.build_frame([pkt.full(pf.CE_TOKEN + 4, 12_000, vol=1_000, exch_ts=1)])
        with mock.patch.object(store, "apply_session_outputs", wraps=store.apply_session_outputs) as spy:
            for i in range(1, 51):
                disp.put_frame((i, self.t("09:16:00") + i, 1, frame, True))
            disp.drain()
        self.assertLessEqual(spy.call_count, 2)           # the CLOCK, then the deferred frame mark at the end
        self.assertEqual(store.get_session_day("s1", pf.EXPIRY)["last_frame_no"], 50)

    def _buy(self):
        act = [{"on_event": 60, "place": {"instrument": self.ces[4], "side": "BUY", "qty": 65, "product": "NRML",
                                          "type": "MARKET"}}]
        return self.session_cfg(strategy={"kind": "script", "actions": act})

    def test_batching_gives_the_same_journal_and_rows_as_item_by_item(self):
        one, many = self.make_day("s1", self._buy()), self.make_day("s2", self._buy())
        feeds = {}
        for sd, batch in ((one, 1), (many, 500)):
            counter = []
            real = sd.feed
            sd.feed = lambda records, last, real=real, counter=counter: (counter.append(1), real(records, last))[1]
            feeds[sd.session_id] = counter
            disp = self.dispatcher([sd], dispatch={"batch_max_items": batch, "batch_max_ms": 1000})
            self.play(disp)
            disp.drain()
        self.assertEqual(self.journal_bytes("s1"), self.journal_bytes("s2"))
        for table in ("orders", "order_events", "fills", "charges"):
            rows = [[{k: v for k, v in r.items() if k != "session_id"} for r in store.session_rows(sid, table)]
                    for sid in ("s1", "s2")]
            self.assertEqual(rows[0], rows[1], table)
        self.assertEqual(len(store.session_rows("s1", "fills")), 1)
        self.assertLess(len(feeds["s2"]) * 5, len(feeds["s1"]))

    # ── IWB 5.1: the margin poller's snapshots ───────────────────────────────
    def _poller(self, disp):
        import queue as q
        from stockify.daemon import margin_poller as mp
        poller = mp.MarginPoller(q.Queue(maxsize=64), disp, client_factory=lambda: None, clock=disp.clock,
                                 cfg=self.paper)
        disp.margin_poller = poller
        return poller

    def test_a_fill_pokes_the_poller_and_a_snapshot_of_the_book_follows(self):
        sd = self.make_day("s1", self._buy())
        disp = self.dispatcher([sd], replay=False)
        poller = self._poller(disp)
        self.play(disp)
        disp.drain()
        self.assertEqual(len(store.session_rows("s1", "fills")), 1)
        self.assertTrue(poller.pending("s1"))                     # the fill's poke, not yet served
        snaps = []
        while not poller.snapshots.empty():
            snaps.append(poller.snapshots.get_nowait())
        pokes = [x for x in snaps if x.reason == "poke"]
        self.assertEqual(len(pokes), 1)                            # one fill, one snapshot (not one per batch)
        [(iid, units)] = pokes[0].legs
        self.assertEqual((iid, units), (self.ces[4], 65))
        self.assertEqual(pokes[0].session_id, "s1")
        self.assertEqual(pokes[0].portfolio_sha256, sd.engine.margin_now()["portfolio_sha256"])
        self.assertTrue(pokes[0].fallback)

    def test_periodic_snapshots_come_every_every_s_of_stamp_time_and_only_with_legs(self):
        sd = self.make_day("s1", self._buy())
        disp = self.dispatcher([sd], replay=False)
        poller = self._poller(disp)
        self.play(disp)
        disp.drain()
        snaps = []
        while not poller.snapshots.empty():
            snaps.append(poller.snapshots.get_nowait())
        periodic = [x for x in snaps if x.reason == "periodic"]
        self.assertTrue(all(x.legs for x in periodic))             # flat books put nothing
        times = [x.engine_ns for x in periodic]
        self.assertTrue(all(b - a >= poller.every_ns for a, b in zip(times, times[1:])), times)

    def test_a_replay_dispatcher_takes_no_snapshots_and_a_flat_session_pokes_nothing(self):
        sd = self.make_day("s1", self._buy())
        disp = self.dispatcher([sd], replay=True)
        poller = self._poller(disp)
        self.play(disp)
        disp.drain()
        self.assertTrue(poller.snapshots.empty())
        self.assertFalse(poller.pending("s1"))

    def test_a_session_with_another_margin_model_is_never_polled(self):
        flat = self.session_cfg(strategy={"kind": "script", "actions": self._buy()["strategy"]["actions"]},
                                margin={"model": "flat"})
        sd = self.make_day("s1", flat)
        disp = self.dispatcher([sd], replay=False)
        poller = self._poller(disp)
        self.play(disp)
        disp.drain()
        self.assertTrue(poller.snapshots.empty())
        self.assertFalse(poller.pending("s1"))
        self.assertEqual(len(store.session_rows("s1", "fills")), 1)    # the day itself was unaffected

    def test_throughput_100k_frames_for_3_sessions(self):
        days = [self.make_day(f"t{i}") for i in range(3)]
        disp = self.dispatcher(days)
        frame, base = throughput_frame(), self.t("09:15:00")
        t0 = time.perf_counter()
        for i in range(100_000):
            disp.put_frame((i + 1, base + i * 10_000_000, 1, frame, True))
            if i % 20_000 == 19_999:
                disp.drain()
        disp.drain()
        took = time.perf_counter() - t0
        rate = 100_000 / took
        print(f"\nthroughput: 100000 frames x 3 sessions in {took:.1f} s = {rate:,.0f} frames/s", flush=True)
        self.assertEqual(disp.stats.frames, 100_000)
        self.assertLess(took, 150.0, f"{rate:,.0f} frames/s")
        for sd in days:
            self.assertEqual(store.get_session_day(sd.session_id, pf.EXPIRY)["last_frame_no"], 100_000)

    def test_a_dropped_gap_end_does_not_leave_the_gap_open(self):
        sd = self.make_day("s1")
        disp = self.dispatcher([sd], dispatcher_queue_max=2)
        t = self.t("09:20:00")
        disp.put_gap("start", "disconnect", t, None)
        disp.drain()
        disp.put_frame((0, t + 2 * SEC, 1, b"\x00", True))
        disp.put_frame((0, t + 3 * SEC, 1, b"\x00", True))
        disp.put_gap("end", "disconnect", t + 4 * SEC, None)         # dropped
        disp.put_frame((0, t + 5 * SEC, 1, b"\x00", True))           # dropped
        disp.drain()
        gaps = [(r["phase"], r["reason"]) for r in decode("s1") if r["kind"] == "GAP"]
        self.assertEqual(gaps, [(0, "disconnect"), (0, "stall"), (1, "stall"), (1, "disconnect")])

    def test_a_full_queue_at_command_enqueue_is_a_delay_not_a_loss(self):
        sd = self.make_day("s1")
        store.add_command("s1", "pause", None, 1)
        store.add_command("s1", "resume", None, 2)
        disp = self.dispatcher([sd], dispatcher_queue_max=2)
        t = self.t("09:20:00")
        disp.put_frame((0, t, 1, b"\x00", True))
        disp.put_frame((0, t + 1, 1, b"\x00", True))
        disp._process(disp.queue.get_nowait())          # polls with one free slot: one command fits, one waits
        self.assertEqual((disp.stats.dropped, disp.stats.alarms, disp.stats.commands_delayed), (0, [], 1))
        disp.drain()
        self.assertEqual([r for r in decode("s1") if r["kind"] == "GAP"], [])
        self.assertEqual(store.pending_commands(), [])          # offered again at the next chance, both applied
        self.assertEqual(self.controls("s1"), [{"enabled": False, "reasons": ["owner"]},
                                               {"enabled": True, "reasons": []}])

    def test_items_inside_a_stall_are_counted_when_the_end_clamps_them(self):
        sd = self.make_day("s1")
        disp = self.dispatcher([sd], dispatcher_queue_max=2)
        t = self.t("09:20:00")

        def beat(s):
            return (0, s, 1, b"\x00", True)

        disp.put_frame(beat(t + 1 * SEC))
        disp.put_frame(beat(t + 2 * SEC))
        disp.put_frame(beat(t + 3 * SEC))               # dropped: the loss starts here
        disp._process(disp.queue.get_nowait())
        disp.put_frame(beat(t + 4 * SEC))               # accepted, stamp inside the loss
        disp.put_frame(beat(t + 5 * SEC))               # dropped: the loss ends here
        disp.drain()
        self.assertEqual(disp.stats.clamped, 1)

    def test_a_failed_session_leaves_a_timeline_row(self):
        bad, good = self.make_day("s1"), self.make_day("s2")
        disp = self.dispatcher([bad, good])
        bad.engine = None
        self.play(disp)
        disp.drain()
        rows = [r for r in store.timeline("s1") if r["kind"] == "session_failed"]
        self.assertEqual(len(rows), 1)
        self.assertIn("not open", rows[0]["detail"]["error"])
        self.assertEqual([r for r in store.timeline("s2") if r["kind"] == "session_failed"], [])

    def test_dispatch_bounds_are_checked_by_the_loader(self):
        tmp = Path(tempfile.mkdtemp(dir=str(support.SANDBOX_ROOT)))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        good = json.loads((loader.CONFIG_DIR / "paper.json").read_text(encoding="utf-8"))
        self.assertEqual(good["dispatch"], {"batch_max_items": 500, "batch_max_ms": 50})
        (tmp / "paper.json").write_text(json.dumps({**good, "dispatch": {"batch_max_items": 100_000,
                                                                          "batch_max_ms": 1000}}), encoding="utf-8")
        loader.load_json("paper.json", tmp)
        for bad in ({"batch_max_items": 100_001, "batch_max_ms": 50}, {"batch_max_items": 500, "batch_max_ms": 1001},
                    {"batch_max_items": 0, "batch_max_ms": 50}, {"batch_max_items": 500}):
            with self.subTest(bad=bad):
                (tmp / "paper.json").write_text(json.dumps({**good, "dispatch": bad}), encoding="utf-8")
                with self.assertRaises(loader.ConfigError):
                    loader.load_json("paper.json", tmp)


if __name__ == "__main__":
    unittest.main()
