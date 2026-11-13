"""stockify.data.ticks: one reader over archive and landing, identical answers (D-38)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import paths
from stockify.data import compact, refdata, ticks
from tests.stockify.data.test_compact import DAY, EQ, record_day
from tests.stockify.daemon import packets_fixture as pf


class TicksTest(unittest.TestCase):
    def setUp(self):
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        support.reset_sqlite_singleton(refdata)
        record_day()
        p = patch.object(compact, "session_over", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(support.reset_sqlite_singleton, refdata)

    def both(self):
        landing = ticks.open_day(DAY)
        self.assertEqual(landing.source, "landing")
        with patch.object(compact, "ROW_GROUP", 60):
            self.assertEqual(compact.compact(DAY).status, "verified")
        archive = ticks.open_day(DAY)
        self.assertEqual(archive.source, "archive")
        return landing, archive


class IdenticalAnswerTests(TicksTest):
    def test_all_packet_columns_match(self):
        landing, archive = self.both()
        cols = [c for c in ticks.PACKET_COLUMNS if c != "raw"] + ["raw"]
        self.assertEqual(list(archive.packets(cols)), list(landing.packets(cols)))

    def test_frames_and_side_tables_match(self):
        landing, archive = self.both()
        self.assertEqual(list(archive.frames()), list(landing.frames()))
        self.assertEqual(archive.heartbeats(), landing.heartbeats())
        self.assertEqual([(g["reason"], g["start_ns"], g["end_ns"]) for g in archive.gaps()],
                         [(g["reason"], g["start_ns"], g["end_ns"]) for g in landing.gaps()])
        self.assertEqual(len(archive.subscriptions()), len(landing.subscriptions()))
        self.assertEqual(archive.meta()["trade_date"], landing.meta()["trade_date"])
        self.assertEqual(archive.first_recv_ns(), landing.first_recv_ns())


class FilterTests(TicksTest):
    def test_by_instrument(self):
        landing, archive = self.both()
        for day in (landing, archive):
            rows = list(day.packets(["seq", "instrument_id", "ltp"], instrument_ids={7}))
            self.assertEqual(len(rows), 301, day.source)                 # 300 quotes + 1 LTP packet
            self.assertTrue(all(r["instrument_id"] == 7 for r in rows))

    def test_by_kind(self):
        landing, archive = self.both()
        for day in (landing, archive):
            idx = list(day.packets(["seq", "pkt_len", "mode"], kinds=("index",)))
            self.assertEqual(len(idx), 100, day.source)
            self.assertEqual({(r["pkt_len"], r["mode"]) for r in idx}, {(32, 3)})
            seqs = [r["seq"] for r in day.packets(["seq"], kinds=("full", "quote"))]
            self.assertEqual(seqs, sorted(seqs))


class AfterDeletionTests(TicksTest):
    def test_the_day_stays_fully_readable_from_the_archive(self):
        landing, archive = self.both()
        before = list(landing.packets(ticks.PACKET_COLUMNS))
        landing.db.close()
        self.assertEqual(compact.apply_retention(DAY), [DAY.isoformat()])
        self.assertFalse(paths.ticks_landing_file(DAY).exists())
        day = ticks.open_day(DAY)
        self.assertEqual(day.source, "archive")
        self.assertEqual(list(day.packets(ticks.PACKET_COLUMNS)), before)

    def test_available_days_and_missing_days(self):
        self.both()
        self.assertEqual(ticks.available_days(), [DAY])
        from datetime import date
        self.assertIsNone(ticks.open_day(date(2026, 11, 11)))

    def test_replay_after_deletion_uses_the_archive(self):
        landing, _ = self.both()
        landing.db.close()
        compact.apply_retention(DAY)
        from stockify.daemon.replay_ticker import ReplayClock, ReplayTicker, source_for
        seen = []
        ticker = ReplayTicker(source_for(DAY), ReplayClock(0), heartbeats=False)
        ticker.subscribe([pf.NIFTY_CE, EQ, pf.NIFTY_INDEX])
        ticker.set_mode("full", [pf.NIFTY_CE, EQ, pf.NIFTY_INDEX])
        ticker.on_message = lambda ws, payload, binary: seen.append(payload)
        ticker.connect()
        self.assertEqual(len(seen), 300)                                   # every recorded frame


if __name__ == "__main__":
    unittest.main()
