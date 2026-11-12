"""M6.4 (AM 6.3): the trial registry: default families, roles, what counts as a trial, the run integration."""
from __future__ import annotations

import json
import random
import shutil
import unittest
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify import paths, runconfig, runs, sessionconfig
from stockify.config import loader
from stockify.research import registry
from tests.stockify.research.fixtures import make_run
from tests.stockify.test_bundle import run as run_config
from tests.stockify.test_runs_launch import BUY, RunsTestBase

REPO = Path(__file__).resolve().parents[3]
ORB = json.loads((REPO / "examples" / "runs" / "orb_reliance_minute.json").read_text(encoding="utf-8"))
SESSION = {"schema": sessionconfig.SCHEMA, "name": "s", "capital_paise": 10_000_000,
           "strategy": {"kind": "python", "ref": "stockify.strategies.buy_and_hold:BuyAndHold",
                        "params": {"symbol": "NSE:RELIANCE"}},
           "fill_model": {}, "universe": ["NSE:RELIANCE", "NSE:TCS"], "costs": {"allow_unverified": True}}


def cfg_with(module: str = "pkg.mod", instruments=("NSE:RELIANCE",)) -> dict:
    return {**ORB, "family_id": None,
            "strategy": {"kind": "python", "ref": f"{module}:Cls", "params": {}},
            "data": {"packs": [{"spec": {"dataset": "bars", "interval": "minute", "instruments": list(instruments),
                                         "from": "2026-11-04", "to": "2026-11-04"}}]}}


class FamilyIdTests(unittest.TestCase):
    def test_default_family_id_shape(self):
        fam = registry.default_family_id(ORB)           # ORB carries its own family_id: ignored
        self.assertTrue(fam.startswith("opening_range_breakout/"), fam)
        self.assertTrue(fam.endswith("/sharpe"), fam)
        self.assertEqual(fam.split("/")[1], "bars-minute-NSE_RELIANCE")
        self.assertEqual(runconfig.validate({**ORB, "family_id": fam})["family_id"], fam)
        self.assertTrue(registry.default_family_id(ORB, "net_pnl").endswith("/net_pnl"))
        with self.assertRaises(ValueError):
            registry.default_family_id(ORB, "luck")

    def test_default_family_segments_are_at_most_64_characters(self):
        cfg = cfg_with("a" * 90, [f"NSE:SYM{i}" for i in range(200)])
        fam = registry.default_family_id(cfg)
        parts = fam.split("/")
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(len(p) <= 64 for p in parts), fam)
        self.assertLessEqual(len(fam), 128)
        self.assertTrue(parts[1].endswith("_plus199"), parts[1])    # the suffix survives the truncation
        runconfig.validate({**cfg, "family_id": fam})

    def test_other_strategy_kinds_and_hash_only_packs(self):
        self.assertTrue(registry.default_family_id({**ORB, "strategy": {"kind": "cpp", "ref": "orb_cpp"}})
                        .startswith("orb_cpp/"))
        self.assertTrue(registry.default_family_id({**ORB, "strategy": {"kind": "script", "actions": []}})
                        .startswith("script/"))
        by_hash = {**ORB, "data": {"packs": [{"sha256": "a" * 64}]}}
        self.assertEqual(registry.default_family_id(by_hash).split("/")[1], "packs")

    def test_default_family_id_properties_over_generated_configs(self):
        """Seeded loop: whatever the module name and instruments, the id is three safe segments, deterministic."""
        rng = random.Random(20261112)
        alphabet = "abcXYZ019._-/ \\é€日*?<>|\"'"
        for n in range(300):
            module = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 120)))
            insts = ["".join(rng.choice(alphabet + ":") for _ in range(rng.randint(1, 40)))
                     for _ in range(rng.randint(1, 300))]
            cfg = cfg_with(module, insts)
            fam = registry.default_family_id(cfg)
            with self.subTest(n=n, fam=fam):
                parts = fam.split("/")
                self.assertEqual(len(parts), 3)
                self.assertTrue(all(len(p) <= 64 for p in parts))
                self.assertLessEqual(len(fam), 128)
                self.assertEqual(fam, registry.default_family_id(cfg))          # deterministic
                self.assertTrue(all(paths.safe_segment(x, 64) for x in parts))
                runconfig.validate({**ORB, "family_id": fam})                   # the run config's own rule

    def test_job_family_id_shape(self):
        self.assertEqual(registry.job_family_id("sweep", "orb-grid"), "sweep/orb-grid")
        self.assertEqual(registry.job_family_id("walkforward", "j" * 40), "walkforward/" + "j" * 40)

    def test_session_gets_a_family_with_dataset_live(self):
        cfg = sessionconfig.validate(SESSION)
        self.assertEqual(cfg["family_id"], "buy_and_hold/live-NSE_RELIANCE_plus1/sharpe")
        self.assertEqual(registry.family_for_session(cfg), cfg["family_id"])
        explicit = sessionconfig.validate({**SESSION, "family_id": "mine/x/y"})
        self.assertEqual(explicit["family_id"], "mine/x/y")

    def test_one_set_of_objectives(self):
        self.assertEqual(set(loader.RESEARCH_OBJECTIVES), set(registry.OBJECTIVES))


