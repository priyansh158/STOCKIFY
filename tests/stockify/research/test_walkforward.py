"""M6.5 (WF 6.4): walk-forward windows, parallel training grids, best-by-objective selection, out-of-sample runs
and the persisted state. The pool is a stub that writes finished runs; nothing runs an engine."""
from __future__ import annotations

import copy
import itertools
import json
import random
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify import research, runconfig
from stockify.research import holdout, registry, sweep, walkforward as wf
from stockify.research.pool import PoolOutcome
from tests.stockify.research.fixtures import make_run

ROOT = Path(__file__).resolve().parents[3]
BASE = json.loads((ROOT / "examples" / "runs" / "orb_reliance_minute.json").read_text(encoding="utf-8"))
CAP = 10_000_000
D = date.fromisoformat
AXIS = "strategy.params.range_minutes"


def base_cfg(frm="2026-01-01", to="2026-06-30") -> dict:
    cfg = copy.deepcopy(BASE)
    cfg["data"]["packs"][0]["spec"].update({"from": frm, "to": to})
    return runconfig.validate(cfg)


def spec_of(values=(5, 10, 15), **kw) -> sweep.SweepSpec:
    return sweep.SweepSpec(name="wf", base=base_cfg(**kw), axes={AXIS: list(values)}, search={"kind": "grid"},
                           objective="net_pnl", workers=None, source=None)


