"""stockify.data.compact: landing → verified Parquet archive (M1.7)."""
from __future__ import annotations

import json
import unittest
from datetime import date
from unittest.mock import patch

import pyarrow.parquet as pq

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import paths, timeutil
from stockify.daemon import kite_packets as kp
from stockify.daemon.clock import ManualClock
from stockify.daemon.landing import LandingDB
from stockify.daemon.recorder import Recorder
from stockify.data import compact, refdata
from tests.stockify.daemon import packets_fixture as pf

DAY = date(2026, 11, 9)
SEC = 1_000_000_000
EQ = 738_561


def record_day() -> LandingDB:
    landing = LandingDB(DAY)
    clock = ManualClock(timeutil.ist_ns(DAY, "09:15"), 1_000)
    rec = Recorder(landing, {pf.NIFTY_CE: 501, pf.NIFTY_INDEX: 1, EQ: 7}.get, clock)
    rec.begin()
    for i in range(300):
        clock.advance(SEC)
        packets = [pf.full(pf.NIFTY_CE, 12_000 + i), pf.quote(EQ, 75_000 + i, 1, 75_000, 100 + i, 5, 6, 1, 2, 3, 4)]
        if i % 3 == 0:
            packets.append(pf.index_full(pf.NIFTY_INDEX, 2_400_000 + i, 1, 1, 1, 1, i - 150, 1_762_660_000 + i))
        if i == 7:
            packets.append(pf.ltp(EQ, 75_007))
        rec.on_message(None, kp.build_frame(packets), True)
        rec.process(rec.queue.get_nowait())
        if i % 20 == 0:
            rec.on_message(None, b"\x00", True)
            rec.process(rec.queue.get_nowait())
    rec.flush()
    landing.add_gap(timeutil.ist_ns(DAY, "09:18"), timeutil.ist_ns(DAY, "09:18:30"), "disconnect")
    landing.log_subscriptions([(1, "subscribe", 501, pf.NIFTY_CE, "full", "start:chain")])
    landing.close()
    return landing


class CompactTest(unittest.TestCase):
    def setUp(self):
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        support.reset_sqlite_singleton(refdata)
        self.landing = record_day()

    def tearDown(self):
        support.reset_sqlite_singleton(refdata)

    def folder(self):
        return paths.ticks_archive_dir() / f"date={DAY:%Y-%m-%d}"


