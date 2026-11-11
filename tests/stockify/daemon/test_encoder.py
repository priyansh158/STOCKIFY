"""Frame taps on the recorder and the live frame encoder (DM 5.2; M5.1 fan-out)."""
from __future__ import annotations

import json
import queue
import shutil
import struct
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

from stockify.config import loader
from stockify.daemon import kite_packets as kp
from stockify.daemon.clock import ManualClock
from stockify.daemon.encoder import FrameEncoder
from stockify.daemon.landing import LandingDB
from stockify.daemon.recorder import Recorder
from stockify.daemon.replay_ticker import ReplayClock, ReplayTicker
from stockify.data import packs
from tests.stockify.daemon import packets_fixture as pf
from tests.stockify.daemon.paper_fixture import CE_TOKEN, DAY, FUT_TOKEN, HEARTBEAT, PaperDayTest

TICK_KINDS = (packs.TICK_QUOTE, packs.TICK_FULL, packs.INDEX_K)


class TapTests(PaperDayTest):
    def replay_into(self, rec: Recorder, clock: ReplayClock) -> None:
        ticker = ReplayTicker(LandingDB(DAY, readonly=True), clock)
        ticker.on_message = rec.on_message
        ticker.subscribe([pf.NIFTY_INDEX, FUT_TOKEN, *(CE_TOKEN + i for i in range(12))])
        ticker.set_mode("full", [pf.NIFTY_INDEX, *(CE_TOKEN + i for i in range(12))])
        ticker.run()

    def sink(self) -> Recorder:
        other = LandingDB(DAY + timedelta(days=1))
        self.addCleanup(other.close)
        clock = ReplayClock(0)
        self.clock = clock
        return Recorder(other, lambda token: token, clock)

    def test_tap_sees_identical_stamps_to_landing(self):
        self.record(seconds=50)
        rec = self.sink()
        tapped: queue.Queue = queue.Queue()
        rec.taps.append(tapped.put_nowait)
        self.replay_into(rec, self.clock)
        rec.drain()
        seen = []
        while not tapped.empty():
            frame_no, recv_ns, mono_ns, payload, is_binary = tapped.get_nowait()
            if frame_no:
                seen.append((frame_no, recv_ns))
        rows = rec.landing.conn.execute("SELECT DISTINCT frame_no, recv_ns FROM packets ORDER BY frame_no").fetchall()
        self.assertEqual(len(seen), 50)
        self.assertEqual(seen, [(r["frame_no"], r["recv_ns"]) for r in rows])
        self.assertEqual(rec.stats.dropped_frames, 0)

    def test_heartbeats_are_tapped(self):
        rec = self.sink()
        tapped: list = []
        rec.taps.append(tapped.append)
        self.clock.set(123)
        rec.on_message(None, HEARTBEAT, True)
        self.assertEqual(len(tapped), 1)
        self.assertEqual((tapped[0][0], tapped[0][1], tapped[0][3], tapped[0][4]), (0, 123, HEARTBEAT, True))
        self.assertEqual(rec.queue.qsize(), 1)

    def test_a_failing_tap_never_costs_the_recorder_a_frame(self):
        rec = self.sink()
        tapped: list = []

        def boom(item):  # noqa: ANN001
            raise queue.Full

        rec.taps.extend([boom, tapped.append])
        rec.on_message(None, kp.build_frame([pf.ltp(1, 5)]), True)
        self.assertEqual(rec.queue.qsize(), 1)
        self.assertEqual(len(tapped), 1)
        self.assertEqual(rec.stats.tap_errors, 1)
        self.assertEqual(rec.stats.dropped_frames, 0)


