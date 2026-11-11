"""stockify.daemon.lifecycle and the session tables of stockify.store (M5.2; D-70 stacked pause reasons)."""
from __future__ import annotations

import sqlite3
import threading
import unittest

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

from stockify import paths, store
from stockify.daemon import lifecycle


class LifecycleTest(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        store.create_session("s1", "first", {"a": 1}, 10)

    def status(self, sid="s1"):
        return store.get_session(sid)["status"]

    def reasons(self, sid="s1"):
        return store.get_session(sid)["pause_reasons"]

    def states(self, sid="s1"):
        return [(r["detail"]["from"], r["detail"]["to"], r["detail"]["reasons"])
                for r in store.timeline(sid, limit=100_000) if r["kind"] == "state"]


class StoreSessionTests(LifecycleTest):
    def test_create_and_get(self):
        s = store.get_session("s1")
        self.assertEqual((s["name"], s["status"], s["pause_reasons"], s["created_ns"]), ("first", "CREATED", [], 10))
        self.assertEqual(s["config"], {"a": 1})
        self.assertIsNone(store.get_session("nope"))

    def test_unsafe_or_duplicate_id_is_refused(self):
        for bad in ("", "..", "a/b", "a\\b", "C:x", "x" * 129, "CON", "a."):
            with self.assertRaises(ValueError, msg=bad):
                store.create_session(bad, "n", {}, 1)
        with self.assertRaises(sqlite3.IntegrityError):
            store.create_session("s1", "again", {}, 1)

    def test_ids_are_unique_regardless_of_case(self):
        with self.assertRaises(sqlite3.IntegrityError):
            store.create_session("S1", "shouts", {}, 1)

    def test_journals_dir_refuses_unsafe_session_ids(self):
        for bad in ("..", "a/b", "a\\b", "CON", "x" * 129):
            with self.assertRaises(ValueError, msg=bad):
                paths.journals_dir(bad)
        self.assertEqual(paths.journals_dir("s1").name, "s1")
        self.assertEqual(paths.journals_dir().name, "journals")

    def test_set_session_state_stores_a_valid_state(self):
        store.set_session_state("s1", "PAUSED", ["error", "owner"])
        s = store.get_session("s1")
        self.assertEqual((s["pause_reasons"], s["pause_reason"]), (["error", "owner"], "error"))
        store.set_session_state("s1", "RUNNING", [])
        s = store.get_session("s1")
        self.assertEqual((s["pause_reasons"], s["pause_reason"]), ([], None))

    def test_set_session_state_refuses_what_the_engine_could_not_carry(self):
        bad = [("LIMBO", []), ("PAUSED", []), ("RUNNING", ["owner"]),            # disabled iff reasons
               ("PAUSED", ["owner", "error"]), ("PAUSED", ["owner", "owner"]),   # sorted and unique
               ("PAUSED", ["because"]), ("PAUSED", [""]),
               ("PAUSED", ["stopped"]), ("ARMED", ["stopped"]),                  # only STOPPED carries it
               ("STOPPED", ["owner"]), ("STOPPED", [])]
        for status, reasons in bad:
            with self.assertRaises(ValueError, msg=(status, reasons)):
                store.set_session_state("s1", status, reasons)
        self.assertEqual(store.get_session("s1")["status"], "CREATED")
        store.set_session_state("s1", "ARMED", ["owner"])                        # waiting sessions may carry reasons
        store.set_session_state("s1", "STOPPED", ["owner", "stopped"])

    def test_set_session_state_on_an_unknown_session_is_a_key_error(self):
        with self.assertRaises(KeyError):
            store.set_session_state("ghost", "ARMED", [])

    def test_timeline_and_commands_refuse_an_unknown_session(self):
        with self.assertRaises(KeyError):
            store.add_timeline("ghost", "note", {}, wall_ns=1, engine_ns=None)
        with self.assertRaises(KeyError):
            store.add_command("ghost", "pause", None, 1)
        self.assertEqual(store.pending_commands(), [])
        self.assertEqual(store.timeline("ghost"), [])
        store.add_command(None, "stop_all", None, 1)                             # every session: allowed

    def test_session_day_fields_are_the_tables_columns(self):
        cols = {r["name"] for r in store._connect().execute("PRAGMA table_info(session_days)")}
        self.assertEqual(set(store.SESSION_DAY_FIELDS), cols - {"session_id", "trade_date"})

    def test_list_sessions_filters_by_status(self):
        store.create_session("s2", "second", {}, 20)
        store.set_session_state("s2", "ARMED", [])
        self.assertEqual([s["session_id"] for s in store.list_sessions()], ["s1", "s2"])
        self.assertEqual([s["session_id"] for s in store.list_sessions(status="ARMED")], ["s2"])

    def test_timeline_in_order_with_paging(self):
        for i in range(5):
            store.add_timeline("s1", "note", {"i": i}, wall_ns=100 - i, engine_ns=None if i else 7)
        rows = store.timeline("s1")
        self.assertEqual([r["detail"]["i"] for r in rows], [0, 1, 2, 3, 4])   # insertion order
        self.assertEqual((rows[0]["engine_ns"], rows[1]["engine_ns"]), (7, None))
        self.assertEqual([r["detail"]["i"] for r in store.timeline("s1", offset=3, limit=5)], [3, 4])
        self.assertEqual(len(store.timeline("s1", limit=2)), 2)

    def test_session_days_upsert_merges_fields(self):
        store.upsert_session_day("s1", "2026-11-09", status="open", bundle_sha256="ab")
        store.upsert_session_day("s1", "2026-11-09", status="finalised", coverage=0.9)
        d = store.get_session_day("s1", "2026-11-09")
        self.assertEqual((d["status"], d["bundle_sha256"], d["coverage"]), ("finalised", "ab", 0.9))
        self.assertIsNone(store.get_session_day("s1", "2026-11-10"))
        with self.assertRaises(ValueError):
            store.upsert_session_day("s1", "2026-11-09", bogus=1)

    def test_commands_come_back_in_insertion_order_until_marked(self):
        a = store.add_command("s1", "pause", {"reason": "owner"}, 5)
        b = store.add_command(None, "stop_all", None, 3)       # an earlier stamp does not jump the queue
        c = store.add_command("s1", "resume", None, 9)
        self.assertEqual([x["cmd_id"] for x in store.pending_commands()], [a, b, c])
        self.assertEqual(store.pending_commands()[0]["args"], {"reason": "owner"})
        self.assertIsNone(store.pending_commands()[1]["session_id"])
        store.mark_command(a, 50, "ok")
        self.assertEqual([x["cmd_id"] for x in store.pending_commands()], [b, c])


class TransitionTests(LifecycleTest):
    def test_day_cycle(self):
        lifecycle.arm("s1", wall_ns=1)
        self.assertEqual(self.status(), "ARMED")
        self.assertEqual(lifecycle.start_continuous("s1", wall_ns=2, engine_ns=20), "RUNNING")
        self.assertEqual(self.status(), "RUNNING")
        lifecycle.arm("s1", wall_ns=3)
        self.assertEqual(self.status(), "ARMED")
        self.assertEqual(self.states(), [("CREATED", "ARMED", []), ("ARMED", "RUNNING", []), ("RUNNING", "ARMED", [])])
        rows = [r for r in store.timeline("s1") if r["kind"] == "state"]
        self.assertEqual((rows[1]["wall_ns"], rows[1]["engine_ns"]), (2, 20))

    def test_pause_running_then_resume(self):
        lifecycle.arm("s1", wall_ns=1)
        lifecycle.start_continuous("s1", wall_ns=2, engine_ns=2)
        self.assertEqual(lifecycle.pause("s1", "owner", wall_ns=3, engine_ns=3), ["owner"])
        self.assertEqual(self.status(), "PAUSED")
        self.assertEqual(lifecycle.resume("s1", "owner", wall_ns=4), [])
        self.assertEqual(self.status(), "RUNNING")

    def test_owner_pause_survives_token_expiry_and_login(self):
        lifecycle.arm("s1", wall_ns=0)
        lifecycle.start_continuous("s1", wall_ns=0, engine_ns=0)
        lifecycle.pause("s1", "owner", wall_ns=1)
        lifecycle.pause("s1", "token_expired", wall_ns=2)
        self.assertEqual(lifecycle.resume("s1", "token_expired", wall_ns=3), ["owner"])
        self.assertEqual(store.get_session("s1")["status"], "PAUSED")
        self.assertEqual(self.reasons(), ["owner"])

    def test_owner_resume_also_clears_stop_all_but_nothing_else(self):
        lifecycle.arm("s1", wall_ns=0)
        lifecycle.start_continuous("s1", wall_ns=0, engine_ns=0)
        for r in ("stop_all", "owner", "error"):
            lifecycle.pause("s1", r, wall_ns=1)
        self.assertEqual(lifecycle.resume("s1", "owner", wall_ns=2), ["error"])
        self.assertEqual(self.status(), "PAUSED")

    def test_reasons_carry_through_arm_and_start(self):
        lifecycle.pause("s1", "owner", wall_ns=1)          # CREATED keeps the reason
        self.assertEqual((self.status(), self.reasons()), ("CREATED", ["owner"]))
        lifecycle.arm("s1", wall_ns=2)
        self.assertEqual((self.status(), self.reasons()), ("ARMED", ["owner"]))
        self.assertEqual(lifecycle.start_continuous("s1", wall_ns=3, engine_ns=3), "PAUSED")
        lifecycle.arm("s1", wall_ns=4)                      # PAUSED -> ARMED, reasons kept
        self.assertEqual((self.status(), self.reasons()), ("ARMED", ["owner"]))
        self.assertEqual(lifecycle.resume("s1", "owner", wall_ns=5), [])
        self.assertEqual(self.status(), "ARMED")             # resuming an armed session does not start it
        self.assertEqual(lifecycle.start_continuous("s1", wall_ns=6, engine_ns=6), "RUNNING")

    def test_repeat_pause_and_unknown_resume_are_noops_without_timeline_rows(self):
        lifecycle.pause("s1", "owner", wall_ns=1)
        n = len(store.timeline("s1"))
        self.assertEqual(lifecycle.pause("s1", "owner", wall_ns=2), ["owner"])
        self.assertEqual(lifecycle.resume("s1", "error", wall_ns=3), ["owner"])
        self.assertEqual(len(store.timeline("s1")), n)

    def test_unknown_reason_and_unknown_session_raise(self):
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.pause("s1", "because", wall_ns=1)
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.resume("s1", "because", wall_ns=1)
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.arm("ghost", wall_ns=1)

    def test_start_requires_armed(self):
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.start_continuous("s1", wall_ns=1, engine_ns=1)      # CREATED

    def test_stopped_is_final(self):
        lifecycle.arm("s1", wall_ns=1)
        lifecycle.stop("s1", wall_ns=9)
        s = store.get_session("s1")
        self.assertEqual((s["status"], s["stopped_ns"], s["pause_reasons"]), ("STOPPED", 9, ["stopped"]))
        self.assertEqual(self.states()[-1], ("ARMED", "STOPPED", ["stopped"]))
        for call in (lambda: lifecycle.arm("s1", wall_ns=10),
                     lambda: lifecycle.pause("s1", "owner", wall_ns=10),
                     lambda: lifecycle.resume("s1", "owner", wall_ns=10),
                     lambda: lifecycle.start_continuous("s1", wall_ns=10, engine_ns=10)):
            with self.assertRaises(lifecycle.LifecycleError):
                call()
        n = len(store.timeline("s1"))
        lifecycle.stop("s1", wall_ns=11)                     # a retried stop changes nothing
        s = store.get_session("s1")
        self.assertEqual((s["status"], s["stopped_ns"], s["pause_reasons"]), ("STOPPED", 9, ["stopped"]))
        self.assertEqual(len(store.timeline("s1")), n)

    def test_stop_from_paused_keeps_the_earlier_reasons(self):
        lifecycle.arm("s1", wall_ns=1)
        lifecycle.start_continuous("s1", wall_ns=2, engine_ns=2)
        lifecycle.pause("s1", "owner", wall_ns=3)
        lifecycle.stop("s1", wall_ns=4)
        s = store.get_session("s1")
        self.assertEqual((s["status"], s["pause_reasons"], s["stopped_ns"]), ("STOPPED", ["owner", "stopped"], 4))
        self.assertEqual(self.states()[-1], ("PAUSED", "STOPPED", ["owner", "stopped"]))

    def test_stopped_reason_belongs_to_stop_alone(self):
        for call in (lifecycle.pause, lifecycle.resume):
            with self.assertRaises(lifecycle.LifecycleError):
                call("s1", "stopped", wall_ns=1)

    def test_stop_of_an_unknown_session_raises(self):
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.stop("ghost", wall_ns=1)

    def test_resume_on_a_running_session_changes_nothing(self):
        lifecycle.arm("s1", wall_ns=1)
        lifecycle.start_continuous("s1", wall_ns=2, engine_ns=2)
        n = len(store.timeline("s1"))
        self.assertEqual(lifecycle.resume("s1", "owner", wall_ns=3), [])
        self.assertEqual((self.status(), len(store.timeline("s1"))), ("RUNNING", n))

    def test_constants(self):
        self.assertEqual(lifecycle.STATES, ("CREATED", "ARMED", "RUNNING", "PAUSED", "STOPPED"))
        self.assertEqual(lifecycle.PAUSE_REASONS, ("owner", "stop_all", "token_expired", "parity_failure",
                                                   "determinism", "error", "stopped"))
        self.assertTrue(issubclass(lifecycle.LifecycleError, ValueError))


class AtomicityTests(LifecycleTest):
    def other_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(store._db_path()), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def crash_on_timeline_write(self):
        real = store._write_timeline

        def boom(*a, **k):
            raise RuntimeError("crash between the two writes")

        store._write_timeline = boom
        self.addCleanup(setattr, store, "_write_timeline", real)

    def test_a_failure_inside_a_transition_leaves_neither_state_nor_row(self):
        lifecycle.arm("s1", wall_ns=1)
        n = len(store.timeline("s1"))
        self.crash_on_timeline_write()
        with self.assertRaises(RuntimeError):
            lifecycle.pause("s1", "owner", wall_ns=2)
        self.assertEqual((self.status(), self.reasons(), len(store.timeline("s1"))), ("ARMED", [], n))

    def test_a_failed_stop_leaves_stopped_ns_unset(self):
        self.crash_on_timeline_write()
        with self.assertRaises(RuntimeError):
            lifecycle.stop("s1", wall_ns=5)
        s = store.get_session("s1")
        self.assertEqual((s["status"], s["stopped_ns"], s["pause_reasons"]), ("CREATED", None, []))

    def test_two_connections_racing_never_lose_a_reason(self):
        lifecycle.pause("s1", "owner", wall_ns=1)
        errors: list[BaseException] = []
        start = threading.Barrier(2)

        def worker(reason: str) -> None:
            try:
                conn = self.other_connection()
                start.wait()
                for i in range(60):
                    lifecycle.pause("s1", reason, wall_ns=10 + i, conn=conn)
                    lifecycle.resume("s1", reason, wall_ns=10 + i, conn=conn)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(r,)) for r in ("error", "token_expired")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.reasons(), ["owner"])
        rows = self.states()
        self.assertEqual(len(rows), 1 + 4 * 60)               # the owner pause, then every change, none merged
        for (_, _, reasons) in rows:
            self.assertIn("owner", reasons)
        for (prev, nxt) in zip(rows, rows[1:]):               # each row differs from the last by exactly one reason
            self.assertEqual(len(set(prev[2]) ^ set(nxt[2])), 1)


if __name__ == "__main__":
    unittest.main()
