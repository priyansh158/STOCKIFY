"""M4.12 (CAM 4.1): replaying recorded chains.

A strike the recorder subscribes mid-day (a chain re-centre) gets its REFDATA `instrument_add` at the moment it
was subscribed, not at 00:00, because that is when the daemon learned it (A4 §1.1). Instruments of the session's
start batch keep 00:00. A strategy replaying the day sees the strike with no quote until its first tick, sees it
go stale after it is unsubscribed, and is never handed a quote that was not recorded (I-7), on both engines."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import bundle as bundle_mod, engine, paths, runconfig, timeutil
from stockify.daemon import kite_packets as kp
from stockify.daemon.clock import ManualClock
from stockify.daemon.landing import LandingDB
from stockify.daemon.recorder import Recorder
from stockify.data import instruments, packs
from stockify.oracle import run_bundle as oracle_run
from stockify.strategy import Strategy
from tests.stockify.daemon import packets_fixture as pf
from tests.stockify.data.test_packs import DAY, FUT_TOKEN, SEC, PackTest, t

STRIKE_TOKEN = 90005           # NIFTY26N1724000… the fixture's strikes are 90000 + i for 23500, 23600, …
STRIKE_SYMBOL = "NIFTY26N1724000CE"


class ChainReplayTest(PackTest):
    """The fixture day, plus one strike subscribed at 09:15:10 by a re-centre and dropped at 09:15:15."""

    def record(self):
        self.strike = instruments.find("NFO", "NIFTY26N1724000CE")
        self.mid = instruments.find("NFO", "NIFTY26N1724100CE")
        landing = LandingDB(DAY)
        clock = ManualClock(t("09:14:59"))
        mid_token = 90006
        tokens = {pf.NIFTY_CE: self.ce, pf.NIFTY_INDEX: self.idx, FUT_TOKEN: self.fut, mid_token: self.mid}
        rec = Recorder(landing, tokens.get, clock)
        rec.begin()
        landing.log_subscriptions([(t("09:15:00"), "subscribe", self.ce, pf.NIFTY_CE, "full", "start:chain"),
                                   (t("09:15:00"), "subscribe", self.idx, pf.NIFTY_INDEX, "full", "start:index"),
                                   (t("09:15:00"), "subscribe", self.fut, FUT_TOKEN, "quote", "start:future"),
                                   (t("09:15:10"), "subscribe", self.mid, mid_token, "full",
                                    "recenter:chain:NIFTY:weekly:0"),
                                   (t("09:15:15"), "unsubscribe", self.mid, mid_token, "full",
                                    "recenter:chain:NIFTY:weekly:0")])
        for s in range(20):
            clock.set(t("09:15:00") + s * SEC)
            frame = [pf.full(pf.NIFTY_CE, 12_000 + s, exch_ts=t("09:15:00") // SEC + s),
                     pf.index_full(pf.NIFTY_INDEX, 2_400_000 + s, 2_401_000, 2_399_000, 2_400_000, 2_390_000,
                                   1000 + s, t("09:15:00") // SEC + s),
                     pf.quote(FUT_TOKEN, 2_410_000, 65, 2_409_000, 100 + s, 10, 20, 1, 2, 3, 4)]
            if 10 <= s < 15:                                   # the mid-day strike ticks while subscribed
                frame.append(pf.full(mid_token, 9_000 + s, exch_ts=t("09:15:00") // SEC + s))
            rec.on_message(None, kp.build_frame(frame), True)
            rec.process(rec.queue.get_nowait())
        rec.flush()
        landing.close()

    def setUp(self):
        super().setUp()
        self.record()


class InstrumentAddTimeTests(ChainReplayTest):
    def test_a_mid_day_strike_is_added_when_it_was_subscribed(self):
        sha = packs.build(self.spec())
        _, recs = packs.read(packs.pack_path(sha))
        adds = {r["instrument_id"]: r["t_event"] for r in recs if r["kind"] == "REFDATA"
                and r["subtype"] == "instrument_add"}
        midnight = timeutil.ist_ns(DAY, "00:00")
        self.assertEqual(adds[self.ce], midnight)                  # start batch: known all day
        self.assertEqual(adds[self.idx], midnight)
        self.assertEqual(adds[self.mid], t("09:15:10"))            # re-centre: known from its subscription
        first_tick = min(r["t_event"] for r in recs if r["instrument_id"] == self.mid and r["kind"] == "TICK_FULL")
        self.assertLessEqual(adds[self.mid], first_tick)
        order = [(r["kind"], r["instrument_id"]) for r in recs]
        self.assertLess(order.index(("REFDATA", self.mid)), order.index(("TICK_FULL", self.mid)))

    def test_same_bytes_from_landing_and_archive(self):
        """P-05: the mid-day add time comes out the same whether the day is read from landing or the archive."""
        from stockify.data import compact
        a = packs.build(self.spec())
        self.assertEqual(packs.build(self.spec()), a)
        self.assertEqual(compact.compact(DAY).status, "verified")
        compact.apply_retention(DAY)
        self.assertFalse(paths.ticks_landing_file(DAY).exists())
        self.assertEqual(packs.build(self.spec()), a)
        self.assertEqual(packs.manifest(a)["sources"][0]["source"], "archive")


class Watcher(Strategy):
    """Logs, every second, what it can see of the mid-day strike: its quote, staleness and the chain."""

    def __init__(self, strike: int):
        self.strike = strike

    def on_start(self, ctx):  # noqa: ANN001
        ctx.schedule_every(SEC, ctx.now() + SEC)

    def on_timer(self, ctx, timer_id):  # noqa: ANN001
        tk = ctx.last_tick(self.strike)
        chain = [r["instrument_id"] for r in ctx.chain("NIFTY", "2026-11-17")]
        ctx.log("info", f"strike ltp={tk.ltp if tk else None} stale={ctx.stale(self.strike)} "
                        f"in_chain={self.strike in chain}")


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class StrategyViewTests(ChainReplayTest):
    def test_no_quote_before_staleness_after_and_nothing_invented(self):
        sha = packs.build(self.spec())
        cfg = runconfig.load({"schema": "stockify.run/1", "name": "chainreplay",
                              "strategy": {"kind": "python", "ref": f"{__name__}:Watcher"},      # the one run below
                              "data": {"packs": [{"sha256": sha}]}, "capital_paise": 10**8, "price_basis": "raw",
                              "fill_model": {"kind": "snapshot_quote"}, "costs": {"allow_unverified": True}})
        b = bundle_mod.resolve_bundle(cfg)
        b["pricing"]["stale_after_ms"] = 2000
        pack = paths.lab_dir() / b["packs"][0]["path"]
        logs = {}
        with tempfile.TemporaryDirectory(prefix="chain-replay-") as tmp:
            for name, run in (("oracle", oracle_run), ("engine", engine.run_bundle)):
                folder = Path(tmp) / name
                run(b, [pack], folder, strategy=Watcher(self.mid), report=False)
                logs[name] = [x.split(" ", 2)[2] for x in (folder / "events_log.txt").read_text().splitlines()
                              if " strike ltp=" in x]
        self.assertEqual(logs["engine"], logs["oracle"])
        seen = logs["oracle"]
        ltps = [x.split()[1] for x in seen]
        self.assertIn("strike ltp=None stale=True in_chain=True", seen[0])          # listed, but no quote yet
        recorded = {f"ltp={9_000 + s}" for s in range(10, 15)}
        self.assertTrue(set(v for v in ltps if v != "ltp=None") <= recorded)       # only recorded prices (I-7)
        self.assertTrue(any(v in recorded for v in ltps))
        self.assertEqual(seen[-1].split()[2], "stale=True")                         # dropped: goes stale
        self.assertIn("stale=False", " ".join(seen))


if __name__ == "__main__":
    unittest.main()
