"""LCM 6.2: the data coverage matrix behind the /data page.

Per calendar date and dataset: was it recorded, archived, downloaded from NSE? Plus the day's recording gaps,
the cached bar ranges and the storage numbers. Everything here READS: no row is written, no store is created
(a missing refdata.sqlite or bars.sqlite reads as empty), no tick file is opened except the one day `gaps` is
asked about, and the NSE download folder is never walked (its size comes from `nse_fetch_log.bytes`).

Units: percentages are percent (0..100) as the quality report stores them; bytes are bytes; times are UTC epoch ns,
turned into IST text only for display (`start_ist`).

Cell states: ok, partial, missing, not_reported, closed, not_applicable. A day that is not a trading day is
`closed` (never a zero); a day after today is `not_applicable` ("not yet").
"""
from __future__ import annotations

import math
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from stockify import paths, store, timeutil
from stockify.config import loader
from stockify.data import calendar, compact, ticks

STATES = ("ok", "partial", "missing", "not_reported", "closed", "not_applicable")
NSE_LABELS = {"fo": ("nse_fo", "NSE F&O bhavcopy", "FO"),
              "cm": ("nse_cm", "NSE equity bhavcopy", "CM"),
              "idx": ("nse_idx", "NSE index closes", "CM")}
UNLOGGED_TEXT = "instruments recorded before mode logging; not counted"
NO_INSTRUMENTS = "no instruments recorded in this mode"
_DAY_METRICS = ("coverage_full_n", "coverage_quote_n", "coverage_unlogged_n", "coverage_full_pct",
                "coverage_quote_pct")
_BAR_LIMIT = 100
_DAY_NS = 86_400 * timeutil.NS_PER_S


class NoRecording(LookupError):
    """No tick recording (landing or archive) exists for the day."""


@dataclass(frozen=True)
class Cell:
    state: str
    value: float | None = None
    detail: str | None = None


@dataclass(frozen=True)
class CoverageDay:
    date: date
    trading: bool
    cells: dict[str, Cell]


@dataclass
class CoverageMatrix:
    from_: date
    to: date
    datasets: list[dict[str, str]]
    days: list[CoverageDay]
    bars: list[dict]
    storage: dict
    recorder: dict | None = field(default=None)


def _num(x) -> float | None:  # noqa: ANN001
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _ok_pct() -> float:
    from stockify import research
    return float(Decimal(str(research.settings()["coverage"]["ok_pct"])))


def datasets() -> list[dict[str, str]]:
    known = loader.load_json("sources.json")["datasets"]
    out = [{"key": "recorder_full", "label": "Recorder, FULL mode coverage (%)"},
           {"key": "recorder_quote", "label": "Recorder, QUOTE mode coverage (%)"},
           {"key": "archive", "label": "Tick archive"}]
    out += [{"key": key, "label": label} for name, (key, label, _seg) in NSE_LABELS.items() if name in known]
    return out


def _rows(db_path, sql: str, args: tuple = ()) -> list:  # noqa: ANN001
    """A read of refdata.sqlite or bars.sqlite on its own READ-ONLY connection (`mode=ro`), closed after use.

    Never through `refdata._connect()` / `history._connect()`: those open read-write, switch on WAL, create the
    folder and apply migrations, and this runs while the daemon is writing the same files. A missing file or a
    missing table or column (an older schema) reads as no rows.
    """
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _recorder_cell(group: str, tick_day, metrics: dict, ok_pct: float) -> Cell:  # noqa: ANN001
    if tick_day is None:
        return Cell("missing", None, "no recording for this day")
    n = _num(metrics.get(f"coverage_{group}_n"))
    if n is None:
        return Cell("not_reported", None, "the quality report has not run for this day")
    unlogged = _num(metrics.get("coverage_unlogged_n")) or 0
    note = f"{int(unlogged)} {UNLOGGED_TEXT}" if unlogged > 0 else None
    if n <= 0:
        return Cell("not_applicable", None, "; ".join([NO_INSTRUMENTS] + ([note] if note else [])))
    pct = _num(metrics.get(f"coverage_{group}_pct"))
    if pct is None:
        return Cell("not_reported", None, "; ".join(["no coverage value stored"] + ([note] if note else [])))
    return Cell("ok" if pct >= ok_pct else "partial", pct, note)


