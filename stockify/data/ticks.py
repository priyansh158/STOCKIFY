"""One way to read a recorded day, for every consumer (D-38).

    day = ticks.open_day(date(2026, 11, 5))
    for p in day.packets(columns=["seq", "recv_ns", "ltp"], instrument_ids={501}):
        ...

`open_day` returns the verified Parquet archive when it exists (the record
after the close), otherwise the landing file (only while a day is still being
recorded or before its compaction). Both sources answer the same questions
with identical results:

* `packets(columns, instrument_ids, kinds)`: rows as dicts in `seq` order;
  `kinds` ⊆ {"full", "quote", "index"} (184-byte, other tradable, index)
* `frames()`: (frame_no, recv_ns, mono_ns, instrument_token, raw) in `seq`
  order, for the replay ticker
* `gaps()`, `subscriptions()`, `heartbeats()`, `meta()`

Replay, the quality report (M1.9), event packs (M1.8) and backtests read ticks
only through this module.
"""
from __future__ import annotations

import heapq
import json
from datetime import date
from pathlib import Path

from stockify import paths

KINDS = ("full", "quote", "index")
PACKET_COLUMNS = ["seq", "frame_no", "recv_ns", "mono_ns", "instrument_id", "instrument_token", "mode",
                  "pkt_len", "ltp", "last_qty", "atp", "volume", "buy_qty", "sell_qty", "open", "high",
                  "low", "close", "last_trade_ts_s", "oi", "oi_high", "oi_low", "exch_ts_s", "raw"]
_INDEX_HAS = {"seq", "frame_no", "recv_ns", "mono_ns", "instrument_id", "instrument_token", "ltp", "high",
              "low", "open", "close", "change", "exch_ts_s", "raw"}


def kind_of(pkt_len: int) -> str:
    return "full" if pkt_len == 184 else "index" if pkt_len in (28, 32) else "quote"


class _Day:
    """`with ticks.open_day(d) as day:` closes the landing file afterwards (Windows
    cannot delete a file that is still open, D-38)."""

    def close(self) -> None:
        pass

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        self.close()


class ArchiveDay(_Day):
    source = "archive"

    def __init__(self, day: date, folder: Path):
        self.day, self.folder = day, folder

    def _stream(self, kind: str, columns: list[str], instrument_ids):
        from stockify.data import compact
        want = list(dict.fromkeys(["seq", *columns, *(["instrument_id"] if instrument_ids else [])]))
        present = [c for c in want if kind != "index" or c in _INDEX_HAS]
        for row in compact.iter_rows(self.folder / f"{kind}.parquet", present):
            rec = dict(zip(present, row))
            if instrument_ids and rec.get("instrument_id") not in instrument_ids:
                continue
            if kind == "index":
                rec.update({c: None for c in want if c not in rec})
            yield rec

    def packets(self, columns: list[str] | None = None, instrument_ids=None, kinds=KINDS):
        from stockify.daemon import kite_packets as kp
        columns = list(columns or [c for c in PACKET_COLUMNS if c != "raw"])
        derive = {"pkt_len", "mode"} & set(columns)            # index.parquet stores neither
        cols = list(dict.fromkeys(columns + (["raw"] if derive else [])))
        streams = [self._stream(k, cols, set(instrument_ids) if instrument_ids else None)
                   for k in KINDS if k in kinds]
        for rec in heapq.merge(*streams, key=lambda r: r["seq"]):
            if derive and rec.get("pkt_len") is None:
                rec["pkt_len"] = len(rec["raw"])
            if "mode" in derive and rec.get("mode") is None:
                rec["mode"] = kp.LENGTHS[rec["pkt_len"]]
            yield {c: rec.get(c) for c in columns}

    def frames(self):
        for rec in self.packets(["frame_no", "recv_ns", "mono_ns", "instrument_token", "raw"]):
            yield rec["frame_no"], rec["recv_ns"], rec["mono_ns"], rec["instrument_token"], rec["raw"]

    def _table(self, name: str) -> list[dict]:
        import pyarrow.parquet as pq
        t = pq.read_table(self.folder / f"{name}.parquet").to_pylist()
        return t

    def gaps(self) -> list[dict]:
        return self._table("gaps")

    def subscriptions(self) -> list[dict]:
        return self._table("subscriptions")

    def heartbeats(self) -> list[tuple[int, int]]:
        return sorted((r["minute_ns"], r["count"]) for r in self._table("heartbeats"))

    def meta(self) -> dict:
        return json.loads((self.folder / "meta.json").read_text(encoding="utf-8"))["recorder_meta"]

    def first_recv_ns(self) -> int | None:
        first = next(self.packets(["recv_ns"]), None)
        return first["recv_ns"] if first else None


