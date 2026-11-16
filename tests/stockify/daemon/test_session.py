"""stockify.daemon.ticker.RecorderSession: one gap row per reason, re-centring from
live prices, health rows (A3 U-13 part 2), driven by the replay ticker."""
from __future__ import annotations

import json
import unittest
from datetime import date

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import paths, store, timeutil
from stockify.daemon import exitcodes, kite_packets as kp
from stockify.daemon.clock import ManualClock
from stockify.daemon.landing import LandingDB
from stockify.daemon.recorder import Recorder
from stockify.daemon.replay_ticker import ReplayClock, ReplayTicker, parse_fault
from stockify.daemon.subscriptions import SubscriptionManager
from stockify.daemon.ticker import RecorderSession
from stockify.data import instruments, refdata
from tests.stockify.daemon import packets_fixture as pf

DAY = date(2026, 11, 9)
EXPIRY = "2026-11-17"
SEC = 1_000_000_000
HEADER = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
          "tick_size,lot_size,instrument_type,segment,exchange")
CALL_24000, CALL_24050 = pf.NIFTY_CE, pf.NIFTY_CE + 1


def t(hhmmss: str) -> int:
    return timeutil.ist_ns(DAY, hhmmss)


def dumps() -> dict[str, str]:
    nse = [HEADER, f"{pf.NIFTY_INDEX},1,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE"]
    nfo = [HEADER]
    token = CALL_24000
    for k in (24_000, 24_050, 24_100, 24_150):
        nfo.append(f"{token},{token},NIFTY{k}CE,NIFTY,0,{EXPIRY},{k}.0,0.05,65,CE,NFO-OPT,NFO")
        token += 1
    return {"NSE": "\n".join(nse) + "\n", "NFO": "\n".join(nfo) + "\n"}


UNIVERSE = {"indices_full": ["NSE:NIFTY 50"], "futures_full": [], "equities_full": [],
            "stock_futures_full": [], "equities_quote": [],
            "chains": [{"underlying": "NIFTY", "expiry": "weekly:0", "n_full": 0, "n_quote": 0,
                        "recenter_threshold": 1, "hysteresis": 0}],
            "limits": {"max_per_connection": 3000, "warn_fraction": "0.8"}}


