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

Saturdays and Sundays are ignored. The audit only reads: files are tested for existence, the databases are opened
read-only, and nothing is fetched (stockify.paths makes a lab folder that is missing, as for every reader; no file
is written). Recorded ticks are not looked at: they begin long after the other stores. The dates themselves live
only in the config; this module holds none.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from stockify import paths, timeutil
from stockify.config import loader
from stockify.data import calendar, nse_files, nse_margin_files

EOD_TABLES = ("fo_eod", "cm_eod", "index_eod")


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


def _open_ro(path: Path) -> sqlite3.Connection | None:
    """A read-only connection, or None when the file is not there. The stores are WAL databases, and even a
    read-only connection to one makes the missing `-wal`/`-shm` files and writes the `-shm`. While the `-wal` is
    missing or empty every row is in the main file, so the file is opened as immutable and nothing beside it is
    made or touched; a `-wal` with rows in it has to be read, and then SQLite updates the `-shm` (never the data).
    """
    if not path.is_file():
        return None
    wal = path.with_name(path.name + "-wal")
    in_wal = wal.is_file() and wal.stat().st_size > 0
    return sqlite3.connect(f"{path.as_uri()}?mode=ro{'' if in_wal else '&immutable=1'}", uri=True)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _bar_days(start: date, end: date) -> set[date]:
    conn = _open_ro(paths.bars_db())
    if conn is None:
        return set()
    try:
        if not _has_table(conn, "bars"):
            return set()
        rows = conn.execute("SELECT DISTINCT ts_ns FROM bars WHERE ts_ns >= ? AND ts_ns < ?",
                            (timeutil.ist_ns(start, "00:00"), timeutil.ist_ns(end + timedelta(days=1), "00:00")))
        return {timeutil.ns_to_ist(t).date() for (t,) in rows}
    finally:
        conn.close()


def _eod_days(days: list[date]) -> dict[str, set[date]]:
    out: dict[str, set[date]] = {t: set() for t in EOD_TABLES}
    conn = _open_ro(paths.refdata_db())
    if conn is None:
        return out
    try:
        for table in EOD_TABLES:
            if not _has_table(conn, table):
                continue
            for d in days:             # one primary-key probe per day: a range scan would read every row
                if conn.execute(f"SELECT 1 FROM {table} WHERE trade_date=? LIMIT 1", (d.isoformat(),)).fetchone():
                    out[table].add(d)
        return out
    finally:
        conn.close()


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


def data_stores(start: date, end: date) -> dict[date, tuple[str, ...]]:
    """Each weekday of the range that has data, with the stores that hold it."""
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    days = [d for d in days if d.weekday() < 5]
    stores: dict[str, set[date]] = {"span": {d for d in days if nse_margin_files.eod_archived(d)},
                                    "bars": _bar_days(start, end)}
    stores.update(_eod_days(days))
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
    data = data_stores(start, end)
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
                 sum(c[2] for c in years.values()), tuple(silent), tuple(odd), by_year)


def report(a: Audit) -> str:
    lines = [f"NSE calendar audit, segment {a.segment}, {a.start} .. {a.end}",
             f"weekdays {a.weekdays}, with data {a.with_data}, calendar holidays {a.holidays}",
             "", f"{'year':<6}{'weekdays':>9}{'with data':>11}{'holidays':>10}"]
    lines += [f"{y:<6}{w:>9}{n:>11}{h:>10}" for y, w, n, h in a.by_year]
    lines += ["", f"Trading days with no data ({len(a.silent_trading_days)}): a holiday missing from "
                  "calendar/nse_holidays.csv, or a gap in the archive"]
    lines += [f"  {d} {d:%a}" for d in a.silent_trading_days] or ["  none"]
    lines += ["", f"Holidays with data ({len(a.holidays_with_data)}): a Muhurat or special session, or a wrong row"]
    lines += [f"  {d} {d:%a}  {name}  [{', '.join(stores)}]" for d, name, stores in a.holidays_with_data] or ["  none"]
    return "\n".join(lines)
