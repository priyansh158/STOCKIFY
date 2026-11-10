"""Compaction: a finished day's landing file → a verified Parquet archive (M1.7;
A1_schemas.md §3.2, D-09, I-13).

For one trade date:

1. `PRAGMA quick_check` on `ticks/landing/ticks_YYYYMMDD.sqlite`.
2. Stream packets in `seq` order and split them:
   * `full.parquet`  tradable 184-byte packets: every landing column plus the
     5-level depth parsed from the raw bytes (`bid_px_1..5`, `bid_qty_1..5`,
     `bid_orders_1..5`, `ask_*`)
   * `quote.parquet` other tradable packets (44-byte quote, 8-byte LTP)
   * `index.parquet` index packets (28/32 bytes), with the signed `change`
   Copies of `gaps`, `subscription_log` and `heartbeats`, and `meta.json`.
   zstd (`storage.json` → `parquet_zstd_level`), row groups of about 1M rows.
3. Verify: re-read the Parquet files, merge them by `seq`, and compare row
   counts and a SHA-256 over (seq, frame_no, instrument_id, raw) with the same
   digest computed from the landing file.
4. Record the result in `refdata.sqlite` → `tick_days`.

The archive is written to a temporary folder and moved into place only after
verification. Landing files are deleted only by `apply_retention`: once the day is verified
(and re-verified just before deletion), the landing file still holds exactly the
archived number of packets, and the day is at least `storage.json` →
`retention_days` old; 0 means right after the close (D-38), null never.
After that the archive is the day's only record and every reader uses it
(`stockify.data.ticks`).

An archive is never made smaller (I-13). A day that is `verified` or
`landing_deleted` is not compacted again unless `force=True`, and no run, forced
or not, replaces an archive with one that holds fewer packets. A landing file
that turns up for a `landing_deleted` day is left where it is and reported.

Replacing an archive never deletes it first. The old folder is renamed aside
(`old.date=YYYY-MM-DD`, next to it), the new one is renamed into place, checked
there and recorded in `tick_days`, and only then is the old one removed. If a
step fails the old archive is renamed back and the run reports `failed`. A run
that was killed part-way is undone by the next `compact()` or `apply_retention`
for the day, before it does anything else (`_recover`).
"""
from __future__ import annotations

import hashlib
import heapq
import json
import logging
import shutil
import sqlite3
import struct
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from stockify import numerics, paths, timeutil
from stockify.config import loader
from stockify.daemon import kite_packets as kp
from stockify.data import refdata

log = logging.getLogger("stockify.data.compact")

COMPACT_VERSION = "2"
# D-37: each row group covers a contiguous seq range; inside it rows are ordered by
# (instrument_id, seq) and integers are delta-encoded. Readers sort by seq.
LAYOUT = "rowgroup_by_instrument_delta"
ROW_GROUP = 1_000_000
FETCH = 100_000
ARCHIVED = ("verified", "landing_deleted")     # tick_days statuses of a day whose archive is the record
PACKET_FILES = ("full", "quote", "index")

PACKET_COLS = ["seq", "frame_no", "recv_ns", "mono_ns", "instrument_id", "instrument_token", "mode",
               "pkt_len", "ltp", "last_qty", "atp", "volume", "buy_qty", "sell_qty", "open", "high",
               "low", "close", "last_trade_ts_s", "oi", "oi_high", "oi_low", "exch_ts_s", "raw"]
DEPTH_COLS = ([f"bid_px_{i}" for i in range(1, 6)] + [f"bid_qty_{i}" for i in range(1, 6)]
              + [f"bid_orders_{i}" for i in range(1, 6)] + [f"ask_px_{i}" for i in range(1, 6)]
              + [f"ask_qty_{i}" for i in range(1, 6)] + [f"ask_orders_{i}" for i in range(1, 6)])
# index.parquet: A1 §3.2 columns plus mono_ns and instrument_token, so a replay
# from the archive can rebuild frames exactly (RECON 2026-11-05).
INDEX_COLS = ["seq", "frame_no", "recv_ns", "mono_ns", "instrument_id", "instrument_token", "ltp",
              "high", "low", "open", "close", "change", "exch_ts_s", "raw"]


