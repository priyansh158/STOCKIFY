"""`refdata.sqlite`: instruments with history, NSE end-of-day tables, tick-day
status and data quality (A1_schemas.md §1).

Same connection pattern as `stockify.store` (one process-wide connection,
WAL, `check_same_thread=False`, `sqlite3.Row`). The schema comes from
versioned migrations (`stockify/data/migrations/refdata/`), never from
inline DDL (I-13).
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Optional

from stockify import paths
from stockify.data import migrate

STORE = "refdata"

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _db_path():
    return paths.refdata_db()


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    with _lock:
        if _conn is not None:
            return _conn
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        migrate.apply(conn, STORE)
        _conn = conn
        return _conn


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
