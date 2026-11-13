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
    logged = True

    def record(self):
        landing = LandingDB(DAY)
        clock = ManualClock(t("09:14:59"))
        tokens = {pf.NIFTY_CE: self.opt, pf.NIFTY_INDEX: self.idx}
        rec = Recorder(landing, tokens.get, clock)
        rec.begin()
        rows = [(t("09:15:00"), "subscribe", self.opt, pf.NIFTY_CE, "full", "start"),
                (t("09:25:00"), "unsubscribe", self.opt, pf.NIFTY_CE, "full", "end")]
        if self.idx_mode:                                           # None: an old-style recording, no row for it
            rows += [(t("09:15:00"), "subscribe", self.idx, pf.NIFTY_INDEX, self.idx_mode, "start"),
                     (t("09:25:00"), "unsubscribe", self.idx, pf.NIFTY_INDEX, self.idx_mode, "end")]
        if self.logged:
            landing.log_subscriptions(rows)
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


class OptionOnlyLoggedTest(MixedModeTest):
    idx_mode = None                                                  # the index has no subscription row


class OldRecordingTest(MixedModeTest):
    logged = False                                                   # no subscription_log at all


class CoverageTests(MixedModeTest):
    def test_quality_report_writes_the_two_day_metrics(self):
        r = quality.report(DAY)
        self.assertAlmostEqual(r["day"]["coverage_full_pct"], 90.0)      # the option
        self.assertAlmostEqual(r["day"]["coverage_quote_pct"], 45.0)     # the index
        day_rows = quality.stored(DAY)[0]
        self.assertAlmostEqual(day_rows["coverage_full_pct"], 90.0)
        self.assertAlmostEqual(day_rows["coverage_quote_pct"], 45.0)
        self.assertEqual((day_rows["coverage_full_n"], day_rows["coverage_quote_n"],
                          day_rows["coverage_unlogged_n"]), (1, 1, 0))

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
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM data_quality WHERE metric IN "
                                      "('coverage_full_n','coverage_quote_n','coverage_unlogged_n')").fetchone()[0], 3)
        self.assertEqual(quality.stored(DAY)[0]["coverage_full_n"], 1)

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
                                  "coverage_quote_pct", "coverage_full_n", "coverage_quote_n",
                                  "coverage_unlogged_n"})
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
        day_rows = quality.stored(DAY)[0]
        self.assertNotIn("coverage_quote_pct", day_rows)                 # no row for an empty group
        self.assertEqual((day_rows["coverage_full_n"], day_rows["coverage_quote_n"],
                          day_rows["coverage_unlogged_n"]), (2, 0, 0))     # but the count says 0


class UnloggedCoverageTests(OldRecordingTest):
    def test_all_unlogged_old_style_day(self):
        r = quality.report(DAY)
        d = r["day"]
        self.assertEqual((d["coverage_full_n"], d["coverage_quote_n"], d["coverage_unlogged_n"]), (0, 0, 2))
        self.assertIsNone(d["coverage_full_pct"])
        self.assertIsNone(d["coverage_quote_pct"])
        day_rows = quality.stored(DAY)[0]
        self.assertNotIn("coverage_full_pct", day_rows)
        self.assertNotIn("coverage_quote_pct", day_rows)
        self.assertEqual((day_rows["coverage_full_n"], day_rows["coverage_quote_n"],
                          day_rows["coverage_unlogged_n"]), (0, 0, 2))
        self.assertIn("no mode logged for 2", quality.summary_markdown(r))

    def test_rerun_is_idempotent(self):
        quality.report(DAY)
        first = quality.stored(DAY)
        quality.report(DAY)
        self.assertEqual(quality.stored(DAY), first)


class PartlyLoggedCoverageTests(OptionOnlyLoggedTest):
    def test_unlogged_instrument_is_counted_not_averaged(self):
        d = quality.report(DAY)["day"]
        self.assertAlmostEqual(d["coverage_full_pct"], 90.0)             # the option only
        self.assertIsNone(d["coverage_quote_pct"])                       # the unlogged index is not QUOTE
        self.assertEqual((d["coverage_full_n"], d["coverage_quote_n"], d["coverage_unlogged_n"]), (1, 0, 1))


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
                mode = rng.choice(["full", "quote", "ltp", None])        # None: no subscription row
                if mode:
                    subs.append({"recv_ns": window[0], "action": "subscribe", "instrument_id": iid,
                                 "instrument_token": iid, "mode": mode, "reason": "start"})
                    if rng.random() < 0.3:                               # later switched mode: still FULL if ever full
                        other = rng.choice(["full", "quote"])
                        subs.append({"recv_ns": window[0] + SEC, "action": "subscribe", "instrument_id": iid,
                                     "instrument_token": iid, "mode": other, "reason": "mode"})
                        mode = "full" if "full" in (mode, other) else mode
                seen = set(rng.sample(range(n), rng.randint(0, 40)))
                for s in sorted(seen):
                    pkts.append({"instrument_id": iid, "recv_ns": (start_s + s) * SEC + 5, "pkt_len": 8,
                                 "volume": None, "exch_ts_s": 0, "raw": b""})
                if seen:                                  # an instrument with no packet is not in the report
                    expect[iid] = (mode, 100.0 * len(seen) / n)
            pkts.sort(key=lambda p: p["recv_ns"])
            full = [c for m, c in expect.values() if m == "full"]
            rest = [c for m, c in expect.values() if m not in ("full", None)]
            unlogged = sum(1 for m, _ in expect.values() if m is None)
            day = _FakeDay(subs, pkts)
            res = quality._report(DAY, day, write=False)
            got = res["day"]
            for key, group in (("coverage_full_pct", full), ("coverage_quote_pct", rest)):
                if group:
                    self.assertAlmostEqual(got[key], sum(group) / len(group), delta=0.0011)   # coverage_pct is rounded to 3 places
                else:
                    self.assertIsNone(got[key])
            self.assertEqual((got["coverage_full_n"], got["coverage_quote_n"], got["coverage_unlogged_n"]),
                             (len(full), len(rest), unlogged))
            self.assertEqual(set(day.calls), {"subscriptions", "packets", "heartbeats", "gaps"})


if __name__ == "__main__":
    unittest.main()
