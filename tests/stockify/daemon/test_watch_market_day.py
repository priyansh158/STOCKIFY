"""CW 3: the watchlist in the market day. A live run subscribes the watched stocks in QUOTE mode (`watch:SYMBOL`); a
replay never adds them and never emails; a corrupt, slow or hanging watchlist never costs or delays the recording."""
from __future__ import annotations

import threading
import time
from datetime import date, timedelta
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


def put_bars(iid: int) -> None:
    """A year and more of daily candles ending the day before DAY: high 2,500, low 2,400, close 2,495 rupees."""
    from stockify.data import history
    from tests.stockify.watchlist.test_ranges import flat
    rows = flat(DAY - timedelta(days=400), 400, high=250_000, low=240_000, close=249_500)
    with history._connect() as conn:
        conn.executemany("INSERT INTO bars VALUES (?, 'day', ?, ?, ?, ?, ?, 1, NULL, ?)",
                         [(iid, b["ts_ns"], b["open_paise"], b["high_paise"], b["low_paise"], b["close_paise"],
                           "2026-11-16") for b in rows])


def rel_frame(high: int = 251_000) -> bytes:
    return kp.build_frame([pk.quote(REL_TOKEN, 250_000, 1, 250_000, 10, 1, 1, 249_000, high, 248_000, 249_500)])


class FrameTicker:
    """A live ticker that connects at once; on its own thread it waits for `before_frames()`, delivers `frames`,
    then asks the run to stop."""

    def __init__(self, frames: list[bytes], stop: threading.Event, before_frames=None):
        self.frames, self.stop, self.before_frames = frames, stop, before_frames
        self.subscribed: list[int] = []
        self.connected_at: float | None = None
        self.on_connect = self.on_message = self.on_close = self.on_error = self.on_noreconnect = None

    def connect(self, threaded: bool = False) -> None:
        self.connected_at = time.monotonic()
        self.on_connect(self, {})

        def feed():
            if self.before_frames is not None:
                self.before_frames()
            for f in self.frames:
                self.on_message(self, f, True)
            self.stop.set()

        threading.Thread(target=feed, daemon=True).start()

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

    def run_live(self, frames: list[bytes], before_frames=None) -> tuple[int, FrameTicker]:
        from stockify import daemon, kite_readonly
        from stockify.daemon import ticker as ticker_mod
        stop = threading.Event()
        tk = FrameTicker(frames, stop, before_frames)
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
        return rel_frame()

    def watch_ready(self):
        """Spy on the evaluator market_day builds; the returned `wait()` blocks until it has prepared on its thread."""
        built = []
        real = market_day._watch_alerts
        self.patch(market_day, "_watch_alerts", lambda *a, **kw: (built.append(real(*a, **kw)), built[-1])[1])

        def wait():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not (built and built[0] is not None and built[0].ready):
                time.sleep(0.01)
        return wait

    def test_a_live_run_subscribes_the_watched_stocks_in_quote_mode(self):
        wstore.add("RELIANCE")
        code, tk = self.run_live([self.frame()])
        self.assertEqual(code, exitcodes.OK)
        self.assertIn(REL_TOKEN, tk.subscribed)
        self.assertEqual(self.reasons()[self.rel], ("quote", "start:watch:RELIANCE"))
        self.assertEqual(self.landing().packet_count(), 1)

    def test_a_new_high_in_a_live_run_is_one_email_and_the_recorder_sees_no_tap_error(self):
        wstore.add("RELIANCE")
        put_bars(self.rel)
        ready = self.watch_ready()
        from stockify.daemon import notify
        from stockify.daemon.recorder import Recorder
        stats = []
        real_start = Recorder.start
        self.patch(Recorder, "start", lambda rec: (stats.append(rec.stats), real_start(rec))[1])
        with mock.patch.object(notify, "post", return_value=True) as post:
            code, _ = self.run_live([self.frame()], before_frames=ready)
        self.assertEqual(code, exitcodes.OK)
        [call] = post.call_args_list
        self.assertEqual(call.args[0], "watch_high_low")
        self.assertTrue(call.kwargs["key"].startswith(f"52w:{DAY.isoformat()}:1:g"))
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

    def test_a_hanging_watchlist_read_cannot_delay_the_subscribe_or_the_connect(self):
        from stockify.watchlist import feed
        release = threading.Event()
        self.addCleanup(release.set)
        wstore.add("RELIANCE")
        self.patch(feed, "BUDGET_S", 0.2)
        self.patch(feed, "_read_symbols", lambda cap: (release.wait(30), ["RELIANCE"])[1])
        started = time.monotonic()
        with self.assertLogs("stockify.watchlist.feed", "ERROR") as cm:
            code, tk = self.run_live([self.frame()])
        self.assertEqual(code, exitcodes.OK)
        self.assertLess(tk.connected_at - started, 5)
        self.assertIn("did not answer", "\n".join(cm.output))
        self.assertNotIn(REL_TOKEN, tk.subscribed)
        self.assertEqual(self.landing().packet_count(), 1)

    def test_a_hanging_or_failing_evaluator_build_cannot_delay_the_recording(self):
        from stockify.watchlist import live
        release = threading.Event()
        self.addCleanup(release.set)
        wstore.add("RELIANCE")
        self.patch(live, "JOIN_S", 0.2)
        self.patch(live.HighLowWatch, "prepare", lambda w: release.wait(30))
        started = time.monotonic()
        code, tk = self.run_live([self.frame()])
        self.assertEqual(code, exitcodes.OK)
        self.assertLess(tk.connected_at - started, 5)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(self.landing().packet_count(), 1)
        release.set()
        self.patch(live.HighLowWatch, "prepare", lambda w: 1 / 0)
        code, _ = self.run_live([self.frame()])
        self.assertEqual(code, exitcodes.OK)
        self.assertEqual(self.landing().packet_count(), 2)


