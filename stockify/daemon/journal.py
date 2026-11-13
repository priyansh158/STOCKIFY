"""The session journal (DM 5.1, 5.2; M5.3 journal-first; D-68): every input a paper session sees is written here
before the engine gets it, so any day can be replayed exactly.

A journal is an `.evt` file (A1 §5), byte for byte the pack format `packs.read` and the engine read: header flag
bit 0 set, record count 0 while open. Its integrity data lives beside it, never in it:

* `<journal>.crc`: one little-endian CRC32 per record (of the whole record, `t` and `seq` included), written
  *before* the record it covers. Each record is checked against it.
* `<journal>.torn`: bytes that were cut off a journal or a crashed creation, never deleted (see below).

A journal is a record of truth: append-only, records never rewritten.

* `JournalWriter.append` validates a whole batch first, sets each record's `t` and `seq` (`ordered_t`, D-68, so the
  engine's ordering check can never refuse the journal), writes checksums and records, flushes, and returns the
  exact bytes for `Engine.push`. After any write or sync error the writer is *failed* and refuses everything but
  `close`; reopening the file repairs the tail.
* A crash may leave a torn tail: a short record, zeros where the file grew but the data did not, or a record whose
  prefix landed but whose body did not (caught by its checksum, which is why it is there). The tail is the first
  record that fails its checksum (or lacks one) up to the end of the file, provided every record after it fails too.
  `read` reports it and never uses it. The writer, on reopening, copies those bytes to `<journal>.torn` (with their
  offset and length, so a repeat after a crash between copy and cut is recognised), syncs, and only then cuts the
  file and the checksum file back.
* A damaged record with a good record after it, a bad kind or size, a seq that is not the next one, a record that
  sorts before its predecessor, or an open journal with no checksum file is a `JournalError`: nothing is repaired.
* A file of 1-63 bytes that is zeros or the start of our header, or a 64-byte zero header, is a crashed creation and
  holds no records: the writer logs it, keeps the bytes in `.torn`, and creates the journal afresh.
* `close(finalise=True)` fsyncs the records, then writes the header (count, bounds, flag bit 2 = finalised) in one
  64-byte write, then fsyncs again; so a finalised header never describes records that are not on disk. After it
  the file is a plain pack and takes no more records. `is_finalised(path)` says so without catching error text.

fsync runs on a monotonic-clock cadence supplied by the caller; that clock is never written into a record.
"""
from __future__ import annotations

import logging
import os
import struct
import zlib
from pathlib import Path
from typing import Callable, NamedTuple

from stockify.data import packs

log = logging.getLogger(__name__)

PREFIX = packs.PREFIX                  # <HHIqQ: kind, size, instrument_id, t, seq
HEADER_SIZE = 64
FLAG_JOURNAL = 1                       # A1 §5 header flags: bit 0 journal, bit 1 has CLOCK, bit 2 finalised
FLAG_FINAL = 4
TORN_MAGIC = b"STKTORN1"
TORN_HEAD = struct.Struct("<8sQQI")    # marker, source offset, length, CRC32 of the bytes; the bytes follow


class JournalError(ValueError):
    """A journal that cannot be read or written: a bad record, a foreign header, corruption before the tail, a
    failed writer."""


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


def _crc_path(path: Path) -> Path:
    return Path(str(path) + ".crc")


def _torn_path(path: Path) -> Path:
    return Path(str(path) + ".torn")


def _check_header(data: bytes) -> dict:
    try:
        header = packs.read_header(data)
    except (packs.PackError, struct.error) as e:
        raise JournalError(f"not a journal: {e}") from e
    if not header["flags"] & FLAG_JOURNAL:
        raise JournalError("not a journal: header flag bit 0 (journal) is not set")
    return header


def _final(header: dict) -> bool:
    return bool(header["flags"] & FLAG_FINAL) or header["count"] > 0


def is_finalised(path: Path) -> bool:
    """Whether the journal at `path` was finalised (a day that is over). Raises JournalError if there is no
    readable journal header there."""
    try:
        with open(path, "rb") as f:
            data = f.read(HEADER_SIZE)
    except OSError as e:
        raise JournalError(f"{path}: {e}") from e
    if len(data) < HEADER_SIZE:
        raise JournalError(f"{path}: {len(data)} bytes, shorter than the {HEADER_SIZE}-byte header")
    return _final(_check_header(data))