def _schema(cols: list[str]) -> pa.Schema:
    return pa.schema([(c, pa.binary() if c == "raw" else pa.int64()) for c in cols])


SCHEMAS = {"full": _schema(PACKET_COLS + DEPTH_COLS), "quote": _schema(PACKET_COLS),
           "index": _schema(INDEX_COLS)}
TABLE_COPIES = {"gaps": "SELECT * FROM gaps ORDER BY gap_id",
                "subscriptions": "SELECT * FROM subscription_log ORDER BY rowid",
                "heartbeats": "SELECT * FROM heartbeats ORDER BY minute_ns"}


class CompactionError(RuntimeError):
    """The landing file is damaged or the archive does not match it."""


@dataclass
class Result:
    day: str
    status: str                       # verified | no_landing | session_open | failed
    packets: int = 0
    counts: dict = field(default_factory=dict)
    landing_bytes: int = 0
    archive_bytes: int = 0
    digest: str = ""
    archive_path: str = ""
    error: str = ""


# ── digest ───────────────────────────────────────────────────────────────────

_ROW = struct.Struct(">qqqI")


def _digest_update(h, seq: int, frame_no: int, instrument_id: int | None, raw: bytes) -> None:
    h.update(_ROW.pack(seq, frame_no, -1 if instrument_id is None else instrument_id, len(raw)))
    h.update(raw)


# ── writing ──────────────────────────────────────────────────────────────────

class _Sink:
    def __init__(self, path: Path, schema: pa.Schema, level: int):
        self.path, self.schema, self.level = path, schema, level
        self.cols = {name: [] for name in schema.names}
        self.writer: pq.ParquetWriter | None = None
        self.rows = 0

    def add(self, values: dict) -> None:
        for name, col in self.cols.items():
            col.append(values.get(name))
        self.rows += 1
        if len(self.cols["seq"]) >= ROW_GROUP:
            self.flush()

    def flush(self) -> None:
        if not self.cols["seq"]:
            return
        table = pa.table({n: pa.array(v, type=self.schema.field(n).type) for n, v in self.cols.items()},
                         schema=self.schema)
        order = pc.sort_indices(table, sort_keys=[("instrument_id", "ascending"), ("seq", "ascending")],
                                null_placement="at_start")
        table = table.take(order)
        if self.writer is None:
            ints = [f.name for f in self.schema if f.name != "raw"]
            self.writer = pq.ParquetWriter(self.path, self.schema, compression="zstd",
                                           compression_level=self.level, use_dictionary=False,
                                           column_encoding={c: "DELTA_BINARY_PACKED" for c in ints})
        self.writer.write_table(table, row_group_size=ROW_GROUP)
        self.cols = {name: [] for name in self.schema.names}

    def close(self) -> None:
        self.flush()
        if self.writer is None:          # an empty but valid file keeps the layout uniform
            pq.write_table(self.schema.empty_table(), self.path, compression="zstd")
        else:
            self.writer.close()


def _route(row: sqlite3.Row) -> tuple[str, dict]:
    values = {c: row[c] for c in PACKET_COLS}
    raw = bytes(row["raw"])
    values["raw"] = raw
    if row["pkt_len"] in kp.INDEX_LENGTHS:
        values["change"] = kp.index_change(raw)
        return "index", values
    if row["pkt_len"] == 184:
        depth = kp.parse_depth(raw)
        for i, (qty, px, orders) in enumerate(depth[:5], 1):
            values[f"bid_qty_{i}"], values[f"bid_px_{i}"], values[f"bid_orders_{i}"] = qty, px, orders
        for i, (qty, px, orders) in enumerate(depth[5:], 1):
            values[f"ask_qty_{i}"], values[f"ask_px_{i}"], values[f"ask_orders_{i}"] = qty, px, orders
        return "full", values
    return "quote", values


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _copy_table(conn: sqlite3.Connection, sql: str, path: Path, level: int) -> int:
    cur = conn.execute(sql)
    names = [d[0] for d in cur.description]
    rows = cur.fetchall()
    table = pa.table({n: [r[i] for r in rows] for i, n in enumerate(names)}) if rows else \
        pa.table({n: pa.array([], type=pa.int64() if n.endswith(("_ns", "_id", "count", "token"))
                                else pa.string()) for n in names})
    pq.write_table(table, path, compression="zstd", compression_level=level)
    return len(rows)


