"""Crash recovery for a paper session-day, and the owner's salvage tool for a damaged journal (GK 5.4; M5.4).

    sd, result = recover("orb", day, clock=clock)      # the day, rebuilt from its journal and checked against the store

The journal is the truth. A crash can leave the engine and the store behind it (the journal is written first), and a
power cut can leave the store ahead of it (SQLite had committed what the journal's last fsync had not yet made
durable). `recover` therefore builds a fresh engine from the day's saved bundle (which carries its `sod_state`),
feeds it the journal (`SessionDay.replay`, which also repairs a torn tail the way `JournalWriter` always does) and
compares what the engine drained with what the store holds, table by table, row by row:

    stored rows that equal their regenerated twins          nothing to do;
    regenerated rows the store lacks (it is behind)         written;
    stored rows the journal does not account for (ahead)    deleted, timelined as `journal_tail_lost`;
    any other difference (a stored row that disagrees       the session is paused with `determinism`, its
    with the row the journal gives)                         `strategy_control` journaled first (D-68), the
                                                            timeline gets `determinism_mismatch` with the first
                                                            differing row, `log.error` says so, and the store is
                                                            left exactly as found: it is the evidence.

An order's row follows its events and fills, so a stored order row that differs from its twin is only a stale or an
advanced row (rewritten) when its events or fills differ too; with the same events and fills it is a mismatch. The
repair is one transaction (`store.replace_session_outputs`): a crash in recovery leaves the old rows or the new, and
running it again finishes the job. The decision is a function of the journal and the store alone, and every
recovery writes a `recovered` timeline row with it. A journal that holds a damaged record with good records after it
is a `JournalError`: recovery never repairs that; it raises and touches nothing, and the owner decides (see below).

Recovery never deletes or rewrites a journal.

The salvage tool (run by the owner, never by the daemon or anything else here):

    report = salvage(journal_path)       # writes <journal>.salvaged (+ .crc) and <journal>.salvage.json beside it
    adopt_salvage(journal_path)          # only then, with the daemon stopped: the salvaged copy replaces the journal

`salvage` reads the journal and writes only new files: the longest good prefix (records that check out, in sequence,
in order) as a valid open journal, byte for byte the original's records, and a report of what was dropped and why.
The damaged record and everything after it stay in the original, which is never edited. `adopt_salvage` takes the
daemon's lock, checks that the journal is still the one that was salvaged, keeps the damaged journal and its
checksum file as `<journal>.damaged[N]` (and `.damaged[N].crc`), then replaces the journal and its checksum file with
the copy; a crash in between is finished by running it again. Records dropped by salvage are gone from the day: the
store rows they made are then deleted by `recover` as `journal_tail_lost`, and what was dropped stays on disk in the
damaged copy.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import struct
import time
import zlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from stockify import paths, store
from stockify.daemon import journal, lifecycle
from stockify.daemon.clock import Clock, WallClock
from stockify.daemon.session_day import SessionDay, SessionDayError
from stockify.data import packs

log = logging.getLogger("stockify.daemon.recovery")

ROW_TABLES = ("order_events", "fills", "charges", "orders")      # the order of the comparison: the first difference wins


@dataclass
class RecoveryResult:
    session_id: str
    day: date
    records: int                       # whole records the journal holds
    written: int                       # regenerated rows written to the store (missing or stale there)
    tail_lost_rows: int                # stored rows the journal does not account for, deleted
    mismatch: str | None               # the first stored row that disagrees with the journal's, else None


@dataclass
class _Diff:
    written: int = 0
    tail_lost: int = 0
    tail_by_table: dict | None = None
    mismatch: dict | None = None       # {"table", "key", "stored", "regenerated"}

    @property
    def text(self) -> str | None:
        m = self.mismatch
        if m is None:
            return None
        return (f"{m['table']}[{m['key']}]: stored {json.dumps(m['stored'], sort_keys=True)} != regenerated "
                f"{json.dumps(m['regenerated'], sort_keys=True)}")


def _order_of(table: str, row: dict) -> int | None:
    return int(row["order_id"]) if table in ("order_events", "fills") and "order_id" in row else None


def compare(stored: dict, twin: dict) -> _Diff:
    """Stored rows against their regenerated twins (see the module doc). Pure: the same two inputs always give the
    same verdict and the same first difference."""
    diff = _Diff(tail_by_table={})
    for table in ROW_TABLES[:3]:
        have, want = stored[table], twin[table]
        for key in sorted(have):
            if key not in want:
                diff.tail_lost += 1
                diff.tail_by_table[table] = diff.tail_by_table.get(table, 0) + 1
            elif have[key] != want[key]:
                diff.mismatch = {"table": table, "key": key, "stored": have[key], "regenerated": want[key]}
                return diff
        diff.written += sum(1 for key in want if key not in have)

    def trail(rows_by_table: dict) -> dict[int, tuple[frozenset, frozenset]]:
        out: dict[int, list[set]] = {}
        for i, table in enumerate(("order_events", "fills")):
            for key, row in rows_by_table[table].items():
                oid = _order_of(table, row)
                if oid is not None:
                    out.setdefault(oid, [set(), set()])[i].add(key)
        return {oid: (frozenset(a), frozenset(b)) for oid, (a, b) in out.items()}

    have_trail, want_trail = trail(stored), trail(twin)
    have, want = stored["orders"], twin["orders"]
    for oid in sorted(have):
        if oid not in want:
            diff.tail_lost += 1
            diff.tail_by_table["orders"] = diff.tail_by_table.get("orders", 0) + 1
        elif have[oid] != want[oid]:
            if have_trail.get(oid) == want_trail.get(oid):
                diff.mismatch = {"table": "orders", "key": oid, "stored": have[oid], "regenerated": want[oid]}
                return diff
            diff.written += 1                     # behind or ahead on its events and fills: its row is rewritten
    diff.written += sum(1 for oid in want if oid not in have)
    return diff


def recover(session_id: str, day: date, *, clock: Clock | None = None, log_sink=None,  # noqa: ANN001
            fsync_every_s: int | None = None) -> tuple[SessionDay, RecoveryResult]:
    """Rebuild a day from its journal and check it against the store (see the module doc). Returns the day, open
    and taking input (replay-only when its journal is finalised), and what was found. Raises `SessionDayError` when
    there is nothing to replay, and `JournalError` for a journal that cannot be read: nothing is changed then."""
    clock = clock or WallClock()
    session = store.get_session(session_id)
    if session is None or not session.get("config"):
        raise LookupError(f"no such session: {session_id!r}")
    cfg = session["config"]
    sd = SessionDay(session_id, day, cfg, mono=clock.mono_ns, log_sink=log_sink, fsync_every_s=fsync_every_s)
    d = day.isoformat()
    latency_ns = int(cfg.get("latency_ms", 0)) * 1_000_000
    found: dict = {}

    def reconcile(outputs: dict) -> list[int]:
        stored, twin, carried = store.preview_session_outputs(session_id, d, outputs, latency_ns=latency_ns)
        diff = compare(stored, twin)
        found["diff"] = diff
        if diff.mismatch is None and (diff.written or diff.tail_lost):
            return store.replace_session_outputs(session_id, d, outputs, latency_ns=latency_ns)
        return carried

    info = sd.replay(reconcile)
    diff: _Diff = found["diff"]
    result = RecoveryResult(session_id, day, info["records"], diff.written, diff.tail_lost, diff.text)
    now = clock.now_ns()
    try:
        _record(sd, result, diff, now)
    except BaseException:
        sd.close()
        raise
    return sd, result


def _timeline(session_id: str, kind: str, detail: dict, now: int) -> None:
    store.add_timeline(session_id, kind, detail, wall_ns=now, engine_ns=now)


def _record(sd: SessionDay, result: RecoveryResult, diff: _Diff, now: int) -> None:
    sid, d = result.session_id, result.day.isoformat()
    if diff.tail_lost:
        log.warning("recovered %s: %d stored rows are beyond the journal (power loss?) and were deleted", sid,
                    diff.tail_lost)
        _timeline(sid, "journal_tail_lost", {"day": d, "rows": diff.tail_lost, "by_table": diff.tail_by_table}, now)
    summary = {"day": d, "records": result.records, "written": result.written,
               "tail_lost_rows": result.tail_lost_rows, "mismatch": result.mismatch}
    if sd.torn_bytes:
        summary["torn_bytes"] = sd.torn_bytes
    _timeline(sid, "recovered", summary, now)
    if diff.mismatch is None:
        log.info("recovered %s: %d records, 0 mismatches (%d rows written, %d beyond the journal deleted)", sid,
                 result.records, result.written, result.tail_lost_rows)
        return
    log.error("recovered %s: %d records, 1 mismatch: the stored rows disagree with the journal's (determinism): %s",
              sid, result.records, result.mismatch)
    _timeline(sid, "determinism_mismatch", {"day": d, **diff.mismatch}, now)
    held = store.get_session(sid)["pause_reasons"]
    if store.get_session(sid)["status"] == "STOPPED":
        return                                       # a stopped session cannot be paused; the timeline has it
    reasons = sorted({*held, "determinism"})
    if "determinism" not in held:
        try:
            sd.control(False, reasons, now)          # the journal first: the engine must stop trading on it
        except SessionDayError:                      # a finalised day takes no input; the store still says paused
            log.warning("session %s: the day's journal is finalised, so the determinism pause is not journaled", sid)
        lifecycle.pause(sid, "determinism", wall_ns=now, engine_ns=now)


# ── the owner's salvage tool ─────────────────────────────────────────────────────────────────────────────────────


class SalvageError(RuntimeError):
    """A journal that cannot be salvaged, or a salvage that cannot be adopted."""


@dataclass
class SalvageReport:
    journal: Path
    copy: Path
    report: Path
    kept: int
    dropped: int
    first_bad_offset: int
    why: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _salvaged(path: Path) -> Path:
    return Path(str(path) + ".salvaged")


def _report_path(path: Path) -> Path:
    return Path(str(path) + ".salvage.json")


def _damaged(path: Path, n: int) -> Path:
    return Path(str(path) + (".damaged" if n == 1 else f".damaged{n}"))


def _walk(data: bytes, header: dict, crcs: list[int] | None) -> tuple[int, int, dict | None]:
    """(end of the good prefix, its record count, the first bad record's {offset, seq, kind, why} or None)."""
    off, n, last_key = journal.HEADER_SIZE, 0, None
    while off < len(data):
        left = len(data) - off
        if left < packs.PREFIX.size:
            return off, n, {"offset": off, "seq": None, "kind": None, "why": f"{left} bytes at the end are too short for a record"}
        kind, size = struct.unpack_from("<HH", data, off)
        if kind not in packs.KIND_NAMES or size < packs.PREFIX.size or (kind in packs.SIZE and size != packs.SIZE[kind]) \
                or (kind == packs.REFDATA_K and size < 32):
            return off, n, {"offset": off, "seq": None, "kind": None,
                            "why": f"corrupt record header: kind {kind}, size {size}"}
        name = packs.KIND_NAMES[kind]
        if size > left:
            return off, n, {"offset": off, "seq": None, "kind": name, "why": "the record runs past the end of the file"}
        seq = struct.unpack_from("<Q", data, off + 16)[0]
        if crcs is not None:
            if n >= len(crcs):
                return off, n, {"offset": off, "seq": seq, "kind": name, "why": "it has no entry in the checksum file"}
            if crcs[n] != zlib.crc32(data[off:off + size]):
                return off, n, {"offset": off, "seq": seq, "kind": name,
                                "why": "it does not match its checksum in the checksum file"}
        try:
            rec, _ = packs.decode(data, off)
        except (packs.PackError, struct.error, ValueError, KeyError) as e:
            return off, n, {"offset": off, "seq": seq, "kind": name, "why": f"it cannot be decoded: {e}"}
        if rec["seq"] != n + 1:
            return off, n, {"offset": off, "seq": seq, "kind": name,
                            "why": f"it has seq {rec['seq']}, the next one is {n + 1}"}
        key = (rec["t_event"], packs.RANK[kind], rec["seq"])
        if last_key is not None and key < last_key:
            return off, n, {"offset": off, "seq": seq, "kind": name, "why": "it sorts before the record before it"}
        last_key = key
        n += 1
        off += size
    return off, n, None


def _dropped(data: bytes, start: int) -> tuple[int, bool, dict, list[dict]]:
    """What follows the first bad record, as far as sizes allow it to be walked: (records, exact, {kind: n}, the first
    twenty as {offset, seq, kind, t})."""
    off, n, by_kind, first = start, 0, {}, []
    exact = True
    while off < len(data):
        if len(data) - off < packs.PREFIX.size:
            exact = False
            break
        kind, size = struct.unpack_from("<HH", data, off)
        if kind not in packs.KIND_NAMES or size < packs.PREFIX.size or off + size > len(data):
            exact = False
            break
        _, _, _, t, seq = packs.PREFIX.unpack_from(data, off)
        name = packs.KIND_NAMES[kind]
        by_kind[name] = by_kind.get(name, 0) + 1
        if len(first) < 20:
            first.append({"offset": off, "seq": seq, "kind": name, "t_ns": t})
        n += 1
        off += size
    return n, exact, by_kind, first


def salvage(path: Path) -> SalvageReport:
    """Owner-run, never called by the daemon: write the good prefix of a journal that holds a damaged record with
    good records after it, as `<journal>.salvaged` (a valid open journal, with its checksum file), and the report
    `<journal>.salvage.json` of what was dropped and why. The journal itself is only read. Refuses a journal that
    reads cleanly, one that already has a salvaged copy beside it, and one whose trouble is not a damaged record
    (a missing checksum file is for `journal.adopt`)."""
    path = Path(path)
    copy, rpath = _salvaged(path), _report_path(path)
    for p in (copy, Path(str(copy) + ".crc"), rpath):
        if p.exists():
            raise SalvageError(f"{p.name} is already beside the journal: adopt it, or move it away first")
    try:
        journal.read(path)
    except journal.JournalError as e:
        reason = str(e)
    else:
        raise SalvageError(f"{path.name} reads cleanly: there is nothing to salvage (a torn tail is repaired by "
                           "reopening the journal)")
    data = path.read_bytes()
    try:
        header = journal._check_header(data)
        crcs = journal._load_crcs(path, header, data)
    except journal.JournalError as e:
        raise SalvageError(f"{path.name} cannot be salvaged: {e} (a missing or short checksum file is repaired "
                           "with journal.adopt, a foreign header cannot be repaired)") from e
    end, kept, bad = _walk(data, header, crcs)
    if bad is None:
        raise SalvageError(f"every record of {path.name} checks out; what is wrong is not a damaged record: {reason}")
    dropped, exact, by_kind, first = _dropped(data, end)
    writer = None
    try:
        writer = journal.JournalWriter(copy, fsync_every_mono_ns=1 << 62, mono=time.monotonic_ns,
                                       price_basis=header["price_basis"])
        pos, batch = journal.HEADER_SIZE, []
        while pos < end:
            size = struct.unpack_from("<H", data, pos + 2)[0]
            kind, _, _, t, _ = packs.PREFIX.unpack_from(data, pos)
            batch.append((t, packs.RANK[kind], data[pos:pos + size]))
            pos += size
            if len(batch) >= 2000:
                writer.append(batch)
                batch = []
        if batch:
            writer.append(batch)
        writer.close(finalise=False)
        writer = None
        if copy.read_bytes()[journal.HEADER_SIZE:] != data[journal.HEADER_SIZE:end]:
            raise SalvageError("the salvaged copy is not byte for byte the original's good records; it was removed")
    except BaseException:
        if writer is not None:
            writer.close(finalise=False)
        for p in (copy, Path(str(copy) + ".crc")):
            p.unlink(missing_ok=True)
        raise
    crc = journal._crc_path(path)
    text = {
        "schema": "stockify.journal_salvage/1",
        "journal": path.name,
        "original_bytes": len(data),
        "original_sha256": hashlib.sha256(data).hexdigest(),
        "original_crc_sha256": _sha256(crc) if crc.exists() else None,
        "original_finalised": journal._final(header),
        "salvaged_sha256": _sha256(copy),
        "salvaged_crc_sha256": _sha256(Path(str(copy) + ".crc")),
        "kept_records": kept,
        "kept_bytes": end - journal.HEADER_SIZE,
        "first_bad": bad,
        "dropped_records": dropped,
        "dropped_records_exact": exact,
        "dropped_bytes": len(data) - end,
        "dropped_by_kind": dict(sorted(by_kind.items())),
        "dropped_after": first,
        "why_dropped": "the first bad record breaks the journal's sequence, so nothing after it can be replayed in "
                       "order; the records after it are not renumbered or rewritten, they stay in the original",
        "next_step": "Read this report. If the prefix is right, stop the daemon and run adopt_salvage on the journal: "
                     "the damaged journal is kept beside it as .damaged, never deleted.",
    }
    tmp = rpath.with_name(rpath.name + ".tmp")
    tmp.write_text(json.dumps(text, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, rpath)
    log.warning("journal %s: salvaged %d records into %s; %d records (%d bytes) dropped; see %s", path.name, kept,
                copy.name, dropped, len(data) - end, rpath.name)
    return SalvageReport(path, copy, rpath, kept, dropped, bad["offset"], bad["why"])


def _preserve(path: Path, want_sha: str | None, *, crc: bool = False) -> None:
    """Make sure a byte-identical copy of the damaged file is kept as `<journal>.damaged[N]` (its checksum file as
    `.damaged[N].crc`); one already there from an earlier attempt is not duplicated."""
    if want_sha is None:
        return
    n = 1
    while True:
        target = _damaged(path, n)
        target = Path(str(target) + ".crc") if crc else target
        if not target.exists():
            break
        if _sha256(target) == want_sha:
            return
        n += 1
    tmp = target.with_name(target.name + ".tmp")
    src = journal._crc_path(path) if crc else path
    shutil.copyfile(src, tmp)
    with open(tmp, "rb+") as f:
        os.fsync(f.fileno())
    os.replace(tmp, target)


def adopt_salvage(path: Path) -> None:
    """Owner-run, with the daemon stopped: put the salvaged copy in the journal's place (see the module doc). The
    daemon's lock and the day's finalise claim are taken first (`AlreadyRunning` if the daemon is up); the journal
    must still be exactly the one that was salvaged. The damaged journal and its checksum file are kept as
    `<journal>.damaged[N]`. A crash between the steps is finished by running this again."""
    from stockify.daemon import finalise, singleton
    path = Path(path)
    copy, rpath = _salvaged(path), _report_path(path)
    if not rpath.exists():
        raise SalvageError(f"nothing was salvaged for {path.name}: there is no {rpath.name}")
    rep = json.loads(rpath.read_text(encoding="utf-8"))
    crc, copy_crc = journal._crc_path(path), Path(str(copy) + ".crc")
    with singleton.acquire():
        claim = finalise.try_claim(path.with_suffix(".finalise.lock"), 0.5)
        if claim is None:
            raise SalvageError(f"{path.name} is being finalised: try again when that is over")
        try:
            live = _sha256(path)
            if live == rep["original_sha256"]:
                if not copy.exists() or _sha256(copy) != rep["salvaged_sha256"]:
                    raise SalvageError(f"{copy.name} is missing or is not the copy that was reported: salvage again")
                _preserve(path, rep["original_sha256"])
                if crc.exists():
                    _preserve(path, rep["original_crc_sha256"], crc=True)
                os.replace(copy, path)
            elif live != rep["salvaged_sha256"]:
                raise SalvageError(f"{path.name} is neither the journal that was salvaged nor its salvaged copy: it "
                                   "changed since; salvage it again")
            if not crc.exists() or _sha256(crc) != rep["salvaged_crc_sha256"]:
                if not copy_crc.exists() or _sha256(copy_crc) != rep["salvaged_crc_sha256"]:
                    raise SalvageError(f"{copy_crc.name} is missing or is not the copy that was reported")
                if crc.exists():
                    _preserve(path, rep["original_crc_sha256"], crc=True)
                os.replace(copy_crc, crc)
            os.replace(rpath, Path(str(path) + ".salvage.adopted.json"))
        finally:
            claim.release()
    log.warning("journal %s: the salvaged copy (%d records) is now the journal; the damaged one is kept as %s",
                path.name, rep["kept_records"], _damaged(path, 1).name)