class ArchiveTests(CompactTest):
    def test_packets_are_split_and_verified(self):
        res = compact.compact(DAY)
        self.assertEqual(res.status, "verified", res.error)
        self.assertEqual((res.counts["full"], res.counts["quote"], res.counts["index"]), (300, 301, 100))
        self.assertEqual(res.packets, 701)
        # heartbeats every 20 s over 5 minutes fill 5 per-minute rows
        self.assertEqual((res.counts["gaps"], res.counts["subscriptions"], res.counts["heartbeats"]),
                         (1, 1, 5))
        names = sorted(p.name for p in self.folder().iterdir())
        self.assertEqual(names, ["full.parquet", "gaps.parquet", "heartbeats.parquet", "index.parquet",
                                 "meta.json", "quote.parquet", "subscriptions.parquet"])

    def test_depth_and_index_change_are_parsed(self):
        compact.compact(DAY)
        full = pq.read_table(self.folder() / "full.parquet").to_pydict()
        self.assertEqual((full["bid_px_1"][0], full["bid_qty_1"][0], full["bid_orders_1"][0]), (11_995, 75, 1))
        self.assertEqual((full["ask_px_1"][0], full["ask_qty_1"][0], full["ask_orders_1"][0]), (12_005, 65, 2))
        idx = pq.read_table(self.folder() / "index.parquet").to_pydict()
        self.assertEqual(idx["change"][0], -150)                       # signed
        self.assertEqual(idx["instrument_token"][0], pf.NIFTY_INDEX)

    def test_rows_are_sorted_by_seq_and_raw_bytes_survive(self):
        compact.compact(DAY)
        conn = LandingDB(DAY, readonly=True).conn
        landing = {r[0]: bytes(r[1]) for r in conn.execute("SELECT seq, raw FROM packets")}
        merged = list(compact._iter_archive(self.folder()))
        self.assertEqual([r[0] for r in merged], sorted(landing))
        self.assertTrue(all(raw == landing[seq] for seq, _, _, raw in merged))

    def test_tick_days_records_the_verified_day(self):
        res = compact.compact(DAY)
        row = refdata._connect().execute("SELECT * FROM tick_days WHERE trade_date=?", (DAY.isoformat(),)).fetchone()
        self.assertEqual((row["status"], row["packets"], row["digest"]), ("verified", 701, res.digest))
        self.assertGreater(row["landing_bytes"], row["archive_bytes"])

    def test_meta_json(self):
        res = compact.compact(DAY)
        meta = json.loads((self.folder() / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["landing_digest"], res.digest)
        self.assertEqual(set(meta["files"]), {"full.parquet", "quote.parquet", "index.parquet", "gaps.parquet",
                                              "subscriptions.parquet", "heartbeats.parquet"})
        self.assertEqual(meta["recorder_meta"]["trade_date"], DAY.isoformat())

    def test_second_run_is_a_no_op_and_force_rebuilds(self):
        first = compact.compact(DAY)
        second = compact.compact(DAY)
        self.assertEqual((second.status, second.digest), ("verified", first.digest))
        self.assertEqual(second.counts, {})                           # skipped, not rebuilt
        third = compact.compact(DAY, force=True)
        self.assertEqual((third.status, third.digest), ("verified", first.digest))

    def test_no_landing_file(self):
        self.assertEqual(compact.compact(date(2026, 11, 11)).status, "no_landing")


class VerificationTests(CompactTest):
    def test_tampered_archive_is_detected(self):
        compact.compact(DAY)
        folder = self.folder()
        table = pq.read_table(folder / "quote.parquet")
        pq.write_table(table.slice(1), folder / "quote.parquet")      # lose one packet
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        with self.assertRaises(compact.CompactionError):
            compact.verify(folder, meta["landing_digest"], meta["packets"])

    def test_failed_verification_keeps_the_landing_and_no_archive(self):
        with patch.object(compact, "verify", side_effect=compact.CompactionError("boom")):
            res = compact.compact(DAY)
        self.assertEqual((res.status, res.error), ("failed", "boom"))
        self.assertFalse(self.folder().exists())
        self.assertTrue(paths.ticks_landing_file(DAY).exists())
        row = refdata._connect().execute("SELECT status FROM tick_days").fetchone()
        self.assertEqual(row["status"], "landing")


class RetentionTests(CompactTest):
    def test_null_retention_never_deletes(self):
        compact.compact(DAY)
        self.assertEqual(compact.apply_retention(date(2030, 1, 1)), [])
        self.assertTrue(paths.ticks_landing_file(DAY).exists())

    def test_set_retention_deletes_only_old_verified_landing_files(self):
        compact.compact(DAY)
        cfg = dict(compact.loader.load_json("storage.json"), retention_days=14)
        with patch.object(compact.loader, "load_json", return_value=cfg):
            self.assertEqual(compact.apply_retention(date(2026, 11, 20)), [])          # only 11 days old
            self.assertEqual(compact.apply_retention(date(2026, 11, 30)), [DAY.isoformat()])
        self.assertFalse(paths.ticks_landing_file(DAY).exists())
        self.assertTrue((self.folder() / "meta.json").exists())                       # archive untouched
        row = refdata._connect().execute("SELECT status FROM tick_days").fetchone()
        self.assertEqual(row["status"], "landing_deleted")

    def test_disk_warnings(self):
        self.assertEqual(compact.disk_warnings(free_bytes=400 * 10**9, total_bytes=1000 * 10**9), [])
        self.assertEqual(len(compact.disk_warnings(free_bytes=5 * 10**9, total_bytes=1000 * 10**9)), 2)


if __name__ == "__main__":
    unittest.main()