class RegistryTests(SandboxedDBTest):
    db_modules = (store,)

    def test_variants_and_oos_runs_are_not_trials(self):
        make_run("t1", "f/a/sharpe")
        for role in ("sensitivity", "oos", "rerun"):
            make_run(f"x-{role}", "f/a/sharpe", role=role)
        self.assertEqual(len(store.family_runs("f/a/sharpe", roles=None)), 4)
        self.assertEqual([t.run_id for t in registry.trials("f/a/sharpe")], ["t1"])

    def test_failed_queued_and_running_runs_are_not_trials(self):
        make_run("ok", "f/a/sharpe")
        for status in ("failed", "queued", "running", "cancelled"):
            make_run(f"r-{status}", "f/a/sharpe", status=status)
        self.assertEqual([t.run_id for t in registry.trials("f/a/sharpe")], ["ok"])

    def test_pruned_folder_still_counts(self):
        make_run("t1", "f/a/sharpe", with_folder=True, seed=7)
        before = registry.trials("f/a/sharpe")
        shutil.rmtree(paths.run_path("t1"))
        after = registry.trials("f/a/sharpe")
        self.assertEqual(before, after)
        [t] = after
        self.assertEqual((t.seed, t.capital_paise, len(t.dates), len(t.navs)), (7, 100_000_000, 10, 10))
        self.assertIsInstance(t.dates, tuple)

    def test_best_trial_follows_the_family_objective(self):
        smooth = [0.001, 0.0011, 0.001, 0.0012, 0.001, 0.0011]     # small and steady: the higher Sharpe
        bumpy = [0.03, -0.02, 0.04, -0.02, 0.03, -0.01]            # large and noisy: the higher net P&L
        for fam, objective in (("f/x/sharpe", "sharpe"), ("f/x/net_pnl", "net_pnl")):
            make_run(f"a-{objective}", fam, daily_returns=smooth, name="a")
            make_run(f"b-{objective}", fam, daily_returns=bumpy, name="b")
            registry.ensure_family(fam, objective)
        self.assertEqual(registry.best_trial("f/x/sharpe").name, "a")
        self.assertEqual(registry.best_trial("f/x/net_pnl").name, "b")

    def test_best_trial_ties_and_null_values(self):
        same = [0.01, -0.005, 0.02, 0.0, 0.01]
        make_run("r2", "f/t/sharpe", daily_returns=same, name="same")
        make_run("r1", "f/t/sharpe", daily_returns=same, name="same")
        make_run("r0", "f/t/sharpe", daily_returns=same, name="zzz")
        self.assertEqual(registry.best_trial("f/t/sharpe").run_id, "r1")        # lowest name, then run id
        make_run("flat", "f/n/sharpe", daily_returns=[0.0] * 5)                 # sd 0: sharpe is null
        [t] = registry.trials("f/n/sharpe")
        self.assertIsNone(t.objective_value)
        self.assertIsNone(registry.best_trial("f/n/sharpe"))                    # nobody eligible
        make_run("ok", "f/n/sharpe", daily_returns=[0.01, -0.01, 0.02, 0.0, 0.01])
        self.assertEqual(registry.best_trial("f/n/sharpe").run_id, "ok")
        self.assertIsNone(registry.best_trial("f/none/sharpe"))

    def test_objective_value(self):
        make_run("a", "f/o/sharpe", daily_returns=[0.01, -0.005, 0.02, 0.0, 0.01])
        row = store.get_run("a")
        self.assertEqual(registry.objective_value(row, "net_pnl"),
                         float(json.loads(row["metrics_json"])["nav_end_paise"] - 100_000_000))
        for o in registry.OBJECTIVES:
            self.assertIsNotNone(registry.objective_value(row, o), o)
        self.assertIsNone(registry.objective_value({"metrics_json": None}, "sharpe"))
        self.assertIsNone(registry.objective_value({"metrics_json": '{"sharpe": null}'}, "sharpe"))
        with self.assertRaises(ValueError):
            registry.objective_value(row, "luck")

    def test_backfill_fills_only_null_families_and_dry_run_changes_nothing(self):
        cfg = runconfig.load(run_config(name="old", strategy=BUY))
        store.create_run({"run_id": "old", "name": "old", "status": "done", "config_json": json.dumps(cfg)})
        store.create_run({"run_id": "bad", "name": "bad", "status": "done", "config_json": "{not json"})
        store.create_run({"run_id": "kept", "name": "kept", "status": "done", "family_id": "mine/x/y",
                          "config_json": json.dumps(cfg)})
        self.assertEqual(registry.backfill(dry_run=True), 1)
        self.assertIsNone(store.get_run("old")["family_id"])
        self.assertEqual(store.list_families(), [])
        self.assertEqual(registry.backfill(), 1)
        want = registry.default_family_id(cfg)
        self.assertEqual(store.get_run("old")["family_id"], want)
        self.assertIsNone(store.get_run("bad")["family_id"])
        self.assertEqual(store.get_run("kept")["family_id"], "mine/x/y")
        self.assertEqual([f["family_id"] for f in store.list_families()], [want])
        self.assertIsNotNone(store.get_family(store.get_run("old")["family_id"]))    # a backfilled family has its row
        self.assertIsNone(store.get_family("mine/x/y"))                              # a kept one is not touched
        self.assertEqual(registry.backfill(), 0)