def _archive_cell(tick_day, today: bool = False) -> Cell:  # noqa: ANN001
    if tick_day is None:
        return Cell("missing", None, "no recording for this day")
    status = tick_day["status"]
    if status in ("verified", "landing_deleted"):
        return Cell("ok", None, "verified")
    if status in ("recording", "landing") and today:
        return Cell("partial", None, "recording in progress")
    if status in ("recording", "landing", "archived"):
        return Cell("partial", None, status)
    return Cell("not_reported", None, f"unknown status {status}")


def _nse_cell(row) -> Cell:  # noqa: ANN001
    if row is None:
        return Cell("missing", None, "not downloaded")
    outcome = row["outcome"]
    if outcome == "ok":
        if row["parsed_ok"] == 0:
            return Cell("partial", None, "downloaded, not parsed")
        return Cell("ok")
    if outcome == "missing":
        return Cell("missing", None, "NSE has no file for this day")
    return Cell("missing", None, f"download failed (http {row['http_status']})" if row["http_status"]
                else "download failed")


def _bars(limit: int) -> list[dict]:
    rows = _rows(paths.bars_db(),
                 "SELECT instrument_id, interval, MIN(from_ns) AS a, MAX(to_ns) AS b, SUM(rows) AS n "
                 "FROM bar_fetch_log WHERE outcome='ok' GROUP BY instrument_id, interval "
                 "ORDER BY b DESC, instrument_id, interval LIMIT ?", (int(limit),))
    out = []
    ids = sorted({int(r["instrument_id"]) for r in rows})
    names = {r["instrument_id"]: r["tradingsymbol"] for r in _rows(
        paths.refdata_db(), f"SELECT instrument_id, tradingsymbol FROM instruments WHERE instrument_id IN "
        f"({','.join('?' * len(ids))})", tuple(ids))} if ids else {}
    for r in rows:
        a, b = int(r["a"]), int(r["b"])
        first, last = timeutil.ns_to_ist(a).date(), timeutil.ns_to_ist(b).date()
        out.append({"instrument_id": r["instrument_id"], "symbol": names.get(r["instrument_id"]),
                    "interval": r["interval"], "from_ns": a, "to_ns": b, "from_ist": timeutil.ns_to_ist(a).isoformat(),
                    "to_ist": timeutil.ns_to_ist(b).isoformat(), "days": (last - first).days + 1,
                    "rows": int(r["n"] or 0)})
    return out


def _size(path) -> int:  # noqa: ANN001
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = path.with_name(path.name + suffix)
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def _storage() -> dict:
    """`lab_bytes` is lab.sqlite plus bars.sqlite (with their -wal/-shm); the tick landing files, the archive and
    the NSE folder are not in it (the first two are `landing_bytes` / `archive_bytes`, from `tick_days`)."""
    sums = _rows(paths.refdata_db(),
                 "SELECT COALESCE(SUM(CASE WHEN status != 'landing_deleted' THEN landing_bytes END), 0) AS l, "
                 "COALESCE(SUM(archive_bytes), 0) AS a FROM tick_days")
    nse = _rows(paths.refdata_db(), "SELECT COALESCE(SUM(bytes), 0) AS n FROM nse_fetch_log")
    usage = shutil.disk_usage(paths.lab_dir())
    return {"landing_bytes": int(sums[0]["l"]) if sums else 0, "archive_bytes": int(sums[0]["a"]) if sums else 0,
            "refdata_bytes": _size(paths.refdata_db()),
            "lab_bytes": _size(paths.lab_db()) + _size(paths.bars_db()),
            "nse_bytes": int(nse[0]["n"]) if nse else 0,
            "free_bytes": int(usage.free), "total_bytes": int(usage.total),
            "warnings": compact.disk_warnings(usage.free, usage.total)}