# ── verification ─────────────────────────────────────────────────────────────

def iter_rows(path: Path, columns: list[str]):
    """Rows of one packet file in seq order (D-37: re-sort each row group by seq)."""
    pf = pq.ParquetFile(path)
    if "seq" not in columns:
        columns = ["seq", *columns]
    last = 0
    for i in range(pf.num_row_groups):
        group = pf.read_row_group(i, columns=columns)
        group = group.take(pc.sort_indices(group, sort_keys=[("seq", "ascending")]))
        cols = group.to_pydict()
        if cols["seq"] and cols["seq"][0] <= last:
            raise CompactionError(f"{path.name}: row group {i} overlaps the previous seq range")
        if cols["seq"]:
            last = cols["seq"][-1]
        yield from zip(*(cols[c] for c in columns))


def _iter_archive(folder: Path):
    """(seq, frame_no, instrument_id, raw) merged across the three packet files by seq."""
    cols = ["seq", "frame_no", "instrument_id", "raw"]
    return heapq.merge(*(iter_rows(folder / f"{name}.parquet", cols) for name in PACKET_FILES),
                       key=lambda r: r[0])


def verify(folder: Path, expected_digest: str, expected_rows: int) -> None:
    h = hashlib.sha256()
    n, last = 0, 0
    for seq, frame_no, iid, raw in _iter_archive(folder):
        if seq <= last:
            raise CompactionError(f"archive seq not strictly increasing at {seq}")
        last = seq
        _digest_update(h, seq, frame_no, iid, raw)
        n += 1
    if n != expected_rows:
        raise CompactionError(f"archive has {n} packets, landing has {expected_rows}")
    if h.hexdigest() != expected_digest:
        raise CompactionError("archive digest does not match the landing file")
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    for name, digest in meta["files"].items():
        if _sha256_file(folder / name) != digest:
            raise CompactionError(f"{name} changed after it was written")


# ── the job ──────────────────────────────────────────────────────────────────

def _record(res: Result, day: date, landing: Path) -> None:
    with refdata.transaction() as conn:
        conn.execute(
            "INSERT INTO tick_days (trade_date, landing_path, archive_path, status, packets, landing_bytes, "
            "archive_bytes, digest, verified_at_ns) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (trade_date) DO UPDATE SET landing_path=excluded.landing_path, "
            "archive_path=excluded.archive_path, status=excluded.status, packets=excluded.packets, "
            "landing_bytes=excluded.landing_bytes, archive_bytes=excluded.archive_bytes, "
            "digest=excluded.digest, verified_at_ns=excluded.verified_at_ns",
            (day.isoformat(), str(landing), res.archive_path or None,
             "verified" if res.status == "verified" else "landing", res.packets, res.landing_bytes,
             res.archive_bytes or None, res.digest or None,
             timeutil.now_ns() if res.status == "verified" else None))


def _has_archive(row) -> bool:
    """True when the day's tick_days row says an archive was made for it, whatever its status is now."""
    return row is not None and (row["status"] in ARCHIVED or bool(row["archive_path"]))


def _tmp(target: Path) -> Path:
    """Where a run builds the day's archive before it is moved into place."""
    return target.with_name(f"{target.name}.tmp")


def _aside(target: Path) -> Path:
    """Where the old archive waits while its replacement is moved in, checked and recorded.

    A sibling of the archive folder, so the moves are renames on one volume. The name does not start with
    `date=`, so no reader takes it for a day's archive.
    """
    return target.with_name(f"old.{target.name}")


def _archive_state(folder: Path) -> tuple[int, str] | None:
    """(packets, landing digest) of a complete archive folder; None when it is missing or incomplete.

    Complete: meta.json can be read, every file it lists is there, and the packet files hold the number of
    packets it states. Contents are not hashed here; `verify` does that.
    """
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        packets, digest = int(meta["packets"]), str(meta["landing_digest"])
        if not all((folder / name).is_file() for name in meta["files"]):
            return None
        in_files = sum(pq.read_metadata(folder / f"{name}.parquet").num_rows for name in PACKET_FILES)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, pa.ArrowException):
        return None
    return (packets, digest) if in_files == packets else None


