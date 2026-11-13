"""LCM 6.1: the quality report adds FULL and QUOTE recorder coverage for each day (day rows, instrument_id 0)."""
from __future__ import annotations

import hashlib
import random
import unittest
from unittest.mock import patch

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import paths
from stockify.daemon import kite_packets as kp
from stockify.daemon.clock import ManualClock
from stockify.daemon.landing import LandingDB
from stockify.daemon.recorder import Recorder
from stockify.data import instruments, quality, refdata
from tests.stockify.daemon import packets_fixture as pf
from tests.stockify.data import test_quality as tq

DAY = tq.DAY
SEC = tq.SEC
t = tq.t


class MixedModeTest(tq.QualityTest):
    """The planted-problems day, but the index is subscribed in QUOTE mode (option stays FULL)."""
    idx_mode = "quote"

    def record(self):
        landing = LandingDB(DAY)
        clock = ManualClock(t("09:14:59"))
        tokens = {pf.NIFTY_CE: self.opt, pf.NIFTY_INDEX: self.idx}
        rec = Recorder(landing, tokens.get, clock)
        rec.begin()
        landing.log_subscriptions([(t("09:15:00"), "subscribe", self.opt, pf.NIFTY_CE, "full", "start"),
                                   (t("09:15:00"), "subscribe", self.idx, pf.NIFTY_INDEX, self.idx_mode, "start"),
                                   (t("09:25:00"), "unsubscribe", self.opt, pf.NIFTY_CE, "full", "end"),
                                   (t("09:25:00"), "unsubscribe", self.idx, pf.NIFTY_INDEX, self.idx_mode, "end")])
        for s in range(0, 600):
            now = t("09:15:00") + s * SEC
            if t("09:20:00") <= now < t("09:21:00"):
                continue
            clock.set(now)
            exch = now // SEC - 1
            packets = [pf.full(pf.NIFTY_CE, 12_000, vol=1000 + s, exch_ts=exch)]
            if s % 2 == 0:
                packets.append(pf.index_full(pf.NIFTY_INDEX, 2_400_000, 1, 1, 1, 1, 0, now // SEC))
            rec.on_message(None, kp.build_frame(packets), True)
            rec.process(rec.queue.get_nowait())
        rec.flush()
        landing.close()


class AllFullTest(MixedModeTest):
    idx_mode = "full"


class CoverageTests(MixedModeTest):
    def test_quality_report_writes_the_two_day_metrics(self):
        r = quality.report(DAY)
        self.assertAlmostEqual(r["day"]["coverage_full_pct"], 90.0)      # the option
        self.assertAlmostEqual(r["day"]["coverage_quote_pct"], 45.0)     # the index
        day_rows = quality.stored(DAY)[0]
        self.assertAlmostEqual(day_rows["coverage_full_pct"], 90.0)
        self.assertAlmostEqual(day_rows["coverage_quote_pct"], 45.0)

    def test_quality_rerun_keeps_the_foreign_metrics(self):
        with refdata.transaction() as c:
            c.execute("INSERT INTO data_quality (trade_date, instrument_id, metric, value, detail) "
                      "VALUES (?, ?, ?, ?, ?)", (DAY.isoformat(), self.opt, "fsp_vs_close_paise", 7.0, None))
        quality.report(DAY)
        quality.report(DAY)
        conn = refdata._connect()
        rows = conn.execute("SELECT value FROM data_quality WHERE trade_date=? AND metric='fsp_vs_close_paise'",
                            (DAY.isoformat(),)).fetchall()
        self.assertEqual([r[0] for r in rows], [7.0])
        n = conn.execute("SELECT COUNT(*) FROM data_quality WHERE metric IN "
                         "('coverage_full_pct','coverage_quote_pct')").fetchone()[0]
        self.assertEqual(n, 2)                                           # replaced, not duplicated

    def test_existing_metrics_are_unchanged(self):
        r = quality.report(DAY)
        o, i = r["instruments"][self.opt], r["instruments"][self.idx]
        self.assertEqual((o["packets"], o["longest_gap_s"], o["latency_p50_ms"]), (540, 60, 1000.0))
        self.assertAlmostEqual(o["coverage_pct"], 90.0)
        self.assertEqual((i["longest_gap_s"], i["latency_p50_ms"]), (61, 0.0))
        self.assertAlmostEqual(i["coverage_pct"], 45.0)
        d = r["day"]
        self.assertEqual((d["instruments"], d["packets"], d["silent_minutes"]), (2, 540 + 270, 1))
        self.assertEqual(set(d), {"instruments", "packets", "silent_minutes", "coverage_full_pct",
                                  "coverage_quote_pct"})
        stored = quality.stored(DAY)
        self.assertEqual(set(stored[0]), set(d))
        self.assertEqual(set(stored[self.opt]), {"packets", "coverage_pct", "longest_gap_s", "crossed_books",
                                                 "volume_decreases", "exch_ts_out_of_order", "latency_p50_ms",
                                                 "latency_p95_ms", "latency_p99_ms"})

    def test_report_reads_packets_only_and_writes_only_data_quality_rows(self):
        def fingerprint():
            conn = refdata._connect()
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                                 "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            return {n: hashlib.sha256(repr([tuple(r) for r in conn.execute(f'SELECT * FROM "{n}" ORDER BY 1,2,3')])
                                      .encode()).hexdigest() for n in tables if n != "data_quality"}
        landing = paths.ticks_landing_file(DAY)
        before_file = hashlib.sha256(landing.read_bytes()).hexdigest()
        before = fingerprint()
        quality.report(DAY)
        after = fingerprint()
        self.assertEqual([k for k in after if after[k] != before[k]], [])
        self.assertEqual(after, before)
        self.assertEqual(hashlib.sha256(landing.read_bytes()).hexdigest(), before_file)


