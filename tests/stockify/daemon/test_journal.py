"""DM 5.1 (M5.3, D-68): the session journal is append-only, crash-safe, and always accepted by the engine."""
from __future__ import annotations

import itertools
import struct
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.daemon import paper_fixture as pf

from stockify import bundle as bundle_mod, engine, paths
from stockify.daemon import journal
from stockify.data import packs
from stockify.strategies._script import Script

CLOCK_RANK = packs.RANK[packs.CLOCK]
SEC = 1_000_000_000


def clock_records(n: int, t0: int = 1_000) -> list[tuple[int, int, bytes]]:
    return [(t0 + i, CLOCK_RANK, packs.enc_clock(0)) for i in range(n)]


def writer(path: Path, **kw) -> journal.JournalWriter:
    ticks = itertools.count(0, 10)
    return journal.JournalWriter(path, fsync_every_mono_ns=kw.pop("fsync_every_mono_ns", 1_000),
                                 mono=kw.pop("mono", lambda: next(ticks)), **kw)


def recrc(path: Path) -> None:
    """Rewrite the checksum sidecar to match the journal's bytes (to build a corruption the checksums do not catch)."""
    import zlib
    data = path.read_bytes()
    out, off = b"", 64
    while off + 4 <= len(data):
        size = struct.unpack_from("<H", data, off + 2)[0]
        out += struct.pack("<I", zlib.crc32(data[off:off + size]))
        off += size
    Path(str(path) + ".crc").write_bytes(out)


class OrderedTTests(unittest.TestCase):
    def test_lower_rank_at_same_t_is_bumped_one_ns(self):
        self.assertEqual(journal.ordered_t(100, 2, 100, 4), 101)
        self.assertEqual(journal.ordered_t(100, 4, 100, 4), 100)
        self.assertEqual(journal.ordered_t(90, 5, 100, 4), 100)

    def test_the_rest_of_the_ordering(self):
        self.assertEqual(journal.ordered_t(7, 1, None, None), 7)               # the first record is as intended
        self.assertEqual(journal.ordered_t(150, 1, 100, 9), 150)               # later intended time wins
        self.assertEqual(journal.ordered_t(90, 1, 100, 4), 101)                # late and lower rank: bumped
        self.assertEqual(journal.ordered_t(100, 5, 100, 4), 100)               # higher rank: no bump needed

    def test_a_sequence_is_never_out_of_order(self):
        import random
        rng = random.Random(68)
        last_t = last_rank = None
        for _ in range(2000):
            rank, intended = rng.randint(0, 10), rng.randint(0, 40)
            t = journal.ordered_t(intended, rank, last_t, last_rank)
            if last_t is not None:
                self.assertGreaterEqual((t, rank), (last_t, last_rank))
            last_t, last_rank = t, rank


