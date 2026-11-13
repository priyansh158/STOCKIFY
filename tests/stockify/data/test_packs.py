"""Event packs (M1.8): encoding, ordering, determinism (P-05), sources."""
from __future__ import annotations

import json
import struct
import unittest
from datetime import date
from unittest.mock import patch

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import paths, timeutil
from stockify.daemon import kite_packets as kp
from stockify.daemon.clock import ManualClock
from stockify.daemon.landing import LandingDB
from stockify.daemon.recorder import Recorder
from stockify.data import compact, history, instruments, packs, refdata
from tests.stockify.daemon import packets_fixture as pf

DAY = date(2026, 11, 17)
SEC = 1_000_000_000
HEADER = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
          "tick_size,lot_size,instrument_type,segment,exchange")
FUT_TOKEN, QUOTE_CE = 9001, 9003


def t(hhmmss: str) -> int:
    return timeutil.ist_ns(DAY, hhmmss)


class PackTest(unittest.TestCase):
    def setUp(self):
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        for mod in (refdata, history):
            support.reset_sqlite_singleton(mod)
            self.addCleanup(support.reset_sqlite_singleton, mod)
        strikes = "".join(f"{90000 + i},{20 + i},NIFTY26N17{s}CE,NIFTY,0,2026-11-17,{s},0.05,65,CE,NFO-OPT,NFO\n"
                          for i, s in enumerate(range(23500, 24600, 100)))
        instruments.sync(date(2026, 11, 16), {
            "NSE": f"{HEADER}\n{pf.NIFTY_INDEX},1,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n",
            "NFO": (f"{HEADER}\n{pf.NIFTY_CE},2,NIFTY26N1724000CE,NIFTY,0,2026-11-17,24000,0.05,65,CE,NFO-OPT,NFO\n"
                    f"{FUT_TOKEN},3,NIFTY26NOVFUT,NIFTY,0,2026-11-24,0,0.1,65,FUT,NFO-FUT,NFO\n" + strikes),
        }, underlyings={"NIFTY": "NSE:NIFTY 50"})
        self.ce = instruments.find("NFO", "NIFTY26N1724000CE")
        self.idx = instruments.find("NSE", "NIFTY 50")
        self.fut = instruments.find("NFO", "NIFTY26NOVFUT")
        p = patch.object(compact, "session_over", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def record(self):
        landing = LandingDB(DAY)
        clock = ManualClock(t("09:14:59"))
        tokens = {pf.NIFTY_CE: self.ce, pf.NIFTY_INDEX: self.idx, FUT_TOKEN: self.fut}
        rec = Recorder(landing, tokens.get, clock)
        rec.begin()
        landing.log_subscriptions([(t("09:15:00"), "subscribe", self.ce, pf.NIFTY_CE, "full", "start"),
                                   (t("09:15:00"), "subscribe", self.idx, pf.NIFTY_INDEX, "full", "start"),
                                   (t("09:15:00"), "subscribe", self.fut, FUT_TOKEN, "quote", "start")])
        for s in range(20):
            clock.set(t("09:15:00") + s * SEC)
            frame = [pf.full(pf.NIFTY_CE, 12_000 + s, exch_ts=t("09:15:00") // SEC + s),
                     pf.index_full(pf.NIFTY_INDEX, 2_400_000 + s, 2_401_000, 2_399_000, 2_400_000, 2_390_000, 1000 + s,
                                   t("09:15:00") // SEC + s),
                     pf.quote(FUT_TOKEN, 2_410_000, 65, 2_409_000, 100 + s, 10, 20, 1, 2, 3, 4)]
            rec.on_message(None, kp.build_frame(frame), True)
            rec.process(rec.queue.get_nowait())
        rec.flush()
        landing.add_gap(t("09:15:05"), t("09:15:08"), "disconnect")
        landing.add_gap(t("09:15:10"), t("09:15:11"), "stall", "tier:full")
        landing.close()

    def spec(self, **kw):
        return {"dataset": "ticks", "from": DAY.isoformat(), "to": DAY.isoformat(), **kw}


class EncodingTests(unittest.TestCase):
    def test_sizes_match_a1(self):
        self.assertEqual(packs.HEADER.size, 64)
        self.assertEqual(packs.PREFIX.size, 24)
        p = {"raw": pf.full(pf.NIFTY_CE, 12_000)}
        self.assertEqual(len(packs.enc_tick(p, 7, 1)[1]), 384)
        self.assertEqual(len(packs.enc_tick({"raw": pf.quote(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)}, 7, 1)[1]), 104)
        self.assertEqual(len(packs.enc_tick({"raw": pf.index_full(pf.NIFTY_INDEX, 1, 2, 3, 4, 5, 6, 7)}, 7, 1)[1]), 80)
        self.assertEqual(len(packs.enc_bar(1, 2, 60, 3, 1, 2, 3, 4, 5, 6)), 88)
        self.assertEqual(len(packs.enc_clock(5)), 24)
        self.assertEqual(len(packs.enc_gap(0, 5, 0, "disconnect")), 32)
        self.assertEqual(len(packs.enc_settle(1, 5, 0, "2026-11-17", 100)), 40)
        self.assertEqual(len(packs.enc_refdata(1, 5, "instrument_add", {"a": 1})), 32 + len('{"a":1}'))

    def test_full_tick_round_trip(self):
        raw = pf.full(pf.NIFTY_CE, 12_345, vol=777, oi=50_000, exch_ts=1_762_316_200)
        _, rec = packs.enc_tick({"raw": raw}, 42, 99)
        out, end = packs.decode(rec, 0)
        self.assertEqual(end, 384)
        f = kp.parse_packet(raw)
        for k in ("ltp", "last_qty", "atp", "volume", "buy_qty", "sell_qty", "open", "high", "low", "close",
                  "last_trade_ts_s", "oi", "oi_high", "oi_low", "exch_ts_s"):
            self.assertEqual(out[k], f[k], k)
        self.assertEqual(out["depth"], [(p, q, o) for q, p, o in kp.parse_depth(raw)])    # price, qty, orders
        self.assertEqual((out["instrument_id"], out["t_event"], out["kind"]), (42, 99, "TICK_FULL"))

    def test_index_change_is_signed(self):
        raw = bytearray(pf.index_full(pf.NIFTY_INDEX, 1, 2, 3, 4, 5, 0, 7))
        struct.pack_into(">i", raw, 24, -1234)
        out, _ = packs.decode(packs.enc_tick({"raw": bytes(raw)}, 1, 1)[1], 0)
        self.assertEqual((out["kind"], out["value"], out["change"], out["exch_ts_s"]), ("INDEX", 1, -1234, 7))

    def test_ltp_mode_and_unknown_kind_rejected(self):
        with self.assertRaises(packs.PackError):
            packs.enc_tick({"raw": pf.ltp(1, 2)}, 1, 1)
        with self.assertRaises(packs.PackError):
            packs.decode(struct.pack("<HHIqQ", 99, 24, 0, 0, 0), 0)
        with self.assertRaises(packs.PackError):
            packs.enc_gap(0, 1, 0, "solar_flare")


class TicksPackTests(PackTest):
    def setUp(self):
        super().setUp()
        self.record()

    def test_records_and_order(self):
        sha = packs.build(self.spec())
        header, recs = packs.read(packs.pack_path(sha))
        self.assertEqual(header["price_basis"], "raw")
        self.assertEqual([r["seq"] for r in recs], list(range(1, len(recs) + 1)))
        kinds = [r["kind"] for r in recs]
        self.assertEqual(kinds[:3], ["REFDATA"] * 3)                             # instrument_add at 00:00 IST
        self.assertEqual(recs[0]["t_event"], timeutil.ist_ns(DAY, "00:00"))
        self.assertEqual((kinds.count("TICK_FULL"), kinds.count("INDEX"), kinds.count("TICK_QUOTE")), (20, 20, 20))
        gaps = [r for r in recs if r["kind"] == "GAP"]
        self.assertEqual([(g["reason"], g["phase"], g["scope"], g["instrument_id"]) for g in gaps],
                         [("disconnect", 0, 0, 0), ("disconnect", 1, 0, 0),
                          *[("stall", 0, 1, i) for i in sorted((self.idx, self.ce))],   # tier:full → per instrument
                          *[("stall", 1, 1, i) for i in sorted((self.idx, self.ce))]])
        at_gap = [r["kind"] for r in recs if r["t_event"] == t("09:15:05")]
        self.assertEqual(at_gap[0], "GAP")                                        # rank 1 before market data
        self.assertEqual(header["count"], len(recs))
        self.assertEqual((header["t_min"], header["t_max"]), (recs[0]["t_event"], recs[-1]["t_event"]))
        m = packs.manifest(sha)
        self.assertEqual(m["counts_by_kind"], {"GAP": 6, "INDEX": 20, "REFDATA": 3, "TICK_FULL": 20, "TICK_QUOTE": 20})
        self.assertEqual(m["sources"][0]["source"], "landing")

    def test_same_bytes_from_landing_and_archive(self):
        """P-05, and the pack does not depend on where the ticks were read from."""
        a = packs.build(self.spec())
        self.assertEqual(packs.build(self.spec()), a)
        self.assertEqual(compact.compact(DAY).status, "verified")
        compact.apply_retention(DAY)
        self.assertFalse(paths.ticks_landing_file(DAY).exists())
        self.assertEqual(packs.build(self.spec()), a)
        self.assertEqual(packs.manifest(a)["sources"][0]["source"], "archive")

    def test_clock_events_only_add_records(self):
        plain = packs.read(packs.pack_path(packs.build(self.spec())))[1]
        sha = packs.build(self.spec(clock_cadence_ms=5000))
        header, clocked = packs.read(packs.pack_path(sha))
        self.assertTrue(header["flags"] & packs.FLAG_CLOCK)
        clocks = [r for r in clocked if r["kind"] == "CLOCK"]
        self.assertEqual([r["t_event"] for r in clocks], [t("09:15:00"), t("09:15:05"), t("09:15:10"), t("09:15:15")])
        strip = lambda rs: [{k: v for k, v in r.items() if k != "seq"} for r in rs if r["kind"] != "CLOCK"]  # noqa: E731
        self.assertEqual(strip(clocked), strip(plain))

    def test_selectors_and_tiers(self):
        sha = packs.build(self.spec(instruments=[{"symbols": ["NSE:NIFTY 50"]}], tiers=["index"], include_gaps=False))
        recs = packs.read(packs.pack_path(sha))[1]
        self.assertEqual({r["kind"] for r in recs}, {"REFDATA", "INDEX"})
        self.assertEqual({r["instrument_id"] for r in recs}, {self.idx})

    def test_chain_centred_on_previous_close(self):
        with refdata.transaction() as conn:
            conn.execute("INSERT INTO index_eod VALUES ('2026-11-16', ?, 0, 0, 0, 2410000)", (self.idx,))
        ids = packs.resolve([{"chain": {"underlying": "NIFTY", "expiry": "nearest", "strikes": 2, "futures": True}}], DAY)
        strikes = sorted(instruments.get(i)["strike_paise"] for i in ids if i != self.fut)
        self.assertEqual(strikes, [2390000, 2400000, 2410000, 2420000, 2430000])   # ATM 24100 ± 2
        self.assertIn(self.fut, ids)

    def test_chain_without_reference_refuses(self):
        with self.assertRaisesRegex(packs.PackError, "previous-day close"):
            packs.resolve([{"chain": {"underlying": "NIFTY", "strikes": 2}}], DAY)

    def test_missing_day(self):
        spec = self.spec(to="2026-11-18")
        with self.assertRaisesRegex(packs.PackError, "not recorded"):
            packs.build(spec)
        sha = packs.build({**spec, "skip_missing_days": True})
        self.assertEqual(len(packs.manifest(sha)["sources"]), 1)

    def test_settlement_records(self):
        with refdata.transaction() as conn:
            conn.execute("INSERT INTO fo_eod (trade_date, instrument_id, settle_paise) VALUES ('2026-11-17', ?, 1234)",
                         (self.ce,))
            conn.execute("INSERT INTO settlement_prices VALUES (?, '2026-11-17', 2400500, 'fo_eod', 'ok')", (self.idx,))
        recs = packs.read(packs.pack_path(packs.build(self.spec(include_gaps=False))))[1]
        settles = [r for r in recs if r["kind"] == "SETTLE"]
        self.assertEqual([(r["subtype"], r["instrument_id"], r["price_paise"], r["t_event"]) for r in settles],
                         [(0, self.idx, 2400500, t("16:00")), (1, self.ce, 1234, t("19:00"))])


class TablePackTests(PackTest):
    def test_bars_pack_event_time_is_bar_end(self):
        with history._Tx() as conn:
            for i in range(3):
                start = t("09:15") + i * 60 * SEC
                conn.execute("INSERT INTO bars VALUES (?, 'minute', ?, 1, 2, 0, ?, 10, NULL, '2026-11-17')",
                             (self.fut, start, 100 + i))
        sha = packs.build({"dataset": "bars", "interval": "minute", "from": "2026-11-17", "to": "2026-11-17",
                           "instruments": [{"ids": [self.fut]}]})
        header, recs = packs.read(packs.pack_path(sha))
        self.assertEqual(header["price_basis"], "adjusted")
        bars = [r for r in recs if r["kind"] == "BAR"]
        self.assertEqual([(r["start_ns"], r["t_event"], r["close"], r["interval_s"]) for r in bars],
                         [(t("09:15") + i * 60 * SEC, t("09:16") + i * 60 * SEC, 100 + i, 60) for i in range(3)])
        self.assertEqual(packs.build({"dataset": "bars", "interval": "minute", "from": "2026-11-17",
                                      "to": "2026-11-17", "instruments": [{"ids": [self.fut]}]}), sha)

    def test_bhav_eod_pack(self):
        with refdata.transaction() as conn:
            conn.execute("INSERT INTO fo_eod (trade_date, instrument_id, open_paise, high_paise, low_paise, close_paise, "
                         "settle_paise, volume, oi) VALUES ('2026-11-17', ?, 10, 20, 5, 15, 16, 7, 8)", (self.fut,))
            conn.execute("INSERT INTO index_eod VALUES ('2026-11-17', ?, 1, 2, 3, 4)", (self.idx,))
        sha = packs.build({"dataset": "bhav_eod", "from": "2026-11-17", "to": "2026-11-17",
                           "instruments": [{"ids": [self.fut, self.idx]}]})
        recs = packs.read(packs.pack_path(sha))[1]
        bars = {r["instrument_id"]: r for r in recs if r["kind"] == "BAR"}
        self.assertEqual((bars[self.fut]["close"], bars[self.fut]["oi"], bars[self.fut]["t_event"]), (15, 8, t("19:00")))
        self.assertEqual(bars[self.idx]["interval_s"], 86400)
        settle = next(r for r in recs if r["kind"] == "SETTLE")
        self.assertEqual((settle["subtype"], settle["price_paise"]), (1, 16))
        kinds = [r["kind"] for r in recs if r["t_event"] == t("19:00")]
        self.assertEqual(kinds, ["SETTLE", "BAR", "BAR"])                          # rank 3 before rank 4

    def test_manifest_and_cli(self):
        from stockify.__main__ import main
        spec_file = paths.lab_dir() / "spec.json"
        spec_file.write_text(json.dumps({"dataset": "bhav_eod", "from": "2026-11-16", "to": "2026-11-17",
                                         "instruments": [{"ids": [self.idx]}]}))
        with patch("sys.stdout") as out:
            self.assertEqual(main(["pack", "build", "--spec", str(spec_file)]), 0)
        sha = "".join(c.args[0] for c in out.write.call_args_list).strip()
        self.assertEqual(len(sha), 64)
        m = packs.manifest(sha)
        self.assertEqual((m["sha256"], m["format"], m["price_basis"]), (sha, "evt/1", "raw"))
        with patch("sys.stdout"):
            self.assertEqual(main(["pack", "show", sha[:10]]), 0)


if __name__ == "__main__":
    unittest.main()