class EncoderTests(PaperDayTest):
    def tokens(self) -> dict[int, int]:
        return {pf.NIFTY_INDEX: self.idx, FUT_TOKEN: self.fut,
                **{CE_TOKEN + i: iid for i, iid in enumerate(self.ces)}}

    def test_encoder_matches_pack_encoder(self):
        self.record(seconds=20)
        sha = packs.build({"dataset": "ticks", "from": DAY.isoformat(), "to": DAY.isoformat()})
        data = packs.read_bytes(packs.pack_path(sha))
        want, off = [], 64
        while off < len(data):
            kind, size = struct.unpack_from("<HH", data, off)
            if kind in TICK_KINDS:
                rec = data[off:off + size]
                want.append(rec[:16] + bytes(8) + rec[24:])       # the pack numbers its records; a live frame has no seq
            off += size
        enc = FrameEncoder(self.tokens())
        got = []
        landing = LandingDB(DAY, readonly=True)
        self.addCleanup(landing.close)
        by_frame: dict[int, list] = {}
        for r in landing.conn.execute("SELECT frame_no, recv_ns, raw FROM packets ORDER BY seq"):
            by_frame.setdefault((r["frame_no"], r["recv_ns"]), []).append(r["raw"])
        for (_, recv_ns), raws in by_frame.items():
            got += [rec for _, rec in enc.encode(kp.build_frame(raws), recv_ns)]
        self.assertEqual(len(want), 20 * 14)
        self.assertEqual(got, want)
        self.assertEqual(enc.stats.ltp_skipped + enc.stats.unknown_tokens, 0)
        iids = [i for i, _ in enc.encode(kp.build_frame([pf.full(CE_TOKEN, 12_000)]), 1)]
        self.assertEqual(iids, [self.ces[0]])

    def test_heartbeats_give_nothing(self):
        enc = FrameEncoder(self.tokens())
        self.assertEqual(enc.encode(HEARTBEAT, 5), [])

    def test_ltp_packets_skipped_and_counted(self):
        enc = FrameEncoder({pf.NIFTY_INDEX: 7, CE_TOKEN: 9})
        out = enc.encode(kp.build_frame([pf.ltp(CE_TOKEN, 100), pf.ltp(777, 5),
                                         pf.full(777, 12_000), pf.full(CE_TOKEN, 12_000)]), 42)
        self.assertEqual([i for i, _ in out], [9])
        self.assertEqual(enc.stats.ltp_skipped, 2)
        self.assertEqual(enc.stats.unknown_tokens, 1)


class PaperConfigTests(unittest.TestCase):
    def test_shipped_values(self):
        with support.isolated_env():
            doc = loader.load_json("paper.json")
        self.assertEqual(doc["clock_every_s"], 1)
        self.assertEqual(doc["journal"], {"fsync_every_s": 5})
        self.assertEqual(doc["dispatcher_queue_max"], 200000)
        self.assertEqual(doc["margin_poll"], {"debounce_s": 2, "every_s": 300, "max_rps": 10})
        self.assertEqual((doc["token_poll_s"], doc["commands_every_s"], doc["sod_state_warn_mb"]), (15, 1, 64))
        self.assertEqual(doc["coverage_threshold"], "0.98")

    def test_bad_values_are_refused(self):
        tmp = Path(tempfile.mkdtemp(dir=str(support.SANDBOX_ROOT)))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        good = json.loads((loader.CONFIG_DIR / "paper.json").read_text(encoding="utf-8"))
        for key, bad in (("clock_every_s", 0), ("journal", {"fsync_every_s": 0}), ("dispatcher_queue_max", -1),
                         ("margin_poll", {"debounce_s": 2, "every_s": 300}), ("token_poll_s", True),
                         ("coverage_threshold", "1.5"), ("coverage_threshold", 0.98),
                         ("commands_every_s", "1"), ("sod_state_warn_mb", 0)):
            with self.subTest(key=key, bad=bad):
                (tmp / "paper.json").write_text(json.dumps({**good, key: bad}), encoding="utf-8")
                with self.assertRaises(loader.ConfigError):
                    loader.load_json("paper.json", tmp)
        for key in good:
            if key != "schema":
                with self.subTest(missing=key):
                    (tmp / "paper.json").write_text(json.dumps({k: v for k, v in good.items() if k != key}),
                                                    encoding="utf-8")
                    with self.assertRaises(loader.ConfigError):
                        loader.load_json("paper.json", tmp)


if __name__ == "__main__":
    unittest.main()