class AllFullCoverageTests(AllFullTest):
    def test_empty_group_gives_none(self):
        r = quality.report(DAY)
        self.assertIsNone(r["day"]["coverage_quote_pct"])
        self.assertAlmostEqual(r["day"]["coverage_full_pct"], (90.0 + 45.0) / 2)
        self.assertNotIn("coverage_quote_pct", quality.stored(DAY)[0])   # no row for an empty group


class _FakeDay:
    source = "fake"

    def __init__(self, subs, pkts):
        self._subs, self._pkts, self.calls = subs, pkts, []

    def subscriptions(self):
        self.calls.append("subscriptions")
        return self._subs

    def packets(self, cols):
        self.calls.append("packets")
        return iter(self._pkts)

    def heartbeats(self):
        self.calls.append("heartbeats")
        return []

    def gaps(self):
        self.calls.append("gaps")
        return []


class MeansProperty(unittest.TestCase):
    """Seeded loop: random instruments, modes and coverage; the two metrics equal independently computed means."""

    def setUp(self):
        p = patch.object(instruments, "get", return_value={})      # every instrument is a CM one: one window
        p.start()
        self.addCleanup(p.stop)

    def test_day_metrics_equal_the_independent_means(self):
        window = quality._continuous("CM", DAY)
        start_s, end_s = window[0] // SEC, window[1] // SEC
        n = end_s - start_s
        rng = random.Random(61)
        for _ in range(60):
            subs, pkts, expect = [], [], {}
            for iid in range(1, rng.randint(1, 6) + 1):
                mode = rng.choice(["full", "quote", "ltp"])
                subs.append({"recv_ns": window[0], "action": "subscribe", "instrument_id": iid,
                             "instrument_token": iid, "mode": mode, "reason": "start"})
                seen = set(rng.sample(range(n), rng.randint(0, 40)))
                for s in sorted(seen):
                    pkts.append({"instrument_id": iid, "recv_ns": (start_s + s) * SEC + 5, "pkt_len": 8,
                                 "volume": None, "exch_ts_s": 0, "raw": b""})
                if seen:                                  # an instrument with no packet is not in the report
                    expect[iid] = (mode, 100.0 * len(seen) / n)
            pkts.sort(key=lambda p: p["recv_ns"])
            full = [c for m, c in expect.values() if m == "full"]
            rest = [c for m, c in expect.values() if m != "full"]
            day = _FakeDay(subs, pkts)
            res = quality._report(DAY, day, write=False)
            got = res["day"]
            for key, group in (("coverage_full_pct", full), ("coverage_quote_pct", rest)):
                if group:
                    self.assertAlmostEqual(got[key], sum(group) / len(group), delta=0.0011)   # coverage_pct is rounded to 3 places
                else:
                    self.assertIsNone(got[key])
            self.assertEqual(set(day.calls), {"subscriptions", "packets", "heartbeats", "gaps"})


if __name__ == "__main__":
    unittest.main()
