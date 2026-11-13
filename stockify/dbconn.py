"""One SQLite connection per thread for a module's database (D-143).

A single `check_same_thread=False` connection shared by the API's worker threads is not safe for reads:
two threads interleave `execute` / `fetch` on the same handle and SQLite answers `InterfaceError('bad
parameter or other API misuse')` or hands back rows as None. WAL lets any number of connections read at once
while one writes, so each thread gets its own connection; a module's writes stay serialised by its own write
lock and `BEGIN IMMEDIATE`, exactly as before.

A module keeps its `_conn` global (the first connection it opened, where migrations ran). Tests and
`span_validate.read_only` put their own connection in `_conn`; a connection the pool did not open is handed to
every thread unchanged. Setting `_conn = None` (or `close_all`) retires every connection the pool opened.
"""
from __future__ import annotations

import sqlite3
import sys
import threading
from typing import Callable


class Pool:
    def __init__(self, module_name: str, open_fn: Callable[[], sqlite3.Connection],
                 init_fn: Callable[[sqlite3.Connection], None] | None = None) -> None:
        """`open_fn()` returns a configured connection (PRAGMAs, row_factory) to the module's current database
        path; `init_fn(conn)` runs once per database (migrations, schema) on the first connection."""
        self._module_name = module_name
        self._open_fn = open_fn
        self._init_fn = init_fn
        self._guard = threading.RLock()
        self._local = threading.local()
        self._owned: list[tuple[threading.Thread, sqlite3.Connection]] = []
        self._generation = 0

    def _module(self):
        return sys.modules[self._module_name]

    def _is_owned(self, conn) -> bool:
        return any(c is conn for _, c in self._owned)

    def _prune(self) -> None:
        primary = self._module()._conn
        live = []
        for thread, conn in self._owned:
            if thread.is_alive() or conn is primary:      # the primary outlives its thread: `_conn` still names it
                live.append((thread, conn))
            else:
                _close(conn)
        self._owned = live

    def get(self) -> sqlite3.Connection:
        mod = self._module()
        primary = mod._conn
        if primary is not None and not self._is_owned(primary):
            return primary                       # an injected connection (read-only block, a test's own)
        local = self._local
        if primary is not None and getattr(local, "generation", None) == self._generation:
            conn = getattr(local, "conn", None)
            if conn is not None:
                return conn
        with self._guard:
            primary = mod._conn
            if primary is not None and not self._is_owned(primary):
                return primary
            if primary is None:                  # first use, or the module was reset: start a new generation
                self._retire_all()
                conn = self._open_fn()
                try:
                    if self._init_fn is not None:
                        self._init_fn(conn)
                except Exception:
                    conn.close()
                    raise
                mod._conn = conn
            else:
                self._prune()
                conn = self._open_fn()
            self._owned.append((threading.current_thread(), conn))
            local.conn, local.generation = conn, self._generation
            return conn

    def _retire_all(self) -> None:
        for _, conn in self._owned:
            _close(conn)
        self._owned = []
        self._generation += 1

    def close_all(self) -> None:
        """Close every connection the pool opened and forget the module's `_conn`."""
        with self._guard:
            self._retire_all()
            self._module()._conn = None


def _close(conn: sqlite3.Connection) -> None:
    try:
        conn.close()
    except Exception:
        pass
