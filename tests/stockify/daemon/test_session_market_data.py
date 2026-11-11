"""RM 5.4 (M5.5): market data for sessions. A session pins what it needs (`session:<id>`), a re-centre never drops it,
the subscription cap is checked at ARM, price bands reach the journals in queue order, and a tick for an instrument
outside the bundle is logged once and labelled, never journaled."""
from __future__ import annotations

import itertools
import json
import unittest

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.daemon import packets_fixture as pkt, paper_fixture as pf, test_subscriptions as ts

from stockify import engine, paths, store
from stockify.config import loader
from stockify.daemon import journal, kite_packets as kp, lifecycle, market_day, session_day
from stockify.daemon.clock import ManualClock
from stockify.daemon.dispatcher import Dispatcher
from stockify.daemon.encoder import FrameEncoder
from stockify.daemon.landing import LandingDB
from stockify.daemon.replay_ticker import MODE_FULL, ReplayClock, ReplayTicker
from stockify.daemon.subscriptions import SubscriptionManager
from stockify.data import instruments, limits, packs

DAY = pf.DAY


class FakeSession:
    def __init__(self, sid: str, ids) -> None:  # noqa: ANN001
        self.session_id, self.ids = sid, frozenset(ids)


class PinTests(ts.SubscriptionTest):
    """The manager side, without an engine: pins survive a re-centre and follow the book."""

    def pinned_strike(self, m):
        m.build({self.index: 2_501_000})                       # chain 24,800 .. 25,200 (n_quote 4)
        edge = instruments.option_chain("NIFTY", ts.WEEKLY, ts.DAY)
        return next(r["instrument_id"] for r in edge if r["strike_paise"] == 2_480_000 and r["kind"] == "CE")

    def test_recentre_never_drops_a_pinned_strike(self):
        m = self.manager()
        iid = self.pinned_strike(m)
        pins = market_day.SessionPins(m, cap=3000)
        self.assertTrue(pins.admit(FakeSession("s1", {iid})))
        self.assertIn("session:s1", m.pins)
        m.update_prices({self.index: 2_600_000})               # far away: the 24,800 call would be dropped
        self.assertIn(iid, m.current)
        self.assertEqual(m.current[iid].mode, "full")
        self.assertEqual(m.current[iid].reason, "pin:session:s1")
        other = self.manager()
        other.build({self.index: 2_501_000})
        other.update_prices({self.index: 2_600_000})
        self.assertNotIn(iid, other.current)                   # the same re-centre without a pin does drop it

    def test_open_position_stays_pinned_until_flat(self):
        m = self.manager()
        base = self.pinned_strike(m)
        far = next(r["instrument_id"] for r in instruments.option_chain("NIFTY", ts.WEEKLY, ts.DAY)
                   if r["strike_paise"] == 2_480_000 and r["kind"] == "PE")
        pins = market_day.SessionPins(m, cap=3000)
        sd = FakeSession("s1", {base})
        self.assertTrue(pins.admit(sd))
        pins.book_changed(sd, frozenset({far}))                # an open position
        m.update_prices({self.index: 2_600_000})
        self.assertIn(far, m.current)
        pins.book_changed(sd, frozenset())                     # flat
        self.assertNotIn(far, m.pins["session:s1"])
        self.assertNotIn(far, m.current)
        self.assertIn(base, m.current)                         # the bundle's own instruments stay for the day

    def test_pin_changes_run_in_the_ticker(self):
        m = self.manager()
        iid = self.pinned_strike(m)
        far = next(r["instrument_id"] for r in instruments.option_chain("NIFTY", ts.WEEKLY, ts.DAY)
                   if r["strike_paise"] == 2_400_000 and r["kind"] == "CE")
        self.assertNotIn(far, m.current)
        calls, applied = [], []

        def call(fn, *args):
            calls.append(fn.__name__)
            fn(*args)

        pins = market_day.SessionPins(m, cap=3000, call_in_ticker=call, apply=applied.append)
        sd = FakeSession("s1", {iid})
        self.assertTrue(pins.admit(sd))
        self.assertEqual((calls, applied), ([], []))            # before the ticker is up: pinned, nothing applied
        pins.live = True
        pins.book_changed(sd, frozenset({far}))
        self.assertEqual(len(calls), 1)                         # through call_in_ticker, once
        self.assertEqual([s.instrument_id for d in applied for s in d.subscribe], [far])