def _recover(target: Path, recorded_digest: str | None) -> str:
    """Finish or undo a replacement that was interrupted (I-13). Returns what it did, "" for nothing.

    While an archive is replaced the old one waits in `_aside(target)`. When that folder is still there:
    * the archive in place is complete and is the one tick_days describes: the replacement was recorded and
      only the removal of the old archive is left, so it is removed now;
    * otherwise, if the set-aside archive is complete, it is put back as the day's archive, and what stood
      in its place (an incomplete folder, or a new archive that was never recorded) is removed;
    * otherwise no complete archive is known: CompactionError naming both folders, and nothing is changed.
    """
    aside = _aside(target)
    if not aside.exists():
        return ""
    live = _archive_state(target)
    if live is not None and recorded_digest and live[1] == recorded_digest:
        try:
            shutil.rmtree(aside)
        except OSError as e:
            return f"the replaced archive could not be removed and is still in {aside}: {e}"
        log.info("removed the replaced archive left in %s", aside)
        return ""
    if _archive_state(aside) is None:
        raise CompactionError(
            f"an interrupted replacement left an incomplete archive in {aside}, and the archive in {target} is "
            f"{'not the one recorded for the day' if live else 'missing or incomplete'}; nothing was changed")
    tmp = _tmp(target)
    displaced = target.exists()
    try:
        if displaced:
            shutil.rmtree(tmp, ignore_errors=True)
            target.rename(tmp)
        aside.rename(target)
    except OSError as e:
        if displaced and tmp.exists() and not target.exists():
            try:
                tmp.rename(target)
            except OSError:
                pass
        raise CompactionError(f"the archive set aside in {aside} could not be put back as {target}: {e}; "
                              "nothing else was changed") from e
    shutil.rmtree(tmp, ignore_errors=True)
    return (f"an interrupted replacement was undone: the archive set aside in {aside} was put back as {target}"
            + (", in place of a folder that was incomplete or never recorded" if displaced else ""))


def _put_back(target: Path, replaced: bool) -> str:
    """Undo a move into place that failed: take the new archive out and rename the old one back.

    Returns "" when the archive folder is as it was before the run, else what is left where; the next run's
    `_recover` then finishes the job.
    """
    tmp, aside = _tmp(target), _aside(target)
    try:
        if target.exists():
            target.rename(tmp)
        if replaced:
            aside.rename(target)
    except OSError as e:
        return (f"it could not be undone ({e}): the old archive is in {aside} and the next run puts it back"
                if replaced else f"the new folder could not be taken out again ({e})")
    shutil.rmtree(tmp, ignore_errors=True)
    return ""


def _landing_size(landing: Path) -> int:
    return sum(p.stat().st_size for p in (landing, Path(str(landing) + "-wal")) if p.exists())


def _landing_packets(landing: Path) -> int:
    """Packets in a landing file, counted through a read-only connection."""
    conn = sqlite3.connect(f"file:{landing.as_posix()}?mode=ro", uri=True, timeout=30.0)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM packets").fetchone()[0])
    finally:
        conn.close()


def _archived_packets(target: Path, row: sqlite3.Row | None) -> int | None:
    """The most packets any record says the day's archive holds (I-13: the floor for a replacement).

    The archive on disk decides, whatever the row's status: the count in its meta.json, and the sum of its
    packet files' Parquet footers when all of them can be read. The row's count is added for a day whose row
    says an archive was made, so an archive that has gone missing is not papered over by a smaller landing file.

    None when an archive folder exists and neither source gives its count: an unreadable count is never 0.
    """
    known = []
    if _has_archive(row) and row["packets"] is not None:
        known.append(int(row["packets"]))
    if target.exists():
        on_disk = []
        try:
            on_disk.append(int(json.loads((target / "meta.json").read_text(encoding="utf-8"))["packets"]))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            on_disk.append(sum(pq.read_metadata(target / f"{name}.parquet").num_rows for name in PACKET_FILES))
        except (OSError, ValueError, pa.ArrowException):
            pass
        if not on_disk:
            return None
        known += on_disk
    return max(known, default=0)


