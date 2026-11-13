"""D-143: every module that used one shared `check_same_thread=False` connection now gives each thread its own.

The bug: FastAPI runs sync endpoints on a thread pool; readers interleaving `execute` / `fetch` on one handle got
`sqlite3.InterfaceError('bad parameter or other API misuse')` or rows that came back None. The stress test below
hammers every converted module from many threads (seeded random lookups and ranges, checked against what the
single-threaded oracle says) while a writer inserts."""
from __future__ import annotations

import gc
import random
import sqlite3
import sys
import threading
import time
import types
import unittest

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import geo_alerts
import tick_buffer
from stockify import dbconn
from stockify.data import history, refdata

MODULES = (refdata, history, tick_buffer, geo_alerts)
ROWS = 400


def value_of(k: int) -> str:
    return f"v{k * 7919 % 10007}"


class PerThreadConnectionTest(SandboxedDBTest):
    db_modules = MODULES

    def seed(self, mod) -> None:
        conn = mod._connect()
        conn.execute("CREATE TABLE IF NOT EXISTS stress_t (k INTEGER PRIMARY KEY, v TEXT NOT NULL)")
        with mod._lock:
            conn.executemany("INSERT OR REPLACE INTO stress_t (k, v) VALUES (?, ?)",
                             [(k, value_of(k)) for k in range(ROWS)])
            conn.commit()

    def test_each_thread_has_its_own_connection_and_keeps_it(self):
        for mod in MODULES:
            with self.subTest(mod.__name__):
                mine = mod._connect()
                self.assertIs(mod._connect(), mine)
                seen = []
                t = threading.Thread(target=lambda: seen.append(mod._connect()))
                t.start()
                t.join()
                self.assertIsNot(seen[0], mine)
                self.assertIs(mod._conn, mine)             # `_conn` stays the first connection, where the schema ran

    def test_the_first_connection_survives_the_thread_that_opened_it(self):
        # The API's first request opens the database on a worker thread that may then end; later threads must not
        # be handed that thread's closed handle (the connection `_conn` names stays open).
        for mod in MODULES:
            with self.subTest(mod.__name__):
                t = threading.Thread(target=mod._connect)
                t.start()
                t.join()
                for _ in range(3):
                    seen = []
                    u = threading.Thread(target=lambda: seen.append(mod._connect().execute("SELECT 1").fetchone()[0]))
                    u.start()
                    u.join()
                    self.assertEqual(seen, [1])
                self.assertEqual(mod._connect().execute("SELECT 1").fetchone()[0], 1)

    def test_a_second_thread_is_never_handed_the_connection_being_published(self):
        # I1: `_conn` was published before the pool recorded it as its own, so a thread in that gap took it for a
        # test's injected connection and shared it. Widen the gap deterministically: the setter sleeps.
        published = threading.Event()

        class Slow(types.ModuleType):
            _c = None

            @property
            def _conn(self):
                return self._c

            @_conn.setter
            def _conn(self, value):
                self._c = value
                published.set()
                time.sleep(0.4)

        fake = Slow("fake_pool_module")
        sys.modules["fake_pool_module"] = fake
        try:
            pool = dbconn.Pool("fake_pool_module", lambda: sqlite3.connect(":memory:", check_same_thread=False))
            first, second = [], []
            a = threading.Thread(target=lambda: first.append(pool.get()))
            a.start()
            self.assertTrue(published.wait(5))
            b = threading.Thread(target=lambda: second.append(pool.get()))
            b.start()
            a.join()
            b.join()
            self.assertIsNot(first[0], second[0])
            pool.close_all()
        finally:
            del sys.modules["fake_pool_module"]

    def test_every_pooled_connection_waits_as_long_as_the_migrations_do(self):
        # I2: only the first connection had the 30 s busy timeout; the others got 10 s.
        for mod in (refdata, history):
            with self.subTest(mod.__name__):
                mod._connect()
                seen = []
                t = threading.Thread(target=lambda: seen.append(mod._connect().execute("PRAGMA busy_timeout").fetchone()[0]))
                t.start()
                t.join()
                self.assertEqual(seen, [30000])
                self.assertEqual(mod._connect().execute("PRAGMA busy_timeout").fetchone()[0], 30000)

    def test_a_failed_write_on_one_thread_does_not_block_a_write_on_another(self):
        # I4: tick_buffer / geo_alerts use implicit transactions; a failed INSERT left the thread's connection in a
        # transaction, holding SQLite's write lock against every other thread's connection.
        guid = {"severity": "high", "disruption_score": 5, "scope": "x", "title": "t", "source": "s", "url": "u",
                "published_at": "p", "why_disruptive": "w", "investor_action": "a", "confidence": 0.5,
                "affected_tickers": [], "article": {}}
        geo_alerts.init_db()
        outcome = []

        def a():
            geo_alerts.save_alert(guid="g1", **guid)
            outcome.append(geo_alerts.save_alert(guid="g1", **guid))          # duplicate: IntegrityError, swallowed

        def b():
            try:
                outcome.append(geo_alerts.mark_seen(guid="other", title="t"))
            except sqlite3.OperationalError as e:
                outcome.append(e)

        ta = threading.Thread(target=a)
        ta.start()
        ta.join()
        tb = threading.Thread(target=b)
        tb.start()
        tb.join(15)
        self.assertFalse(tb.is_alive())
        self.assertEqual(outcome, [None, True])
        # and a write that raises through `locked_write` is rolled back and releases the lock
        conn = tick_buffer._connect()
        with self.assertRaises(RuntimeError):
            with dbconn.locked_write(tick_buffer._lock, conn):
                conn.execute("CREATE TABLE IF NOT EXISTS boom (x)")
                conn.execute("INSERT INTO boom VALUES (1)")
                raise RuntimeError("write failed")
        self.assertFalse(conn.in_transaction)

    def test_an_override_is_private_to_its_thread(self):
        # M2: the read-only block must not turn every thread's reads and writes read-only.
        mine = sqlite3.connect(":memory:", check_same_thread=False)
        refdata._connect()
        with refdata._pool.override(mine):
            self.assertIs(refdata._connect(), mine)
            seen = []
            t = threading.Thread(target=lambda: seen.append(refdata._connect()))
            t.start()
            t.join()
            self.assertIsNot(seen[0], mine)
        self.assertIsNot(refdata._connect(), mine)
        mine.close()

    def test_a_reset_also_closes_a_connection_a_test_put_in_conn(self):
        # M1
        refdata._connect()
        injected = sqlite3.connect(":memory:", check_same_thread=False)
        refdata._conn = injected
        support.reset_sqlite_singleton(refdata)
        with self.assertRaises(sqlite3.ProgrammingError):
            injected.execute("SELECT 1")

    def test_a_connection_is_closed_when_its_thread_ends_even_for_a_foreign_thread(self):
        # M3: `_DummyThread.is_alive()` is always True, so liveness cannot be what frees a connection.
        made = []

        def work():
            made.append(refdata._connect())

        refdata._connect()
        t = threading.Thread(target=work)
        t.start()
        t.join()
        gc.collect()
        with self.assertRaises(sqlite3.ProgrammingError):
            made[0].execute("SELECT 1")
        self.assertIn(refdata._conn, refdata._pool._owned)                      # the first connection stays
        self.assertEqual(refdata._connect().execute("SELECT 1").fetchone()[0], 1)

    def test_resetting_closes_every_thread_connection(self):
        for mod in MODULES:
            with self.subTest(mod.__name__):
                made = []
                t = threading.Thread(target=lambda: made.append(mod._connect()))
                t.start()
                t.join()
                made.append(mod._connect())
                support.reset_sqlite_singleton(mod)
                self.assertIsNone(mod._conn)
                for conn in made:
                    with self.assertRaises(sqlite3.ProgrammingError):
                        conn.execute("SELECT 1")           # closed: no Windows file lock outlives the reset
                self.assertIsNot(mod._connect(), made[0])  # and the next use opens afresh

    def test_a_connection_put_in_conn_is_used_by_every_thread(self):
        # `span_validate.read_only` and the tests inject one; it must reach the worker threads unchanged.
        refdata._connect()
        injected = sqlite3.connect(":memory:", check_same_thread=False)
        saved = refdata._conn
        refdata._conn = injected
        try:
            seen = []
            t = threading.Thread(target=lambda: seen.append(refdata._connect()))
            t.start()
            t.join()
            self.assertIs(seen[0], injected)
            self.assertIs(refdata._connect(), injected)
        finally:
            refdata._conn = saved
            injected.close()

    def test_concurrent_reads_and_a_write_never_misbehave(self):
        threads_n, rounds = 8, 250
        for mod in MODULES:
            with self.subTest(mod.__name__):
                self.seed(mod)
                errors: list[BaseException] = []
                start = threading.Barrier(threads_n + 2)

                def reader(seed: int) -> None:
                    rng = random.Random(seed)
                    try:
                        start.wait(10)
                        for _ in range(rounds):
                            k = rng.randrange(ROWS)
                            row = mod._connect().execute("SELECT k, v FROM stress_t WHERE k = ?", (k,)).fetchone()
                            assert row is not None and (row[0], row[1]) == (k, value_of(k)), (k, row)
                            lo = rng.randrange(ROWS - 20)
                            got = mod._connect().execute(
                                "SELECT k, v FROM stress_t WHERE k BETWEEN ? AND ? ORDER BY k", (lo, lo + 19)).fetchall()
                            assert [(r[0], r[1]) for r in got] == [(i, value_of(i)) for i in range(lo, lo + 20)], lo
                    except BaseException as e:  # noqa: BLE001 — collected, asserted on the main thread
                        errors.append(e)

                def writer() -> None:
                    try:
                        start.wait(10)
                        for i in range(rounds):
                            conn = mod._connect()
                            with mod._lock:
                                conn.execute("INSERT OR REPLACE INTO stress_t (k, v) VALUES (?, ?)",
                                             (10_000 + i, value_of(10_000 + i)))
                                conn.commit()
                    except BaseException as e:  # noqa: BLE001
                        errors.append(e)

                ts = [threading.Thread(target=reader, args=(i,)) for i in range(threads_n)]
                ts.append(threading.Thread(target=writer))
                for t in ts:
                    t.start()
                start.wait(10)
                for t in ts:
                    t.join(120)
                self.assertFalse(any(t.is_alive() for t in ts))
                self.assertEqual(errors, [], f"{mod.__name__}: {errors[:3]!r}")
                n = mod._connect().execute("SELECT COUNT(*) FROM stress_t WHERE k >= 10000").fetchone()[0]
                self.assertEqual(n, rounds)                # every write landed and is visible to a new reader


if __name__ == "__main__":
    unittest.main()