def _load_crcs(path: Path, header: dict, data: bytes) -> list[int] | None:
    f = _crc_path(path)
    if f.exists():
        raw = f.read_bytes()
        return list(struct.unpack_from(f"<{len(raw) // 4}I", raw))
    if _final(header) or len(data) == HEADER_SIZE:
        return None                       # finalised (the header's count was synced after the records) or no records
    raise JournalError(f"{path} has records but its checksum file {f.name} is missing: they cannot be trusted")


def _scan(data: bytes, header: dict, crcs: list[int] | None, *, start: int = HEADER_SIZE, base: int = 0, n0: int = 0,
          last_key0: tuple | None = None, on_record: Callable[[dict], None] | None = None,
          want: Callable[[int, int], bool] | None = None,
          out: dict | None = None) -> tuple[int, int, int | None, int | None, int | None, bool, int]:
    """Walk the records after the header. Returns (end of the last good record, count, first t, last t, last
    rank, any CLOCK, torn bytes). Raises JournalError for corruption before the tail.

    The defaults walk a whole file. To walk on from a known point, pass the bytes that follow it as `data` with
    `start=0`, `base` its byte offset in the file (only for messages), `n0` the records before it, `last_key0` the
    (t, rank, seq) of the last of them and `crcs` the checksums from that record on. `on_record` is called with
    each whole record as it is decoded (decoded once, here); `out["last_key"]` gets the last record's key. With
    `want(instrument_id, kind)` a record it refuses is neither decoded nor passed on: its checksum, size, seq and
    time (all in the 24-byte prefix) are still checked, so only a fault in its body goes unseen.
    `end` and the torn count are about `data`; the count includes `n0`."""
    off, n, slot = start, n0, 0           # slot: the record's place in `crcs`, good or bad
    first_t = last_t = last_rank = None
    has_clock = False
    last_key = last_key0
    bad_from = None                     # the first record that failed its checksum
    end = None
    while off < len(data):
        left = len(data) - off
        if left < PREFIX.size:
            end = bad_from if bad_from is not None else off
            break
        kind, size = struct.unpack_from("<HH", data, off)
        if kind not in packs.KIND_NAMES or size < PREFIX.size or (kind in packs.SIZE and size != packs.SIZE[kind]) \
                or (kind == packs.REFDATA_K and size < 32):
            if not any(data[off:]):               # zeros where the file grew but the data did not: a torn tail
                end = bad_from if bad_from is not None else off
                break
            raise JournalError(f"corrupt record at byte {base + off}: kind {kind}, size {size}")
        if size > left:
            end = bad_from if bad_from is not None else off
            break
        if crcs is not None:
            ok = slot < len(crcs) and crcs[slot] == zlib.crc32(data[off:off + size])
        else:
            ok = True
        if not ok:
            if _final(header):
                raise JournalError(f"record at byte {base + off} does not match its checksum in a finalised journal")
            if bad_from is None:
                bad_from = off
            off += size
            slot += 1
            continue
        if bad_from is not None:
            raise JournalError(f"record at byte {base + bad_from} is damaged and whole records follow it")
        if want is not None:
            _, _, iid, t_event, seq = PREFIX.unpack_from(data, off)
            if not want(iid, kind):
                rec = {"t_event": t_event, "seq": seq}
                on_record_here = None
            else:
                on_record_here = on_record
                rec = None
        else:
            on_record_here = on_record
            rec = None
        if rec is None:
            try:
                rec, _ = packs.decode(data, off)
            except (packs.PackError, struct.error, ValueError, KeyError) as e:
                raise JournalError(f"corrupt record at byte {base + off}: {e}") from e
        if rec["seq"] != n + 1:
            raise JournalError(f"record at byte {base + off} has seq {rec['seq']}, expected {n + 1}")
        rank = packs.RANK[kind]
        key = (rec["t_event"], rank, rec["seq"])
        if last_key is not None and key < last_key:
            raise JournalError(f"record at byte {base + off} (seq {rec['seq']}) sorts before the record before it")
        last_key, last_t, last_rank = key, rec["t_event"], rank
        if first_t is None:
            first_t = rec["t_event"]
        has_clock = has_clock or kind == packs.CLOCK
        if on_record_here is not None:
            on_record_here(rec)
        n += 1
        slot += 1
        off += size
    if end is None:
        end = bad_from if bad_from is not None else off
    if out is not None:
        out["last_key"] = last_key
    return end, n, first_t, last_t, last_rank, has_clock, len(data) - end


