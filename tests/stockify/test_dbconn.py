"""D-143: every module that used one shared `check_same_thread=False` connection now gives each thread its own.

The bug: FastAPI runs sync endpoints on a thread pool; readers interleaving `execute` / `fetch` on one handle got
`sqlite3.InterfaceError('bad parameter or other API misuse')` or rows that came back None. The stress test below
hammers every converted module from many threads (seeded random lookups and ranges, checked against what the
single-threaded oracle says) while a writer inserts."""
from __future__ import annotations

import random
import sqlite3
import threading
import unittest

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import geo_alerts
import tick_buffer
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
