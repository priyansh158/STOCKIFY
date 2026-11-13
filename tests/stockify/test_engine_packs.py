"""M3.2: the C++ pack reader and event sources against the Python pack builder (A1 §5, A4 §1.3).

Packs written by Python (every golden scenario, plus a synthetic pack holding all eight kinds) are read by C++,
decoded into the same records as `stockify.data.packs.read`, re-encoded byte for byte, merged in the oracle's
order and pushed through the live batch source. Skipped when the engine is not built.
"""
from __future__ import annotations

import random
import struct
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden
from stockify.data import packs as P
from stockify.oracle import runner


def _testing():
    return engine.load().testing


def _synthetic_records(seed: int, n: int = 400) -> list[bytes]:
    """Records of all eight kinds with random field values, in pack order with seq 1…N."""
    rng = random.Random(seed)
    events = []
    t = 1_761_000_000_000_000_000
    for i in range(n):
        t += rng.choice([0, 0, 1, 1_000_000, 60_000_000_000])
        iid = rng.randint(1, 50)
        kind = rng.choice(["TICK_QUOTE", "TICK_FULL", "INDEX", "BAR", "CLOCK", "GAP", "REFDATA", "SETTLE"])
        v = [rng.randint(-2**62, 2**62) for _ in range(20)]
        if kind == "TICK_QUOTE":
            rec, rank = P._prefix(P.TICK_QUOTE, 104, iid, t) + P.QUOTE.pack(*v[:10]), 4
        elif kind == "TICK_FULL":
            depth = b"".join(P.LEVEL.pack(rng.randint(0, 10**9), rng.randint(0, 10**7), rng.randint(0, 999), 0)
                             for _ in range(10))
            rec = P._prefix(P.TICK_FULL, 384, iid, t) + P.QUOTE.pack(*v[:10]) + P.FULL_EXTRA.pack(*v[10:15]) + depth
            rank = 4
        elif kind == "INDEX":
            rec, rank = P._prefix(P.INDEX_K, 80, iid, t) + P.INDEX.pack(*v[:7]), 4
        elif kind == "BAR":
            rec, rank = P.enc_bar(iid, t, rng.choice([60, 300, 86400]), t - 60, *v[:6]), 4
        elif kind == "CLOCK":
            rec, rank = P.enc_clock(t), 5
        elif kind == "GAP":
            rec, rank = P.enc_gap(rng.choice([0, iid]), t, rng.randint(0, 1), rng.choice(list(P.GAP_REASONS))), 1
        elif kind == "REFDATA":
            payload = {"instrument_id": iid, "lower_paise": v[0], "note": "₹ band, \"quoted\"", "x": [1, 2.5, None]}
            rec, rank = P.enc_refdata(iid, t, rng.choice(list(P.REFDATA_SUBTYPES)), payload), 2
        else:
            rec, rank = P.enc_settle(iid, t, rng.randint(0, 1), "2026-11-03", v[0]), 3
        events.append(((t, rank, i, iid), rec))
    events.sort(key=lambda x: x[0])
    return [P._with_seq(r, k) for k, (_, r) in enumerate(events, start=1)]


def _write_pack(path: Path, records: list[bytes], basis: str = "raw") -> Path:
    keys = [struct.unpack_from("<q", r, 8)[0] for r in records]
    header = P.HEADER.pack(P.MAGIC, P.VERSION, P.FLAG_CLOCK, 64, len(records), min(keys), max(keys), P.BASIS[basis])
    path.write_bytes(header + b"".join(records))
    return path


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class PackRoundTripTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="engine-packs-")
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _packs(self) -> list[Path]:
        out = [golden.build_pack(g, self.folder) for _, g in golden.goldens()]
        out.append(_write_pack(self.folder / "synthetic_a.evt", _synthetic_records(3201), "adjusted"))
        out.append(_write_pack(self.folder / "synthetic_b.evt", _synthetic_records(3202)))
        return out

    def test_cpp_decodes_what_python_wrote(self):
        t = _testing()
        for path in self._packs():
            with self.subTest(pack=path.name):
                self.assertEqual(t.read_pack(str(path)), P.read(path))

    def test_cpp_re_encodes_byte_for_byte(self):
        t = _testing()
        for path in self._packs():
            with self.subTest(pack=path.name):
                self.assertEqual(t.reencode_pack(str(path)), path.read_bytes())

    def test_merge_order_matches_the_oracle(self):
        t = _testing()
        packs = self._packs()
        rng = random.Random(3203)
        for _ in range(10):
            chosen = rng.sample(packs, rng.randint(1, 4))
            with self.subTest(packs=[p.name for p in chosen]):
                expected = [key for key, _ in runner.load_records(chosen)]
                self.assertEqual(t.merge_keys([str(p) for p in chosen]), expected)

    def test_live_batches_give_the_same_stream(self):
        t = _testing()
        records = _synthetic_records(3204)
        rng = random.Random(3205)
        for _ in range(20):
            cuts = sorted(rng.sample(range(1, len(records)), rng.randint(0, 12)))
            batches, start = [], 0
            for c in cuts + [len(records)]:
                batches.append(b"".join(records[start:c]))
                start = c
            expected = [(struct.unpack_from("<q", r, 8)[0], P.RANK[struct.unpack_from("<H", r)[0]], 0,
                         struct.unpack_from("<Q", r, 16)[0]) for r in records]
            with self.subTest(cuts=cuts):
                self.assertEqual(t.live_keys(batches), expected)

    def test_malformed_packs_fail_on_both(self):
        mod = engine.load()
        good = _synthetic_records(3206, 20)
        cases = {
            "unknown_kind": good[:3] + [struct.pack("<H", 9) + good[3][2:]] + good[4:],
            "bad_size": good[:2] + [good[2][:2] + struct.pack("<H", 99) + good[2][4:]] + good[3:],
            "out_of_order": [good[1], good[0]] + good[2:],
        }
        gap = P.enc_gap(1, 10**18, 0, "stall")
        cases["unknown_gap_reason"] = [gap[:24] + struct.pack("<BBHI", 0, 0, 9, 1)]
        for name, records in cases.items():
            path = _write_pack(self.folder / f"{name}.evt", records)
            with self.subTest(case=name):
                with self.assertRaises(Exception):
                    P.read(path)
                with self.assertRaises(mod.FormatError):
                    mod.testing.read_pack(str(path))
        count = self.folder / "count.evt"
        _write_pack(count, good)
        data = bytearray(count.read_bytes())
        data[16:24] = struct.pack("<Q", len(good) + 1)
        count.write_bytes(bytes(data))
        with self.assertRaisesRegex(mod.FormatError, "header says"):
            mod.testing.read_pack(str(count))


if __name__ == "__main__":
    unittest.main()