def read(path: Path) -> tuple[dict, bytes, int]:
    """(header, the whole records as bytes, torn-tail byte count) of a journal, open or finalised. Reads only; a
    torn tail is reported, never returned and never removed. Raises JournalError for a short or foreign header,
    a missing checksum file on an open journal, and corruption before the tail."""
    path = Path(path)
    data = path.read_bytes()
    if len(data) < HEADER_SIZE:
        raise JournalError(f"{path}: {len(data)} bytes, shorter than the {HEADER_SIZE}-byte header")
    header = _check_header(data)
    end, n, _, _, _, _, torn = _scan(data, header, _load_crcs(path, header, data))
    if _final(header) and header["count"] != n:
        raise JournalError(f"{path} is finalised with count {header['count']} but holds {n} whole records")
    return header, data[HEADER_SIZE:end], torn


CHUNK_BYTES = 8 << 20                  # `read_from` takes at most this many bytes of a file at a time
PROBE = 64                             # the bytes at each end of what was read that tell a replaced file from a grown one


class ReadState(NamedTuple):
    """Where `read_from` left off in one journal: enough to read on, and to know the file is still the same one."""
    ident: tuple                       # (device, file id, version, price basis): count, flags and bounds change as it is written
    offset: int                        # the byte offset (in the file) just after the last whole record taken
    n: int                             # records taken
    last_key: tuple | None             # (t, rank, seq) of the last of them
    head: bytes                        # up to PROBE bytes from the first record on
    tail: bytes                        # up to PROBE bytes ending at `offset`


def _still_ours(f, size: int, ident: tuple, state: ReadState) -> bool:  # noqa: ANN001
    if ident != state.ident or size < state.offset:
        return False
    f.seek(HEADER_SIZE)
    if f.read(len(state.head)) != state.head:
        return False
    if state.tail:
        f.seek(state.offset - len(state.tail))
        return f.read(len(state.tail)) == state.tail
    return True