class WindowTests(unittest.TestCase):
    def test_windows_for_a_known_span(self):
        ws = wf.windows(D("2026-01-01"), D("2026-06-30"), 120, 30, 30)
        self.assertEqual(len(ws), 2)       # a third would start 2026-03-02 and test until 2026-07-29
        self.assertEqual((ws[0].index, ws[0].train_from, ws[0].train_to, ws[0].test_from, ws[0].test_to),
                         (0, D("2026-01-01"), D("2026-04-30"), D("2026-05-01"), D("2026-05-30")))
        self.assertEqual((ws[-1].index, ws[-1].train_from, ws[-1].train_to, ws[-1].test_from, ws[-1].test_to),
                         (1, D("2026-01-31"), D("2026-05-30"), D("2026-05-31"), D("2026-06-29")))

    def test_step_smaller_than_test_refused(self):
        with self.assertRaisesRegex(ValueError, "out-of-sample pieces would overlap"):
            wf.windows(D("2026-01-01"), D("2026-12-31"), 60, 30, 29)

    def test_partial_last_window_is_dropped(self):
        ws = wf.windows(D("2026-01-01"), D("2026-05-29"), 120, 30, 30)    # the first test would end 05-30
        self.assertEqual(ws, [])
        ws = wf.windows(D("2026-01-01"), D("2026-05-30"), 120, 30, 30)    # exactly fits: inclusive end
        self.assertEqual(len(ws), 1)

    def test_parse_span(self):
        self.assertEqual(wf.parse_span("120d"), 120)
        for bad in ("4m", "120", "-3d", "0d", "d", "1.5d", " 5d", "5D", "5d\n", "٣d", ""):
            with self.assertRaisesRegex(ValueError, "calendar days"):
                wf.parse_span(bad)
        with self.assertRaisesRegex(ValueError, "calendar days"):
            wf.parse_span(120)  # type: ignore[arg-type]

    def test_window_arithmetic_properties_over_seeded_cases(self):
        rng = random.Random(20)
        for _ in range(400):
            train, test = rng.randint(1, 200), rng.randint(1, 60)
            step = rng.randint(test, test + 40)
            start = D("2025-01-01") + timedelta(days=rng.randint(0, 800))
            end = start + timedelta(days=rng.randint(0, 700))
            ws = wf.windows(start, end, train, test, step)
            span = (end - start).days + 1
            expected = max(0, (span - train - test) // step + 1)
            self.assertEqual(len(ws), expected)
            for k, w in enumerate(ws):
                self.assertEqual(w.index, k)
                self.assertEqual(w.train_from, start + timedelta(days=k * step))
                self.assertEqual((w.train_to - w.train_from).days + 1, train)      # inclusive ends
                self.assertEqual(w.test_from, w.train_to + timedelta(days=1))      # no gap, no overlap with train
                self.assertEqual((w.test_to - w.test_from).days + 1, test)
                self.assertLessEqual(w.test_to, end)
                if k:
                    self.assertGreater(w.test_from, ws[k - 1].test_to)             # out-of-sample pieces disjoint
            nxt = start + timedelta(days=len(ws) * step)                            # the dropped next one overshoots
            self.assertGreater(nxt + timedelta(days=train + test - 1), end)

    def test_step_below_test_is_refused_over_seeded_cases(self):
        rng = random.Random(21)
        for _ in range(200):
            test = rng.randint(2, 60)
            with self.assertRaises(ValueError):
                wf.windows(D("2026-01-01"), D("2027-12-31"), rng.randint(1, 100), test, rng.randint(1, test - 1))

    def test_bad_sizes_refused(self):
        for args in ((0, 30, 30), (120, 0, 30), (120, 30, 0), (-1, 30, 30)):
            with self.assertRaises(ValueError):
                wf.windows(D("2026-01-01"), D("2026-12-31"), *args)


class WindowConfigTests(unittest.TestCase):
    def test_window_config_replaces_every_pack_range(self):
        cfg = base_cfg()
        cfg["data"]["packs"].append({"spec": {"dataset": "bars", "interval": "day", "instruments": ["NSE:RELIANCE"],
                                              "from": "2025-01-01", "to": "2026-02-02"}})
        before = copy.deepcopy(cfg)
        out = wf.window_config(cfg, D("2026-03-01"), D("2026-03-31"))
        for p in out["data"]["packs"]:
            self.assertEqual((p["spec"]["from"], p["spec"]["to"]), ("2026-03-01", "2026-03-31"))
        self.assertEqual(cfg, before)                                  # the input is untouched
        self.assertEqual(out["data"]["packs"][1]["spec"]["interval"], "day")

    def test_hash_only_packs_refused(self):
        cfg = base_cfg()
        cfg["data"]["packs"] = [{"sha256": "a" * 64}]
        with self.assertRaisesRegex(wf.WalkforwardError, "walk-forward needs pack specs"):
            wf.window_config(cfg, D("2026-03-01"), D("2026-03-31"))


class SelectTests(unittest.TestCase):
    def test_select_best_ties_by_grid_index_and_skips_none(self):
        rows = [{"index": 3, "status": "done", "objective_value": 2.0},
                {"index": 1, "status": "done", "objective_value": 2.0},
                {"index": 0, "status": "done", "objective_value": None},
                {"index": 2, "status": "failed", "objective_value": 9.0},
                {"index": 4, "status": "done", "objective_value": float("nan")},
                {"index": 5, "status": "done", "objective_value": 1.0}]
        self.assertEqual(wf.select_best(rows, "sharpe")["index"], 1)
        self.assertIsNone(wf.select_best([rows[2], rows[3]], "sharpe"))
        self.assertIsNone(wf.select_best([], "sharpe"))

    def test_select_best_takes_the_value_from_metrics_when_a_row_has_none_listed(self):
        row = {"index": 0, "status": "done", "metrics_json": json.dumps({"sharpe": 1.5})}
        self.assertEqual(wf.select_best([row], "sharpe")["index"], 0)

    def test_select_best_is_order_independent(self):
        rng = random.Random(7)
        for _ in range(200):
            rows = [{"index": i, "status": rng.choice(["done", "done", "failed"]),
                     "objective_value": rng.choice([None, 1.0, 2.0, 3.0])} for i in range(rng.randint(0, 8))]
            shuffled = rows[:]
            rng.shuffle(shuffled)
            self.assertEqual(wf.select_best(rows, "sharpe"), wf.select_best(shuffled, "sharpe"))


_IDS = itertools.count(1)


class FakePool:
    """Stands in for pool.run_all: a finished run per job whose net P&L is `net_of(config)`."""

    def __init__(self, net_of=lambda cfg: cfg["strategy"]["params"]["range_minutes"] * 100, fail=(), boom_on=None):
        self.net_of, self.fail, self.boom_on = net_of, tuple(fail), boom_on
        self.calls: list[list] = []

    def __call__(self, jobs, *, workers=None):
        self.calls.append(list(jobs))
        if self.boom_on == len(self.calls):
            raise RuntimeError("the pool fell over")
        out = []
        for j in jobs:
            if j.config["name"] in self.fail:
                out.append(PoolOutcome(None, "failed", "the run broke on purpose"))
                continue
            rid = f"r{next(_IDS)}"
            make_run(rid, j.config.get("family_id"), navs=[CAP + self.net_of(j.config)], capital_paise=CAP,
                     role=j.role, job_id=j.job_id, name=j.config["name"])
            if j.config.get("unlock_holdout"):          # what runs.launch does for a run over the holdout
                store.log_holdout_unlock(rid, j.config.get("family_id"), j.config["unlock_holdout"], job_id=j.job_id)
            out.append(PoolOutcome(rid, "done", None))
        return out


class RunTests(SandboxedDBTest):
    db_modules = (store,)
    KW = dict(train=120, test=30, step=30, objective="net_pnl", now_ns=1_700_000_000_000_000_000)

    def test_one_pool_call_for_all_training_runs_then_one_for_tests(self):
        fake = FakePool()
        res = wf.run_windows(spec_of(), pool_run=fake, **self.KW)
        self.assertEqual(len(fake.calls), 2)
        train, tests = fake.calls
        self.assertEqual(len(train), 2 * 3)                        # two windows x three grid points
        self.assertEqual({j.role for j in train}, {"trial"})
        self.assertEqual({j.job_id for j in train + tests}, {res.job_id})
        self.assertEqual([j.config["name"] for j in train],
                         [f"wf-w{w:02d}-t{t:03d}" for w in (0, 1) for t in (0, 1, 2)])
        self.assertEqual({j.role for j in tests}, {"oos"})
        self.assertEqual([j.config["name"] for j in tests], ["wf-w00-oos", "wf-w01-oos"])
        packs = [j.config["data"]["packs"][0]["spec"] for j in train[:1] + tests[:1]]
        self.assertEqual((packs[0]["from"], packs[0]["to"]), ("2026-01-01", "2026-04-30"))
        self.assertEqual((packs[1]["from"], packs[1]["to"]), ("2026-05-01", "2026-05-30"))
        # the winner of each window is the largest range_minutes (net pnl grows with it): 15
        self.assertEqual([j.config["strategy"]["params"]["range_minutes"] for j in tests], [15, 15])

    def test_walkforward_is_its_own_family_and_n_is_windows_times_grid(self):
        res = wf.run_windows(spec_of(), pool_run=FakePool(), **self.KW)
        fam = f"walkforward/{res.job_id}"
        self.assertEqual(res.family_id, fam)
        self.assertEqual(store.get_family(fam)["objective"], "net_pnl")
        self.assertEqual(store.get_research_job(res.job_id)["family_id"], fam)
        self.assertEqual(len(registry.trials(fam)), 6)

    def test_test_runs_have_role_oos_and_do_not_change_n(self):
        res = wf.run_windows(spec_of(), pool_run=FakePool(), **self.KW)
        oos = store.family_runs(res.family_id, roles=("oos",))
        self.assertEqual(len(oos), 2)
        self.assertEqual(len(registry.trials(res.family_id)), 6)
        self.assertEqual({r["job_id"] for r in oos}, {res.job_id})

    def test_window_without_an_eligible_candidate_is_listed(self):
        fake = FakePool(fail=[f"wf-w01-t{t:03d}" for t in range(3)])
        res = wf.run_windows(spec_of(), pool_run=fake, **self.KW)
        self.assertEqual(len(res.windows), 2)
        w1 = res.windows[1]
        self.assertIsNone(w1.chosen)
        self.assertIsNone(w1.test_run_id)
        self.assertEqual(w1.test_status, "skipped")
        self.assertEqual(len(fake.calls[1]), 1)                    # only window 0 was tested
        self.assertEqual(res.windows[0].test_status, "done")
        job = store.get_research_job(res.job_id)
        self.assertEqual(job["status"], "running")        # Task 21 finish marks it done
        self.assertIsNone(json.loads(job["state_json"])["windows"][1]["chosen_params"])

    def test_a_failed_test_run_is_listed_with_its_error(self):
        res = wf.run_windows(spec_of(), pool_run=FakePool(fail=["wf-w00-oos"]), **self.KW)
        self.assertEqual((res.windows[0].test_status, res.windows[0].error), ("failed", "the run broke on purpose"))
        self.assertEqual(res.windows[1].test_status, "done")

    def test_state_json_holds_windows_chosen_params_and_run_ids_after_each_phase(self):
        seen = []

        class Watching(FakePool):
            def __call__(self, jobs, *, workers=None):
                job = store.get_research_job(jobs[0].job_id)
                seen.append((job["status"], job["state_json"]))
                return super().__call__(jobs, workers=workers)
        res = wf.run_windows(spec_of(), pool_run=Watching(), **self.KW)
        self.assertEqual(seen[0], ("running", None))
        phase1 = json.loads(seen[1][1])["windows"]                  # what the database held before phase 2
        self.assertEqual(len(phase1), 2)
        self.assertEqual(len(phase1[0]["train_run_ids"]), 3)
        self.assertEqual(phase1[0]["chosen_params"], {AXIS: 15})
        self.assertIsNone(phase1[0]["test_run_id"])
        final = json.loads(store.get_research_job(res.job_id)["state_json"])["windows"]
        self.assertEqual(set(final[0]), {"index", "train_from", "train_to", "test_from", "test_to",
                                         "train_run_ids", "chosen_params", "chosen_run_id", "test_run_id",
                                         "test_status", "error"})
        self.assertEqual((final[0]["train_from"], final[0]["test_to"]), ("2026-01-01", "2026-05-30"))
        self.assertEqual(final[0]["chosen_run_id"], res.windows[0].chosen["run_id"])
        self.assertEqual(final[0]["test_run_id"], res.windows[0].test_run_id)
        self.assertEqual(final[0]["test_status"], "done")
        spec = json.loads(store.get_research_job(res.job_id)["spec_json"])
        self.assertEqual((spec["train"], spec["test"], spec["step"], spec["objective"]), (120, 30, 30, "net_pnl"))
        self.assertIs(spec["unlock_given"], False)

    def test_span_defaults_to_the_base_config_and_can_be_narrowed(self):
        res = wf.run_windows(spec_of(), pool_run=FakePool(), **self.KW)
        self.assertEqual(res.span, ("2026-01-01", "2026-06-30"))
        res = wf.run_windows(spec_of(), pool_run=FakePool(), start=D("2026-01-01"), end=D("2026-05-30"),
                             **{**self.KW, "now_ns": 1_700_000_000_000_000_001})
        self.assertEqual((res.span, len(res.windows)), (("2026-01-01", "2026-05-30"), 1))

    def test_walkforward_test_window_crossing_holdout_is_locked(self):
        patched = {**research.settings(), "holdout_from": "2026-08-01"}
        spec = spec_of(frm="2026-04-01", to="2026-09-30")
        kw = dict(train=60, test=30, step=30, objective="net_pnl", end=D("2026-08-29"),
                  now_ns=1_700_000_000_000_000_000)
        with mock.patch.object(research, "settings", return_value=patched):
            fake = FakePool()
            with self.assertRaises(holdout.HoldoutLocked) as cm:
                wf.run_windows(spec, pool_run=fake, **kw)
            self.assertEqual(fake.calls, [])
            (job,) = store.list_research_jobs(kind="walkforward")
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["error"], f"HoldoutLocked: {cm.exception}")
            self.assertEqual(wf.failure_reason(job), "the holdout is locked")
            self.assertEqual(store.holdout_unlock_rows(), [])
            res = wf.run_windows(spec, pool_run=fake, unlock_reason="looking once on purpose",
                                 **{**kw, "now_ns": 1_700_000_000_000_000_005})
        self.assertTrue(all(j.config["unlock_holdout"] == "looking once on purpose" for c in fake.calls for j in c))
        rows = store.holdout_unlock_rows(res.family_id)
        self.assertEqual(len(rows), 1)                              # one unlock row for the job, not one per run
        self.assertEqual(rows[0]["job_id"], res.job_id)

    def test_a_window_clear_of_the_holdout_needs_no_reason(self):
        patched = {**research.settings(), "holdout_from": "2026-08-01"}
        with mock.patch.object(research, "settings", return_value=patched):
            res = wf.run_windows(spec_of(), pool_run=FakePool(), **self.KW)
        self.assertEqual(store.holdout_unlock_rows(), [])
        self.assertEqual(store.get_research_job(res.job_id)["status"], "running")

    def test_job_row_is_updated_when_the_pool_raises(self):
        for which in (1, 2):
            with self.subTest(phase=which):
                with self.assertRaisesRegex(RuntimeError, "fell over"):
                    wf.run_windows(spec_of(), pool_run=FakePool(boom_on=which),
                                   **{**self.KW, "now_ns": 1_700_000_000_000_000_000 + which})
        jobs = store.list_research_jobs(kind="walkforward")
        self.assertEqual({j["status"] for j in jobs}, {"failed"})
        self.assertTrue(all("fell over" in j["error"] and j["finished_ns"] for j in jobs))
        # the stored plain reason is from the fixed table; the raw text is in the error column and the log only
        self.assertEqual({wf.failure_reason(j) for j in jobs}, {"an internal error; see the log"})

    def test_the_plain_failure_table_lives_only_in_plain_py(self):
        from stockify.research import plain
        self.assertFalse(hasattr(wf, "_PLAIN_FAILURES"))
        for raw, line in (("SensitivityRunError: /x/y bad", "the run pool returned the wrong number of results"),
                          ("SweepSpecError: C:/p", "the run pool returned the wrong number of results"),
                          ("RunConfigError: v=7", "the settings were not accepted"),
                          ("HoldoutLocked: C:/x", "the holdout is locked")):
            self.assertEqual(wf.plain_error({"error": raw}), line)
        self.assertIn(wf.plain_failure(wf.WalkforwardRunError("x")), {t for _c, t in plain.PLAIN_FAILURES})
        self.assertEqual(wf.plain_error({"error": "ValueError: C:/secret/path"}), "Failed")
        self.assertEqual(wf.plain_error({"error": ""}), None)

    def test_plain_failure_is_a_fixed_table_keyed_by_the_error_class(self):
        self.assertEqual(wf.plain_failure(wf.WalkforwardRunError("the pool returned 1 outcomes for 2 runs")),
                         "the run pool returned the wrong number of results")
        self.assertEqual(wf.plain_failure(holdout.HoldoutLocked("C:/x/y locked")), "the holdout is locked")
        for e in (ValueError("bad window"), KeyError("x"), OSError("disk full at C:/z")):
            self.assertEqual(wf.plain_failure(e), "an internal error; see the log")
        self.assertIsNone(wf.failure_reason({"state_json": "not json"}))
        self.assertIsNone(wf.failure_reason({"state_json": None}))

    def test_zero_windows_is_a_clear_error(self):
        with self.assertRaisesRegex(wf.WalkforwardError, "no window fits"):
            wf.run_windows(spec_of(to="2026-03-01"), pool_run=FakePool(), **self.KW)
        self.assertEqual(store.list_research_jobs(kind="walkforward"), [])

    def test_the_job_stays_running_until_finish_and_has_no_finish_time(self):
        res = wf.run_windows(spec_of(), pool_run=FakePool(), **self.KW)
        job = store.get_research_job(res.job_id)
        self.assertEqual((job["status"], job["finished_ns"], job["error"]), ("running", None, None))
        self.assertIsNotNone(job["state_json"])

    def test_windows_times_grid_above_max_trials_is_refused_before_a_job(self):
        sw = {**research.settings()["sweep"], "max_trials": 5}
        patched = {**research.settings(), "sweep": sw}
        spec = spec_of()
        with mock.patch.object(research, "settings", return_value=patched),                 mock.patch.object(runconfig, "validate", side_effect=AssertionError("validated")):
            fake = FakePool()
            with self.assertRaises(wf.WalkforwardError) as cm:
                wf.run_windows(spec, pool_run=fake, **self.KW)    # 2 windows x 3 points = 6 > 5
        msg = str(cm.exception)
        for part in ("2 windows", "3 grid points", "6", "5"):
            self.assertIn(part, msg)
        self.assertEqual((fake.calls, store.list_research_jobs(kind="walkforward")), ([], []))

    def test_windows_times_grid_at_max_trials_is_allowed(self):
        sw = {**research.settings()["sweep"], "max_trials": 6}
        with mock.patch.object(research, "settings", return_value={**research.settings(), "sweep": sw}):
            wf.run_windows(spec_of(), pool_run=FakePool(), **self.KW)

    def test_a_failing_status_update_does_not_mask_the_original_error(self):
        with mock.patch.object(store, "update_research_job", side_effect=OSError("db locked")):
            with self.assertRaisesRegex(RuntimeError, "fell over") as cm:
                wf.run_windows(spec_of(), pool_run=FakePool(boom_on=1), **self.KW)
        self.assertIsNone(cm.exception.__cause__)

    def test_an_unknown_objective_is_refused_before_a_job_is_made(self):
        with self.assertRaises(ValueError):
            wf.run_windows(spec_of(), pool_run=FakePool(), **{**self.KW, "objective": "luck"})
        self.assertEqual(store.list_research_jobs(kind="walkforward"), [])

    def test_open_ended_base_needs_an_explicit_end(self):
        spec = spec_of()
        spec.base["data"]["packs"][0]["spec"]["to"] = None
        with self.assertRaisesRegex(wf.WalkforwardError, "end"):
            wf.run_windows(spec, pool_run=FakePool(), **self.KW)


if __name__ == "__main__":
    unittest.main()