def record_reliance(seconds: int = 20) -> None:
    """Land DAY from 09:15:00 with RELIANCE in quote mode, its day high rising past 2,500 rupees at 09:15:06."""
    from stockify.daemon.recorder import Recorder
    rel = instruments.find("NSE", "RELIANCE")
    landing = LandingDB(DAY)
    clock = ManualClock(pf.t("09:14:59"))
    rec = Recorder(landing, {REL_TOKEN: rel}.get, clock)
    rec.begin()
    landing.log_subscriptions([(pf.t("09:15:00"), "subscribe", rel, REL_TOKEN, "quote", "start")])
    for s in range(seconds):
        clock.set(pf.t("09:15:00") + s * pf.SEC)
        rec.on_message(None, rel_frame(high=249_000 + 200 * s), True)
        rec.process(rec.queue.get_nowait())
    rec.flush()
    landing.close()


class ReplayNeverEmailsTests(WatchMarketDayCase):
    def test_a_forced_cross_in_a_replay_is_recorded_and_never_emailed(self):
        real = loader.load_json
        with_rel = {**pf.UNIVERSE, "equities_quote": ["NSE:RELIANCE"]}     # recorded as part of the universe
        self.patch(loader, "load_json",
                   lambda name, *a, **kw: with_rel if name == "universe.json" else real(name, *a, **kw))
        record_reliance()
        put_bars(self.rel)
        wstore.add("RELIANCE")
        from stockify.daemon import notify
        with mock.patch.object(notify, "post") as post, mock.patch.object(notify, "send") as send, \
                self.assertLogs("stockify.watchlist.live", "INFO") as cm:
            self.assertEqual(market_day.run_market_day(ticker_spec=SPEC, session_ids=[]), exitcodes.OK)
        post.assert_not_called()
        send.assert_not_called()
        self.assertIn("NOTIFY watch_high_low (replay, not sent)", "\n".join(cm.output))
        [alert] = wstore.alerts()
        self.assertEqual((alert["symbol"], alert["kind"], alert["delivery"]), ("RELIANCE", "high52", "pending"))


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