def _crcs_from(path: Path, header: dict, data: bytes, slot: int) -> list[int] | None:
    """The checksums of the records in `data`, from record `slot` on (read after the data, so they cover all of it). A
    record is at least `PREFIX.size` bytes, so `data` holds at most `len(data) // PREFIX.size` of them: that many
    entries (and one to spare) are read, never the rest of the sidecar."""
    try:
        with open(_crc_path(path), "rb") as h:
            h.seek(4 * slot)
            raw = h.read(4 * (len(data) // PREFIX.size + 1))
    except FileNotFoundError:
        if _final(header) or not data:
            return None
        raise JournalError(f"{path} has records but its checksum file {_crc_path(path).name} is missing: they cannot be "
                           "trusted") from None
    return list(struct.unpack_from(f"<{len(raw) // 4}I", raw))


def _read_chunk(path: Path, state: ReadState | None, on_record: Callable[[dict], None],
                on_restart: Callable[[], None] | None, want: Callable[[int, int], bool] | None) -> tuple[dict, ReadState, int, bool, bool]:
    restarted = False
    with open(path, "rb") as f:
        st = os.fstat(f.fileno())
        head = f.read(HEADER_SIZE)
        if len(head) < HEADER_SIZE:
            raise JournalError(f"{path}: {len(head)} bytes, shorter than the {HEADER_SIZE}-byte header")
        header = _check_header(head)
        ident = (st.st_dev, st.st_ino, header["version"], header["price_basis"])
        if state is not None and not _still_ours(f, st.st_size, ident, state):
            state, restarted = None, True
            if on_restart is not None:
                on_restart()
        start = state.offset if state else HEADER_SIZE
        f.seek(start)
        data = f.read(CHUNK_BYTES)
    n0 = state.n if state else 0
    out: dict = {}
    end, n, _, _, _, _, torn = _scan(data, header, _crcs_from(path, header, data, n0), start=0, base=start, n0=n0,
                                     last_key0=state.last_key if state else None, on_record=on_record, want=want, out=out)
    full = len(data) == CHUNK_BYTES
    if _final(header) and not full and header["count"] != n:
        raise JournalError(f"{path} is finalised with count {header['count']} but holds {n} whole records")
    new = data[:end]
    nxt = ReadState(ident, start + end, n, out["last_key"], ((state.head if state else b"") + new)[:PROBE],
                    ((state.tail if state else b"") + new)[-PROBE:])
    return header, nxt, (0 if full else torn), restarted, full and end > 0


def read_from(path: Path, state: ReadState | None, on_record: Callable[[dict], None], *,
              on_restart: Callable[[], None] | None = None, want: Callable[[int, int], bool] | None = None) -> tuple[dict, ReadState, int, bool]:
    """Read the whole records of a journal after `state` (None: from the first), calling `on_record(record)` for each
    as it is decoded: (header, the state after them, torn-tail byte count, restarted). A poll of a growing journal
    costs the new bytes and a few small probes, not the file.

    The state is dropped, `on_restart()` called before any record, and the file read from its first record, when it
    is no longer the file the state came from: another file, a shorter one, or other bytes at either probe. Reads
    only (never writes, locks, renames or repairs); a torn tail is reported and never taken; a short or foreign
    header, corruption before the tail, or a checksum file that lags whole records raises JournalError. The file is
    read `CHUNK_BYTES` at a time. `want(instrument_id, kind)`, when given, names the records worth decoding: the
    others are checked by checksum and prefix only, never decoded and never passed to `on_record`."""
    path = Path(path)
    restarted = False
    while True:
        header, state, torn, again, more = _read_chunk(path, state, on_record, on_restart, want)
        restarted = restarted or again
        if not more:
            return header, state, torn, restarted


def _parse_torn(data: bytes) -> tuple[list[tuple[int, bytes]], list[tuple[int, bytes]]]:
    """(whole entries, partial pieces) of a `.torn` file. An entry is whole when its marker, length and CRC agree. A
    piece that is not (a crash cut the write short) is reported and skipped by searching for the next marker, so
    entries written after it are never hidden."""
    entries, partial, pos = [], [], 0
    while pos < len(data):
        if data[pos:pos + 8] == TORN_MAGIC and pos + TORN_HEAD.size <= len(data):
            _, offset, length, crc = TORN_HEAD.unpack_from(data, pos)
            body = data[pos + TORN_HEAD.size:pos + TORN_HEAD.size + length]
            if len(body) == length and zlib.crc32(body) == crc:
                entries.append((offset, body))
                pos += TORN_HEAD.size + length
                continue
        nxt = data.find(TORN_MAGIC, pos + 1)
        partial.append((pos, data[pos:] if nxt < 0 else data[pos:nxt]))
        if nxt < 0:
            break
        pos = nxt
    return entries, partial


def torn_entries(path: Path, *, with_partial: bool = False):
    """What `<journal>.torn` holds: [(source offset, bytes)] for each whole set-aside. With `with_partial`, returns
    (entries, pieces) where pieces are [(file offset, bytes)] of entries a crash cut short; they never hide a whole
    entry written after them."""
    f = _torn_path(Path(path))
    entries, partial = _parse_torn(f.read_bytes() if f.exists() else b"")
    return (entries, partial) if with_partial else entries


def adopt(path: Path) -> int:
    """Owner-run (never called by the daemon): rebuild the checksum file of a journal whose sidecar is missing or
    short, from the journal's own bytes. The journal is read and never written; every whole record must parse and
    be in strict seq and key order, else JournalError and nothing changes. An existing sidecar is kept beside the
    new one as `.crc.before-adopt` (a numbered copy if that exists). Returns the number of records covered. A torn
    tail, if any, is left for the writer to set aside as usual."""
    path = Path(path)
    data = path.read_bytes()
    if len(data) < HEADER_SIZE:
        raise JournalError(f"{path}: {len(data)} bytes, shorter than the {HEADER_SIZE}-byte header")
    header = _check_header(data)
    end, n, _, _, _, _, _ = _scan(data, header, None)
    crcs, pos = bytearray(), HEADER_SIZE
    while pos < end:
        size = struct.unpack_from("<H", data, pos + 2)[0]
        crcs += struct.pack("<I", zlib.crc32(data[pos:pos + size]))
        pos += size
    crc = _crc_path(path)
    tmp = Path(str(crc) + ".adopt")
    with open(tmp, "wb") as f:
        f.write(bytes(crcs))
        f.flush()
        os.fsync(f.fileno())
    if crc.exists():
        keep, i = Path(str(crc) + ".before-adopt"), 1
        while keep.exists():
            i += 1
            keep = Path(str(crc) + f".before-adopt{i}")
        os.replace(crc, keep)
    os.replace(tmp, crc)
    log.warning("journal %s: checksum file rebuilt from the journal's bytes for %d records (adopt)", path, n)
    return n


class JournalWriter:
    """Appends to one day's journal. Reopening an existing journal resumes it: `records`, `next_seq`, `last_t` and
    `last_rank` come from its last good record, and a torn tail is set aside (see the module doc)."""

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
        self._failed = False
        self._basis = price_basis
        self._f = self._c = None
        data = self.path.read_bytes() if self.path.exists() else b""
        if data and self._is_crashed_creation(data):
            self._set_aside(data, 0)
            log.warning("journal %s: %d bytes of a creation that never finished (%s) set aside in %s; starting "
                        "it afresh", self.path, len(data), "zeros" if not any(data) else "a partial header",
                        _torn_path(self.path).name)
            data = b""
        try:
            if data:
                self._resume(data)
            else:
                self._f = open(self.path, "wb")
                self._c = open(_crc_path(self.path), "wb")
                self._f.write(self._header(0, 0, 0, False, False))
                self._f.flush()
                os.fsync(self._f.fileno())
                os.fsync(self._c.fileno())
        except BaseException:
            for f in (self._f, self._c):
                if f is not None:
                    f.close()
            raise
        self._last_sync = self._mono()

    @staticmethod
    def _is_crashed_creation(data: bytes) -> bool:
        if len(data) > HEADER_SIZE or (len(data) == HEADER_SIZE and any(data)):
            return False
        return not any(data) or packs.MAGIC.startswith(data[:8])

    def _resume(self, data: bytes) -> None:
        if len(data) < HEADER_SIZE:
            raise JournalError(f"{self.path}: {len(data)} bytes, shorter than the {HEADER_SIZE}-byte header: "
                               "not overwritten")
        header = _check_header(data)
        if header["price_basis"] != self._basis:
            raise JournalError(f"{self.path} holds {header['price_basis']} prices, not {self._basis}")
        if _final(header):
            raise JournalError(f"{self.path} is finalised and takes no more records")
        crcs = _load_crcs(self.path, header, data)
        end, self.records, self._first_t, self.last_t, self.last_rank, self._has_clock, torn = _scan(data, header, crcs)
        self._f = open(self.path, "r+b")
        self._c = open(_crc_path(self.path), "r+b" if _crc_path(self.path).exists() else "w+b")
        if torn:
            self._warn_unchecked(data, end, len(crcs or ()))
            self._set_aside(data[end:], end)
            self._cut(end)
            self.torn_bytes = torn
        self._c.truncate(self.records * 4)          # entries of records that never landed (or were cut) go too
        self._c.flush()
        os.fsync(self._c.fileno())
        self._f.seek(end)
        self._c.seek(self.records * 4)

    def _warn_unchecked(self, data: bytes, end: int, entries: int) -> None:
        """Be loud when the tail being set aside is whole, well-formed records that only lack a checksum entry (a short
        or empty checksum file): they are kept in `.torn`, and `adopt` can rebuild the sidecar instead."""
        pos, seq, slot, found = end, self.records + 1, self.records, 0
        while pos + PREFIX.size <= len(data) and slot >= entries:
            kind, size = struct.unpack_from("<HH", data, pos)
            if kind not in packs.KIND_NAMES or size < PREFIX.size or pos + size > len(data):
                break
            try:
                rec, _ = packs.decode(data, pos)
            except (packs.PackError, struct.error, ValueError, KeyError):
                break
            if rec["seq"] != seq:
                break
            found, pos, seq, slot = found + 1, pos + size, seq + 1, slot + 1
        if found:
            log.warning("journal %s: checksum file has %d entries but %d whole records without checksums follow "
                        "the %d verified ones (seq %d..%d); they are being moved to %s. If they are good, stop and "
                        "run journal.adopt(path) to rebuild the checksum file instead", self.path, entries,
                        found, self.records, self.records + 1, self.records + found, _torn_path(self.path).name)

    @property
    def next_seq(self) -> int:
        return self.records + 1

    def _header(self, count: int, t_min: int, t_max: int, clock: bool, final: bool) -> bytes:
        flags = FLAG_JOURNAL | (packs.FLAG_CLOCK if clock else 0) | (FLAG_FINAL if final else 0)
        return packs.HEADER.pack(packs.MAGIC, packs.VERSION, flags, HEADER_SIZE, count, t_min, t_max,
                                 packs.BASIS[self._basis])

    def _set_aside(self, torn: bytes, offset: int) -> None:
        """Keep bytes cut from the journal in `<journal>.torn`, once: the same offset and bytes already there (a crash
        between this copy and the cut) are not written again. The sidecar is never deleted."""
        if (offset, torn) in torn_entries(self.path):
            return
        with open(_torn_path(self.path), "ab") as f:
            f.write(TORN_HEAD.pack(TORN_MAGIC, offset, len(torn), zlib.crc32(torn)) + torn)
            f.flush()
            os.fsync(f.fileno())

    def _cut(self, end: int) -> None:
        self._f.truncate(end)
        self._f.flush()
        os.fsync(self._f.fileno())

    def append(self, records: list[tuple[int, int, bytes]]) -> bytes:
        """Write (intended t, rank, record) triples, in order, as one batch; return the bytes written, to be
        pushed to the engine as they are. The whole batch is checked first: one bad record writes nothing. If the
        write itself fails, or the fsync that follows it does, the writer is failed: it refuses further appends until the
        file is reopened. After ANY error from `append` or `fsync` the caller must reopen the journal and replay from
        it, not retry: the records of the failed batch may already be in the file (and then are valid), though the
        caller never received their bytes."""
        if self._closed:
            raise JournalError("the journal is closed")
        if self._failed:
            raise JournalError("the journal writer failed on an earlier write: reopen the journal to repair its tail")
        last_t, last_rank, seq = self.last_t, self.last_rank, self.next_seq
        out = bytearray()
        crcs = bytearray()
        first_t, clock = self._first_t, self._has_clock
        for n, (intended_t, rank, rec) in enumerate(records, start=1):
            try:
                parsed, end = packs.decode(rec, 0)
            except (packs.PackError, struct.error, ValueError, KeyError) as e:
                raise JournalError(f"record {n} of the batch: {e}") from e
            if end != len(rec):
                raise JournalError(f"record of kind {parsed['kind']} has {len(rec) - end} trailing bytes")
            kind = struct.unpack_from("<H", rec, 0)[0]
            if rank != packs.RANK[kind]:
                raise JournalError(f"{parsed['kind']} has rank {packs.RANK[kind]}, not {rank}")
            t = ordered_t(intended_t, rank, last_t, last_rank)
            stamped = rec[:8] + struct.pack("<qQ", t, seq) + rec[24:]
            out += stamped
            crcs += struct.pack("<I", zlib.crc32(stamped))
            last_t, last_rank, seq = t, rank, seq + 1
            first_t = t if first_t is None else first_t
            clock = clock or kind == packs.CLOCK
        if not out:
            return b""
        data = bytes(out)
        try:
            self._c.write(bytes(crcs))             # the checksums first: a record never lacks the check that tells
            self._c.flush()                        # whether it landed whole
            self._f.write(data)
            self._f.flush()
        except BaseException as e:
            self._failed = True
            raise JournalError(f"write to {self.path} failed ({e}); the writer is failed until the journal is "
                               "reopened") from e
        self.records, self.last_t, self.last_rank = seq - 1, last_t, last_rank
        self._first_t, self._has_clock = first_t, clock
        if self._mono() - self._last_sync >= self._every:
            self.fsync()
        return data

    def fsync(self) -> None:
        try:
            self._f.flush()
            self._c.flush()
            os.fsync(self._c.fileno())              # the checksums first: a record is never durable without its check
            os.fsync(self._f.fileno())
        except BaseException as e:
            self._failed = True
            raise JournalError(f"sync of {self.path} failed ({e}); the writer is failed until the journal is "
                               "reopened") from e
        self._last_sync = self._mono()

    def close(self, *, finalise: bool) -> None:
        """Sync and close. `finalise` syncs the records, then writes the header (count, bounds, finalised flag) in
        one 64-byte write at the start, then syncs again. A failed writer is closed without finalising and
        raises. Closing twice is harmless."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._failed:
                if finalise:
                    raise JournalError("a failed writer does not finalise: reopen the journal to repair its tail")
                return
            self._f.flush()
            self._c.flush()
            os.fsync(self._c.fileno())                      # checksums, then records, are on disk first ...
            os.fsync(self._f.fileno())
            if finalise:
                self._f.seek(0)
                self._f.write(self._header(self.records, self._first_t or 0, self.last_t or 0, self._has_clock, True))
                self._f.flush()
                os.fsync(self._f.fileno())                  # ... and only then does the header say so
        finally:
            self._f.close()
            self._c.close()
