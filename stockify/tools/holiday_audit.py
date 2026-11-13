"""The calendar against the lab's own data (D-75): `python -m stockify calendar audit --from D --to D`.

A weekday on which NSE traded leaves data in the lab; a weekday on which it was closed leaves none. For a date
range the audit lists the two ways the holiday list (`calendar/nse_holidays.csv`) and the data can disagree:

* a weekday the calendar calls a trading day that has no data in any store: a holiday the list lacks, or a gap
  in the archive (the 2009-2023 rows were made from this list; 2021-10-11 is the one known gap);
* a weekday the calendar calls a holiday that has data: a Muhurat or other special session (the owner's standing
  decision is no special sessions, so such a day stays a holiday), or a wrong row.

What counts as data for a day:

    span         the end-of-day SPAN file nsccl.<YYYYMMDD>.s.zip (intraday files are made ahead of the day)
    bars         any bar in bars.sqlite that starts on the day (IST)
    fo_eod, cm_eod, index_eod    any row of the day in refdata.sqlite
    raw:<set>    any NSE raw file of the day under nse/raw/<set>, for the datasets NSE publishes per trade date
                 (sources.json; a `current_only` dataset and the Kite instrument dumps are named by the day they
                 were downloaded, which says nothing about the exchange)

Saturdays and Sundays are ignored. The audit only reads: files are tested for existence, nothing is fetched, and
a database is opened only as `mode=ro&immutable=1`, which makes and touches no file beside it (stockify.paths makes
a lab folder that is missing, as for every reader; no file is written). An immutable connection cannot see rows a
writer still has in the `-wal` (or under a rollback journal), so a store with such a file is not opened at all: the
report names it "busy, not checked" and the command exits 1.

Run the audit only while no recorder or daemon is writing. An immutable read takes no lock, so a write that starts
under it can make the report wrong, never the stores. (The audit looks at the `-wal` again after the read and drops
what it read if one appeared; a writer that opens, writes and closes inside the read is not seen.)

Recorded ticks are not looked at: they begin long after the other stores. The dates themselves live only in the
config; this module holds none.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TypeVar

from stockify import paths, timeutil
from stockify.config import loader
from stockify.data import calendar, nse_files, nse_margin_files

EOD_TABLES = ("fo_eod", "cm_eod", "index_eod")
WRITER_FILES = ("-wal", "-journal")                            # beside a database: not empty while a write is open

T = TypeVar("T")


@dataclass(frozen=True)
class Audit:
    start: date
    end: date
    segment: str
    weekdays: int                                              # Mondays to Fridays in the range
    with_data: int                                             # of those, days with data in any store
    holidays: int                                              # of those, days the calendar calls a holiday
    silent_trading_days: tuple[date, ...]                      # calendar trading days with no data
    holidays_with_data: tuple[tuple[date, str, tuple[str, ...]], ...]    # (day, the list's description, stores)
    by_year: tuple[tuple[int, int, int, int], ...]             # (year, weekdays, with data, holidays)
    not_checked: tuple[tuple[str, str], ...] = ()              # (database file, why it was not read)


def _busy(path: Path) -> str | None:
    """Why the database at `path` is not to be read now, or None. A writer has rows outside the main file."""
    for suffix in WRITER_FILES:
        side = path.with_name(path.name + suffix)
        if side.is_file() and side.stat().st_size > 0:
            return f"busy, not checked (its {suffix} file is not empty)"
    return None


def _read(path: Path, query: Callable[[sqlite3.Connection], T], nothing: T, not_checked: list) -> T:
    """`query` over the database at `path`, or `nothing` when the file is not there or was not read. A database
    that was not read is added to `not_checked` as (file name, why).

    The stores are WAL databases, and even a `mode=ro` connection to one makes the missing `-wal`/`-shm` files and
    writes the `-shm`. So the file is only ever opened as immutable, which makes and touches nothing beside it and
    reads the main file alone. That is the whole store while the `-wal` is missing or empty. When it is not, the
    store is busy and is not opened. The `-wal` is looked at again after the read, because an immutable read takes
    no lock and a writer can start under it.
    """
    if not path.is_file():
        return nothing
    why = _busy(path)
    if why is None:
        got = nothing
        try:
            conn = sqlite3.connect(paths.readonly_uri(path, immutable=True), uri=True)
            try:
                got = query(conn)
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:                   # not a database, or a page torn by a checkpoint
            why = f"not readable, not checked ({exc})"
        why = why or _busy(path)
        if why is None:
            return got
    not_checked.append((path.name, why))
    return nothing


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _bar_days(start: date, end: date, not_checked: list) -> set[date]:
    def query(conn: sqlite3.Connection) -> set[date]:
        if not _has_table(conn, "bars"):
            return set()
        rows = conn.execute("SELECT DISTINCT ts_ns FROM bars WHERE ts_ns >= ? AND ts_ns < ?",
                            (timeutil.ist_ns(start, "00:00"), timeutil.ist_ns(end + timedelta(days=1), "00:00")))
        return {timeutil.ns_to_ist(t).date() for (t,) in rows}

    return _read(paths.bars_db(), query, set(), not_checked)


def _eod_days(days: list[date], not_checked: list) -> dict[str, set[date]]:
    def query(conn: sqlite3.Connection) -> dict[str, set[date]]:
        out: dict[str, set[date]] = {t: set() for t in EOD_TABLES}
        for table in EOD_TABLES:
            if not _has_table(conn, table):
                continue
            for d in days:             # one primary-key probe per day: a range scan would read every row
                if conn.execute(f"SELECT 1 FROM {table} WHERE trade_date=? LIMIT 1", (d.isoformat(),)).fetchone():
                    out[table].add(d)
        return out

    return _read(paths.refdata_db(), query, {t: set() for t in EOD_TABLES}, not_checked)


def _raw_days() -> dict[str, set[date]]:
    """The dates that start a file name in the nse/raw folder of each dataset NSE publishes per trade date."""
    dated = {name for name, rows in nse_files.config()["datasets"].items()
             if not any(row.get("current_only") for row in rows)}
    out: dict[str, set[date]] = {}
    for folder in sorted(p for p in paths.nse_raw_dir().iterdir() if p.is_dir() and p.name in dated):
        days = set()
        for f in folder.iterdir():
            stamp = f.name[:8]
            if stamp.isascii() and stamp.isdigit():
                try:
                    days.add(date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:])))
                except ValueError:
                    pass
        out[f"raw:{folder.name}"] = days
    return out


def data_stores(start: date, end: date, not_checked: list | None = None) -> dict[date, tuple[str, ...]]:
    """Each weekday of the range that has data, with the stores that hold it. A database that was not read (busy,
    or not readable) counts as holding nothing and is added to `not_checked` as (file name, why)."""
    not_checked = [] if not_checked is None else not_checked
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    days = [d for d in days if d.weekday() < 5]
    stores: dict[str, set[date]] = {"span": {d for d in days if nse_margin_files.eod_archived(d)},
                                    "bars": _bar_days(start, end, not_checked)}
    stores.update(_eod_days(days, not_checked))
    stores.update(_raw_days())
    out = {}
    for d in days:
        found = tuple(name for name, have in stores.items() if d in have)
        if found:
            out[d] = found
    return out


def audit(start: date, end: date, segment: str = "FO") -> Audit:
    segment = segment.upper()
    names = {}
    for row in loader.load_csv("calendar/nse_holidays.csv"):
        if segment in [s.strip().upper() for s in row["segments"].split(";")]:
            names.setdefault(row["date"], row["description"])
    not_checked: list = []
    data = data_stores(start, end, not_checked)
    silent, odd, years = [], [], {}
    d = start
    while d <= end:
        if d.weekday() < 5:
            trading = calendar.is_trading_day(segment, d)
            count = years.setdefault(d.year, [0, 0, 0])
            count[0] += 1
            count[1] += d in data
            count[2] += not trading
            if trading and d not in data:
                silent.append(d)
            elif not trading and d in data:
                odd.append((d, names.get(d.isoformat(), ""), data[d]))
        d += timedelta(days=1)
    by_year = tuple((y, *c) for y, c in sorted(years.items()))
    return Audit(start, end, segment, sum(c[0] for c in years.values()), sum(c[1] for c in years.values()),
                 sum(c[2] for c in years.values()), tuple(silent), tuple(odd), by_year, tuple(not_checked))


def report(a: Audit) -> str:
    lines = [f"NSE calendar audit, segment {a.segment}, {a.start} .. {a.end}",
             f"weekdays {a.weekdays}, with data {a.with_data}, calendar holidays {a.holidays}"]
    if a.not_checked:
        lines += ["", f"Not checked ({len(a.not_checked)}): what only these hold is missing below; run the audit "
                      "again while no recorder or daemon is writing"]
        lines += [f"  {name}: {why}" for name, why in a.not_checked]
    lines += ["", f"{'year':<6}{'weekdays':>9}{'with data':>11}{'holidays':>10}"]
    lines += [f"{y:<6}{w:>9}{n:>11}{h:>10}" for y, w, n, h in a.by_year]
    lines += ["", f"Trading days with no data ({len(a.silent_trading_days)}): a holiday missing from "
                  "calendar/nse_holidays.csv, or a gap in the archive"]
    lines += [f"  {d} {d:%a}" for d in a.silent_trading_days] or ["  none"]
    lines += ["", f"Holidays with data ({len(a.holidays_with_data)}): a Muhurat or special session, or a wrong row"]
    lines += [f"  {d} {d:%a}  {name}  [{', '.join(stores)}]" for d, name, stores in a.holidays_with_data] or ["  none"]
    return "\n".join(lines)
