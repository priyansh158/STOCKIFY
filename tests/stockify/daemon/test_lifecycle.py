"""stockify.daemon.lifecycle and the session tables of stockify.store (M5.2; D-70 stacked pause reasons)."""
from __future__ import annotations

import unittest

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

from stockify import store
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
                for r in store.timeline(sid) if r["kind"] == "state"]


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
        with self.assertRaises(Exception):
            store.create_session("s1", "again", {}, 1)

    def test_reasons_are_stored_sorted_unique(self):
        store.set_session_state("s1", "PAUSED", ["owner", "error", "owner"])
        s = store.get_session("s1")
        self.assertEqual(s["pause_reasons"], ["error", "owner"])
        self.assertEqual(s["pause_reason"], "error")
        store.set_session_state("s1", "RUNNING", [])
        s = store.get_session("s1")
        self.assertEqual((s["pause_reasons"], s["pause_reason"]), ([], None))

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

    def test_commands_come_back_oldest_first_until_marked(self):
        a = store.add_command("s1", "pause", {"reason": "owner"}, 5)
        b = store.add_command(None, "stop_all", None, 3)
        c = store.add_command("s1", "resume", None, 9)
        self.assertEqual([x["cmd_id"] for x in store.pending_commands()], [b, a, c])   # by created_ns
        self.assertEqual(store.pending_commands()[1]["args"], {"reason": "owner"})
        self.assertIsNone(store.pending_commands()[0]["session_id"])
        store.mark_command(b, 50, "ok")
        self.assertEqual([x["cmd_id"] for x in store.pending_commands()], [a, c])


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
        self.assertEqual((s["status"], s["stopped_ns"]), ("STOPPED", 9))
        self.assertTrue(s["pause_reasons"])                  # a stopped session is disabled in the engine's terms
        for call in (lambda: lifecycle.arm("s1", wall_ns=10),
                     lambda: lifecycle.pause("s1", "owner", wall_ns=10),
                     lambda: lifecycle.resume("s1", "owner", wall_ns=10),
                     lambda: lifecycle.start_continuous("s1", wall_ns=10, engine_ns=10)):
            with self.assertRaises(lifecycle.LifecycleError):
                call()
        lifecycle.stop("s1", wall_ns=11)                     # idempotent
        self.assertEqual(store.get_session("s1")["stopped_ns"], 9)
        self.assertEqual(store.get_session("s1")["status"], "STOPPED")

    def test_constants(self):
        self.assertEqual(lifecycle.STATES, ("CREATED", "ARMED", "RUNNING", "PAUSED", "STOPPED"))
        self.assertEqual(lifecycle.PAUSE_REASONS, ("owner", "stop_all", "token_expired", "parity_failure",
                                                   "determinism", "error", "stopped"))
        self.assertTrue(issubclass(lifecycle.LifecycleError, ValueError))


if __name__ == "__main__":
    unittest.main()