def record_source(landing: LandingDB, *, index_silent=None, index_jump_at=None) -> None:
    """09:15–09:25: the index every 2 s and the 24,000 and 24,050 calls every 2 s."""
    clock = ManualClock(t("09:15:00"))
    rec = Recorder(landing, lambda tok: None, clock)
    rec.begin()
    for second in range(0, 600, 2):
        now = t("09:15:00") + second * SEC
        clock.set(now)
        index_price = 2_405_000 if index_jump_at and now >= t(index_jump_at) else 2_400_500
        packets = [pf.full(CALL_24000, 15_000), pf.full(CALL_24050, 12_000)]
        if not (index_silent and t(index_silent[0]) <= now < t(index_silent[1])):
            packets.append(pf.index_full(pf.NIFTY_INDEX, index_price, 1, 1, 1, 1, 0, now // SEC))
        rec.on_message(None, kp.build_frame(packets), True)
        rec.process(rec.queue.get_nowait())
    rec.flush()


class SessionTest(unittest.TestCase):
    def setUp(self):
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        support.reset_sqlite_singleton(refdata)
        support.reset_sqlite_singleton(store)
        instruments.sync(DAY, dumps(), underlyings={"NIFTY": "NSE:NIFTY 50"})

    def tearDown(self):
        support.reset_sqlite_singleton(refdata)
        support.reset_sqlite_singleton(store)

    def run_session(self, *, faults=(), on_stall=None, **source_kw):
        source = LandingDB(DAY)
        record_source(source, **source_kw)
        source.close()
        clock = ReplayClock(t("09:14:59"))
        ticker = ReplayTicker(LandingDB(DAY, readonly=True), clock,
                              faults=[parse_fault(f, DAY) for f in faults])
        target = LandingDB(DAY, paths.ticks_replay_file(DAY))
        tokens = {r[0]: r[1] for r in refdata._connect().execute(
            "SELECT instrument_token, instrument_id FROM instrument_tokens")}
        rec = Recorder(target, tokens.get, clock)
        rec.begin()
        manager = SubscriptionManager(DAY, UNIVERSE, clock, log_rows=target.log_subscriptions)
        session = RecorderSession(ticker, rec, manager, clock, on_stall=on_stall)
        session.start()

        def on_message(ws, payload, is_binary):          # synchronous for determinism
            rec.on_message(ws, payload, is_binary)
            rec.process(rec.queue.get_nowait())
            session.tick()

        ticker.on_message = on_message
        ticker.connect(threaded=False)
        if not session.stopped.is_set():
            session.end_session()
        rec.flush()
        self.addCleanup(target.close)
        return session, target, manager

    def gaps(self, target):
        return [(g["reason"], g["scope"], g["start_ns"], g["end_ns"]) for g in target.gaps()]

    def subscribed_strikes(self, manager):
        return sorted(instruments.get(s.instrument_id)["strike_paise"] // 100
                      for s in manager.current.values()
                      if instruments.get(s.instrument_id)["kind"] == "CE")


class EndSessionThreadTests(SessionTest):
    def test_end_session_unsubscribes_on_the_ticker_thread_under_the_subscription_lock(self):
        """M-4: the manager is only ever changed in the ticker's thread, in the ticker's order."""
        source = LandingDB(DAY)
        record_source(source)
        source.close()
        clock = ReplayClock(t("09:14:59"))
        ticker = ReplayTicker(LandingDB(DAY, readonly=True), clock, faults=[])
        target = LandingDB(DAY, paths.ticks_replay_file(DAY))
        self.addCleanup(target.close)
        rec = Recorder(target, lambda tok: None, clock)
        manager = SubscriptionManager(DAY, UNIVERSE, clock, log_rows=target.log_subscriptions)
        jobs = []
        session = RecorderSession(ticker, rec, manager, clock, call_in_ticker=lambda fn, *a: jobs.append((fn, a)))
        session.start()
        manager.build({})
        before = dict(manager.current)
        session.end_session()
        self.assertTrue(session.stopped.is_set())
        self.assertEqual(manager.current, before)                      # nothing touched on the calling thread
        self.assertEqual(len(jobs), 1)
        fn, args = jobs[0]
        fn(*args)                                                       # the ticker thread runs it
        self.assertEqual(manager.current, {})


class StallRaceTests(SessionTest):
    def test_a_stop_from_the_ticker_thread_during_the_stall_check_is_harmless(self):
        """LCB 5.2: a 403 stops the session on the ticker thread (`stop()` clears the stall gaps) while the main
        thread's health tick is between reading a stall gap and closing it; that used to raise KeyError from `tick()`
        and take the daemon down before its shutdown ran."""
        import types
        session, target, _ = self.run_session()

        class Racy(dict):
            def get(self, key, default=None):
                value = super().get(key, default)
                self.clear()                                # what `stop()` does on the other thread
                return value

        session.clock = ManualClock(t("09:20:00"))
        session.connected = True
        session.manager = types.SimpleNamespace(
            current={1: types.SimpleNamespace(mode="full", reason="index:NIFTY", instrument_id=7)})
        session._last_packet = {7: t("09:19:59")}
        session._stall_gaps = Racy({7: 1})
        taps = []
        session.gap_taps.append(lambda *a: taps.append(a))
        session._check_stalls(t("09:20:00"))                # must not raise
        self.assertEqual(taps, [])                          # the stop already ended it: no second end


class AuthFailureTests(SessionTest):
    """LCB 5.24 (I-3): only a real authentication failure is a token expiry (exit 76, sessions paused)."""

    def fake_session(self):
        from unittest import mock
        clock = mock.Mock()
        clock.now_ns.return_value = 1_000
        manager = mock.MagicMock()
        manager.reference_ids.return_value = set()
        return RecorderSession(mock.MagicMock(), mock.MagicMock(), manager, clock)

    def test_a_transport_error_that_mentions_a_token_does_not_exit_76(self):
        for code, reason in ((1006, "connection lost to wss://example.invalid/?access_token=dummy"),
                             (0, "token bucket exhausted"), (1011, "Invalid token length in frame")):
            with self.subTest(reason=reason):
                session = self.fake_session()
                session._on_error(None, code, reason)
                self.assertEqual(session.exit_code, exitcodes.OK)
                self.assertFalse(session.stopped.is_set())

    def test_kites_403_and_handshake_rejection_exit_76(self):
        for code, reason in ((403, "anything"), ("403", "x"), (0, "Handshake status 403 Forbidden")):
            with self.subTest(code=code, reason=reason):
                session = self.fake_session()
                session._on_error(None, code, reason)
                self.assertEqual(session.exit_code, exitcodes.TOKEN_EXPIRED)

    def test_the_same_with_a_typed_exception_when_kiteconnect_is_present(self):
        class TokenException(Exception):
            pass

        TokenException.__module__ = "kiteconnect.exceptions"             # the SDK itself is absent in tests
        session = self.fake_session()
        session._on_error(None, 0, TokenException("Incorrect `api_key` or `access_token`."))
        self.assertEqual(session.exit_code, exitcodes.TOKEN_EXPIRED)

    def test_a_403_on_the_margin_poll_takes_the_same_path(self):
        """IWB 5.1: the poller's `token_rejected` is the ticker's own 403 path (gap, exit 76), safe from any thread."""
        session = self.fake_session()
        session.token_rejected("Kite rejected the token on a margin request")
        self.assertEqual(session.exit_code, exitcodes.TOKEN_EXPIRED)
        self.assertTrue(session.stopped.is_set())
        session.token_rejected("again")                                  # a second 403 changes nothing
        self.assertEqual(session.exit_code, exitcodes.TOKEN_EXPIRED)

    def test_two_403s_make_one_gap_and_one_stop(self):
        """IWB 5.2: the ticker's 403 and the poller's, in either order or at the same instant, are one expiry."""
        for first, second in (("on_error", "rejected"), ("rejected", "on_error"), ("on_error", "on_error"),
                              ("rejected", "rejected")):
            session = self.fake_session()
            taps = []
            session.gap_taps.append(lambda *a, taps=taps: taps.append(a))
            for how in (first, second):
                if how == "on_error":
                    session._on_error(None, 403, "Handshake status 403")
                else:
                    session.token_rejected("margin 403")
            starts = [c for c in session.recorder.landing.open_gap.call_args_list if c.args[1] == "token_expired"]
            self.assertEqual(len(starts), 1, (first, second))
            self.assertEqual(len([t for t in taps if t[:2] == ("start", "token_expired")]), 1)
            self.assertEqual(session.exit_code, exitcodes.TOKEN_EXPIRED)

    def test_two_threads_with_a_403_each_make_one_gap_and_one_stop(self):
        import threading
        for _ in range(60):
            session = self.fake_session()
            taps = []
            session.gap_taps.append(lambda *a, taps=taps: taps.append(a))
            gate = threading.Barrier(2)

            def ticker():
                gate.wait()
                session._on_error(None, 403, "Handshake status 403")

            def poller():
                gate.wait()
                session.token_rejected("margin 403")

            threads = [threading.Thread(target=ticker), threading.Thread(target=poller)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            starts = [c for c in session.recorder.landing.open_gap.call_args_list if c.args[1] == "token_expired"]
            self.assertEqual(len(starts), 1)
            self.assertEqual(len([t for t in taps if t[:2] == ("start", "token_expired")]), 1)
            self.assertEqual(session.exit_code, exitcodes.TOKEN_EXPIRED)

    def test_a_lookalike_exception_from_elsewhere_is_not_an_expiry(self):
        class TokenException(Exception):
            pass

        session = self.fake_session()
        session._on_error(None, 0, TokenException("token"))
        self.assertEqual(session.exit_code, exitcodes.OK)


class StoppedStallTests(SessionTest):
    def test_no_stall_gap_is_opened_after_the_session_stopped(self):
        import types
        session, target, _ = self.run_session()
        session.clock = ManualClock(t("09:20:00"))
        session.connected = True
        session.manager = types.SimpleNamespace(
            current={1: types.SimpleNamespace(mode="full", reason="index:NIFTY", instrument_id=7)})
        session._last_packet = {7: t("09:10:00")}
        session.stopped.set()
        before = len(target.gaps())
        session._check_stalls(t("09:20:00"))
        self.assertEqual(len(target.gaps()), before)


    def test_clean_day_has_no_gaps(self):
        session, target, _ = self.run_session()
        self.assertEqual(self.gaps(target), [])
        self.assertEqual(session.exit_code, exitcodes.OK)

    def test_disconnect_gap_spans_the_outage(self):
        _, target, _ = self.run_session(faults=["disconnect@09:18:00+40s"])
        self.assertEqual(self.gaps(target), [("disconnect", "all", t("09:18:00"), t("09:18:40"))])

    def test_token_expiry_gap_and_exit_code(self):
        session, target, _ = self.run_session(faults=["token_expiry@09:20:00"])
        self.assertEqual([g[0] for g in self.gaps(target)], ["token_expired"])
        self.assertEqual(session.exit_code, exitcodes.TOKEN_EXPIRED)

    def test_stall_gap_is_scoped_to_the_silent_index(self):
        _, target, _ = self.run_session(index_silent=("09:20:00", "09:21:00"))
        index_id = instruments.find("NSE", "NIFTY 50")
        rows = self.gaps(target)
        self.assertEqual(len(rows), 1)
        reason, scope, start, end = rows[0]
        self.assertEqual((reason, scope), ("stall", f"instrument:{index_id}"))
        self.assertEqual(start, t("09:19:58"))              # last index packet before the silence
        self.assertEqual(end, t("09:21:00"))                # first one after it


class StallNoticeTests(SessionTest):
    """RM 7.1: the transition into a stall is told once (`on_stall`); a stall that goes on, or ends, tells nothing."""

    def test_a_stall_is_told_once_with_the_last_packet_time(self):
        told = []
        self.run_session(index_silent=("09:20:00", "09:21:00"), on_stall=lambda iid, last: told.append((iid, last)))
        self.assertEqual(told, [(instruments.find("NSE", "NIFTY 50"), t("09:19:58"))])     # 60 s silent, many checks

    def test_two_separate_stalls_are_told_separately(self):
        told = []
        session, _, _ = self.run_session(on_stall=lambda iid, last: told.append((iid, last)))
        self.assertEqual(told, [])                                   # no silence, no notice
        import types
        session.stopped.clear()
        session.connected = True
        session.manager = types.SimpleNamespace(
            current={1: types.SimpleNamespace(mode="full", reason="index:NIFTY", instrument_id=7)})
        session._stall_gaps.clear()
        session._last_packet = {7: t("09:20:00")}
        session._check_stalls(t("09:21:00"))
        session._check_stalls(t("09:21:05"))                         # still the same stall
        session._last_packet = {7: t("09:22:00")}
        session._check_stalls(t("09:22:01"))                         # resumed: the gap closes, nothing is told
        session._last_packet = {7: t("09:22:01")}
        session._check_stalls(t("09:23:00"))                         # a second stall
        self.assertEqual(told, [(7, t("09:20:00")), (7, t("09:22:01"))])

    def test_a_failing_notice_never_reaches_the_health_loop(self):
        def boom(iid, last):
            raise RuntimeError("mail down")

        from tests.stockify.daemon.test_engine_log import enable_logging
        enable_logging(self)
        with self.assertLogs("stockify.daemon.ticker", "ERROR"):
            _, target, _ = self.run_session(index_silent=("09:20:00", "09:21:00"), on_stall=boom)
        self.assertEqual([g["reason"] for g in target.gaps()], ["stall"])        # the gap is recorded all the same

    def test_with_no_hook_a_stall_only_makes_its_gap(self):
        _, target, _ = self.run_session(index_silent=("09:20:00", "09:21:00"))
        self.assertEqual([g["reason"] for g in target.gaps()], ["stall"])


class RecentreAndHealthTests(SessionTest):
    def test_chain_centres_on_the_first_index_price_then_follows_it(self):
        _, target, manager = self.run_session(index_jump_at="09:20:00")
        logged = target.conn.execute(
            "SELECT action, instrument_token FROM subscription_log WHERE instrument_token >= ? ORDER BY rowid",
            (CALL_24000,)).fetchall()
        self.assertEqual([tuple(r) for r in logged][:1], [("subscribe", CALL_24000)])     # 24,005 -> 24,000
        self.assertIn(("subscribe", CALL_24000 + 1), [tuple(r) for r in logged])          # 24,050 after the jump
        self.assertIn(("unsubscribe", CALL_24000), [tuple(r) for r in logged])

    def test_health_rows_every_5_seconds(self):
        self.run_session()
        rows = store._connect().execute("SELECT * FROM recorder_status ORDER BY wall_ns").fetchall()
        # Frames arrive every 2 s, so a >= 5 s cadence writes a row every 6 s: 600 s -> 100 rows.
        self.assertEqual(len(rows), 100)
        spacing = {b["wall_ns"] - a["wall_ns"] for a, b in zip(rows, rows[1:])}
        self.assertTrue(all(s >= 5 * SEC for s in spacing))
        last = dict(rows[-1])
        # The index in FULL; the chain has n_full 0, so its ATM call is QUOTE (RM 1.6).
        self.assertEqual((last["subs_full"], last["subs_quote"]), (1, 1))
        ages = json.loads(last["last_packet_age_ms_json"])
        self.assertEqual(set(ages), {"full", "quote"})
        self.assertGreater(last["bytes_today"], 0)


if __name__ == "__main__":
    unittest.main()


class SubscriptionCountLogTests(SessionTest):
    """RM 7.5: the log states the full/quote totals once chains are centred, once per change (not per tick)."""

    def make(self, counts):
        from unittest import mock
        clock = mock.Mock()
        clock.now_ns.return_value = 1_000
        manager = mock.MagicMock()
        manager.reference_ids.return_value = set()
        manager.current = {}
        manager.chains = []
        manager.update_prices.return_value = mock.MagicMock(empty=lambda: True)
        manager.counts.side_effect = lambda: dict(counts[0])
        return RecorderSession(mock.MagicMock(), mock.MagicMock(), manager, clock)

    def test_the_total_is_logged_when_it_changes_and_not_otherwise(self):
        from tests.stockify.daemon.test_engine_log import enable_logging
        enable_logging(self)
        counts = [{"full": 5, "quote": 3}]
        session = self.make(counts)
        with self.assertLogs("stockify.daemon.ticker", level="INFO") as ctx:
            session._on_connect(None, None)
            session._reconcile({1: 100})                          # unchanged since connect: silent
            counts[0] = {"full": 105, "quote": 71}
            session._reconcile({1: 101})
            session._reconcile({1: 102})                          # same totals again: silent
            counts[0] = {"full": 104, "quote": 71}
            session._reconcile({1: 103})
        lines = [r for r in ctx.output if "subscriptions" in r]
        self.assertEqual(len(lines), 3, lines)
        self.assertIn("105 full, 71 quote", lines[1])
        self.assertIn("104 full, 71 quote", lines[2])
