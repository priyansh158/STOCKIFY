"""stockify.daemon.kite_packets on hand-built frames (A3 U-01)."""
from __future__ import annotations

import unittest

import tests.support  # noqa: F401  — must precede project imports

from stockify.daemon import kite_packets as kp
from tests.stockify.daemon import packets_fixture as pf


class FrameTests(unittest.TestCase):
    def test_heartbeat(self):
        frame = kp.split_frame(b"\x00")
        self.assertTrue(frame.heartbeat)
        self.assertEqual(frame.packets, ())

    def test_split_and_build_round_trip(self):
        packets = [pf.ltp(pf.NIFTY_CE, 12_050), pf.full(pf.NIFTY_CE, 12_050),
                   pf.index_full(pf.NIFTY_INDEX, 2_396_060, 2_400_000, 2_380_000, 2_390_000, 2_385_000, -1234, 1_762_316_101)]
        payload = kp.build_frame(packets)
        self.assertEqual(kp.split_frame(payload).packets, tuple(packets))
        self.assertEqual(kp.build_frame(kp.split_frame(payload).packets), payload)

    def test_the_live_check_frame_size(self):
        # The 2026-11-05 live check received a 36-byte frame: 2 + 2 + one 32-byte index packet.
        payload = kp.build_frame([pf.index_full(pf.NIFTY_INDEX, 1, 1, 1, 1, 1, 0, 1)])
        self.assertEqual(len(payload), 36)

    def test_malformed_frames_raise(self):
        good = kp.build_frame([pf.ltp(1, 2)])
        for bad in (b"", good[:-1], good + b"\x00", b"\x00\x02" + good[2:]):
            with self.subTest(bad=bad), self.assertRaises(kp.PacketError):
                kp.split_frame(bad)


class PacketTests(unittest.TestCase):
    def test_ltp(self):
        p = kp.parse_packet(pf.ltp(pf.NIFTY_CE, 12_050))
        self.assertEqual((p["instrument_token"], p["mode"], p["pkt_len"], p["ltp"]),
                         (pf.NIFTY_CE, kp.MODE_LTP, 8, 12_050))
        self.assertIsNone(p["volume"])

    def test_index_quote_and_full(self):
        q = kp.parse_packet(pf.index_quote(pf.NIFTY_INDEX, 2_396_060, 2_400_000, 2_380_000,
                                           2_390_000, 2_385_000, -1234))
        self.assertEqual((q["mode"], q["ltp"], q["high"], q["low"], q["open"], q["close"]),
                         (kp.MODE_QUOTE, 2_396_060, 2_400_000, 2_380_000, 2_390_000, 2_385_000))
        self.assertIsNone(q["exch_ts_s"])
        f = pf.index_full(pf.NIFTY_INDEX, 2_396_060, 2_400_000, 2_380_000, 2_390_000, 2_385_000,
                          -1234, 1_762_316_101)
        self.assertEqual(kp.parse_packet(f)["exch_ts_s"], 1_762_316_101)
        self.assertEqual(kp.index_change(f), -1234)          # signed, unlike the SDK's unsigned read
        self.assertTrue(kp.is_index_token(pf.NIFTY_INDEX))
        self.assertFalse(kp.is_index_token(pf.NIFTY_CE))

    def test_quote(self):
        p = kp.parse_packet(pf.quote(pf.NIFTY_CE, 12_050, 75, 12_000, 1_000_000, 10, 20, 11_000, 12_500, 10_900, 11_800))
        self.assertEqual((p["mode"], p["last_qty"], p["atp"], p["volume"], p["buy_qty"], p["sell_qty"],
                          p["open"], p["high"], p["low"], p["close"]),
                         (kp.MODE_QUOTE, 75, 12_000, 1_000_000, 10, 20, 11_000, 12_500, 10_900, 11_800))
        self.assertIsNone(p["exch_ts_s"])

    def test_full_and_depth(self):
        raw = pf.full(pf.NIFTY_CE, 12_050, vol=5_000, ltt=1_762_316_100, oi=40_000, exch_ts=1_762_316_101)
        self.assertEqual(len(raw), 184)
        p = kp.parse_packet(raw)
        self.assertEqual((p["mode"], p["volume"], p["last_trade_ts_s"], p["oi"], p["oi_high"],
                          p["oi_low"], p["exch_ts_s"]),
                         (kp.MODE_FULL, 5_000, 1_762_316_100, 40_000, 40_500, 39_500, 1_762_316_101))
        depth = kp.parse_depth(raw)
        self.assertEqual(depth[0], (75, 12_045, 1))          # best bid
        self.assertEqual(depth[5], (65, 12_055, 2))          # best ask
        self.assertEqual(len(depth), 10)

    def test_unknown_length(self):
        with self.assertRaises(kp.PacketError):
            kp.parse_packet(b"\x00" * 12)
        with self.assertRaises(kp.PacketError):
            kp.parse_depth(pf.ltp(1, 2))


if __name__ == "__main__":
    unittest.main()
