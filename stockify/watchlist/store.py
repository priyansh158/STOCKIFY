"""CW 2: the watchlist store `watchlist.sqlite`: the stocks the owner watches, the alerts raised on them, and a change
counter (`version`) that a running evaluator polls.

The list is the owner's choice, not derived data: it is backed up with the other stores and nothing here drops a row.
An alert row stays after its stock leaves the list; its unique key (instrument, kind, ref) is the once-only record that
survives a restart.

The store pattern of `stockify.fundamentals.store`: `_lock`, `_db_path()` through `stockify.paths`, one connection per
thread from `dbconn.Pool` (D-143), migrations under `stockify/data/migrations/watchlist/`, writes under the module lock
with BEGIN IMMEDIATE.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Optional

from stockify import dbconn, paths
from stockify.config import loader
from stockify.data import instruments, migrate

STORE = "watchlist"

KINDS = ("high52", "low52", "results")
DELIVERIES = ("pending", "sent", "capped", "failed")

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


class WatchError(ValueError):
    """A stock could not be put on the list; the message is plain words for the page."""


class NotFound(WatchError):
    """No NSE listing has this symbol."""


class NotAStock(WatchError):
    """The symbol is listed, but is not an NSE share (an index, a bond ...)."""


class AlreadyWatched(WatchError):
    """The stock is already on the list."""


class ListFull(WatchError):
    """The list is at its cap."""


def _db_path():
    return paths.watchlist_db()


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


def max_stocks() -> int:
    """The cap on the list (watchlist.json)."""
    return int(loader.load_json("watchlist.json")["max_stocks"])


def _clean(symbol: str) -> str:
    return str(symbol).strip().upper()


# ── the list ─────────────────────────────────────────────────────────────────

def add(symbol: str, *, now_ns: int | None = None) -> dict:
    """Put an NSE share on the list, last; returns its row. NotFound when NSE has no such symbol (instrument lookup
    by `instruments.find("NSE", symbol)`), NotAStock for an index or anything not an NSE equity, AlreadyWatched for a
    stock already on the list, ListFull at the cap."""
    sym = _clean(symbol)
    iid = instruments.find("NSE", sym) if sym else None
    if iid is None:
        raise NotFound(f"{sym or 'That'} is not an NSE symbol the lab knows")
    info = instruments.get(iid) or {}
    if info.get("exchange") != "NSE" or info.get("kind") != "EQ":     # the check desk/nightly.resolve makes
        raise NotAStock(f"{sym} is not an NSE share")
    if now_ns is None:
        from stockify import timeutil
        now_ns = timeutil.now_ns()
    cap = max_stocks()
    conn = _connect()
    with dbconn.locked_write(_lock, conn):
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM watchlist WHERE instrument_id = ?", (iid,)).fetchone():
            conn.execute("ROLLBACK")
            raise AlreadyWatched(f"{sym} is already on the watchlist")
        count, last = conn.execute("SELECT COUNT(*), COALESCE(MAX(position), 0) FROM watchlist").fetchone()
        if count >= cap:
            conn.execute("ROLLBACK")
            raise ListFull(f"The watchlist holds at most {cap} stocks; remove one to add another")
        conn.execute("INSERT INTO watchlist (instrument_id, symbol, added_ns, position) VALUES (?, ?, ?, ?)",
                     (iid, sym, int(now_ns), last + 1))
        conn.execute("UPDATE meta SET version = version + 1")
        conn.execute("COMMIT")
    return {"instrument_id": iid, "symbol": sym, "added_ns": int(now_ns), "position": last + 1}


def remove(symbol: str) -> bool:
    """Take a stock off the list; False when it was not on it. Its past alerts stay."""
    sym = _clean(symbol)
    conn = _connect()
    with dbconn.locked_write(_lock, conn):
        conn.execute("BEGIN IMMEDIATE")
        n = conn.execute("DELETE FROM watchlist WHERE symbol = ?", (sym,)).rowcount
        if n:
            conn.execute("UPDATE meta SET version = version + 1")
        conn.execute("COMMIT")
    return n > 0


def rows() -> list[dict]:
    """The watched stocks in the order they were added: instrument_id, symbol, added_ns, position."""
    return [dict(r) for r in _connect().execute("SELECT * FROM watchlist ORDER BY position, instrument_id")]


def ids() -> list[int]:
    """The watched instrument ids, in list order (the daily-candle job reads this)."""
    return [r["instrument_id"] for r in rows()]


def symbols() -> list[str]:
    return [r["symbol"] for r in rows()]


def version() -> int:
    """Counts every change to the list (an add or a removal), so a reader can tell it changed."""
    return int(_connect().execute("SELECT version FROM meta").fetchone()["version"])


# ── alerts ───────────────────────────────────────────────────────────────────

def record_alert(instrument_id: int, symbol: str, kind: str, ref: str, *, level_paise: int | None = None,
                 prior_paise: int | None = None, prior_date: str | None = None, at_ns: int,
                 delivery: str = "pending") -> bool:
    """Record an alert once: True only when this call inserted it, False when (instrument, kind, ref) was already
    recorded (the first row stays as it was). `ref` is the IST day of a 52-week cross or the date of a results
    meeting."""
    if kind not in KINDS:
        raise ValueError(f"alert kind must be one of {KINDS}, got {kind!r}")
    if delivery not in DELIVERIES:
        raise ValueError(f"alert delivery must be one of {DELIVERIES}, got {delivery!r}")
    conn = _connect()
    with dbconn.locked_write(_lock, conn):
        conn.execute("BEGIN IMMEDIATE")
        n = conn.execute(
            "INSERT OR IGNORE INTO watch_alerts (instrument_id, symbol, kind, ref, level_paise, prior_paise, "
            "prior_date, at_ns, delivery) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(instrument_id), _clean(symbol), kind, str(ref), level_paise, prior_paise, prior_date, int(at_ns),
             delivery)).rowcount
        conn.execute("COMMIT")
    return n > 0


def set_delivery(instrument_id: int, kind: str, ref: str, delivery: str) -> None:
    """Mark how an alert was delivered (sent, or capped when the day's email cap was reached, or failed)."""
    if delivery not in DELIVERIES:
        raise ValueError(f"alert delivery must be one of {DELIVERIES}, got {delivery!r}")
    conn = _connect()
    with dbconn.locked_write(_lock, conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE watch_alerts SET delivery = ? WHERE instrument_id = ? AND kind = ? AND ref = ?",
                     (delivery, int(instrument_id), kind, str(ref)))
        conn.execute("COMMIT")


def alerts(since_ns: int = 0) -> list[dict]:
    """Alerts found at or after `since_ns` (UTC epoch ns), newest first."""
    return [dict(r) for r in _connect().execute(
        "SELECT * FROM watch_alerts WHERE at_ns >= ? ORDER BY at_ns DESC, instrument_id, kind", (int(since_ns),))]