def compact(day: date, *, landing: Path | None = None, archive_root: Path | None = None,
            force: bool = False) -> Result:
    """Build and verify the day's archive from its landing file.

    A day already `verified` or `landing_deleted` is left alone and reported as `verified` unless `force` is
    given. With or without `force`, an archive is never replaced by one holding fewer packets: the result is
    `failed`, its error names both counts, and only this run's temporary folder is removed (I-13). The count
    to beat is the one of the archive on disk, whatever the row's status; an archive folder whose count cannot
    be read is not replaced at all, forced or not.

    A run that fails on a day that has, or had, an archive leaves its tick_days row exactly as it was; the
    failure is in the result only.

    An archive is replaced by renames, never by deleting it first (module docstring). A replacement that
    an earlier run left half done is undone before anything else; `error` then says so, whatever the status.
    """
    landing = landing or paths.ticks_landing_file(day)
    archive_root = archive_root or paths.ticks_archive_dir()
    target = archive_root / f"date={day:%Y-%m-%d}"
    res = Result(day=day.isoformat(), status="no_landing")
    existing = refdata._connect().execute(
        "SELECT status, digest, packets, archive_path FROM tick_days WHERE trade_date=?",
        (day.isoformat(),)).fetchone()
    notes: list[str] = []                       # what the owner should know, whatever the status

    def done(status: str, text: str = "") -> Result:
        res.status, res.error = status, "; ".join(n for n in (*notes, text) if n)
        return res

    try:
        note = _recover(target, existing["digest"] if existing else None)
    except CompactionError as e:
        log.error("%s: %s", day, e)
        return done("failed", str(e))
    if note:
        notes.append(note)
        log.warning("%s: %s", day, note)
    if not landing.exists():
        if existing and existing["status"] == "landing_deleted" and (target / "meta.json").exists():
            res.digest, res.packets, res.archive_path = existing["digest"], existing["packets"], str(target)
            return done("verified")
        return done("no_landing")
    if landing == paths.ticks_landing_file(day) and not session_over(day):
        return done("session_open", "the day is still being recorded; compact it after the close")
    level = int(loader.load_json("storage.json")["parquet_zstd_level"])
    if existing and existing["status"] in ARCHIVED and target.exists() and not force:
        res.digest, res.packets, res.archive_path = existing["digest"], existing["packets"], str(target)
        if existing["status"] == "landing_deleted":
            # The day's landing file was deleted after verification, yet one is here again. It is not the
            # day's record: leave it where it is, say so, and keep the archive (I-13).
            try:
                found = f"{_landing_packets(landing)} packets"
            except sqlite3.Error as e:
                found = f"unreadable: {e}"
            stray = (f"a landing file is present again ({found}) although the day's landing file was deleted "
                     f"after its archive was verified ({existing['packets']} packets); the file was left in "
                     f"place and not compacted, and the archive is unchanged: {landing}")
            log.warning("%s: %s", day, stray)
            return done("verified", stray)
        log.info("%s already compacted and verified", day)
        return done("verified")

    archived = _archived_packets(target, existing)
    if archived is None:
        refusal = (f"refused: the packet count of the archive in {target} cannot be read (neither its meta.json "
                   "nor its Parquet footers), and an unreadable count is never taken as 0; the archive is not "
                   "replaced, with or without force, and its tick_days row and the landing file are left as they "
                   "were")
        log.error("compaction of %s %s", day, refusal)
        return done("failed", refusal)
    tmp = _tmp(target)
    if not target.exists() and _has_archive(existing) and _archive_state(tmp) is not None:
        # Versions before the rename-aside replacement removed the old archive first; one stopped right there
        # leaves the new archive here and nothing in place. It may be the day's only archive: keep it.
        refusal = (f"{tmp} holds a complete archive while the day's archive folder {target} is missing; it may be "
                   "the day's only archive, so it was not deleted: rename it to the archive folder to keep it, or "
                   "remove it, then run again")
        log.error("compaction of %s: %s", day, refusal)
        return done("failed", refusal)
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    conn = sqlite3.connect(str(landing), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise CompactionError(f"landing quick_check failed: {check}")
        sinks = {name: _Sink(tmp / f"{name}.parquet", schema, level) for name, schema in SCHEMAS.items()}
        h = hashlib.sha256()
        cur = conn.execute(f"SELECT {', '.join(PACKET_COLS)} FROM packets ORDER BY seq")
        while True:
            rows = cur.fetchmany(FETCH)
            if not rows:
                break
            for row in rows:
                kind, values = _route(row)
                sinks[kind].add(values)
                _digest_update(h, row["seq"], row["frame_no"], row["instrument_id"], bytes(row["raw"]))
        for s in sinks.values():
            s.close()
        res.counts = {name: s.rows for name, s in sinks.items()}
        res.packets = sum(res.counts.values())
        res.digest = h.hexdigest()
        for name, sql in TABLE_COPIES.items():
            res.counts[name] = _copy_table(conn, sql, tmp / f"{name}.parquet", level)
        meta_rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM recorder_meta")}
    finally:
        conn.close()

    if res.packets < archived:
        shutil.rmtree(tmp, ignore_errors=True)
        refusal = (f"refused: the landing file holds {res.packets} packets and the day's archive holds {archived}; "
                   "an archive is never replaced by a smaller one, so the archive and the landing file are "
                   "left as they were")
        log.error("compaction of %s %s", day, refusal)
        return done("failed", refusal)

    files = {p.name: _sha256_file(p) for p in sorted(tmp.glob("*.parquet"))}
    (tmp / "meta.json").write_text(numerics.dumps_canonical({
        "trade_date": day.isoformat(), "compact_version": COMPACT_VERSION, "layout": LAYOUT,
        "parser_version": kp.PARSER_VERSION, "row_counts": res.counts, "packets": res.packets,
        "landing_digest": res.digest, "files": files, "recorder_meta": meta_rows,
        "created_ns": timeutil.now_ns(),
    }) + "\n", encoding="utf-8")
    try:
        verify(tmp, res.digest, res.packets)
    except CompactionError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        if not target.exists() and not _has_archive(existing):
            # A first compaction: note the day as still in its landing file. A day that has, or had, an
            # archive keeps its row exactly as it was (I-13): the failure is reported in the result only.
            res.status = "failed"
            res.landing_bytes = _landing_size(landing)
            _record(res, day, landing)
        log.error("compaction of %s failed verification: %s (landing kept)", day, e)
        return done("failed", str(e))

    # Into place (I-13). The old archive is never deleted first: it is renamed aside, the new one is renamed
    # in, checked where it now lives and recorded, and only then is the old one removed. Until the row is
    # written, any failure puts the old archive back; a run killed in between is undone by _recover.
    aside = _aside(target)
    replaced = target.exists()
    if replaced:
        try:
            target.rename(aside)
        except OSError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            refusal = (f"the archive in {target} could not be set aside for its replacement (a file in it may be "
                       f"open): {e}; the archive, its tick_days row and the landing file are left as they were")
            log.error("compaction of %s: %s", day, refusal)
            return done("failed", refusal)
    failure = None
    try:
        tmp.rename(target)
        if replaced:
            verify(target, res.digest, res.packets)
        res.status = "verified"
        res.archive_path = str(target)
        res.landing_bytes = _landing_size(landing)
        res.archive_bytes = sum(p.stat().st_size for p in target.iterdir())
        _record(res, day, landing)
    except Exception as e:  # noqa: BLE001  whatever went wrong, the old archive goes back
        failure = str(e) if isinstance(e, CompactionError) else f"{type(e).__name__}: {e}"
    if failure is not None:                     # outside the handler: the traceback would keep files open
        res.archive_path, res.archive_bytes = "", 0
        undone = _put_back(target, replaced)
        refusal = (f"the new archive could not be put in place: {failure}; "
                   + (undone or ("the old archive was put back and its tick_days row is unchanged" if replaced
                                 else "the new folder was removed")) + "; the landing file is kept")
        log.error("compaction of %s: %s", day, refusal)
        return done("failed", refusal)
    if replaced:
        try:
            shutil.rmtree(aside)
        except OSError as e:
            notes.append(f"the replaced archive could not be removed and is still in {aside}: {e}; "
                         "the next run removes it")
            log.warning("%s: %s", day, notes[-1])
    log.info("compacted %s: %d packets, %.1f MB -> %.1f MB (%.1fx), verified",
             day, res.packets, res.landing_bytes / 1e6, res.archive_bytes / 1e6,
             res.landing_bytes / max(1, res.archive_bytes))
    return done("verified")


# ── retention and disk space ─────────────────────────────────────────────────

def session_over(day: date, now_ns: int | None = None) -> bool:
    """True once the day's recording has stopped (schedule.json unsubscribe time)."""
    now_ns = timeutil.now_ns() if now_ns is None else now_ns
    unsubscribe = loader.load_json("schedule.json")["jobs"]["unsubscribe"]
    return now_ns >= timeutil.ist_ns(day, unsubscribe)


def apply_retention(today: date, now_ns: int | None = None) -> list[str]:
    """Delete the landing files of verified days (D-38).

    A landing file goes once its archive is verified and the day is at least
    `retention_days` old (0 = right after verification, once the session is
    over). The archive is re-verified against the digest in tick_days just
    before deleting; if that fails the day drops back to status 'landing' and
    the file is kept. The landing file's packets are then counted: unless the
    count equals the archived count, the file is kept, a warning is logged and
    the day is not in the returned list. `retention_days: null` deletes nothing.

    A replacement of the day's archive that was interrupted is undone first
    (`_recover`), with a warning; if that cannot be done the day is skipped.
    """
    days = loader.load_json("storage.json").get("retention_days")
    if days is None:
        return []
    cutoff = (today - timedelta(days=int(days))).isoformat()
    deleted = []
    rows = refdata._connect().execute(
        "SELECT trade_date, landing_path, archive_path, digest, packets FROM tick_days "
        "WHERE status='verified' AND trade_date <= ?", (cutoff,)).fetchall()
    for row in rows:
        day = date.fromisoformat(row["trade_date"])
        if not session_over(day, now_ns):
            continue                                   # still being recorded
        landing, archive = Path(row["landing_path"]), Path(row["archive_path"] or "")
        if row["archive_path"]:
            try:
                note = _recover(archive, row["digest"])
            except CompactionError as e:
                log.error("%s: %s; landing kept", row["trade_date"], e)
                continue
            if note:
                log.warning("%s: %s", row["trade_date"], note)
        try:
            verify(archive, row["digest"], row["packets"])
        except (CompactionError, FileNotFoundError, OSError, ValueError) as e:
            log.error("%s: archive failed re-verification (%s); landing kept", row["trade_date"], e)
            with refdata.transaction() as conn:
                conn.execute("UPDATE tick_days SET status='landing' WHERE trade_date=?", (row["trade_date"],))
            continue
        if landing.exists():
            try:
                found = f"{_landing_packets(landing)} packets"
            except sqlite3.Error as e:
                found = f"cannot be counted ({e})"
            if found != f"{row['packets']} packets":
                log.warning("%s: landing file %s: %s, but the verified archive holds %d packets; landing kept",
                            row["trade_date"], landing, found, row["packets"])
                continue
        for p in (landing, Path(str(landing) + "-wal"), Path(str(landing) + "-shm")):
            p.unlink(missing_ok=True)
        with refdata.transaction() as conn:
            conn.execute("UPDATE tick_days SET status='landing_deleted' WHERE trade_date=?", (row["trade_date"],))
        log.info("%s: landing file deleted; the verified archive is the record", row["trade_date"])
        deleted.append(row["trade_date"])
    return deleted


def disk_warnings(free_bytes: int | None = None, total_bytes: int | None = None) -> list[str]:
    cfg = loader.load_json("storage.json")
    if free_bytes is None or total_bytes is None:
        usage = shutil.disk_usage(paths.lab_dir())
        free_bytes, total_bytes = usage.free, usage.total
    out = []
    if free_bytes < float(cfg["disk_warn_free_gb"]) * 1e9:
        out.append(f"only {free_bytes / 1e9:.1f} GB free (warning below {cfg['disk_warn_free_gb']} GB)")
    if free_bytes < float(cfg["disk_warn_free_fraction"]) * total_bytes:
        out.append(f"only {100 * free_bytes / total_bytes:.1f}% of the disk free")
    return out
