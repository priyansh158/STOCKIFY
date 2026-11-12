"""M6.2 (AM 6.2): lab migration 0005 and the research functions of the store."""
from __future__ import annotations

import random
import sqlite3
import unittest

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store


def _run(run_id, name="n", **kw):
    store.create_run({"run_id": run_id, "name": name, "status": "done", **kw})


class ResearchStoreTests(SandboxedDBTest):
    db_modules = (store,)

    def test_migration_0005_applies_over_a_populated_0004_lab(self):
        from tests.stockify.test_store_migrations import LabMigrationTests
        path = LabMigrationTests._make_v1_lab(self)
        conn = sqlite3.connect(str(path))
        conn.execute("INSERT INTO holdout_unlocks (unlock_ns, run_id, family_id, reason) VALUES (5, 'r1', 'f', 'x')")
        conn.commit()
        conn.close()
        self.assertEqual(store.schema_version(), 5)
        self.assertEqual(store.get_run("r1")["role"], "trial")
        self.assertIsNone(store.get_run("r1")["job_id"])
        self.assertEqual(len(store.session_days("s1")), 1)
        [u] = store.holdout_unlock_rows()
        self.assertEqual((u["run_id"], u["reason"], u["job_id"]), ("r1", "x", None))

    def test_runs_cannot_be_deleted(self):
        _run("r1")
        with self.assertRaises(sqlite3.DatabaseError):
            with store._lock:
                store._connect().execute("DELETE FROM runs WHERE run_id = 'r1'")
        self.assertIsNotNone(store.get_run("r1"))

    def test_holdout_unlock_log_is_append_only(self):
        self.assertTrue(store.log_holdout_unlock("r1", "f", "why"))
        conn = store._connect()
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE holdout_unlocks SET reason = 'z'")
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM holdout_unlocks")
        self.assertEqual([r["reason"] for r in store.holdout_unlock_rows()], ["why"])

    def test_unlock_is_logged_once_per_job_and_reason(self):
        self.assertTrue(store.log_holdout_unlock("r1", "f", "oos", job_id="j1", now_ns=1))
        self.assertFalse(store.log_holdout_unlock("r2", "f", "oos", job_id="j1", now_ns=2))
        self.assertTrue(store.log_holdout_unlock("r2", "f", "other", job_id="j1", now_ns=3))
        self.assertTrue(store.log_holdout_unlock("r3", "f", "oos", now_ns=4))
        self.assertTrue(store.log_holdout_unlock("r4", "f", "oos", now_ns=5))
        self.assertEqual(len(store.holdout_unlock_rows()), 4)
        self.assertEqual(len(store.holdout_unlock_rows("g")), 0)
        self.assertEqual(len(store.holdout_unlock_rows("f")), 4)

    def test_family_runs_orders_by_name_then_run_id_and_filters_roles_and_status(self):
        store.add_family("f", objective="sharpe")
        _run("r3", "b", family_id="f", created_ns=1)
        _run("r2", "a", family_id="f", created_ns=9)
        _run("r1", "a", family_id="f", created_ns=5)
        _run("s1", "a", family_id="f", role="sensitivity")
        _run("q1", "a", family_id="f", status="failed")
        _run("o1", "z", family_id="g")
        self.assertEqual([r["run_id"] for r in store.family_runs("f")], ["r1", "r2", "r3"])
        self.assertEqual([r["run_id"] for r in store.family_runs("f", roles=None, status=None)],
                         ["q1", "r1", "r2", "s1", "r3"])
        self.assertEqual([r["run_id"] for r in store.family_runs("f", roles=("sensitivity", "oos"), status=None)],
                         ["s1"])
        self.assertEqual([r["run_id"] for r in store.family_runs("f", status="failed")], ["q1"])
        self.assertEqual(store.family_runs("f", roles=()), [])

    def test_family_runs_property_matches_filtered_set_in_order(self):
        rng = random.Random(20261112)
        roles, statuses = ("trial", "sensitivity", "oos", "rerun"), ("done", "failed", "queued")
        rows = []
        for i in range(60):
            r = {"run_id": f"r{rng.randrange(10**6):06d}-{i}", "name": rng.choice("abcd"),
                 "family_id": rng.choice(("f", "g", None)), "role": rng.choice(roles),
                 "status": rng.choice(statuses)}
            rows.append(r)
            store.create_run(r)
        for fam in ("f", "g"):
            for sel_roles in (("trial",), None, ("oos", "rerun"), ("trial", "sensitivity")):
                for sel_status in ("done", None, "failed"):
                    want = sorted((r for r in rows if r["family_id"] == fam
                                   and (sel_roles is None or r["role"] in sel_roles)
                                   and (sel_status is None or r["status"] == sel_status)),
                                  key=lambda r: (r["name"], r["run_id"]))
                    got = store.family_runs(fam, roles=sel_roles, status=sel_status)
                    self.assertEqual([r["run_id"] for r in got], [r["run_id"] for r in want])

    def test_run_nav_series_comes_from_run_equity(self):
        _run("r1")
        store.replace_run_rows("r1", orders=[], fills=[], charges=[], equity=[
            {"trade_date": "2026-01-02", "nav_paise": 20, "cash_paise": 0, "margin_paise": 0},
            {"trade_date": "2026-01-01", "nav_paise": 10, "cash_paise": 0, "margin_paise": 0}])
        self.assertEqual(store.run_nav_series("r1"), [("2026-01-01", 10), ("2026-01-02", 20)])
        self.assertEqual(store.run_nav_series("none"), [])

    def test_research_job_roundtrip_with_state_json(self):
        store.create_research_job({"job_id": "j1", "kind": "sweep", "status": "queued", "created_ns": 1,
                                   "spec_json": '{"a":1}'})
        self.assertTrue(store.update_research_job("j1", status="done", state_json='{"w":[1]}', finished_ns=9))
        j = store.get_research_job("j1")
        self.assertEqual((j["status"], j["state_json"], j["finished_ns"], j["spec_json"]),
                         ("done", '{"w":[1]}', 9, '{"a":1}'))
        self.assertFalse(store.update_research_job("nope", status="x"))
        self.assertFalse(store.update_research_job("j1"))
        self.assertIsNone(store.get_research_job("nope"))
        with self.assertRaises(ValueError):
            store.update_research_job("j1", **{"status = 'x', kind": "y"})
        with self.assertRaises(ValueError):
            store.create_research_job({"job_id": "j9", "kind": "sweep", "status": "q", "created_ns": 1, "bogus": 1})
        store.create_research_job({"job_id": "j2", "kind": "walkforward", "status": "queued", "created_ns": 2})
        self.assertEqual([j["job_id"] for j in store.list_research_jobs()], ["j2", "j1"])
        self.assertEqual([j["job_id"] for j in store.list_research_jobs("sweep")], ["j1"])
        self.assertEqual(len(store.list_research_jobs(limit=1)), 1)
        self.assertEqual(len(store.list_research_jobs(limit=10**9)), 2)
        self.assertEqual(store.list_research_jobs(limit=-5, offset=-5), [])

    def test_naming_a_family_survives_add_family(self):
        self.assertTrue(store.add_family("f", objective="sharpe", now_ns=7))
        self.assertEqual(store.get_family("f")["named"], 0)
        store.name_family("f", "my family")
        self.assertFalse(store.add_family("f", objective="other", description="auto", named=False))
        f = store.get_family("f")
        self.assertEqual((f["named"], f["description"], f["objective"], f["created_ns"]),
                         (1, "my family", "sharpe", 7))
        self.assertEqual([x["family_id"] for x in store.list_families()], ["f"])
        self.assertIsNone(store.get_family("zz"))

    def test_backfill_updates_only_null_family_rows(self):
        _run("a", family_id=None, metrics_json="m")
        _run("b", family_id="keep")
        _run("c", family_id=None)
        seen = []

        def compute(row):
            seen.append(row["run_id"])
            return None if row["run_id"] == "c" else "fam-" + row["run_id"]
        self.assertEqual(store.backfill_null_families(compute), 1)
        self.assertEqual(sorted(seen), ["a", "c"])
        self.assertEqual(store.get_run("a")["family_id"], "fam-a")
        self.assertEqual(store.get_run("a")["metrics_json"], "m")
        self.assertEqual(store.get_run("b")["family_id"], "keep")
        self.assertIsNone(store.get_run("c")["family_id"])


if __name__ == "__main__":
    unittest.main()