class BookEngine:
    """The real engine with a scripted `book()`."""

    def __init__(self, real, positions) -> None:  # noqa: ANN001
        self._real, self.positions = real, list(positions)

    def book(self):
        return list(self.positions)

    def __getattr__(self, name):
        return getattr(self._real, name)


def decode(sid: str) -> list[dict]:
    _, body, _ = journal.read(paths.journal_file(sid, DAY))
    out, off = [], 0
    while off < len(body):
        rec, off = packs.decode(body, off)
        out.append(rec)
    return out


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class SessionDataTest(pf.PaperDayTest):
    def setUp(self):
        super().setUp()
        self.record(seconds=20)
        self.token_map = {pf.pf.NIFTY_INDEX: self.idx, pf.FUT_TOKEN: self.fut,
                          **{pf.CE_TOKEN + i: iid for i, iid in enumerate(self.ces)}}
        self.ticks = itertools.count(0, 10)
        self.cfg = self.session_cfg(strategy={"kind": "script", "actions": []})
        # the index alone: the recorder ticks every strike, none of them is this session's
        self.narrow_cfg = self.session_cfg(strategy={"kind": "script", "actions": []}, universe=["NSE:NIFTY 50"])

    def make_day(self, sid, cfg=None):
        store.create_session(sid, sid, {}, 1)
        sd = session_day.SessionDay(sid, DAY, cfg or self.cfg, mono=lambda: next(self.ticks))
        self.addCleanup(sd.close)
        sd.open(None, [], self.t("09:14:00"))
        return sd

    def dispatcher(self, sessions):
        cfg = {**loader.load_json("paper.json"), "dispatch": {"batch_max_items": 1, "batch_max_ms": 50}}
        return Dispatcher(sessions, FrameEncoder(self.token_map), clock=ManualClock(self.t("09:14:59")), cfg=cfg,
                          replay=True)

    def play(self, disp):
        clock = ReplayClock(self.t("09:14:59"))
        src = LandingDB(DAY, readonly=True)
        self.addCleanup(src.close)
        ticker = ReplayTicker(src, clock)
        tokens = list(self.token_map)
        ticker.subscribe(tokens)
        ticker.set_mode(MODE_FULL, tokens)
        n = itertools.count(1)

        def on_message(ws, payload, is_binary):
            disp.put_frame((next(n) if len(payload) > 1 else 0, clock.now_ns(), clock.mono_ns(), payload, True))

        ticker.on_message = on_message
        ticker.connect(threaded=False)

    def outside(self, sd):
        iid = self.ces[4]
        self.assertNotIn(iid, sd.ids)
        return iid

    def labels(self, sid):
        return json.loads(store.get_session_day(sid, pf.EXPIRY)["labels_json"] or "[]")

    def kinds(self, sid, kind):
        return [r for r in store.timeline(sid) if r["kind"] == kind]

    def test_price_band_reaches_journal_in_fifo_order(self):
        sd, narrow = self.make_day("s1"), self.make_day("s2", self.narrow_cfg)
        disp = self.dispatcher([sd, narrow])
        inside = outside = self.ces[5]     # the one strike the chain holds at 24,000
        self.assertIn(inside, sd.ids)
        self.outside(narrow)
        manager = SubscriptionManager(DAY, pf.UNIVERSE, ManualClock(self.t("09:15:00")))
        manager.build({self.idx: 2_400_000})
        quotes = {}
        for iid in (inside, outside):
            info = instruments.get(iid)
            quotes[f"{info['exchange']}:{info['tradingsymbol']}"] = {"upper_circuit_limit": 120.55,
                                                                     "lower_circuit_limit": 80.1}

        class Client:
            def quote(self, names):  # noqa: ANN001
                return {n: quotes[n] for n in names if n in quotes}

        frame = kp.build_frame([pkt.index_full(pkt.NIFTY_INDEX, 2_400_000, 2_401_000, 2_399_000, 2_400_000,
                                               2_390_000, 1000, 1)])
        t1, t2 = self.t("09:16:00"), self.t("09:16:02")
        clock = ManualClock(self.t("09:16:01"))
        snap = limits.Snapshotter(DAY, manager, client=Client(), clock=clock,
                                  on_band=market_day.band_sink(disp, [sd, narrow]))
        disp.put_frame((1, t1, 1, frame, True))
        snap.tick()                                            # stamped 09:16:01 by the clock
        disp.put_frame((2, t2, 2, frame, True))
        disp.drain()
        records = decode("s1")
        bands = [r for r in records if r["kind"] == "REFDATA" and r["subtype"] == "price_band"]
        self.assertEqual([b["instrument_id"] for b in bands], [inside])
        self.assertEqual([r for r in decode("s2") if r["kind"] == "REFDATA"], [])   # not for a bundle that lacks it
        self.assertEqual((bands[0]["payload"]["upper_paise"], bands[0]["payload"]["lower_paise"]), (12055, 8010))
        ticks = [r["t_event"] for r in records if r["kind"] == "INDEX"]
        self.assertEqual(len(ticks), 2)
        self.assertLess(ticks[0], bands[0]["t_event"])
        self.assertLess(bands[0]["t_event"], ticks[1])         # queue order is journal order

    def test_on_band_builds_the_refdata_record(self):
        sd = self.make_day("s1")
        disp = self.dispatcher([sd])
        iid = self.ces[4]
        market_day.band_sink(disp, [sd])(iid, 8010, 12055, self.t("09:16:00"))
        disp.drain()
        (band,) = [r for r in decode("s1") if r["kind"] == "REFDATA"]
        self.assertEqual((band["subtype"], band["instrument_id"]), ("price_band", iid))
        self.assertEqual((band["payload"]["lower_paise"], band["payload"]["upper_paise"]), (8010, 12055))

    def test_unbundled_instrument_logged_once_and_labelled(self):
        sd = self.make_day("s1", self.narrow_cfg)
        extra = self.outside(sd)
        sd.engine = BookEngine(sd.engine, [(extra, "MIS", 65)])
        disp = self.dispatcher([sd])
        calls = []
        disp.on_book = lambda session, positions: calls.append((session.session_id, positions))
        self.play(disp)
        disp.drain()
        rows = self.kinds("s1", "unbundled_instrument")
        self.assertEqual([r["detail"]["instrument_id"] for r in rows], [extra])           # once, though ticked 20 times
        self.assertEqual(self.labels("s1"), ["unbundled_instrument"])
        self.assertNotIn(extra, {r["instrument_id"] for r in decode("s1") if "ltp" in r})   # never journaled
        self.assertEqual(calls, [("s1", frozenset({extra}))])                             # the book is reported on change

    def test_book_goes_flat_is_reported(self):
        sd = self.make_day("s1", self.narrow_cfg)
        extra = self.outside(sd)
        book = BookEngine(sd.engine, [(extra, "MIS", 65)])
        sd.engine = book
        disp = self.dispatcher([sd])
        calls = []
        disp.on_book = lambda session, positions: calls.append(positions)
        self.play(disp)
        disp.drain()
        book.positions = []
        disp.put_frame((0, self.t("09:30:00"), 1, b"\x00", True))
        disp.drain()
        self.assertEqual(calls, [frozenset({extra}), frozenset()])

    def test_universe_over_cap_does_not_arm(self):
        store.create_session("s1", "s1", self.cfg, 1)
        lifecycle.arm("s1", wall_ns=2)
        manager = SubscriptionManager(DAY, pf.UNIVERSE, ManualClock(self.t("09:14:00")))
        told = []
        pins = market_day.SessionPins(manager, cap=3, notify=lambda event, detail: told.append((event, detail)))
        opened = market_day.open_sessions(market_day.choose_sessions(["s1"]), DAY, ManualClock(self.t("09:14:00")),
                                          pins=pins)
        self.assertEqual(opened, [])
        self.assertEqual(store.get_session("s1")["status"], "ARMED")
        (row,) = self.kinds("s1", "subscription_cap")
        self.assertEqual(row["detail"]["cap"], 3)
        self.assertGreater(row["detail"]["total"], 3)
        self.assertEqual([e for e, _ in told], ["subscription_cap"])
        self.assertEqual(manager.pins, {})                      # nothing pinned for a session that did not arm

    def test_universe_under_cap_arms_and_pins(self):
        store.create_session("s1", "s1", self.cfg, 1)
        lifecycle.arm("s1", wall_ns=2)
        manager = SubscriptionManager(DAY, pf.UNIVERSE, ManualClock(self.t("09:14:00")))
        pins = market_day.SessionPins(manager, cap=3000)
        opened = market_day.open_sessions(market_day.choose_sessions(["s1"]), DAY, ManualClock(self.t("09:14:00")),
                                          pins=pins)
        self.addCleanup(lambda: [sd.close() for sd in opened])
        self.assertEqual([sd.session_id for sd in opened], ["s1"])
        self.assertEqual(store.get_session("s1")["status"], "RUNNING")
        self.assertEqual(manager.pins["session:s1"], set(opened[0].ids))


if __name__ == "__main__":
    unittest.main()
