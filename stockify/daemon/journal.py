"""The session journal (DM 5.1; M5.3 journal-first; D-68): every input a paper session sees is written here before
the engine gets it, so any day can be replayed exactly.

A journal is an `.evt` file (A1 §5) with header flag bit 0 set and record count 0 while open. It is a record of
truth: append-only, records are never rewritten, and a crash never costs a whole record's worth of trust:

* `JournalWriter.append` validates a whole batch first, sets each record's `t` and `seq`, writes it, flushes, and
  returns the exact bytes for `Engine.push`. Because `t` is `ordered_t` (D-68), the engine's ordering check on a
  pushed batch can never refuse what the journal holds.
* A crash may leave a torn last record (a short write, or zeros where the file grew but the data did not).
  `read` reports it (`torn` bytes) and never uses it; the writer, on reopening, first copies the torn bytes to
  `<journal>.torn`, syncs that, and only then cuts the file back to its last whole record.
* Corruption anywhere else (an unknown kind, a size that is not the kind's, a `seq` that is not the next one, a
  record that sorts before its predecessor) is a `JournalError`: nothing is repaired or truncated.
* `close(finalise=True)` writes the header's count and `t_min`/`t_max` (one 64-byte write, then fsync), after which
  the file is a valid pack (`packs.read`) and takes no more records.

fsync runs on a monotonic-clock cadence supplied by the caller; that clock is never written into a record.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Callable

from stockify.data import packs

PREFIX = packs.PREFIX                  # <HHIqQ: kind, size, instrument_id, t, seq
HEADER_SIZE = 64
FLAG_JOURNAL = 1


class JournalError(ValueError):
    """A journal that cannot be read or written: a bad record, a torn header, corruption before the tail."""


def ordered_t(intended_t: int, rank: int, last_t: int | None, last_rank: int | None) -> int:
    """The `t` a record gets in the journal (D-68, amending A4 §1.1): `max(intended_t, last_t)`, and one nanosecond
    more than `last_t` when that equals `last_t` and the record's rank is below the previous one's, so the
    engine's key `(t, rank, src, seq)` never goes backwards. The journal `t` may differ from the receive time."""
    if last_t is None:
        return intended_t
    t = max(intended_t, last_t)
    if t == last_t and last_rank is not None and rank < last_rank:
        return last_t + 1
    return t


def _kind_of(data: bytes, offset: int) -> tuple[int, int]:
    kind, size = struct.unpack_from("<HH", data, offset)
    return kind, size


def _check_header(data: bytes) -> dict:
    try:
        header = packs.read_header(data)
    except (packs.PackError, struct.error) as e:
        raise JournalError(f"not a journal: {e}") from e
    if not header["flags"] & FLAG_JOURNAL:
        raise JournalError("not a journal: header flag bit 0 (journal) is not set")
    return header


def _scan(data: bytes, header: dict) -> tuple[int, int, int | None, int | None, int | None, bool, int]:
    """Walk the records after the header. Returns (end of the last whole record, count, first t, last t, last
    rank, any CLOCK, torn bytes). Raises JournalError for corruption before the tail."""
    off, n = HEADER_SIZE, 0
    first_t = last_t = last_rank = None
    has_clock = False
    last_key = None
    while off < len(data):
        left = len(data) - off
        if left < PREFIX.size:
            torn = left
            break
        kind, size = _kind_of(data, off)
        if kind not in packs.KIND_NAMES or size < PREFIX.size or (kind in packs.SIZE and size != packs.SIZE[kind]) \
                or (kind == packs.REFDATA_K and size < 32):
            if not any(data[off:]):               # zeros where the file grew but the data did not: a torn tail
                torn = left
                break
            raise JournalError(f"corrupt record at byte {off}: kind {kind}, size {size}")
        if size > left:
            torn = left
            break
        try:
            rec, _ = packs.decode(data, off)
        except (packs.PackError, struct.error, ValueError, KeyError) as e:
            raise JournalError(f"corrupt record at byte {off}: {e}") from e
        if rec["seq"] != n + 1:
            raise JournalError(f"record at byte {off} has seq {rec['seq']}, expected {n + 1}")
        rank = packs.RANK[kind]
        key = (rec["t_event"], rank, rec["seq"])
        if last_key is not None and key < last_key:
            raise JournalError(f"record at byte {off} (seq {rec['seq']}) sorts before the record before it")
        last_key, last_t, last_rank = key, rec["t_event"], rank
        if first_t is None:
            first_t = rec["t_event"]
        has_clock = has_clock or kind == packs.CLOCK
        n += 1
        off += size
    else:
        return off, n, first_t, last_t, last_rank, has_clock, 0
    return off, n, first_t, last_t, last_rank, has_clock, torn


def read(path: Path) -> tuple[dict, bytes, int]:
    """(header, the whole records as bytes, torn-tail byte count) of a journal, open or finalised. Reads only; a
    torn last record is reported, never returned and never removed. Raises JournalError for a short or foreign
    header and for corruption before the tail."""
    data = Path(path).read_bytes()
    if len(data) < HEADER_SIZE:
        raise JournalError(f"{path}: {len(data)} bytes, shorter than the {HEADER_SIZE}-byte header")
    header = _check_header(data)
    end, _, _, _, _, _, torn = _scan(data, header)
    return header, data[HEADER_SIZE:end], torn