def matrix(from_: date, to: date, *, bar_limit: int = _BAR_LIMIT) -> CoverageMatrix:
    if to < from_:
        raise ValueError("to is before from")
    refdb = paths.refdata_db()
    lo, hi = from_.isoformat(), to.isoformat()
    tick_days = {r["trade_date"]: r for r in _rows(
        refdb, "SELECT trade_date, status FROM tick_days WHERE trade_date BETWEEN ? AND ?", (lo, hi))}
    quality: dict[str, dict] = {}
    for r in _rows(refdb, "SELECT trade_date, metric, value FROM data_quality WHERE instrument_id=0 AND metric IN "
                   f"({','.join('?' * len(_DAY_METRICS))}) AND trade_date BETWEEN ? AND ?", (*_DAY_METRICS, lo, hi)):
        quality.setdefault(r["trade_date"], {})[r["metric"]] = r["value"]
    fetches = {(r["dataset"], r["trade_date"]): r for r in _rows(
        refdb, "SELECT dataset, trade_date, outcome, http_status, parsed_ok FROM nse_fetch_log "
        "WHERE trade_date BETWEEN ? AND ?", (lo, hi))}
    listed = datasets()
    ok_pct = _ok_pct()
    today = timeutil.ist_today()
    days: list[CoverageDay] = []
    d = from_
    while d <= to:
        iso = d.isoformat()
        cm, fo = calendar.is_trading_day("CM", d), calendar.is_trading_day("FO", d)
        trading = cm or fo
        cells: dict[str, Cell] = {}
        for ds in listed:
            key = ds["key"]
            seg = next((s for _n, (k, _l, s) in NSE_LABELS.items() if k == key), None)
            open_ = (cm if seg == "CM" else fo) if seg else trading
            if not open_:
                cells[key] = Cell("closed")
            elif d > today:
                cells[key] = Cell("not_applicable", None, "not yet")
            elif key.startswith("recorder_"):
                cells[key] = _recorder_cell(key.split("_", 1)[1], tick_days.get(iso), quality.get(iso, {}), ok_pct)
            elif key == "archive":
                cells[key] = _archive_cell(tick_days.get(iso), d == today)
            else:
                name = next(n for n, (k, _l, _s) in NSE_LABELS.items() if k == key)
                cells[key] = _nse_cell(fetches.get((name, iso)))
        days.append(CoverageDay(d, trading, cells))
        d += timedelta(days=1)
    return CoverageMatrix(from_, to, listed, days, _bars(bar_limit), _storage(), store.latest_recorder_status())


def gaps(day: date) -> list[dict]:
    """The day's recording gaps from its gap table; NoRecording when the day was never recorded."""
    opened = ticks.open_day(day)
    if opened is None:
        raise NoRecording(f"no recording for {day.isoformat()}")
    with opened as rec:
        rows = rec.gaps()
    out = []
    for g in sorted(rows, key=lambda r: (r["start_ns"], r.get("gap_id") or 0)):
        a, b = g["start_ns"], g.get("end_ns")
        out.append({"start_ns": a, "end_ns": b, "start_ist": timeutil.ns_to_ist(a).isoformat(),
                    "duration_s": (b - a) / timeutil.NS_PER_S if b is not None else None,
                    "reason": g.get("reason"), "scope": g.get("scope"), "frames": g.get("frames")})
    return out


def _cell_dict(c: Cell) -> dict:
    return {"state": c.state, "value": c.value, "detail": c.detail}


def to_dict(m: CoverageMatrix) -> dict:
    return {"from_date": m.from_.isoformat(), "to_date": m.to.isoformat(), "datasets": m.datasets,
            "days": [{"date": d.date.isoformat(), "trading": d.trading,
                      "cells": {k: _cell_dict(c) for k, c in d.cells.items()}} for d in m.days],
            "bars": m.bars, "storage": m.storage, "recorder": m.recorder}
