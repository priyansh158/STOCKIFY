"""stockify.daemon.recorder: queue, batching, seq continuation, gaps (A3 U-13 part 1)."""
from __future__ import annotations

import queue
import unittest
from datetime import date

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import paths
from stockify.daemon import kite_packets as kp
from stockify.daemon.clock import ManualClock
from stockify.daemon.landing import LandingDB
from stockify.daemon.recorder import Recorder
from tests.stockify.daemon import packets_fixture as pf

DAY = date(2026, 11, 9)
T0 = 1_762_660_800_000_000_000          # 2026-11-09 09:30 IST in UTC ns
MS = 1_000_000


class RecorderTest(unittest.TestCase):
    def setUp(self):
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        self.clock = ManualClock(T0, 0)
        self.tokens = {pf.NIFTY_CE: 501, pf.NIFTY_INDEX: 1}
        self.open()

    def tearDown(self):
        self.landing.close()

    def open(self, **kw) -> None:
        self.landing = LandingDB(DAY)
        support.assert_sandboxed(self.landing.path)
        self.rec = Recorder(self.landing, self.tokens.get, self.clock, **kw)
        self.rec.begin()

    def reopen(self, **kw) -> None:
        self.landing.close()
        self.open(**kw)

    def feed(self, *packets: bytes) -> None:
        self.rec.on_message(None, kp.build_frame(list(packets)), True)
        self.rec.process(self.rec.queue.get_nowait())

    def rows(self, sql: str = "SELECT * FROM packets ORDER BY seq"):
        return self.landing.conn.execute(sql).fetchall()


class StorageTests(RecorderTest):
    def test_parsed_fields_and_exact_raw_bytes_are_stored(self):
        raw = pf.full(pf.NIFTY_CE, 12_050)
        self.feed(raw, pf.index_full(pf.NIFTY_INDEX, 2_396_060, 1, 1, 1, 1, 0, 9))
        self.rec.flush()
        r = self.rows()
        self.assertEqual([x["seq"] for x in r], [1, 2])
        self.assertEqual({x["frame_no"] for x in r}, {1})
        self.assertEqual(r[0]["raw"], raw)
        self.assertEqual((r[0]["instrument_id"], r[0]["ltp"], r[0]["mode"]), (501, 12_050, kp.MODE_FULL))
        self.assertEqual((r[1]["instrument_id"], r[1]["pkt_len"]), (1, 32))
        self.assertEqual(r[0]["recv_ns"], T0)

    def test_unknown_token_is_kept_with_null_instrument(self):
        self.feed(pf.ltp(999, 5))
        self.rec.flush()
        self.assertIsNone(self.rows()[0]["instrument_id"])
        self.assertEqual(self.rec.stats.unknown_tokens, 1)

    def test_heartbeats_are_counted_per_minute(self):
        for _ in range(3):
            self.rec.on_message(None, b"\x00", True)
            self.rec.process(self.rec.queue.get_nowait())
        self.rec.flush()
        hb = self.rows("SELECT minute_ns, count FROM heartbeats")
        self.assertEqual([tuple(x) for x in hb], [(T0 // (60 * 10**9) * 60 * 10**9, 3)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM packets")[0][0], 0)

    def test_text_and_bad_frames_do_not_stop_recording(self):
        self.rec.on_message(None, '{"type":"message","data":"hi"}', False)
        self.rec.process(self.rec.queue.get_nowait())
        self.rec.on_message(None, b"\x00\x01\x00\x08\x00", True)        # truncated
        self.rec.process(self.rec.queue.get_nowait())
        self.feed(pf.ltp(pf.NIFTY_CE, 1))
        self.rec.flush()
        self.assertEqual((self.rec.stats.text_frames, self.rec.stats.bad_frames), (1, 1))
        self.assertEqual(len(self.rows()), 1)


class BatchingTests(RecorderTest):
    def test_flush_at_2000_rows(self):
        for _ in range(1999):
            self.feed(pf.ltp(pf.NIFTY_CE, 1))
        self.assertEqual(len(self.rows()), 0)
        self.feed(pf.ltp(pf.NIFTY_CE, 1))
        self.assertEqual(len(self.rows()), 2000)
        self.assertEqual(self.rec.stats.batches, 1)

    def test_flush_after_250_ms(self):
        self.feed(pf.ltp(pf.NIFTY_CE, 1))
        self.clock.advance(249 * MS)
        self.rec.maybe_flush()
        self.assertEqual(len(self.rows()), 0)
        self.clock.advance(1 * MS)
        self.rec.maybe_flush()
        self.assertEqual(len(self.rows()), 1)


class RestartTests(RecorderTest):
    def test_seq_and_frame_numbers_continue_after_restart(self):
        self.feed(pf.ltp(pf.NIFTY_CE, 1), pf.ltp(pf.NIFTY_CE, 2))
        self.rec.flush()
        self.clock.advance(60 * 10**9)
        self.reopen()
        self.feed(pf.ltp(pf.NIFTY_CE, 3))
        self.rec.flush()
        self.assertEqual([(x["seq"], x["frame_no"]) for x in self.rows()], [(1, 1), (2, 1), (3, 2)])

    def test_restart_records_one_process_stop_gap(self):
        self.feed(pf.ltp(pf.NIFTY_CE, 1))
        self.rec.flush()
        last = T0
        self.clock.advance(90 * 10**9)
        self.reopen()
        gaps = self.landing.gaps()
        self.assertEqual(len(gaps), 1)
        self.assertEqual((gaps[0]["reason"], gaps[0]["start_ns"], gaps[0]["end_ns"]),
                         ("process_stop", last, T0 + 90 * 10**9))

    def test_first_start_of_the_day_has_no_gap(self):
        self.assertEqual(self.landing.gaps(), [])
        self.assertEqual(self.landing.meta()["trade_date"], DAY.isoformat())


class QueueTests(RecorderTest):
    def test_full_queue_drops_counts_and_alarms_without_blocking(self):
        self.reopen(queue_max=2)
        for _ in range(3):
            self.rec.on_message(None, kp.build_frame([pf.ltp(pf.NIFTY_CE, 1)]), True)
        self.assertEqual(self.rec.stats.dropped_frames, 1)
        self.assertEqual(self.rec.stats.alarms, ["queue_full"])
        self.assertIsInstance(self.rec.queue, queue.Queue)

    def test_thread_round_trip(self):
        self.rec.start()
        for i in range(10):
            self.rec.on_message(None, kp.build_frame([pf.ltp(pf.NIFTY_CE, i)]), True)
        self.rec.stop()
        self.assertEqual([x["ltp"] for x in self.rows()], list(range(10)))


class PathTests(unittest.TestCase):
    def test_landing_file_name(self):
        self.assertEqual(paths.ticks_landing_file(DAY).name, "ticks_20261109.sqlite")


if __name__ == "__main__":
    unittest.main()