class LandingDay(_Day):
    source = "landing"

    def close(self) -> None:
        self.db.close()

    def __init__(self, day: date, path: Path | None = None, *, db=None):  # noqa: ANN001
        from stockify.daemon.landing import LandingDB
        self.day = day
        self.db = db if db is not None else LandingDB(day, path, readonly=True)

    @classmethod
    def from_db(cls, db) -> "LandingDay":  # noqa: ANN001
        return cls(db.day, db=db)

    def packets(self, columns: list[str] | None = None, instrument_ids=None, kinds=KINDS):
        columns = list(columns or [c for c in PACKET_COLUMNS if c != "raw"])
        where, args = [], []
        if instrument_ids:
            ids = sorted(set(instrument_ids))
            where.append(f"instrument_id IN ({','.join('?' * len(ids))})")
            args += ids
        lens = []
        for k in kinds:
            lens += {"full": [184], "index": [28, 32], "quote": [8, 44]}[k]
        if set(kinds) != set(KINDS):
            where.append(f"pkt_len IN ({','.join('?' * len(lens))})")
            args += lens
        sql = f"SELECT {', '.join(columns)} FROM packets {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY seq"
        for row in self.db.conn.execute(sql, args):
            rec = {c: row[c] for c in columns}
            if "raw" in rec and rec["raw"] is not None:
                rec["raw"] = bytes(rec["raw"])
            yield rec

    def frames(self):
        for r in self.db.conn.execute("SELECT frame_no, recv_ns, mono_ns, instrument_token, raw FROM packets "
                                      "ORDER BY seq"):
            yield r["frame_no"], r["recv_ns"], r["mono_ns"], r["instrument_token"], bytes(r["raw"])

    def gaps(self) -> list[dict]:
        return [dict(r) for r in self.db.conn.execute("SELECT * FROM gaps ORDER BY gap_id")]

    def subscriptions(self) -> list[dict]:
        return [dict(r) for r in self.db.conn.execute("SELECT * FROM subscription_log ORDER BY rowid")]

    def heartbeats(self) -> list[tuple[int, int]]:
        return [(r["minute_ns"], r["count"]) for r in
                self.db.conn.execute("SELECT minute_ns, count FROM heartbeats ORDER BY minute_ns")]

    def meta(self) -> dict:
        return self.db.meta()

    def first_recv_ns(self) -> int | None:
        return self.db.conn.execute("SELECT MIN(recv_ns) FROM packets").fetchone()[0]


def archive_folder(day: date) -> Path:
    return paths.ticks_archive_dir() / f"date={day:%Y-%m-%d}"


def open_day(day: date, *, prefer: str = "archive", landing_path: Path | None = None):
    """The day's ticks: the verified archive first, the landing file otherwise.

    Returns None when the day was never recorded.
    """
    folder = archive_folder(day)
    has_archive = (folder / "meta.json").exists()
    landing = landing_path or paths.ticks_landing_file(day)
    if prefer == "archive" and has_archive and landing_path is None:
        return ArchiveDay(day, folder)
    if landing.exists():
        return LandingDay(day, landing_path)
    if has_archive:
        return ArchiveDay(day, folder)
    return None


def has_day(day: date) -> bool:
    """Whether the day was recorded (archive or landing), without opening anything."""
    return (archive_folder(day) / "meta.json").exists() or paths.ticks_landing_file(day).exists()


def available_days() -> list[date]:
    days = set()
    for folder in paths.ticks_archive_dir().glob("date=*"):
        if (folder / "meta.json").exists():
            days.add(date.fromisoformat(folder.name[5:]))
    for f in paths.ticks_landing_dir().glob("ticks_*.sqlite"):
        days.add(date(int(f.stem[6:10]), int(f.stem[10:12]), int(f.stem[12:14])))
    return sorted(days)
