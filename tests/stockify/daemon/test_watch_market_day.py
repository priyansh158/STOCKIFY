"""CW 3: the watchlist in the market day. A live run subscribes the watched stocks in QUOTE mode (`watch:SYMBOL`); a
replay subscribes exactly what it did before; a corrupt watchlist.sqlite never costs the recording."""
from __future__ import annotations

import threading
from datetime import date
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
import kite_client
from tests.stockify.daemon import packets_fixture as pk
from tests.stockify.daemon import paper_fixture as pf
from tests.stockify.daemon.test_engine_log import enable_logging
from tests.stockify.daemon.test_market_day import SPEC

from stockify import paths, timeutil
from stockify.config import loader
from stockify.daemon import exitcodes, kite_packets as kp, market_day
from stockify.daemon.clock import ManualClock
from stockify.daemon.landing import LandingDB
from stockify.data import instruments
from stockify.watchlist import store as wstore

DAY = pf.DAY
REL_TOKEN = 738561


def install_reliance() -> int:
    """NSE's dump again for the fixture's day, with RELIANCE beside the index."""
    instruments.sync(date(2026, 11, 16), {"NSE": (
        f"{pf.HEADER}\n{pk.NIFTY_INDEX},1,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n"
        f"{REL_TOKEN},2,RELIANCE,RELIANCE INDUSTRIES,0,,0,0.1,1,EQ,NSE,NSE\n")},
        underlyings={"NIFTY": "NSE:NIFTY 50"})
    return instruments.find("NSE", "RELIANCE")


class FrameTicker:
    """A live ticker that connects at once, delivers `frames`, then asks the run to stop."""

    def __init__(self, frames: list[bytes], stop: threading.Event):
        self.frames, self.stop = frames, stop
        self.subscribed: list[int] = []
        self.on_connect = self.on_message = self.on_close = self.on_error = self.on_noreconnect = None

    def connect(self, threaded: bool = False) -> None:
        self.on_connect(self, {})
        for f in self.frames:
            self.on_message(self, f, True)
        self.stop.set()

    def close(self, *args) -> None:
        pass

    def subscribe(self, tokens) -> None:
        self.subscribed.extend(tokens)

    def unsubscribe(self, tokens) -> None:
        pass

    def set_mode(self, mode, tokens) -> None:
        pass


class WatchMarketDayCase(pf.PaperDayTest):
    db_modules = pf.PaperDayTest.db_modules + (wstore,)

    def setUp(self):
        super().setUp()
        enable_logging(self)
        self.rel = install_reliance()
        real = loader.load_json
        self.patch(loader, "load_json",
                   lambda name, *a, **kw: pf.UNIVERSE if name == "universe.json" else real(name, *a, **kw))

    def patch(self, target, name, value):
        p = mock.patch.object(target, name, value)
        p.start()
        self.addCleanup(p.stop)

    def run_live(self, frames: list[bytes]) -> tuple[int, FrameTicker]:
        from stockify import daemon, kite_readonly
        from stockify.daemon import ticker as ticker_mod
        stop = threading.Event()
        tk = FrameTicker(frames, stop)
        self.patch(ticker_mod, "make_live_ticker", lambda *a, **kw: tk)
        self.patch(ticker_mod, "live_call_in_ticker", lambda fn, *args: fn(*args))
        self.patch(kite_readonly, "client", lambda: None)
        self.patch(kite_client, "is_authenticated", lambda: True)
        self.patch(kite_client, "load_cached_token", lambda: {"access_token": "not-a-real-token"})
        self.patch(kite_client, "_api_key", lambda: "not-a-real-key")
        self.patch(daemon, "_ensure_refdata", lambda day: None)
        code = market_day.run_market_day(ticker_spec="live", session_ids=[], clock=ManualClock(pf.t("10:00:00")),
                                         stop=stop, token_fresh=lambda: True)
        return code, tk

    def landing(self, path=None) -> LandingDB:
        db = LandingDB(DAY, path or paths.ticks_landing_file(DAY), readonly=True)
        self.addCleanup(db.close)
        return db

    def rows(self, path=None) -> list[tuple[int, str, str]]:
        db = LandingDB(DAY, path or paths.ticks_landing_file(DAY), readonly=True)
        try:
            return sorted(tuple(r) for r in db.conn.execute(
                "SELECT instrument_id, mode, reason FROM subscription_log WHERE action = 'subscribe'"))
        finally:
            db.close()

    def reasons(self, path=None) -> dict[int, tuple[str, str]]:
        return {iid: (mode, reason) for iid, mode, reason in self.rows(path)}


