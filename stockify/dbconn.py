"""One SQLite connection per thread for a module's database (D-143).

A single `check_same_thread=False` connection shared by the API's worker threads is not safe for reads:
two threads interleave `execute` / `fetch` on the same handle and SQLite answers `InterfaceError('bad
parameter or other API misuse')` or hands back rows as None. WAL lets any number of connections read at once
while one writes, so each thread gets its own connection; a module's writes stay serialised by its own write
lock (`locked_write`) and `BEGIN IMMEDIATE`, exactly as before.

A module keeps its `_conn` global: the first connection it opened, where migrations ran. It is the pool's own,
and stays open when the thread that opened it ends. Other threads' connections are closed when their thread
ends (any kind of thread). Setting `_conn = None` or calling `close_all` retires every connection; a `_conn`
set to a connection the pool did not open (a test's own) is used by every thread, so that is for tests only:
code that must read through its own connection uses `override`, which is private to the calling thread.
"""
from __future__ import annotations

import contextlib
import sqlite3
import sys
import threading
import weakref
from typing import Callable, Iterator

# One busy timeout for every pooled connection: the one `stockify.data.migrate` sets (30 s), which the single
# shared connection used to carry, so a writer waiting on the daemon's long refdata transaction waits as before.
BUSY_TIMEOUT_S = 30.0


class _Slot:
    """What a thread holds: its connection. Freed with the thread; `weakref.finalize` then closes the connection."""
    __slots__ = ("conn", "generation", "__weakref__")

    def __init__(self, conn: sqlite3.Connection, generation: int) -> None:
        self.conn = conn
        self.generation = generation


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
        self._owned: set[sqlite3.Connection] = set()       # every connection this pool opened and has not closed
        self._generation = 0

    def _module(self):
        return sys.modules[self._module_name]

    def get(self) -> sqlite3.Connection:
        local = self._local
        override = getattr(local, "override", None)
        if override is not None:
            return override
        mod = self._module()
        primary = mod._conn
        if primary is not None and primary not in self._owned:
            return primary                       # a connection a test put in `_conn`
        slot = getattr(local, "slot", None)
        if primary is not None and slot is not None and slot.generation == self._generation:
            return slot.conn
        with self._guard:
            primary = mod._conn
            if primary is not None and primary not in self._owned:
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
                self._owned.add(conn)            # owned BEFORE it is published: a reader that sees `_conn` must
                mod._conn = conn                 # find it in `_owned`, or it would take it for a test's own
                slot = _Slot(conn, self._generation)
            else:
                conn = self._open_fn()
                self._owned.add(conn)
                slot = _Slot(conn, self._generation)
                weakref.finalize(slot, self._release, conn)    # closed when the thread (and its slot) goes
            local.slot = slot
            return conn

    def _release(self, conn: sqlite3.Connection) -> None:
        self._owned.discard(conn)
        _close(conn)

    def _retire_all(self) -> None:
        owned, self._owned = self._owned, set()
        for conn in owned:
            _close(conn)
        self._generation += 1

    @contextlib.contextmanager
    def override(self, conn: sqlite3.Connection) -> Iterator[None]:
        """For the block, THIS thread (only) reads and writes through `conn` (a read-only block, `span_validate`)."""
        local = self._local
        before = getattr(local, "override", None)
        local.override = conn
        try:
            yield
        finally:
            local.override = before

    def close_all(self) -> None:
        """Close every connection the pool opened, and one a test put in `_conn`; forget the module's `_conn`."""
        with self._guard:
            mod = self._module()
            injected = mod._conn
            self._retire_all()
            if injected is not None:
                _close(injected)
            mod._conn = None


@contextlib.contextmanager
def locked_write(lock, conn: sqlite3.Connection) -> Iterator[None]:
    """The module's write lock around a write on `conn`; if the write fails the transaction is rolled back, so this
    thread's connection does not keep the database's write lock against every other thread."""
    with lock:
        try:
            yield
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise


def _close(conn: sqlite3.Connection) -> None:
    try:
        conn.close()
    except Exception:
        pass
