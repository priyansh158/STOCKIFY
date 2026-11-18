"""HD 4.1: the desk store `desk.sqlite`: nightly evidence and the stored view of each holding per day.

Derived data (rebuildable from the bar cache and Kite), so not under the tick-data rule, but nothing here deletes a
row: every day's evidence and view stays readable. Writing the same key again (the nightly job re-run on one day,
a view remade later in the day) replaces that one row.

The store pattern of `stockify.data.history`: `_lock`, `_db_path()` through `stockify.paths`, one connection per
thread from `dbconn.Pool` (D-143), migrations under `stockify/data/migrations/desk/`, writes under the module lock
with BEGIN IMMEDIATE. JSON columns are stored canonical (sorted keys) and handed back parsed.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Optional

from stockify import dbconn, paths
from stockify.data import migrate

STORE = "desk"

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

EVIDENCE_COLUMNS = ("trade_date", "instrument_id", "ref", "params_json", "from_date", "to_date", "trades", "cagr",
                    "buy_hold_cagr", "sharpe", "buy_hold_sharpe", "max_drawdown", "win_rate", "windows_json",
                    "windows_won", "windows_total", "grade", "note")
# What put_evidence takes: the columns, with `params` and `windows` as Python values instead of their JSON text.
EVIDENCE_FIELDS = tuple({"params_json": "params", "windows_json": "windows"}.get(c, c) for c in EVIDENCE_COLUMNS)
GRADES = ("strong", "some", "weak", "none")


def _db_path():
    return paths.desk_db()


def _open() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=dbconn.BUSY_TIMEOUT_S, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


_pool = dbconn.Pool(__name__, _open, lambda conn: migrate.apply(conn, STORE))


def _connect() -> sqlite3.Connection:
    """This thread's connection (D-143: one per thread; the writers still take `_lock`)."""
    return _pool.get()


def _dumps(value) -> str:  # noqa: ANN001
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _write(sql: str, rows: list[tuple]) -> None:
    conn = _connect()
    with dbconn.locked_write(_lock, conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(sql, rows)
        conn.execute("COMMIT")


# ── evidence ─────────────────────────────────────────────────────────────────

def _evidence_tuple(row: dict) -> tuple:
    missing = [f for f in EVIDENCE_FIELDS if f not in row]
    if missing:
        raise ValueError(f"evidence row lacks {', '.join(missing)}")
    if row["grade"] not in GRADES:
        raise ValueError(f"evidence grade must be one of {', '.join(GRADES)}, not {row['grade']!r}")
    out = []
    for col, field in zip(EVIDENCE_COLUMNS, EVIDENCE_FIELDS):
        v = row[field]
        out.append(_dumps(v) if col.endswith("_json") else v)
    return tuple(out)


def put_evidence(rows: list[dict]) -> None:
    """Write evidence rows (keys: EVIDENCE_FIELDS) in one transaction; all or none."""
    values = [_evidence_tuple(r) for r in rows]
    if not values:
        return
    _write(f"INSERT OR REPLACE INTO evidence ({', '.join(EVIDENCE_COLUMNS)}) "
           f"VALUES ({', '.join('?' * len(EVIDENCE_COLUMNS))})", values)


def _evidence_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["params"] = json.loads(d.pop("params_json"))
    d["windows"] = json.loads(d.pop("windows_json"))
    return d


def evidence_for(instrument_id: int, trade_date: str) -> list[dict]:
    """One nightly run's evidence rows for a holding, by ref."""
    rows = _connect().execute("SELECT * FROM evidence WHERE instrument_id = ? AND trade_date = ? ORDER BY ref",
                              (int(instrument_id), str(trade_date))).fetchall()
    return [_evidence_row(r) for r in rows]


def latest_evidence(instrument_id: int) -> list[dict]:
    """The newest nightly run's evidence rows for a holding, by ref; [] if it was never graded."""
    row = _connect().execute("SELECT MAX(trade_date) AS d FROM evidence WHERE instrument_id = ?",
                             (int(instrument_id),)).fetchone()
    return [] if row["d"] is None else evidence_for(instrument_id, row["d"])


# ── views ────────────────────────────────────────────────────────────────────

def put_view(trade_date: str, instrument_id: int, symbol: str, verdict: str, view: dict, made_ns: int) -> None:
    """Store the desk's view of a holding on `trade_date` (IST date); `made_ns` UTC epoch ns."""
    _write("INSERT OR REPLACE INTO views (trade_date, instrument_id, symbol, verdict, json, made_ns) "
           "VALUES (?, ?, ?, ?, ?, ?)",
           [(str(trade_date), int(instrument_id), str(symbol), str(verdict), _dumps(view), int(made_ns))])


def _view_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["view"] = json.loads(d.pop("json"))
    return d


def views_for(instrument_id: int) -> list[dict]:
    """Every stored day's view of a holding, oldest first."""
    rows = _connect().execute("SELECT * FROM views WHERE instrument_id = ? ORDER BY trade_date",
                              (int(instrument_id),)).fetchall()
    return [_view_row(r) for r in rows]


def latest_view(instrument_id: int) -> dict | None:
    """The newest day's view of a holding ({trade_date, instrument_id, symbol, verdict, view, made_ns}), or None."""
    r = _connect().execute("SELECT * FROM views WHERE instrument_id = ? ORDER BY trade_date DESC LIMIT 1",
                           (int(instrument_id),)).fetchone()
    return None if r is None else _view_row(r)


def latest_views() -> list[dict]:
    """The newest view of every holding ever viewed, by instrument_id."""
    rows = _connect().execute(
        "SELECT v.* FROM views v JOIN (SELECT instrument_id, MAX(trade_date) AS d FROM views GROUP BY instrument_id) m"
        " ON v.instrument_id = m.instrument_id AND v.trade_date = m.d ORDER BY v.instrument_id").fetchall()
    return [_view_row(r) for r in rows]