class LiveMergeTests(WatchMarketDayCase):
    def frame(self) -> bytes:
        return kp.build_frame(
            [pk.quote(REL_TOKEN, 250_000, 1, 250_000, 10, 1, 1, 249_000, 251_000, 248_000, 249_500)])

    def test_a_live_run_subscribes_the_watched_stocks_in_quote_mode(self):
        wstore.add("RELIANCE")
        code, tk = self.run_live([self.frame()])
        self.assertEqual(code, exitcodes.OK)
        self.assertIn(REL_TOKEN, tk.subscribed)
        self.assertEqual(self.reasons()[self.rel], ("quote", "start:watch:RELIANCE"))
        self.assertEqual(self.landing().packet_count(), 1)

    def test_a_new_high_in_a_live_run_is_one_email_and_the_recorder_sees_no_tap_error(self):
        from stockify.data import history
        from tests.stockify.watchlist.test_ranges import flat
        from datetime import timedelta
        wstore.add("RELIANCE")
        rows = flat(DAY - timedelta(days=400), 400, high=250_000, low=240_000, close=249_500)
        with history._connect() as conn:
            conn.executemany("INSERT INTO bars VALUES (?, 'day', ?, ?, ?, ?, ?, 1, NULL, ?)",
                             [(self.rel, b["ts_ns"], b["open_paise"], b["high_paise"], b["low_paise"],
                               b["close_paise"], "2026-11-16") for b in rows])
        from stockify.daemon import notify
        from stockify.daemon.recorder import Recorder
        stats = []
        real_start = Recorder.start
        self.patch(Recorder, "start", lambda rec: (stats.append(rec.stats), real_start(rec))[1])
        with mock.patch.object(notify, "post", return_value=True) as post:
            code, _ = self.run_live([self.frame()])
        self.assertEqual(code, exitcodes.OK)
        [call] = post.call_args_list
        self.assertEqual(call.args[0], "watch_high_low")
        self.assertEqual(call.kwargs["key"], f"52w:{DAY.isoformat()}:1")
        self.assertIn("RELIANCE: a new 52-week high of ₹2,510", call.args[2])
        self.assertEqual(stats[0].tap_errors, 0)
        self.assertEqual([a["kind"] for a in wstore.alerts()], ["high52"])

    def test_a_corrupt_watchlist_db_still_records(self):
        support.reset_sqlite_singleton(wstore)
        paths.watchlist_db().parent.mkdir(parents=True, exist_ok=True)
        paths.watchlist_db().write_bytes(b"not a database at all " * 100)
        with self.assertLogs("stockify.watchlist", "ERROR"):
            code, tk = self.run_live([self.frame()])
        self.assertEqual(code, exitcodes.OK)
        self.assertNotIn(REL_TOKEN, tk.subscribed)
        self.assertNotIn(self.rel, self.reasons())
        self.assertEqual(self.landing().packet_count(), 1)                # the frame is recorded all the same


class ReplayUnchangedTests(WatchMarketDayCase):
    def setUp(self):
        super().setUp()
        self.record(seconds=30)

    def test_a_replay_never_adds_the_watched_stocks(self):
        with mock.patch("stockify.watchlist.feed.merged", side_effect=AssertionError("merged in a replay")):
            self.assertEqual(market_day.run_market_day(ticker_spec=SPEC, session_ids=[]), exitcodes.OK)
        before = self.rows(paths.ticks_replay_file(DAY))
        for f in paths.ticks_replay_file(DAY).parent.glob(paths.ticks_replay_file(DAY).name + "*"):
            f.unlink()
        wstore.add("RELIANCE")
        self.assertEqual(market_day.run_market_day(ticker_spec=SPEC, session_ids=[]), exitcodes.OK)
        after = self.rows(paths.ticks_replay_file(DAY))
        self.assertEqual({(i, m) for i, m, _ in after}, {(i, m) for i, m, _ in before})   # the same instruments, modes
        self.assertNotIn(self.rel, {i for i, _, _ in after})
        self.assertFalse(any("watch:" in reason for _, _, reason in after))
