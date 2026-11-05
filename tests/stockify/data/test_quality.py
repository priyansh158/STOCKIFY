"""stockify.data.quality on a day with planted problems (M1.9)."""
from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import paths, timeutil
from stockify.daemon import kite_packets as kp
from stockify.daemon.clock import ManualClock
from stockify.daemon.landing import LandingDB
from stockify.daemon.recorder import Recorder
from stockify.data import compact, instruments, quality, refdata
from tests.stockify.daemon import packets_fixture as pf

DAY = date(2026, 11, 9)
SEC = 1_000_000_000
HEADER = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
          "tick_size,lot_size,instrument_type,segment,exchange")


def t(hhmmss: str) -> int:
    return timeutil.ist_ns(DAY, hhmmss)


def crossed_full(token: int, ltp: int, exch_ts: int, vol: int) -> bytes:
    depth = [(10, ltp + 10, 1)] + [(10, ltp - 5 * i, 1) for i in range(2, 6)] + \
            [(10, ltp + 5, 1)] + [(10, ltp + 5 * i, 1) for i in range(2, 6)]     # best bid above best ask
    return pf.full(token, ltp, vol=vol, exch_ts=exch_ts, depth=depth)


class QualityTest(unittest.TestCase):
    def setUp(self):
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        support.reset_sqlite_singleton(refdata)
        self.addCleanup(support.reset_sqlite_singleton, refdata)
        instruments.sync(DAY, {
            "NSE": f"{HEADER}\n{pf.NIFTY_INDEX},1,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n",
            "NFO": f"{HEADER}\n{pf.NIFTY_CE},2,NIFTY24000CE,NIFTY,0,2026-11-17,24000.0,0.05,65,CE,NFO-OPT,NFO\n",
        }, underlyings={"NIFTY": "NSE:NIFTY 50"})
        self.opt = instruments.find("NFO", "NIFTY24000CE")
        self.idx = instruments.find("NSE", "NIFTY 50")
        self.record()
        p = patch.object(compact, "session_over", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def record(self):
        landing = LandingDB(DAY)
        clock = ManualClock(t("09:14:59"))
        tokens = {pf.NIFTY_CE: self.opt, pf.NIFTY_INDEX: self.idx}
        rec = Recorder(landing, tokens.get, clock)
        rec.begin()
        landing.log_subscriptions([(t("09:15:00"), "subscribe", self.opt, pf.NIFTY_CE, "full", "start"),
                                   (t("09:15:00"), "subscribe", self.idx, pf.NIFTY_INDEX, "full", "start"),
                                   (t("09:25:00"), "unsubscribe", self.opt, pf.NIFTY_CE, "full", "end"),
                                   (t("09:25:00"), "unsubscribe", self.idx, pf.NIFTY_INDEX, "full", "end")])
        vol = 1000
        for s in list(range(0, 600)) + [660, 661]:                    # 660/661 = after unsubscribe
            now = t("09:15:00") + s * SEC
            if t("09:20:00") <= now < t("09:21:00"):
                continue                                             # a 60 s outage for both
            clock.set(now)
            exch = now // SEC - 1                                    # 1 s latency
            if s == 200:
                exch -= 30                                           # exchange time goes backwards
            vol = vol - 50 if s == 100 else vol + 10                 # one day-volume decrease
            packets = [crossed_full(pf.NIFTY_CE, 12_000, exch, vol) if s == 30
                       else pf.full(pf.NIFTY_CE, 12_000, vol=vol, exch_ts=exch)]
            if s % 2 == 0:
                packets.append(pf.index_full(pf.NIFTY_INDEX, 2_400_000, 1, 1, 1, 1, 0, now // SEC))
            rec.on_message(None, kp.build_frame(packets), True)
            rec.process(rec.queue.get_nowait())
        rec.flush()
        landing.add_gap(t("09:20:00"), t("09:21:00"), "disconnect")
        landing.close()


class ReportTests(QualityTest):
    def test_planted_problems_are_found(self):
        r = quality.report(DAY)
        o = r["instruments"][self.opt]
        self.assertEqual(o["packets"], 540)                          # 600 s minus the outage; late packets ignored
        self.assertAlmostEqual(o["coverage_pct"], 90.0)
        self.assertEqual(o["longest_gap_s"], 60)
        self.assertEqual((o["crossed_books"], o["volume_decreases"], o["exch_ts_out_of_order"]), (1, 1, 1))
        self.assertEqual(o["latency_p50_ms"], 1000.0)
        i = r["instruments"][self.idx]
        self.assertAlmostEqual(i["coverage_pct"], 45.0)              # every 2 s, minus the outage
        self.assertEqual(i["longest_gap_s"], 61)
        self.assertEqual(i["latency_p50_ms"], 0.0)
        self.assertEqual(r["day"]["silent_minutes"], 1)
        self.assertEqual(r["day"]["gap_s_disconnect"], 60.0)
        self.assertEqual(r["day"]["instruments"], 2)

    def test_rows_are_stored_and_replaced(self):
        quality.report(DAY)
        quality.report(DAY)
        rows = quality.stored(DAY)
        self.assertEqual(rows[self.opt]["coverage_pct"], 90.0)
        n = refdata._connect().execute("SELECT COUNT(*) FROM data_quality").fetchone()[0]
        self.assertEqual(n, len(rows[self.opt]) + len(rows[self.idx]) + len(rows[0]))

    def test_same_report_from_the_archive_after_deletion(self):
        from_landing = quality.report(DAY, write=False)
        self.assertEqual(compact.compact(DAY).status, "verified")
        compact.apply_retention(DAY)
        self.assertFalse(paths.ticks_landing_file(DAY).exists())
        from_archive = quality.report(DAY, write=False)
        self.assertEqual(from_archive["source"], "archive")
        self.assertEqual(from_archive["instruments"], from_landing["instruments"])
        self.assertEqual(from_archive["day"], from_landing["day"])

    def test_summary_markdown(self):
        r = quality.report(DAY)
        text = paths.lab_dir().joinpath("reports", "quality_20261109.md").read_text(encoding="utf-8")
        self.assertEqual(text, quality.summary_markdown(r))
        self.assertIn("Silent minutes (no packet, no heartbeat): 1", text)
        self.assertIn("| NIFTY 50 | 45.0 | 61 |", text)

    def test_missing_day(self):
        with self.assertRaises(quality.QualityError):
            quality.report(date(2026, 11, 11))


class ApiTests(QualityTest):
    @support.requires_fastapi
    def test_endpoint(self):
        from fastapi.testclient import TestClient
        from server.main import app
        quality.report(DAY)
        client = TestClient(app)
        ok = client.get("/api/data/quality", params={"date": "2026-11-09"})
        self.assertEqual(ok.status_code, 200)
        body = ok.json()
        self.assertEqual(body["day"]["silent_minutes"], 1)
        self.assertEqual({i["symbol"] for i in body["instruments"]}, {"NIFTY24000CE", "NIFTY 50"})
        self.assertEqual(client.get("/api/data/quality", params={"date": "2026-11-11"}).status_code, 404)
        self.assertEqual(client.get("/api/data/quality", params={"date": "bad"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