class PathTests(unittest.TestCase):
    def test_names(self):
        d = date(2026, 11, 17)
        base = paths.journals_dir("s1")
        self.assertEqual(paths.journal_file("s1", d), base / "20261117.evt")
        self.assertEqual(paths.day_bundle_file("s1", d), base / "20261117.bundle.json")
        day = paths.journal_day_dir("s1", d)
        self.assertEqual(day, base / "20261117")
        self.assertTrue(day.is_dir())

    def test_unsafe_session_ids_are_refused_by_all_three(self):
        d = date(2026, 11, 17)
        for fn in (paths.journal_file, paths.day_bundle_file, paths.journal_day_dir):
            for bad in ("..", "a/b", "a\\b", "CON", "", "x" * 129, "C:x"):
                with self.subTest(fn=fn.__name__, bad=bad), self.assertRaises(ValueError):
                    fn(bad, d)


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="journal-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "20261117.evt"

    def test_a_new_journal_is_a_header_and_appended_bytes_are_returned(self):
        w = writer(self.path)
        self.assertEqual((w.records, w.next_seq, w.last_t, w.last_rank), (0, 1, None, None))
        out = w.append(clock_records(2))
        self.assertEqual(len(out), 48)
        self.assertEqual((w.records, w.next_seq, w.last_t, w.last_rank), (2, 3, 1_001, CLOCK_RANK))
        w.close(finalise=False)
        header, body, torn = journal.read(self.path)
        self.assertEqual((header["count"], header["flags"] & 1, torn), (0, 1, 0))     # open: count 0, bit 0 = journal
        self.assertEqual(body, out)
        self.assertEqual([r["seq"] for r in (packs.decode(body, o)[0] for o in (0, 24))], [1, 2])

    def test_t_and_seq_are_set_by_the_writer(self):
        w = writer(self.path)
        w.append([(500, 5, packs.enc_clock(999)), (400, 5, packs.enc_clock(999))])     # the second is late: held at 500
        w.close(finalise=False)
        _, body, _ = journal.read(self.path)
        recs = [packs.decode(body, o)[0] for o in (0, 24)]
        self.assertEqual([(r["t_event"], r["seq"]) for r in recs], [(500, 1), (500, 2)])

    def test_bad_records_are_refused_whole_before_anything_is_written(self):
        w = writer(self.path)
        w.append(clock_records(1))
        size = self.path.stat().st_size
        gap = packs.enc_gap(0, 5, 0, "disconnect")
        bad_batches = [[(1, CLOCK_RANK, packs.enc_clock(0)), (1, CLOCK_RANK, b"\x05\x00")],          # short
                       [(1, CLOCK_RANK, packs.enc_clock(0)), (1, 4, packs.enc_clock(0))],            # rank is not the kind's
                       [(1, CLOCK_RANK, packs.enc_clock(0)), (1, 1, gap + b"x")],                    # trailing byte
                       [(1, CLOCK_RANK, packs.enc_clock(0)), (1, CLOCK_RANK, struct.pack("<HHIqQ", 99, 24, 0, 0, 0))]]
        for batch in bad_batches:
            with self.assertRaises(journal.JournalError):
                w.append(batch)
        w.close(finalise=False)
        self.assertEqual(self.path.stat().st_size, size)
        self.assertEqual(w.records, 1)

    def test_torn_tail_is_ignored_and_writer_continues(self):
        w = writer(self.path)
        w.append(clock_records(10))
        w.close(finalise=False)
        whole = self.path.read_bytes()
        self.path.write_bytes(whole[:-7])
        header, body, torn = journal.read(self.path)
        self.assertEqual((len(body) // 24, torn), (9, 17))
        self.assertEqual(body, whole[64:-24])
        # the reader never changes the file
        self.assertEqual(self.path.read_bytes(), whole[:-7])
        w = writer(self.path)
        self.assertEqual((w.records, w.next_seq, w.last_t, w.torn_bytes), (9, 10, 1_008, 17))
        w.append(clock_records(1, t0=2_000))
        w.close(finalise=False)
        _, body, torn = journal.read(self.path)
        self.assertEqual((len(body) // 24, torn), (10, 0))
        self.assertEqual(packs.decode(body, 9 * 24)[0]["seq"], 10)
        self.assertEqual(body[:9 * 24], whole[64:-24])                  # the records before the tear are untouched
        # the torn bytes are kept beside the journal: a record of truth is never silently dropped
        self.assertEqual(journal.torn_entries(self.path), [(64 + 9 * 24, whole[-24:-7])])

    def test_every_cut_of_the_last_record_is_a_torn_tail(self):
        w = writer(self.path)
        w.append([(1, 5, packs.enc_clock(0)), (2, 2, packs.enc_refdata(7, 2, "strategy_control",
                                                                       {"enabled": False, "reasons": ["owner"]}))])
        w.close(finalise=False)
        whole = self.path.read_bytes()
        for cut in range(1, len(whole) - 64 - 24 + 1):                    # every cut of the REFDATA record
            with self.subTest(cut=cut):
                self.path.write_bytes(whole[:len(whole) - cut])
                _, body, torn = journal.read(self.path)
                self.assertEqual((len(body), torn), (24, len(whole) - cut - 64 - 24))

    def test_a_zero_filled_tail_is_torn_not_corrupt(self):
        w = writer(self.path)
        w.append(clock_records(3))
        w.close(finalise=False)
        with self.path.open("ab") as f:
            f.write(bytes(40))                  # a crash that extended the file but wrote no data
        _, body, torn = journal.read(self.path)
        self.assertEqual((len(body), torn), (72, 40))

    def test_corruption_inside_the_file_is_an_error_not_a_torn_tail(self):
        w = writer(self.path)
        w.append(clock_records(3))
        w.close(finalise=False)
        good = self.path.read_bytes()
        bad_kind = bytearray(good)
        struct.pack_into("<H", bad_kind, 64 + 24, 99)
        bad_seq = bytearray(good)
        struct.pack_into("<Q", bad_seq, 64 + 24 + 16, 7)
        bad_t = bytearray(good)
        struct.pack_into("<q", bad_t, 64 + 48 + 8, 1)                       # third record goes back in time
        for name, data in (("kind", bad_kind), ("seq", bad_seq), ("order", bad_t)):
            with self.subTest(name):
                self.path.write_bytes(bytes(data))
                recrc(self.path)                                            # only the structure is wrong
                with self.assertRaises(journal.JournalError):
                    journal.read(self.path)
                with self.assertRaises(journal.JournalError):
                    writer(self.path)
                self.assertEqual(self.path.read_bytes(), bytes(data))        # and nothing was changed

    def test_a_torn_header_or_foreign_file_is_never_overwritten(self):
        for data in (b"x" * 80, b"x" * 10, bytes(64) + b"\x01"):
            with self.subTest(len=len(data)):
                self.path.write_bytes(data)
                with self.assertRaises(journal.JournalError):
                    journal.read(self.path)
                with self.assertRaises(journal.JournalError):
                    writer(self.path)
                self.assertEqual(self.path.read_bytes(), data)

    def test_an_empty_file_gets_its_header(self):
        self.path.write_bytes(b"")
        w = writer(self.path)
        w.close(finalise=False)
        self.assertEqual((self.path.stat().st_size, journal.read(self.path)[2]), (64, 0))

    def test_finalise_sets_count_and_bounds(self):
        w = writer(self.path, price_basis="raw")
        w.append(clock_records(4, t0=3_000))
        w.append([(10, 5, packs.enc_clock(0))])                # held at the last t, 3_003
        w.close(finalise=True)
        header, body, torn = journal.read(self.path)
        self.assertEqual((header["count"], header["t_min"], header["t_max"], header["price_basis"], torn),
                         (5, 3_000, 3_003, "raw", 0))
        self.assertTrue(header["flags"] & 1 and header["flags"] & packs.FLAG_CLOCK)
        self.assertEqual(len(body), 5 * 24)
        w.close(finalise=True)                                  # closing twice is harmless
        with self.assertRaises(journal.JournalError):
            w.append(clock_records(1))

    def test_a_finalised_journal_takes_no_more_records(self):
        w = writer(self.path)
        w.append(clock_records(2))
        w.close(finalise=True)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(journal.JournalError, "finalised"):
            writer(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_reopening_with_another_price_basis_is_refused(self):
        w = writer(self.path, price_basis="raw")
        w.close(finalise=False)
        with self.assertRaises(journal.JournalError):
            writer(self.path, price_basis="adjusted")

    def test_fsync_follows_the_monotonic_clock_and_nothing_else_is_written(self):
        now = [0]
        with patch.object(journal.os, "fsync") as fsync:
            w = journal.JournalWriter(self.path, fsync_every_mono_ns=100, mono=lambda: now[0])
            base = fsync.call_count                              # the header's own sync
            now[0] = 99
            w.append(clock_records(1))
            self.assertEqual(fsync.call_count, base)
            now[0] = 100
            w.append(clock_records(1, t0=5_000))
            self.assertEqual(fsync.call_count, base + 2)
            now[0] = 150
            w.append(clock_records(1, t0=6_000))
            self.assertEqual(fsync.call_count, base + 2)
            now[0] = 160
            w.fsync()
            self.assertEqual(fsync.call_count, base + 4)
            now[0] = 259
            w.append(clock_records(1, t0=7_000))
            self.assertEqual(fsync.call_count, base + 4)         # an explicit fsync restarts the interval
            w.close(finalise=False)
        # the monotonic clock is never written into a record: the file holds only t values we gave
        _, body, _ = journal.read(self.path)
        self.assertEqual([packs.decode(body, o)[0]["t_event"] for o in range(0, len(body), 24)],
                         [1_000, 5_000, 6_000, 7_000])

    # ── DM 5.2 (review of DM 5.1) ────────────────────────────────────────────

    def test_finalise_syncs_the_records_before_the_header_is_written(self):
        w = writer(self.path)
        w.append(clock_records(4))
        calls = []
        real = journal.os.fsync

        def failing(fd):
            real(fd)
            calls.append(fd)
            if len(calls) == 1:
                raise OSError("power cut between the record sync and the header write")
        with patch.object(journal.os, "fsync", failing):
            with self.assertRaises(OSError):
                w.close(finalise=True)
        self.assertEqual(len(calls), 1)
        header, body, torn = journal.read(self.path)
        self.assertEqual((header["count"], len(body), torn), (0, 96, 0))        # unfinalised, every record intact
        self.assertFalse(journal.is_finalised(self.path))
        w = writer(self.path)                                                   # and the day can go on
        self.assertEqual(w.records, 4)
        w.close(finalise=False)

    def test_is_finalised(self):
        w = writer(self.path)
        self.assertFalse(journal.is_finalised(self.path))
        w.append(clock_records(1))
        w.close(finalise=False)
        self.assertFalse(journal.is_finalised(self.path))
        empty = self.path.with_name("empty.evt")
        e = writer(empty)
        e.close(finalise=True)                                                  # a day with no record is still done
        self.assertTrue(journal.is_finalised(empty))
        self.assertEqual(journal.read(empty)[1:], (b"", 0))
        w = writer(self.path)
        w.close(finalise=True)
        self.assertTrue(journal.is_finalised(self.path))
        with self.assertRaises(journal.JournalError):
            journal.is_finalised(self.path.with_name("missing.evt"))

    def torn_cases(self):
        """(name, rank, a record, how its last bytes look when the prefix landed but the body did not)."""
        from tests.stockify.daemon import packets_fixture as pfx

        def zero_tail(n):
            return lambda b: b[:-n] + bytes(n)
        quote = packs.enc_tick({"raw": pfx.quote(7, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10)}, 7, 1)[1]
        return [("gap", packs.RANK[packs.GAP_K], packs.enc_gap(3, 5, 0, "disconnect"), zero_tail(8)),
                ("clock", CLOCK_RANK, packs.enc_clock(0), lambda b: b[:8] + bytes(16)),      # t and seq not landed
                ("tick", packs.RANK[packs.TICK_QUOTE], quote, lambda b: b[:24] + bytes(len(b) - 24)),
                ("tick half", packs.RANK[packs.TICK_QUOTE], quote, zero_tail(40))]

    def test_a_prefix_without_its_body_is_a_torn_tail(self):
        for name, rank, rec, damage in self.torn_cases():
            with self.subTest(name):
                for f in self.path.parent.iterdir():
                    f.unlink()
                w = writer(self.path)
                w.append(clock_records(3))
                w.append([(5_000, rank, rec)])
                w.close(finalise=False)
                whole = self.path.read_bytes()
                size = len(rec)
                self.path.write_bytes(whole[:-size] + damage(whole[-size:]))
                header, body, torn = journal.read(self.path)
                self.assertEqual((len(body), torn), (72, size))
                w = writer(self.path)
                self.assertEqual((w.records, w.next_seq, w.torn_bytes), (3, 4, size))
                w.append(clock_records(1, t0=6_000))
                w.close(finalise=False)
                _, body, torn = journal.read(self.path)
                self.assertEqual((len(body), torn), (96, 0))
                self.assertEqual([(o, len(b)) for o, b in journal.torn_entries(self.path)], [(64 + 72, size)])

    def test_several_damaged_records_at_the_end_are_one_torn_tail(self):
        w = writer(self.path)
        w.append(clock_records(5))
        w.close(finalise=False)
        whole = self.path.read_bytes()
        self.path.write_bytes(whole[:64 + 3 * 24 + 8] + bytes(16) + whole[64 + 4 * 24:64 + 4 * 24 + 8] + bytes(16))
        _, body, torn = journal.read(self.path)
        self.assertEqual((len(body), torn), (72, 48))

    def test_a_damaged_record_with_whole_ones_after_it_is_corruption(self):
        w = writer(self.path)
        w.append(clock_records(4))
        w.close(finalise=False)
        whole = bytearray(self.path.read_bytes())
        whole[64 + 24 + 8:64 + 48] = bytes(16)                          # record 2 loses t and seq, 3 and 4 are fine
        self.path.write_bytes(bytes(whole))
        with self.assertRaises(journal.JournalError):
            journal.read(self.path)
        with self.assertRaises(journal.JournalError):
            writer(self.path)

    def test_a_bad_record_size_in_the_middle_is_corruption(self):
        w = writer(self.path)
        w.append([(1, 1, packs.enc_gap(0, 1, 0, "disconnect")), (2, 5, packs.enc_clock(0)), (3, 5, packs.enc_clock(0))])
        w.close(finalise=False)
        whole = bytearray(self.path.read_bytes())
        struct.pack_into("<H", whole, 64 + 2, 31)                       # the first record claims 31 bytes, GAP is 32
        self.path.write_bytes(bytes(whole))
        recrc(self.path)
        with self.assertRaisesRegex(journal.JournalError, "corrupt record at byte 64"):
            journal.read(self.path)
        with self.assertRaises(journal.JournalError):
            writer(self.path)
        self.assertEqual(self.path.read_bytes(), bytes(whole))

    def test_without_its_checksum_file_an_open_journal_is_not_trusted(self):
        w = writer(self.path)
        w.append(clock_records(2))
        w.close(finalise=False)
        Path(str(self.path) + ".crc").unlink()
        with self.assertRaisesRegex(journal.JournalError, "checksum"):
            journal.read(self.path)
        done = self.path.with_name("b.evt")
        w = writer(done)
        w.append(clock_records(2))
        w.close(finalise=True)
        Path(str(done) + ".crc").unlink()
        self.assertEqual(len(journal.read(done)[1]), 48)                # finalised: the header's count is the check

    def test_a_cut_short_write_fails_the_writer_until_it_is_reopened(self):
        w = writer(self.path)
        w.append(clock_records(2))

        class Cut:
            def __init__(self, real):
                self.real = real

            def write(self, data):
                self.real.write(data[:len(data) // 2])       # part of the batch reaches the file, then the disk is full
                self.real.flush()
                raise OSError(28, "No space left on device")

            def __getattr__(self, name):
                return getattr(self.real, name)
        w._f = Cut(w._f)
        with self.assertRaises(journal.JournalError):
            w.append(clock_records(3, t0=5_000))
        with self.assertRaisesRegex(journal.JournalError, "failed"):
            w.append(clock_records(1, t0=6_000))
        with self.assertRaises(journal.JournalError):
            w.close(finalise=True)                           # a failed writer never finalises
        self.assertFalse(journal.is_finalised(self.path))
        w = writer(self.path)                                # reopening repairs the tail as for a torn record
        self.assertEqual((w.records, w.torn_bytes), (3, 12))  # 36 bytes: 1 whole record of the batch, then 12 torn
        w.append(clock_records(1, t0=7_000))
        w.close(finalise=True)
        header, recs = packs.read(self.path)
        self.assertEqual((header["count"], [r["seq"] for r in recs]), (4, [1, 2, 3, 4]))

    def test_a_crash_during_creation_leaves_nothing_worth_keeping(self):
        header = packs.HEADER.pack(packs.MAGIC, 1, 1, 64, 0, 0, 0, 0)
        for name, data in (("one byte", b"S"), ("magic only", b"STKEVT01"), ("63 bytes", header[:63]),
                           ("zero header", bytes(64)), ("zero 17", bytes(17))):
            with self.subTest(name):
                for f in self.path.parent.iterdir():
                    f.unlink()
                self.path.write_bytes(data)
                with patch.object(journal, "log") as log:        # the suite disables logging, so look at the call
                    w = writer(self.path)
                self.assertEqual(log.warning.call_count, 1)
                w.append(clock_records(1))
                w.close(finalise=True)
                self.assertEqual(len(packs.read(self.path)[1]), 1)
                self.assertEqual(journal.torn_entries(self.path), [(0, data)])    # the bad bytes are kept

    def test_the_torn_sidecar_has_no_duplicate_after_a_crash_between_copy_and_cut(self):
        w = writer(self.path)
        w.append(clock_records(4))
        w.close(finalise=False)
        whole = self.path.read_bytes()
        self.path.write_bytes(whole[:-5])
        with patch.object(journal.JournalWriter, "_cut", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                writer(self.path)
        self.assertEqual(len(journal.torn_entries(self.path)), 1)
        w = writer(self.path)                                           # the next start finds the same 19 bytes
        w.close(finalise=False)
        self.assertEqual(journal.torn_entries(self.path), [(64 + 3 * 24, whole[64 + 72:-5])])
        self.assertTrue(Path(str(self.path) + ".torn").exists())

    # ── DM 5.3 (re-review of DM 5.2) ─────────────────────────────────────────

    def sync_log(self, w):
        """Patch os.fsync to log which of the writer's two files each call syncs ('crc' or 'journal')."""
        names = {w._c.fileno(): "crc", w._f.fileno(): "journal"}
        events = []
        real = journal.os.fsync

        def logging_fsync(fd):
            real(fd)
            events.append(names.get(fd, "other"))
        return events, patch.object(journal.os, "fsync", logging_fsync)

    def test_the_checksum_file_is_synced_before_the_journal(self):
        w = writer(self.path)
        w.append(clock_records(2))
        events, p = self.sync_log(w)
        with p:
            w.fsync()
        self.assertEqual(events, ["crc", "journal"])
        w.close(finalise=False)

    def test_a_failure_between_the_two_syncs_fails_the_writer_and_loses_nothing(self):
        w = writer(self.path)
        w.append(clock_records(2))
        real = journal.os.fsync
        calls = []

        def failing(fd):
            calls.append(fd)
            if len(calls) == 2:                     # the checksums were synced, the journal's sync never happens
                raise OSError("power cut")
            real(fd)
        with patch.object(journal.os, "fsync", failing):
            with self.assertRaises(journal.JournalError):
                w.fsync()
        self.assertEqual(calls[0], w._c.fileno())
        with self.assertRaisesRegex(journal.JournalError, "failed"):
            w.append(clock_records(1, t0=9_000))
        w.close(finalise=False)
        self.assertEqual((len(journal.read(self.path)[1]), journal.read(self.path)[2]), (48, 0))

    def test_finalise_syncs_checksums_then_records_then_writes_the_header_then_syncs(self):
        w = writer(self.path)
        w.append(clock_records(2))
        events, p = self.sync_log(w)
        real_header = w._header

        def header(*a, **k):
            events.append("header")
            return real_header(*a, **k)
        with p, patch.object(w, "_header", header):
            w.close(finalise=True)
        self.assertEqual(events, ["crc", "journal", "header", "journal"])

    def test_a_sidecar_one_entry_ahead_is_harmless_and_is_cut_back(self):
        w = writer(self.path)
        w.append(clock_records(4))
        w.close(finalise=False)
        crc = Path(str(self.path) + ".crc")
        crc.write_bytes(crc.read_bytes() + struct.pack("<I", 0xDEADBEEF))      # the entry of a record that never landed
        _, body, torn = journal.read(self.path)
        self.assertEqual((len(body), torn), (96, 0))
        w = writer(self.path)
        self.assertEqual((w.records, w.torn_bytes), (4, 0))
        w.append(clock_records(1, t0=9_000))                                     # the stale entry cannot match this one
        w.close(finalise=False)
        self.assertEqual((len(journal.read(self.path)[1]), journal.read(self.path)[2]), (120, 0))
        self.assertEqual(crc.stat().st_size, 20)

    def test_a_torn_last_checksum_entry_makes_the_last_record_unverified_and_keeps_it(self):
        w = writer(self.path)
        w.append(clock_records(4))
        w.close(finalise=False)
        crc = Path(str(self.path) + ".crc")
        whole = self.path.read_bytes()
        crc.write_bytes(crc.read_bytes()[:-2])
        _, body, torn = journal.read(self.path)
        self.assertEqual((len(body), torn), (72, 24))
        with patch.object(journal, "log") as log:
            w = writer(self.path)
        self.assertEqual((w.records, w.torn_bytes), (3, 24))
        self.assertEqual(journal.torn_entries(self.path), [(64 + 72, whole[-24:])])    # the record is not lost
        self.assertTrue(log.warning.called)
        w.close(finalise=False)

    def test_a_short_sidecar_is_loud_and_adopt_rebuilds_it_without_touching_the_journal(self):
        w = writer(self.path)
        w.append(clock_records(4))
        w.close(finalise=False)
        crc = Path(str(self.path) + ".crc")
        before = self.path.read_bytes()
        for name, keep in (("short", 8), ("empty", 0), ("missing", None)):
            with self.subTest(name):
                self.path.write_bytes(before)
                if keep is None:
                    crc.unlink()
                else:
                    recrc(self.path)
                    crc.write_bytes(crc.read_bytes()[:keep])
                for f in self.path.parent.glob("*.torn"):
                    f.unlink()
                if keep is not None:
                    with patch.object(journal, "log") as log:           # opening names the counts and moves nothing silently
                        w = writer(self.path)
                    msg = " ".join(str(a) for a in log.warning.call_args[0])
                    self.assertIn("adopt", msg)
                    self.assertEqual(w.records, keep // 4)
                    w.close(finalise=False)
                    self.path.write_bytes(before)
                    recrc(self.path)
                    crc.write_bytes(crc.read_bytes()[:keep])
                n = journal.adopt(self.path)
                self.assertEqual(n, 4)
                self.assertEqual(self.path.read_bytes(), before)           # the journal bytes are never touched
                self.assertEqual(len(journal.read(self.path)[1]), 96)
                w = writer(self.path)
                self.assertEqual((w.records, w.torn_bytes), (4, 0))
                w.close(finalise=False)

    def test_adopt_refuses_a_journal_that_is_not_whole_records(self):
        w = writer(self.path)
        w.append(clock_records(3))
        w.close(finalise=False)
        data = bytearray(self.path.read_bytes())
        struct.pack_into("<Q", data, 64 + 24 + 16, 9)                       # record 2 has seq 9
        self.path.write_bytes(bytes(data))
        with self.assertRaises(journal.JournalError):
            journal.adopt(self.path)
        self.assertEqual(self.path.read_bytes(), bytes(data))

    def test_a_partial_torn_entry_does_not_hide_the_ones_after_it(self):
        sidecar = Path(str(self.path) + ".torn")
        a = journal.TORN_HEAD.pack(journal.TORN_MAGIC, 100, 5, __import__("zlib").crc32(b"AAAAA")) + b"AAAAA"
        b = journal.TORN_HEAD.pack(journal.TORN_MAGIC, 200, 9, __import__("zlib").crc32(b"BBBBBBBBB")) + b"BBBBBBBBB"
        sidecar.write_bytes(a + b[:-4])                                    # the crash cut the second entry short
        w = writer(self.path)
        w.append(clock_records(3))
        w.close(finalise=False)
        self.path.write_bytes(self.path.read_bytes()[:-5])
        w = writer(self.path)                                              # appends a third entry after the partial one
        w.close(finalise=False)
        entries, partial = journal.torn_entries(self.path, with_partial=True)
        self.assertEqual([e[1] for e in entries][0], b"AAAAA")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[1][0], 64 + 48)
        self.assertEqual(len(partial), 1)
        self.assertEqual(partial[0][1], b[:-4])
        self.assertEqual(journal.torn_entries(self.path), entries)

    def test_an_fsync_failure_inside_append_fails_the_writer_but_the_records_are_in_the_file(self):
        w = writer(self.path, fsync_every_mono_ns=0)
        with patch.object(journal.os, "fsync", side_effect=OSError("disk gone")):
            with self.assertRaises(journal.JournalError):
                w.append(clock_records(2))                     # written and flushed, then the sync failed
        with self.assertRaisesRegex(journal.JournalError, "failed"):
            w.append(clock_records(1, t0=9_000))
        w.close(finalise=False)
        w = writer(self.path)                                  # the caller reopens and replays from the journal
        self.assertEqual(w.records, 2)
        w.close(finalise=False)
        self.assertIn("reopen", journal.JournalWriter.append.__doc__)

    def test_a_finalised_journal_must_hold_exactly_the_records_its_header_counts(self):
        w = writer(self.path)
        w.append(clock_records(4))
        w.close(finalise=True)
        whole = self.path.read_bytes()
        Path(str(self.path) + ".crc").unlink()
        self.path.write_bytes(whole[:-24])                               # a whole record gone: no torn bytes to notice
        with self.assertRaisesRegex(journal.JournalError, "count"):
            journal.read(self.path)
        self.path.write_bytes(whole)
        self.assertEqual(len(journal.read(self.path)[1]), 96)


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class DayJournalTests(pf.PaperDayTest):
    """A landed day's records through the writer, in a live engine."""

    def setUp(self):
        super().setUp()
        self.record(seconds=120)
        self.sha = packs.build({"dataset": "ticks", "from": pf.EXPIRY, "to": pf.EXPIRY, "clock_cadence_ms": 5000})
        data = packs.pack_path(self.sha).read_bytes()
        self.records, off = [], 64
        while off < len(data):
            size = struct.unpack_from("<H", data, off + 2)[0]
            kind, _, _, t, _ = packs.PREFIX.unpack_from(data, off)
            self.records.append((t, packs.RANK[kind], data[off:off + size]))
            off += size
        tmp = tempfile.TemporaryDirectory(prefix="journal-day-")
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    def make_engine(self, name: str):
        folder = self.folder / name
        folder.mkdir()
        b = bundle_mod.resolve_session_day("s1", self.session_cfg(), pf.DAY, None)
        bundle_mod.write(b, folder)
        e = engine.load().Engine(str(folder / "bundle.json"))
        placed = [{"on_event": 60, "place": {"instrument": self.ces[4], "side": "BUY", "qty": 65, "product": "NRML",
                                             "type": "MARKET"}},
                  {"on_event": 400, "place": {"instrument": self.ces[4], "side": "SELL", "qty": 65, "product": "NRML",
                                              "type": "MARKET"}}]
        e.set_strategy_py(Script(placed))
        return folder, e

    def run_engine(self, name: str, batch: int | None):
        folder, e = self.make_engine(name)
        w = journal.JournalWriter(folder / "day.evt", fsync_every_mono_ns=10 ** 9, mono=iter(itertools.count()).__next__)
        records = self.records
        step = batch or len(records)
        for i in range(0, len(records), step):
            e.push(w.append(records[i:i + step]))
        e.finish()
        out = e.drain_outputs()
        state = e.eod_state()
        w.close(finalise=True)
        return out, state, (folder / "day.evt").read_bytes()

    def test_replaying_the_finalised_journal_gives_the_live_run(self):
        out, state, data = self.run_engine("live", 5)
        _, e = self.make_engine("replay")
        e.push(data[64:])
        e.finish()
        self.assertEqual(e.drain_outputs(), out)
        self.assertEqual(e.eod_state(), state)
        _, e2 = self.make_engine("replay2")             # and record by record
        off = 64
        while off < len(data):
            size = struct.unpack_from("<H", data, off + 2)[0]
            e2.push(data[off:off + size])
            off += size
        e2.finish()
        self.assertEqual(e2.drain_outputs(), out)
        self.assertEqual(e2.eod_state(), state)

    def test_appended_bytes_are_accepted_by_the_engine_in_any_batching(self):
        out1, state1, bytes1 = self.run_engine("one", 1)
        out3, state3, bytes3 = self.run_engine("three", 3)
        outn, staten, bytesn = self.run_engine("all", None)
        self.assertEqual(bytes1, bytes3)
        self.assertEqual(bytes1, bytesn)
        self.assertEqual(out1, out3)
        self.assertEqual(out1, outn)
        self.assertEqual((state1, state3), (staten, staten))
        self.assertEqual(len(out1["fills"]), 2)                          # the day really traded

    def test_late_records_are_held_so_the_engine_never_refuses_them(self):
        late = list(self.records)
        late.append((pf.t("09:15:30"), packs.RANK[packs.GAP_K], packs.enc_gap(0, 0, 0, "disconnect")))   # rank 1, long ago
        folder = self.folder / "late"
        folder.mkdir()
        bundle_mod.write(bundle_mod.resolve_session_day("s1", self.session_cfg(), pf.DAY, None), folder)
        e = engine.load().Engine(str(folder / "bundle.json"))
        e.set_strategy_py(Script([]))
        w = journal.JournalWriter(folder / "day.evt", fsync_every_mono_ns=10 ** 9, mono=iter(itertools.count()).__next__)
        e.push(w.append(late))
        e.finish()
        w.close(finalise=True)
        _, body, _ = journal.read(folder / "day.evt")
        self.assertEqual(packs.decode(body, len(body) - 32)[0]["kind"], "GAP")

    def test_journal_reads_like_a_pack_after_finalise(self):
        _, _, data = self.run_engine("pack", 7)
        path = self.folder / "pack.evt"
        path.write_bytes(data)
        header, recs = packs.read(path)                         # the pack reader: size, order and count checks
        jheader, body, torn = journal.read(path)
        self.assertEqual((header["count"], len(recs), torn), (len(self.records), len(self.records), 0))
        self.assertEqual(header, jheader)
        self.assertEqual([r["seq"] for r in recs], list(range(1, len(recs) + 1)))
        self.assertEqual((header["t_min"], header["t_max"]), (recs[0]["t_event"], recs[-1]["t_event"]))
        self.assertTrue(header["flags"] & packs.FLAG_CLOCK)


class ReadFromTests(unittest.TestCase):
    """`read_from`: the whole records after a state, nothing else read, nothing written (the chart's poll)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jr-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "day.evt"

    def append(self, n: int, t0: int, path: Path | None = None) -> None:
        w = writer(path or self.path)
        w.append(clock_records(n, t0))
        w.close(finalise=False)

    def take(self, state=None, path: Path | None = None):
        got: list[dict] = []
        header, st, torn, restarted = journal.read_from(path or self.path, state, got.append)
        return got, st, torn, restarted

    def test_reads_only_the_new_records_and_decodes_only_them(self):
        self.append(10, 1_000)
        got, st, torn, restarted = self.take()
        self.assertEqual(([r["seq"] for r in got], torn, restarted), (list(range(1, 11)), 0, False))
        self.append(5, 2_000)
        real, calls = packs.decode, []

        def counting(data, off):
            calls.append(off)
            return real(data, off)

        with patch.object(packs, "decode", counting):
            more, st2, torn, restarted = self.take(st)
        self.assertEqual(([r["seq"] for r in more], restarted, len(calls)), (list(range(11, 16)), False, 5))
        whole = journal.read(self.path)[1]
        self.assertEqual(st2.offset, journal.HEADER_SIZE + len(whole))
        self.assertEqual(self.take(st2)[0], [])                         # nothing new: nothing read

    def test_a_torn_tail_reads_cleanly_and_is_taken_once_whole(self):
        self.append(6, 1_000)
        whole = self.path.read_bytes()
        crc = Path(str(self.path) + ".crc")
        crc_whole = crc.read_bytes()
        self.path.write_bytes(whole[:-7])
        got, st, torn, _ = self.take()
        self.assertEqual((len(got), torn > 0), (5, True))
        self.assertEqual(self.path.read_bytes(), whole[:-7])              # nothing repaired
        self.path.write_bytes(whole)
        more, _, torn, restarted = self.take(st)
        self.assertEqual(([r["seq"] for r in more], torn, restarted), ([6], 0, False))
        self.assertEqual(crc.read_bytes(), crc_whole)

    def test_a_replaced_file_restarts_the_read(self):
        self.append(5, 1_000)
        _, st, _, _ = self.take()
        other = Path(self.tmp.name) / "other.evt"
        self.append(8, 5_000, other)
        self.path.write_bytes(other.read_bytes())                        # same file id, other and longer content
        Path(str(self.path) + ".crc").write_bytes(Path(str(other) + ".crc").read_bytes())
        got, _, _, restarted = self.take(st)
        self.assertEqual((restarted, [r["t_event"] for r in got][:2]), (True, [5_000, 5_001]))
        self.assertEqual(len(got), 8)

    def test_a_shorter_file_restarts_the_read(self):
        self.append(8, 1_000)
        _, st, _, _ = self.take()
        self.path.unlink()
        Path(str(self.path) + ".crc").unlink()
        self.append(3, 1_000)
        got, _, _, restarted = self.take(st)
        self.assertEqual((restarted, len(got)), (True, 3))

    def test_chunks_give_the_same_records(self):
        self.append(40, 1_000)
        want = self.take()[0]
        with patch.object(journal, "CHUNK_BYTES", 100):
            got, st, torn, _ = self.take()
            self.assertEqual((got, torn), (want, 0))
            self.append(7, 9_000)
            more, _, _, _ = self.take(st)
        self.assertEqual([r["seq"] for r in more], list(range(41, 48)))

    def test_each_chunk_reads_only_the_checksums_it_can_use(self):
        self.append(60, 1_000)
        crc = Path(str(self.path) + ".crc").read_bytes()
        full = list(struct.unpack(f"<{len(crc) // 4}I", crc))
        want = self.take()[0]
        seen = []
        real = journal._crcs_from

        def spy(path, header, data, slot):
            got = real(path, header, data, slot)
            seen.append((slot, got, len(data)))
            return got

        with patch.object(journal, "CHUNK_BYTES", 200), patch.object(journal, "_crcs_from", spy):
            got, st, torn, _ = self.take()
        self.assertEqual((got, torn), (want, 0))                         # the same records
        self.assertGreater(len(seen), 5)                                 # in many chunks
        for slot, crcs, size in seen:
            self.assertEqual(crcs, full[slot:slot + len(crcs)])          # the same checksums as the sidecar holds
            self.assertLessEqual(len(crcs), size // journal.PREFIX.size + 1)   # and no more than the chunk can hold
        self.assertLess(sum(len(c) for _, c, _ in seen), 2 * len(full) + len(seen))    # not the sidecar once per chunk

    def test_records_of_other_instruments_are_not_decoded_but_still_checked(self):
        w = writer(self.path)
        recs = []
        for i in range(12):
            iid = 1 + i % 3
            recs.append((1_000 + i, packs.RANK[packs.BAR_K], packs.enc_bar(iid, 1_000 + i, 60, 1_000 + i, 1, 2, 0, 1, 5, 0)))
        w.append(recs)
        w.close(finalise=False)
        real, calls = packs.decode, []

        def counting(data, off):
            calls.append(off)
            return real(data, off)

        got: list[dict] = []
        with patch.object(packs, "decode", counting):
            _, st, torn, _ = journal.read_from(self.path, None, got.append, want=lambda iid, kind: iid == 2)
        self.assertEqual((len(calls), {r["instrument_id"] for r in got}, len(got)), (4, {2}, 4))
        every = self.take()[1]
        self.assertEqual((st.offset, st.n, st.last_key), (every.offset, every.n, every.last_key))   # same position
        # a checksum failure inside a skipped record is still caught
        data = bytearray(self.path.read_bytes())
        data[journal.HEADER_SIZE + 88 * 2 + 40] ^= 0xFF                  # the third record (instrument 3, skipped)
        self.path.write_bytes(bytes(data))
        with self.assertRaises(journal.JournalError):
            journal.read_from(self.path, None, lambda r: None, want=lambda iid, kind: iid == 2)

    def test_it_writes_nothing(self):
        self.append(6, 1_000)
        files = sorted(Path(self.tmp.name).iterdir())
        before = {f: (f.read_bytes(), f.stat().st_mtime_ns) for f in files}
        self.take()
        self.assertEqual(sorted(Path(self.tmp.name).iterdir()), files)
        self.assertEqual({f: (f.read_bytes(), f.stat().st_mtime_ns) for f in files}, before)

    def test_a_short_or_foreign_file_is_a_journal_error(self):
        self.path.write_bytes(b"short")
        with self.assertRaises(journal.JournalError):
            self.take()
        self.path.write_bytes(b"x" * 200)
        with self.assertRaises(journal.JournalError):
            self.take()


if __name__ == "__main__":
    unittest.main()
