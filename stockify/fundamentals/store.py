"""FS 1: the fundamentals store `fundamentals.sqlite`: the universe as read each day, with each stock's sector (the
Nifty 500 list's "Industry" column, ruling R3).

Derived data (rebuildable from the raw NSE answers under nse/raw/fundamentals), but nothing here deletes another
day's rows: every day's universe stays. Refreshing the universe again on the
same day replaces that day's list (a symbol NSE dropped from the list since the morning goes with it).

The store pattern of `stockify.desk.store`: `_lock`, `_db_path()` through `stockify.paths`, one connection per thread
from `dbconn.Pool` (D-143), migrations under `stockify/data/migrations/fundamentals/`, writes under the module lock
with BEGIN IMMEDIATE.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Optional

from stockify import dbconn, paths
from stockify.data import migrate

STORE = "fundamentals"

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

UNIVERSE_COLUMNS = ("as_of", "symbol", "instrument_id", "isin", "company", "median_value_paise", "liquid",
                    "list_industry")


def _db_path():
    return paths.fundamentals_db()


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


def _tuples(rows: list[dict], columns: tuple, fixed: dict) -> list[tuple]:
    out = []
    for r in rows:
        row = {**r, **fixed}
        missing = [c for c in columns if c not in row]
        if missing:
            raise ValueError(f"row for {row.get('symbol')!r} lacks {', '.join(missing)}")
        out.append(tuple(row[c] for c in columns))
    return out


# ── universe ─────────────────────────────────────────────────────────────────

def put_universe(as_of: str, rows: list[dict]) -> None:
    """The universe read on `as_of` (IST date): replaces that day's rows, all or none. Row keys: UNIVERSE_COLUMNS
    without as_of; `liquid` is stored 0/1."""
    values = _tuples([{**r, "liquid": int(bool(r["liquid"]))} if "liquid" in r else r for r in rows],
                     UNIVERSE_COLUMNS, {"as_of": str(as_of)})
    conn = _connect()
    with dbconn.locked_write(_lock, conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM universe WHERE as_of = ?", (str(as_of),))
        conn.executemany(f"INSERT INTO universe ({', '.join(UNIVERSE_COLUMNS)}) "
                         f"VALUES ({', '.join('?' * len(UNIVERSE_COLUMNS))})", values)
        conn.execute("COMMIT")


def latest_as_of() -> str | None:
    """The newest day a universe was read, or None."""
    return _connect().execute("SELECT MAX(as_of) AS d FROM universe").fetchone()["d"]


def universe_row(as_of: str, symbol: str) -> dict | None:
    """One stock's row in the universe read on `as_of`, or None."""
    r = _connect().execute("SELECT * FROM universe WHERE as_of = ? AND symbol = ?",
                           (str(as_of), str(symbol))).fetchone()
    return None if r is None else dict(r)


def universe_on(as_of: str, *, liquid_only: bool = False) -> list[dict]:
    """The universe read on `as_of`, by symbol."""
    sql = "SELECT * FROM universe WHERE as_of = ?" + (" AND liquid = 1" if liquid_only else "") + " ORDER BY symbol"
    return [dict(r) for r in _connect().execute(sql, (str(as_of),)).fetchall()]


# ── the test record (FS 6) ───────────────────────────────────────────────────

TEST_RECORD_NAME = "fundamentals-basket"                 # the walk-forward job test_record.run starts
TEST_RECORD_BENCHMARK = ("NSE:NIFTY 500", "price index, without dividends")    # Kite's index candles (spec call 9)


def latest_test_record() -> dict | None:
    """The newest test record, or None before the first: the newest lab.sqlite walk-forward job named
    "fundamentals-basket" (test_record.run starts it), as {"job_id", "family_id", "status", "start", "end" (ISO
    days of its span, or None), "benchmark": the benchmark's label, "benchmark_symbol"}."""
    import json

    from stockify import store as lab
    offset = 0
    while True:
        jobs = lab.list_research_jobs("walkforward", offset=offset, limit=200)
        for j in jobs:
            if j.get("name") != TEST_RECORD_NAME:
                continue
            try:
                span = json.loads(j.get("spec_json") or "{}").get("span") or [None, None]
            except ValueError:
                span = [None, None]
            return {"job_id": j["job_id"], "family_id": j.get("family_id"), "status": j.get("status"),
                    "start": span[0], "end": span[1], "benchmark": TEST_RECORD_BENCHMARK[1],
                    "benchmark_symbol": TEST_RECORD_BENCHMARK[0]}
        if len(jobs) < 200:
            return None
        offset += 200