class RunIntegrationTests(RunsTestBase):
    def test_new_run_gets_the_default_family_and_a_family_row(self):
        rid = runs.queue(run_config(name="t", strategy=BUY))
        row = store.get_run(rid)
        fam = registry.default_family_id(runconfig.load(run_config(name="t", strategy=BUY)))
        self.assertEqual((row["family_id"], row["role"], row["job_id"]), (fam, "trial", None))
        self.assertEqual(json.loads(row["config_json"])["family_id"], fam)
        request = json.loads((paths.run_path(rid) / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(request["family_id"], fam)
        self.assertEqual(store.get_family(fam)["objective"], "sharpe")
        rid2 = runs.queue(run_config(name="t2", strategy=BUY), role="sensitivity", job_id="job1")
        self.assertEqual((store.get_run(rid2)["role"], store.get_run(rid2)["job_id"]), ("sensitivity", "job1"))

    def test_explicit_family_is_kept(self):
        rid = runs.queue(run_config(name="t", strategy=BUY, family_id="mine/exp/1"))
        self.assertEqual(store.get_run(rid)["family_id"], "mine/exp/1")
        self.assertIsNotNone(store.get_family("mine/exp/1"))

    def test_a_role_outside_the_registry_is_refused(self):
        cfg = run_config(name="t", strategy=BUY)
        for bad in ("trails", "", None, "TRIAL"):
            with self.subTest(role=bad):
                with self.assertRaisesRegex(ValueError, "role must be one of"):
                    runs.queue(cfg, role=bad)
                with self.assertRaisesRegex(ValueError, "role must be one of"):
                    runs.launch(cfg, role=bad, impl="oracle", report=False)
        self.assertEqual(store.list_runs(), [])

    def test_launch_stores_role_job_and_family(self):
        rid = runs.launch(run_config(name="t", strategy=BUY), impl="oracle", report=False, role="oos", job_id="j9")
        row = store.get_run(rid)
        self.assertEqual((row["status"], row["role"], row["job_id"]), ("done", "oos", "j9"))
        self.assertEqual(row["family_id"],
                         registry.default_family_id(runconfig.load(run_config(name="t", strategy=BUY))))
        self.assertEqual(registry.trials(row["family_id"]), [])             # an oos run is not a trial

    def test_rerun_keeps_the_family_and_marks_the_role_rerun(self):
        rid = runs.launch(run_config(name="t", strategy=BUY, family_id="mine/exp/1"), impl="oracle", report=False)
        new_id, same = runs.rerun(rid)
        self.assertTrue(same)
        row = store.get_run(new_id)
        self.assertEqual((row["family_id"], row["role"]), ("mine/exp/1", "rerun"))
        self.assertEqual([t.run_id for t in registry.trials("mine/exp/1")], [rid])

    def test_rerun_of_a_pre_m6_source_with_a_null_family(self):
        rid = runs.launch(run_config(name="t", strategy=BUY), impl="oracle", report=False)
        store.update_run(rid, family_id=None)                      # a pre-M6 row
        run_json = paths.run_path(rid) / "run.json"
        doc = json.loads(run_json.read_text(encoding="utf-8"))
        default = registry.default_family_id(doc)
        run_json.write_text(json.dumps({**doc, "family_id": "from/folder/1"}), encoding="utf-8")
        new_id, _ = runs.rerun(rid)                               # the folder's family
        self.assertEqual(store.get_run(new_id)["family_id"], "from/folder/1")
        self.assertIsNotNone(store.get_family("from/folder/1"))
        doc.pop("family_id", None)
        run_json.write_text(json.dumps(doc), encoding="utf-8")
        new_id, _ = runs.rerun(rid)                               # neither: the default
        self.assertEqual((store.get_run(new_id)["family_id"], store.get_run(new_id)["role"]), (default, "rerun"))

    def _folder(self, run_id: str, cfg: dict) -> Path:
        d = paths.run_dir(run_id)
        (d / "run.json").write_text(json.dumps(cfg), encoding="utf-8")
        (d / "manifest.json").write_text(json.dumps({"engine": "oracle", "labels": []}), encoding="utf-8")
        (d / "metrics.json").write_text(json.dumps({"sharpe": 1.0}), encoding="utf-8")
        (d / "orders.csv").write_text("order_id\n", encoding="utf-8")
        (d / "fills.csv").write_text("fill_id\n", encoding="utf-8")
        (d / "charges.csv").write_text("row_no,kind,order_id,trade_date,turnover_paise,brokerage,stt,exchange,sebi,"
                                       "stamp,gst,dp,other,total\n", encoding="utf-8")
        (d / "equity.csv").write_text("trade_date,nav_paise,cash_paise,margin_blocked_paise,gross_pnl_paise,"
                                      "charges_paise\n2026-01-05,100,100,0,0,0\n", encoding="utf-8")
        return d

    def test_import_run_keeps_the_stored_family_for_a_pre_m6_folder(self):
        cfg = runconfig.load(run_config(name="old", strategy=BUY))
        cfg.pop("family_id")
        folder = self._folder("old", cfg)
        store.create_run({"run_id": "old", "name": "old", "status": "running", "family_id": "mine/kept/1",
                          "role": "oos", "job_id": "j1"})
        runs.import_run(folder)
        row = store.get_run("old")
        self.assertEqual((row["family_id"], row["role"], row["job_id"], row["status"]),
                         ("mine/kept/1", "oos", "j1", "done"))
        self.assertEqual(json.loads(row["config_json"])["family_id"], "mine/kept/1")
        runs.import_run(self._folder("fresh", {**cfg, "name": "fresh"}))      # no row at all: the default family
        self.assertEqual(store.get_run("fresh")["family_id"], registry.default_family_id(cfg))
        runs.import_run(self._folder("tagged", {**cfg, "name": "tagged", "family_id": "from/folder/1"}))
        self.assertEqual(store.get_run("tagged")["family_id"], "from/folder/1")


if __name__ == "__main__":
    unittest.main()
