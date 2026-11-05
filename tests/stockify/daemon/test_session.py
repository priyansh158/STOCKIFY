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

    def run_session(self, *, faults=(), **source_kw):
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
        session = RecorderSession(ticker, rec, manager, clock)
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


class GapTests(SessionTest):
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
