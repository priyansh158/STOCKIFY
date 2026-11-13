"""M6.10 (WF 6.2): running a sweep: the job row, its own family, results, files, the holdout and the CLI. The pool is a
stub that writes finished runs (nothing runs an engine) except the last test, which runs real children on the oracle."""
from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import random
import unittest
from dataclasses import replace
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify import __main__ as cli
from stockify import paths, research
from stockify.research import holdout, pool, registry, sweep
from tests.stockify.research.fixtures import make_run
from tests.stockify.research.test_walkforward import AXIS, FakePool, base_cfg
from tests.stockify.test_bundle import BundleTest, run as run_config
from tests.stockify.test_runs_launch import BUY

NOW = 1_700_000_000_000_000_000


def spec_of(values=(5, 10, 15), objective="net_pnl", **kw) -> sweep.SweepSpec:
    cfg = base_cfg(**kw)
    cfg["family_id"] = "manual/old-family"           # the base's own family must be ignored
    return sweep.SweepSpec(name="sw", base=cfg, axes={AXIS: list(values)}, search={"kind": "grid"},
                           objective=objective, workers=None, source=None)


class RunSweepTests(SandboxedDBTest):
    db_modules = (store,)

    def go(self, spec=None, fake=None, **kw):  # noqa: ANN001, ANN003
        fake = fake or FakePool()
        res = sweep.run_sweep(spec or spec_of(), pool_run=fake, now_ns=NOW, **kw)
        return res, fake

    def test_failed_trial_is_listed_not_fatal(self):
        res, _ = self.go(fake=FakePool(fail=["sw-t001"]))
        self.assertEqual([r["status"] for r in res.rows], ["done", "failed", "done"])
        bad = res.rows[1]
        self.assertEqual((bad["run_id"], bad["objective_value"], bad["error"]), (None, None, "the run broke on purpose"))
        self.assertEqual(store.get_research_job(res.job_id)["status"], "done")

    def test_ineligible_best_and_tie_break_by_grid_index(self):
        res, _ = self.go(fake=FakePool(net_of=lambda c: 700))          # every trial ties: the lowest index wins
        self.assertEqual(res.best_run_id, res.rows[0]["run_id"])
        res, _ = self.go(fake=FakePool(fail=[f"sw-t{i:03d}" for i in range(3)]))
        self.assertIsNone(res.best_run_id)                              # nothing finished
        res, _ = self.go(spec=spec_of(objective="sharpe"))              # a one-day run has no Sharpe: ineligible
        self.assertIsNone(res.best_run_id)
        self.assertTrue(all(r["status"] == "done" and r["objective_value"] is None for r in res.rows))

    def test_best_is_the_highest_objective(self):
        res, _ = self.go()
        self.assertEqual(res.best_run_id, res.rows[2]["run_id"])       # net = range_minutes * 100

    def test_sweep_is_its_own_family_and_the_base_family_is_ignored(self):
        make_run("manual", "manual/old-family", role="trial")
        res, fake = self.go()
        self.assertEqual(res.family_id, f"sweep/{res.job_id}")
        self.assertTrue(all(j.config["family_id"] == res.family_id for j in fake.calls[0]))
        self.assertEqual(len(registry.trials(res.family_id)), 3)
        self.assertNotIn("manual", {t.run_id for t in registry.trials(res.family_id)})
        self.assertEqual(store.get_research_job(res.job_id)["family_id"], res.family_id)

    def test_family_objective_is_the_sweeps(self):
        res = sweep.run_sweep(spec_of(objective="net_pnl"), pool_run=FakePool(), now_ns=NOW)
        self.assertEqual(store.get_family(res.family_id)["objective"], "net_pnl")
        res = sweep.run_sweep(spec_of(objective="calmar"), pool_run=FakePool(), now_ns=NOW + 1)
        self.assertEqual(store.get_family(res.family_id)["objective"], "calmar")

    def test_sweep_job_row_and_files_are_written(self):
        res, fake = self.go()
        job = store.get_research_job(res.job_id)
        self.assertRegex(res.job_id, r"^sweep-sw-20231114T221320-[0-9a-f]{6}$")
        self.assertEqual((job["kind"], job["status"], job["name"], job["created_ns"]), ("sweep", "done", "sw", NOW))
        self.assertIsNotNone(job["finished_ns"])
        self.assertEqual(json.loads(job["state_json"])["best_run_id"], res.best_run_id)
        self.assertEqual(json.loads(job["spec_json"])["axes"], {AXIS: [5, 10, 15]})
        self.assertEqual({j.role for j in fake.calls[0]}, {"trial"})
        self.assertEqual({j.job_id for j in fake.calls[0]}, {res.job_id})
        folder = paths.research_path(res.job_id)
        doc = json.loads((folder / "sweep.json").read_text(encoding="utf-8"))
        self.assertEqual((doc["job_id"], doc["objective"], len(doc["rows"])), (res.job_id, "net_pnl", 3))
        rows = list(csv.DictReader((folder / "results.csv").read_text(encoding="utf-8").splitlines()))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2][AXIS], "15")
        self.assertEqual(rows[2]["net_pnl_paise"], "1500")
        self.assertEqual(rows[2]["run_id"], res.rows[2]["run_id"])
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["results.csv", "sweep.json"])

    def test_sweep_json_has_no_nan(self):
        res, _ = self.go(spec=spec_of(objective="sharpe"))
        folder = paths.research_path(res.job_id)
        text = (folder / "sweep.json").read_text(encoding="utf-8")
        for bad in ("NaN", "Infinity"):
            self.assertNotIn(bad, text)
        json.loads(text, parse_constant=lambda c: self.fail(c))
        self.assertNotIn("nan", (folder / "results.csv").read_text(encoding="utf-8").lower())

    def test_holdout_locked_base_refuses_without_unlock_and_marks_the_job_failed(self):
        patched = {**research.settings(), "holdout_from": "2026-08-01"}
        spec = spec_of(frm="2026-07-01", to="2026-08-30")
        fake = FakePool()
        with mock.patch.object(research, "settings", return_value=patched):
            with self.assertRaises(holdout.HoldoutLocked) as cm:
                sweep.run_sweep(spec, pool_run=fake, now_ns=NOW)
        self.assertEqual(fake.calls, [])
        (job,) = store.list_research_jobs(kind="sweep")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], f"HoldoutLocked: {cm.exception}")
        self.assertEqual(store.holdout_unlock_rows(), [])

    def test_unlock_reason_reaches_every_trial_and_logs_one_row(self):
        patched = {**research.settings(), "holdout_from": "2026-08-01"}
        spec = spec_of(frm="2026-07-01", to="2026-08-30")
        fake = FakePool()
        with mock.patch.object(research, "settings", return_value=patched):
            res = sweep.run_sweep(spec, pool_run=fake, unlock_reason="looking once on purpose", now_ns=NOW)
        self.assertEqual(len(fake.calls), 1)
        self.assertTrue(all(j.config["unlock_holdout"] == "looking once on purpose" for j in fake.calls[0]))
        rows = store.holdout_unlock_rows(res.family_id)
        self.assertEqual((len(rows), rows[0]["job_id"]), (1, res.job_id))

    def test_the_stored_spec_never_holds_the_unlock_reason(self):
        spec = spec_of()
        spec.base["unlock_holdout"] = "a reason that must not be kept"
        res, _ = self.go(spec=spec)
        text = store.get_research_job(res.job_id)["spec_json"]
        self.assertNotIn("a reason that must not be kept", text)
        self.assertNotIn("unlock_holdout", json.loads(text)["base"])

    def test_pool_that_raises_marks_the_job_failed_and_reraises(self):
        with self.assertRaisesRegex(RuntimeError, "fell over"):
            self.go(fake=FakePool(boom_on=1))
        (job,) = store.list_research_jobs(kind="sweep")
        self.assertEqual((job["status"], job["error"]), ("failed", "RuntimeError: the pool fell over"))

    def test_workers_never_exceed_cpu_minus_one(self):
        cap = max(1, (os.cpu_count() or 1) - 1)
        self.assertLessEqual(pool.default_workers(10 ** 6), cap)
        self.assertLessEqual(pool.default_workers(None), cap)
        seen = []

        def spy(jobs, *, workers=None):
            seen.append(workers)
            return FakePool()(jobs, workers=workers)
        sweep.run_sweep(spec_of(), pool_run=spy, workers=10 ** 6, now_ns=NOW)
        self.assertEqual(seen, [10 ** 6])               # handed to the pool, which bounds it (pool.default_workers)

    def test_a_job_id_is_unique_when_the_stamp_and_hex_would_repeat(self):
        with mock.patch.object(sweep.secrets, "token_hex", side_effect=["aaaaaa", "aaaaaa", "bbbbbb"]):
            a, _ = self.go()
            b, _ = self.go()
        self.assertNotEqual(a.job_id, b.job_id)

    # ---- properties over seeded cases ------------------------------------------------------------------------

    def test_every_trial_maps_to_its_own_result_whatever_the_pool_order(self):
        rng = random.Random(18)

        class Shuffled(FakePool):
            """Runs the jobs in a random order (a pool's completion order) but answers in input order."""

            def __call__(self, jobs, *, workers=None):
                order = list(range(len(jobs)))
                rng.shuffle(order)
                done = {}
                for i in order:
                    done[i] = FakePool.__call__(self, [jobs[i]], workers=workers)[0]
                return [done[i] for i in range(len(jobs))]

        for k in range(12):
            vals = rng.sample(range(1, 60), rng.randint(1, 8))
            fail = {f"sw-t{i:03d}" for i in range(len(vals)) if rng.random() < 0.3}
            res = sweep.run_sweep(spec_of(values=vals), pool_run=Shuffled(fail=fail), now_ns=NOW + k)
            self.assertEqual([r["index"] for r in res.rows], list(range(len(vals))))
            for r in res.rows:
                self.assertEqual(r["params"], {AXIS: vals[r["index"]]})
                if f"sw-t{r['index']:03d}" in fail:
                    self.assertEqual((r["status"], r["run_id"]), ("failed", None))
                else:
                    self.assertEqual(store.get_run(r["run_id"])["name"], f"sw-t{r['index']:03d}")
                    self.assertEqual(r["objective_value"], float(vals[r["index"]] * 100))
            ok = [r for r in res.rows if r["status"] == "done"]
            if ok:
                top = max(ok, key=lambda r: (r["objective_value"], -r["index"]))
                self.assertEqual(res.best_run_id, top["run_id"])
            else:
                self.assertIsNone(res.best_run_id)
            self.assertEqual(len(registry.trials(res.family_id)), len(ok))