class JournalWriter:
    """Appends to one day's journal. Reopening an existing journal resumes it: `records`, `next_seq`, `last_t` and
    `last_rank` come from its last whole record, and a torn tail is set aside (see the module doc)."""

    def __init__(self, path: Path, *, fsync_every_mono_ns: int, mono: Callable[[], int],
                 price_basis: str = "raw") -> None:
        if price_basis not in packs.BASIS:
            raise JournalError(f"unknown price basis {price_basis!r}")
        self.path = Path(path)
        self._every = fsync_every_mono_ns
        self._mono = mono
        self.torn_bytes = 0
        self.last_t: int | None = None
        self.last_rank: int | None = None
        self.records = 0
        self._first_t: int | None = None
        self._has_clock = False
        self._closed = False
        self._basis = price_basis
        existed = self.path.exists() and self.path.stat().st_size > 0
        if existed:
            data = self.path.read_bytes()
            if len(data) < HEADER_SIZE:
                raise JournalError(f"{path}: {len(data)} bytes, shorter than the {HEADER_SIZE}-byte header: "
                                   "not overwritten")
            header = _check_header(data)
            if header["price_basis"] != price_basis:
                raise JournalError(f"{path} holds {header['price_basis']} prices, not {price_basis}")
            if header["count"]:
                raise JournalError(f"{path} is finalised ({header['count']} records) and takes no more")
            end, self.records, self._first_t, self.last_t, self.last_rank, self._has_clock, torn = _scan(data, header)
            self._f = open(self.path, "r+b")
            if torn:
                self._set_aside(data[end:], end)
                self._f.truncate(end)
                self._f.flush()
                os.fsync(self._f.fileno())
                self.torn_bytes = torn
            self._f.seek(end)
        else:
            self._f = open(self.path, "wb")
            self._f.write(self._header(0, 0, 0, False))
            self._f.flush()
            os.fsync(self._f.fileno())
        self._last_sync = self._mono()

    @property
    def next_seq(self) -> int:
        return self.records + 1

    def _header(self, count: int, t_min: int, t_max: int, clock: bool) -> bytes:
        flags = FLAG_JOURNAL | (packs.FLAG_CLOCK if clock else 0)
        return packs.HEADER.pack(packs.MAGIC, packs.VERSION, flags, HEADER_SIZE, count, t_min, t_max,
                                 packs.BASIS[self._basis])

    def _set_aside(self, torn: bytes, offset: int) -> None:
        with open(str(self.path) + ".torn", "ab") as f:
            f.write(torn)
            f.flush()
            os.fsync(f.fileno())

    def append(self, records: list[tuple[int, int, bytes]]) -> bytes:
        """Write (intended t, rank, record) triples, in order, as one batch; return the bytes written, to be
        pushed to the engine as they are. The whole batch is checked first: one bad record writes nothing."""
        if self._closed:
            raise JournalError("the journal is closed")
        last_t, last_rank, seq = self.last_t, self.last_rank, self.next_seq
        out = bytearray()
        first_t, clock = self._first_t, self._has_clock
        for n, (intended_t, rank, rec) in enumerate(records, start=1):
            try:
                parsed, end = packs.decode(rec, 0)
            except (packs.PackError, struct.error, ValueError, KeyError) as e:
                raise JournalError(f"record {n} of the batch: {e}") from e
            if end != len(rec):
                raise JournalError(f"record of kind {parsed['kind']} has {len(rec) - end} trailing bytes")
            kind = _kind_of(rec, 0)[0]
            if rank != packs.RANK[kind]:
                raise JournalError(f"{parsed['kind']} has rank {packs.RANK[kind]}, not {rank}")
            t = ordered_t(intended_t, rank, last_t, last_rank)
            out += rec[:8] + struct.pack("<qQ", t, seq) + rec[24:]
            last_t, last_rank, seq = t, rank, seq + 1
            first_t = t if first_t is None else first_t
            clock = clock or kind == packs.CLOCK
        if not out:
            return b""
        data = bytes(out)
        self._f.write(data)
        self._f.flush()
        self.records, self.last_t, self.last_rank = seq - 1, last_t, last_rank
        self._first_t, self._has_clock = first_t, clock
        if self._mono() - self._last_sync >= self._every:
            self.fsync()
        return data

    def fsync(self) -> None:
        self._f.flush()
        os.fsync(self._f.fileno())
        self._last_sync = self._mono()

    def close(self, *, finalise: bool) -> None:
        """Sync and close. `finalise` also writes the count and bounds into the header (one 64-byte write at the
        start), making the file a plain pack; the records themselves are never touched. Closing twice is harmless."""
        if self._closed:
            return
        self._closed = True
        try:
            self._f.flush()
            if finalise and self.records:
                self._f.seek(0)
                self._f.write(self._header(self.records, self._first_t, self.last_t, self._has_clock))
                self._f.flush()
            os.fsync(self._f.fileno())
        finally:
            self._f.close()
