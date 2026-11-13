"""`refdata.sqlite`: instruments with history, NSE end-of-day tables, tick-day
status and data quality (A1_schemas.md §1).

One connection per thread (`stockify.dbconn`, D-143),
WAL, `sqlite3.Row`. The schema comes from
versioned migrations (`stockify/data/migrations/refdata/`), never from
inline DDL (I-13).
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Optional

from stockify import dbconn, paths
from stockify.data import migrate

STORE = "refdata"

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _db_path():
    return paths.refdata_db()


def _open() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_pool = dbconn.Pool(__name__, _open, lambda conn: migrate.apply(conn, STORE))


def _connect() -> sqlite3.Connection:
    """This thread's connection (D-143: one per thread; the writers still take `_lock`)."""
    return _pool.get()


def init_db() -> None:
    _connect()


def schema_version() -> int:
    row = _connect().execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"] or 0)


class transaction:
    """`with refdata.transaction() as conn:` — one write transaction under the lock."""

    def __enter__(self) -> sqlite3.Connection:
        self._conn = _connect()
        _lock.acquire()
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            _lock.release()
