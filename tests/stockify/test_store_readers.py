"""GK 5.1 (carried): the store's one shared connection must never show a reader another thread's open transaction.

The engine thread, the main thread and the API's workers share `store._conn`. SQLite shows a connection its own
uncommitted writes, so a reader on the same connection would see rows another thread may still roll back. Every
read therefore waits for the store's lock, which a writer holds from BEGIN to COMMIT."""
from __future__ import annotations

import threading
import unittest
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

from stockify import store


class StoreReadersTest(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        store.create_session("s1", "s1", {}, 1)
        store.set_session_state("s1", "ARMED", [])
        store.upsert_session_day("s1", "2026-11-17", last_frame_no=1)
        store.add_command("s1", "pause", None, 5)

    def assert_reader_waits_for_the_open_transaction(self, name, reader, *, uncommitted):
        """Hold a transaction open (state written, not committed) on another thread; `reader` must not return until
        it is over, and must then see what was committed."""
        inside, release = threading.Event(), threading.Event()
        real = store._write_timeline

        def hold_open(*args, **kw):
            real(*args, **kw)                                  # the state change is already written, uncommitted
            inside.set()
            release.wait(3)

        def writer():
            with mock.patch.object(store, "_write_timeline", hold_open):
                store.transition("s1", lambda s: ("PAUSED", ["owner"]), wall_ns=9)

        results = []
        w = threading.Thread(target=writer)
        w.start()
        self.assertTrue(inside.wait(3), name)
        r = threading.Thread(target=lambda: results.append(reader()))
        r.start()
        r.join(0.4)
        early = list(results)
        release.set()
        w.join(10)
        r.join(10)
        self.assertFalse(w.is_alive() or r.is_alive(), name)
        store.set_session_state("s1", "ARMED", [])             # back to the start for the next reader
        self.assertEqual(early, [], f"{name}: a reader returned while another thread's transaction was open: {early}")
        self.assertEqual(len(results), 1, name)
        self.assertTrue(uncommitted(results[0]), f"{name}: {results[0]!r}")

    def test_every_reader_waits_for_a_writers_open_transaction(self):
        readers = {
            "get_session": (lambda: store.get_session("s1"), lambda r: r["status"] == "PAUSED"),
            "list_sessions": (lambda: store.list_sessions(), lambda r: r[0]["status"] == "PAUSED"),
            "timeline": (lambda: store.timeline("s1"), lambda r: r[-1]["kind"] == "state"),
            "get_session_day": (lambda: store.get_session_day("s1", "2026-11-17"), lambda r: r["last_frame_no"] == 1),
            "pending_commands": (lambda: store.pending_commands(), lambda r: len(r) == 1),
            "session_rows": (lambda: store.session_rows("s1", "orders"), lambda r: r == []),
            "latest_recorder_status": (lambda: store.latest_recorder_status(), lambda r: r is None),
            "list_runs": (lambda: store.list_runs(), lambda r: r == []),
            "get_run": (lambda: store.get_run("nope"), lambda r: r is None),
            "schema_version": (lambda: store.schema_version(), lambda r: r == store.SCHEMA_VERSION),
            "margin_calibrations": (lambda: store.margin_calibrations(), lambda r: r == []),
        }
        for name, (reader, ok) in readers.items():
            with self.subTest(name):
                self.assert_reader_waits_for_the_open_transaction(name, reader, uncommitted=ok)

    def test_a_rolled_back_change_is_never_seen(self):
        inside, release = threading.Event(), threading.Event()
        real = store._write_timeline

        def hold_then_fail(*args, **kw):
            real(*args, **kw)
            inside.set()
            release.wait(3)
            raise RuntimeError("boom")

        def writer():
            with mock.patch.object(store, "_write_timeline", hold_then_fail):
                with self.assertRaises(RuntimeError):
                    store.transition("s1", lambda s: ("PAUSED", ["owner"]), wall_ns=9)

        seen = []
        w = threading.Thread(target=writer)
        w.start()
        self.assertTrue(inside.wait(3))
        r = threading.Thread(target=lambda: seen.append(store.get_session("s1")["status"]))
        r.start()
        r.join(0.2)
        release.set()
        w.join(10)
        r.join(10)
        self.assertEqual(seen, ["ARMED"])


if __name__ == "__main__":
    unittest.main()