class CliTests(SandboxedDBTest):
    db_modules = (store,)

    def write(self, name="sw.yaml", values=(5, 10, 15)) -> str:
        import yaml
        path = paths.lab_dir() / name
        path.write_text(yaml.safe_dump({"schema": sweep.SCHEMA, "name": "sw", "base": base_cfg(),
                                        "objective": "net_pnl", "axes": {AXIS: list(values)}}), encoding="utf-8")
        return str(path)

    def cli(self, *argv, stdin_tty=False):  # noqa: ANN002
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin.isatty", return_value=stdin_tty):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_sweep_is_no_longer_a_stub(self):
        self.assertNotIn("sweep", cli._STUBS)

    def test_cli_prints_the_table_and_job_id(self):
        with mock.patch.object(pool, "run_all", FakePool(fail=["sw-t000"])):
            code, out, _ = self.cli("sweep", "--config", self.write())
        self.assertEqual(code, 1)                                    # a trial failed
        (job,) = store.list_research_jobs(kind="sweep")
        self.assertIn(job["job_id"], out)
        self.assertIn("Objective", out)
        self.assertIn("3 trials", out)
        rows = [ln for ln in out.splitlines() if ln.split()[:1] in (["2"], ["1"], ["0"])]
        self.assertEqual([ln.split()[0] for ln in rows], ["2", "1", "0"])      # best first, the failed one last
        self.assertIn("failed", rows[-1])

    def test_clean_sweep_exits_zero(self):
        with mock.patch.object(pool, "run_all", FakePool()):
            code, _, _ = self.cli("sweep", "--config", self.write(), "--workers", "2")
        self.assertEqual(code, 0)

    def test_a_bad_spec_and_a_locked_holdout_exit_two(self):
        code, _, err = self.cli("sweep", "--config", str(paths.lab_dir() / "missing.yaml"))
        self.assertEqual(code, 2)
        self.assertTrue(err)
        path = self.write()
        patched = {**research.settings(), "holdout_from": "2026-01-02"}
        with mock.patch.object(research, "settings", return_value=patched), \
                mock.patch.object(pool, "run_all", FakePool()):
            code, _, err = self.cli("sweep", "--config", path)
        self.assertEqual(code, 2)
        self.assertIn("holdout", err.lower())

    def test_a_large_sweep_asks_and_refuses_without_a_terminal_or_yes(self):
        path = self.write(values=range(1, 52))                         # 51 trials, above the 50 that ask
        fake = FakePool()
        with mock.patch.object(pool, "run_all", fake):
            code, out, err = self.cli("sweep", "--config", path)
            self.assertEqual(code, 2)
            self.assertIn("--yes", err)
            self.assertIn("51 trials", out + err)
            self.assertEqual(fake.calls, [])
            with mock.patch("builtins.input", return_value="n"):
                code, _, _ = self.cli("sweep", "--config", path, stdin_tty=True)
            self.assertEqual((code, fake.calls), (2, []))
            with mock.patch("builtins.input", return_value="y"):
                code, _, _ = self.cli("sweep", "--config", path, stdin_tty=True)
            self.assertEqual((code, len(fake.calls)), (0, 1))
            code, _, _ = self.cli("sweep", "--config", path, "--yes")
            self.assertEqual((code, len(fake.calls)), (0, 2))

    def test_end_of_input_at_the_prompt_is_a_refusal_naming_yes(self):
        path = self.write(values=range(1, 52))
        fake = FakePool()
        with mock.patch.object(pool, "run_all", fake), mock.patch("builtins.input", side_effect=EOFError):
            code, _, err = self.cli("sweep", "--config", path, stdin_tty=True)
        self.assertEqual((code, fake.calls), (2, []))
        self.assertIn("--yes", err)

    def test_workers_below_one_is_refused_at_the_command_line(self):
        for bad in ("0", "-3"):
            with self.subTest(bad=bad):
                fake = FakePool()
                with mock.patch.object(pool, "run_all", fake):
                    code, _, err = self.cli("sweep", "--config", self.write(), "--workers", bad)
                self.assertEqual((code, fake.calls), (2, []))
                self.assertIn("--workers", err)
        self.assertEqual(store.list_research_jobs(kind="sweep"), [])

    def test_fifty_trials_run_without_asking(self):
        with mock.patch.object(pool, "run_all", FakePool()):
            code, _, _ = self.cli("sweep", "--config", self.write(values=range(1, 51)))
        self.assertEqual(code, 0)


class EndToEndTests(BundleTest):
    """Real children on the oracle; the fixture bars end in 2025, before the shipped holdout (2026-08-01)."""

    def setUp(self):
        super().setUp()
        support.reset_sqlite_singleton(store)
        self.addCleanup(support.reset_sqlite_singleton, store)

    def test_sweep_end_to_end_with_the_oracle_on_the_fixture(self):
        from stockify import runconfig

        def oracle_pool(jobs, *, workers=None):
            return pool.run_all([replace(j, impl="oracle") for j in jobs], workers=workers)

        base = runconfig.validate(run_config(name="tiny", strategy=BUY))
        spec = sweep.SweepSpec(name="tiny", base=base, axes={"capital_paise": [10_000_000, 20_000_000],
                                                             "seed": [0, 1]},
                               search={"kind": "grid"}, objective="net_pnl", workers=2, source=None)
        res = sweep.run_sweep(spec, pool_run=oracle_pool)
        self.assertEqual([r["status"] for r in res.rows], ["done"] * 4, [r["error"] for r in res.rows])
        self.assertEqual(len(registry.trials(res.family_id)), 4)
        for r in res.rows:
            run = store.get_run(r["run_id"])
            self.assertEqual((run["role"], run["job_id"], run["family_id"]), ("trial", res.job_id, res.family_id))
        self.assertIsNotNone(res.best_run_id)
        self.assertEqual(store.get_research_job(res.job_id)["status"], "done")
        self.assertTrue((paths.research_path(res.job_id) / "results.csv").is_file())


if __name__ == "__main__":
    unittest.main()
