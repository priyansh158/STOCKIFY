"""DM 7.1 (D-145): jobs the web app starts. POST /api/research/sweeps, /api/research/walkforward,
/api/tests/{id}/sensitivity, /api/data/backfill, /api/data/report and GET /api/jobs.

The run pool is a canned stand-in (nothing runs an engine) and NSE's fetch is patched (nothing touches the network).
Every job runs the same Python function the command line runs; these tests check that, the validation (422), the
limits (409), the lifecycle (running, done, failed, interrupted) and the Origin check."""
from __future__ import annotations

import json
import threading
import time
import unittest
from datetime import date, timedelta
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify import numerics, paths, proc, research
from stockify.data import nse_files, quality
from stockify.research import pool, sensitivity, walkforward, wf_report
from tests.stockify.research.fixtures import make_run
from tests.stockify.research.test_sensitivity import CAP as SENS_CAP, FakePool as SensPool
from tests.stockify.research.test_walkforward import FakePool as WfPool, base_cfg

WEB = support.API_BASE_URL.replace("8001", "3000")
RANGE = "strategy.params.range_minutes"
EXIT = "strategy.params.exit_hhmm"


def _wait_done(client, job_id: str, timeout: float = 20.0) -> dict:  # noqa: ANN001
    end = time.monotonic() + timeout
    while True:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running" or time.monotonic() > end:
            return j
        time.sleep(0.02)


class _Gate:
    """A pool stand-in that holds until released, so a test can look at a job while it runs."""

    def __init__(self, inner):  # noqa: ANN001
        self.inner, self.entered, self.release = inner, threading.Event(), threading.Event()

    def __call__(self, jobs, *, workers=None):  # noqa: ANN001
        self.entered.set()
        self.release.wait(20)
        return self.inner(jobs, workers=workers)


@support.requires_fastapi
class JobsBase(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        from server.services import web_jobs
        self.web_jobs = web_jobs
        web_jobs.set_jobs(web_jobs.WebJobs())
        self.addCleanup(web_jobs.set_jobs, None)
        self.client = TestClient(app, base_url=support.API_BASE_URL)

    def tearDown(self):
        self.web_jobs.get_jobs().join(10)
        super().tearDown()

    def finished_run(self, run_id="base1", cfg=None, name="orb try") -> str:
        cfg = cfg or base_cfg()
        make_run(run_id, cfg.get("family_id"), navs=[SENS_CAP + 5_000], capital_paise=SENS_CAP, name=name,
                 with_folder=True)
        store.update_run(run_id, config_json=numerics.dumps_canonical(cfg))
        return run_id

    def post_sweep(self, run_id="base1", axes=None, **kw):  # noqa: ANN003
        body = {"base_run_id": run_id, "axes": axes if axes is not None else {RANGE: [5, 10, 15]}, **kw}
        return self.client.post("/api/research/sweeps", json=body)


class LimitsTests(JobsBase):
    def test_limits_come_from_research_json(self):
        s = research.settings()
        j = self.client.get("/api/research/limits").json()
        self.assertEqual(j["max_trials"], s["sweep"]["max_trials"])
        self.assertEqual((j["train_days"], j["test_days"], j["step_days"], j["wf_objective"]),
                         (s["walk_forward"]["train_days"], s["walk_forward"]["test_days"],
                          s["walk_forward"]["step_days"], s["walk_forward"]["objective"]))
        self.assertEqual(j["holdout_from"], s["holdout_from"])
        self.assertEqual(j["confirm_above"], 50)


class SweepTests(JobsBase):
    def test_a_sweep_starts_runs_and_finishes_with_progress_and_a_result_page(self):
        self.finished_run()
        gate = _Gate(WfPool())
        with mock.patch.object(pool, "run_all", gate):
            r = self.post_sweep(objective="net_pnl")
            self.assertEqual(r.status_code, 202, r.text)
            job = r.json()
            self.assertTrue(job["job_id"].startswith("sweep-"), job)
            self.assertEqual((job["kind"], job["status"], job["total"], job["target"]), ("sweep", "running", 3, "base1"))
            self.assertTrue(gate.entered.wait(10))
            # in progress: the job is in the store like a command-line one, and the list finds it again (a reload)
            self.assertEqual(store.get_research_job(job["job_id"])["status"], "running")
            self.assertEqual([j["job_id"] for j in self.client.get("/api/jobs").json()], [job["job_id"]])
            self.assertEqual(self.client.get(f"/api/jobs/{job['job_id']}").json()["status"], "running")
            gate.release.set()
            final = _wait_done(self.client, job["job_id"])
        self.assertEqual((final["status"], final["done"], final["total"]), ("done", 3, 3), final)
        self.assertEqual(store.get_research_job(job["job_id"])["status"], "done")
        page = self.client.get(f"/api/research/sweeps/{job['job_id']}")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(page.json()["rows"]), 3)
        # the owner file that lets a restart tell this job was the API's
        self.assertIsNotNone(proc.read_pidfile(paths.research_path(job["job_id"])))

    def test_progress_counts_the_runs_the_job_has_finished(self):
        self.finished_run()
        seen = []

        def counting_pool(jobs, *, workers=None):  # noqa: ANN001
            out = WfPool()(jobs[:2], workers=workers)       # two of three are finished while the third is still going
            seen.append(self.client.get("/api/jobs").json()[0])
            return out + WfPool()(jobs[2:], workers=workers)

        with mock.patch.object(pool, "run_all", counting_pool):
            job = self.post_sweep(objective="net_pnl").json()
            _wait_done(self.client, job["job_id"])
        self.assertEqual((seen[0]["done"], seen[0]["total"]), (2, 3))
        self.assertIn("2 of 3 tries", seen[0]["plain"])

    def test_the_sweep_is_the_one_the_cli_builds(self):
        """The job calls sweep.run_sweep with a spec from sweep.build_spec: same validation, same trial names."""
        self.finished_run()
        fake = WfPool()
        with mock.patch.object(pool, "run_all", fake):
            job = self.post_sweep(axes={RANGE: [5, 10], EXIT: ["14:30", "15:00"]}, name="my-grid", objective="net_pnl").json()
            _wait_done(self.client, job["job_id"])
        names = [j.config["name"] for j in fake.calls[0]]
        self.assertEqual(names, [f"my-grid-t{i:03d}" for i in range(4)])
        self.assertEqual([(j.config["strategy"]["params"]["range_minutes"], j.config["strategy"]["params"]["exit_hhmm"])
                          for j in fake.calls[0]], [(5, "14:30"), (5, "15:00"), (10, "14:30"), (10, "15:00")])
        self.assertEqual(fake.calls[0][0].role, "trial")
        self.assertIsNone(fake.calls[0][0].config.get("unlock_holdout"))

    def test_validation_errors_are_422_in_plain_words(self):
        self.finished_run()
        cases = [
            ({"axes": {"costs.scale": ["1", "2"]}}, "Only the strategy's own settings"),
            ({"axes": {"strategy.params.nope": [1]}}, "Only the strategy's own settings"),
            ({"axes": {RANGE: ["abc"]}}, "Opening range (minutes)"),
            ({"axes": {EXIT: ["25:99"]}}, "Exit time (IST)"),
            ({"axes": {RANGE: [5, 5]}}, "listed twice"),
            ({"axes": {RANGE: [5], "strategy.params.qty": [1]}, "name": "bad name!"}, "letters, digits"),
        ]
        for extra, words in cases:
            with self.subTest(extra=extra):
                body = {"base_run_id": "base1", **extra}
                r = self.client.post("/api/research/sweeps", json=body)
                self.assertEqual(r.status_code, 422, r.text)
                self.assertIn(words, str(r.json()["detail"]))

    def test_an_empty_or_oversized_request_is_refused_by_the_model(self):
        self.finished_run()
        for axes in ({}, {RANGE: []}, {f"strategy.params.p{i}": [1] for i in range(9)}, {RANGE: list(range(101))}):
            with self.subTest(axes=len(axes)):
                self.assertEqual(self.post_sweep(axes=axes).status_code, 422)
        self.assertEqual(self.client.post("/api/research/sweeps", json={"base_run_id": "x" * 129, "axes": {RANGE: [1]}}).status_code, 422)

    def test_the_grid_cap_comes_from_research_json_and_a_big_grid_needs_a_tick(self):
        self.finished_run()
        with mock.patch.object(research, "settings", return_value={**research.settings(), "sweep": {"max_workers": None, "max_trials": 4}}):
            r = self.post_sweep(axes={RANGE: [1, 2, 3, 4, 5]})
            self.assertEqual(r.status_code, 422)
            self.assertIn("the most one sweep may have is 4", r.json()["detail"])
        r = self.post_sweep(axes={RANGE: list(range(1, 52))})              # 51 tries: above the "ask first" count of 50
        self.assertEqual(r.status_code, 422)
        self.assertIn("Tick the box", r.json()["detail"])
        with mock.patch.object(pool, "run_all", WfPool()):
            r = self.post_sweep(axes={RANGE: list(range(1, 52))}, confirm=True, objective="net_pnl")
            self.assertEqual(r.status_code, 202, r.text)
            _wait_done(self.client, r.json()["job_id"])

    def test_the_holdout_is_never_unlocked_from_the_web(self):
        run = self.finished_run("late", base_cfg("2026-09-01", "2026-09-30"))
        with support.holdout_on("2026-08-01"):
            r = self.post_sweep(run)
            r2 = self.client.post("/api/research/sweeps", json={"base_run_id": run, "axes": {RANGE: [5]}, "unlock_holdout": "because"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("held back", r.json()["detail"])
        self.assertIn("2026-08-01", r.json()["detail"])
        self.assertNotIn("unlock_holdout", r.json()["detail"])
        self.assertNotIn("--", r.json()["detail"])
        self.assertEqual(store.list_research_jobs("sweep"), [])
        self.assertEqual(r2.status_code, 422)             # a body that tries to unlock it changes nothing

    def test_an_unknown_or_unfinished_test_is_refused(self):
        self.assertEqual(self.post_sweep("nope").status_code, 404)
        self.finished_run("queued1")
        store.update_run("queued1", status="running")
        r = self.post_sweep("queued1")
        self.assertEqual(r.status_code, 422)
        self.assertIn("not finished", r.json()["detail"])

    def test_a_second_sweep_of_the_same_test_is_409_and_so_is_any_other_while_one_runs(self):
        self.finished_run()
        self.finished_run("base2")
        gate = _Gate(WfPool())
        with mock.patch.object(pool, "run_all", gate):
            first = self.post_sweep(objective="net_pnl")
            self.assertEqual(first.status_code, 202)
            self.assertTrue(gate.entered.wait(10))
            same = self.post_sweep()
            self.assertEqual(same.status_code, 409)
            self.assertIn("already running", same.json()["detail"])
            other = self.post_sweep("base2")
            self.assertEqual(other.status_code, 409)
            self.assertIn("Another sweep", other.json()["detail"])
            sens = self.client.post("/api/tests/base2/sensitivity")
            self.assertEqual(sens.status_code, 409)               # sweeps, walk-forwards and cost checks share one limit
            gate.release.set()
            _wait_done(self.client, first.json()["job_id"])
        with mock.patch.object(pool, "run_all", WfPool()):          # the limit is freed when the job ends
            again = self.post_sweep(objective="net_pnl")
            self.assertEqual(again.status_code, 202)
            _wait_done(self.client, again.json()["job_id"])

    def test_a_pool_that_breaks_marks_the_job_failed_with_a_plain_reason(self):
        self.finished_run()
        with mock.patch.object(pool, "run_all", WfPool(boom_on=1)):
            job = self.post_sweep().json()
            final = _wait_done(self.client, job["job_id"])
        self.assertEqual(final["status"], "failed")
        self.assertNotIn("fell over", json.dumps(final))                # exception text never reaches the page
        self.assertEqual(store.get_research_job(job["job_id"])["status"], "failed")

    def test_the_request_does_not_block_while_the_job_runs(self):
        self.finished_run()
        gate = _Gate(WfPool())
        with mock.patch.object(pool, "run_all", gate):
            started = time.monotonic()
            r = self.post_sweep(objective="net_pnl")
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(r.status_code, 202)
            gate.release.set()
            _wait_done(self.client, r.json()["job_id"])


class InterruptedTests(JobsBase):
    def test_a_job_the_api_started_and_a_restart_cut_short_is_marked_interrupted(self):
        store.create_research_job({"job_id": "sweep-old-1", "kind": "sweep", "name": "old", "status": "running",
                                   "created_ns": 5, "spec_json": json.dumps({"trials": 4})})
        folder = paths.research_path("sweep-old-1")
        folder.mkdir(parents=True)
        (folder / proc.PIDFILE).write_text("999999 1\n", encoding="utf-8")       # no such process
        store.create_research_job({"job_id": "sweep-cli-1", "kind": "sweep", "name": "cli", "status": "running",
                                   "created_ns": 6, "spec_json": "{}"})           # started from a terminal: no owner file
        store.create_research_job({"job_id": "sweep-mine-1", "kind": "sweep", "name": "mine", "status": "running",
                                   "created_ns": 7, "spec_json": "{}"})
        own = paths.research_path("sweep-mine-1")
        own.mkdir(parents=True)
        proc.write_pidfile(own)                                                   # this process: alive
        self.assertEqual(self.web_jobs.recover(), ["sweep-old-1"])
        old = store.get_research_job("sweep-old-1")
        self.assertEqual((old["status"], old["error"]), ("interrupted", self.web_jobs.INTERRUPTED))
        self.assertIsNotNone(old["finished_ns"])
        self.assertEqual(store.get_research_job("sweep-cli-1")["status"], "running")
        self.assertEqual(store.get_research_job("sweep-mine-1")["status"], "running")
        j = self.client.get("/api/jobs/sweep-old-1").json()                        # found in the store after a restart
        self.assertEqual(j["status"], "interrupted")
        self.assertIn("STOCKIFY was closed", j["plain"])
        row = next(x for x in self.client.get("/api/research/jobs").json() if x["job_id"] == "sweep-old-1")
        self.assertIn("STOCKIFY was closed", row["plain"])

    def test_an_unknown_job_is_404(self):
        self.assertEqual(self.client.get("/api/jobs/nope").status_code, 404)
        self.assertEqual(self.client.get("/api/jobs/" + "x" * 90).status_code, 404)


class WalkforwardTests(JobsBase):
    def sweep_job(self) -> str:
        from stockify.research import sweep
        spec = sweep.build_spec("wfsweep", base_cfg(), {RANGE: [5, 10, 15]}, objective="net_pnl")
        return sweep.run_sweep(spec, pool_run=WfPool(), now_ns=1_700_000_000_000_000_000).job_id

    def test_a_walkforward_runs_finishes_and_writes_its_report_with_config_defaults(self):
        job_id = self.sweep_job()
        fake = WfPool()
        with mock.patch.object(pool, "run_all", fake):
            r = self.client.post("/api/research/walkforwards", json={"sweep_job_id": job_id})
            self.assertEqual(r.status_code, 202, r.text)
            job = r.json()
            final = _wait_done(self.client, job["job_id"])
        self.assertTrue(job["job_id"].startswith("wf-"))
        self.assertEqual(final["status"], "done", final)
        self.assertEqual(store.get_research_job(job["job_id"])["status"], "done")         # `finish` ran
        self.assertTrue((paths.research_path(job["job_id"]) / "report.md").is_file())     # and so did the report
        s = research.settings()["walk_forward"]
        row = json.loads(store.get_research_job(job["job_id"])["spec_json"])
        self.assertEqual((row["train"], row["test"], row["step"], row["objective"]),
                         (s["train_days"], s["test_days"], s["step_days"], s["objective"]))
        self.assertEqual(self.client.get(f"/api/research/walkforward/{job['job_id']}").status_code, 200)

    def test_settings_are_checked_before_a_job_exists(self):
        job_id = self.sweep_job()
        cases = [
            ({"step_days": 10, "test_days": 30}, 422, "overlap"),
            ({"train_days": 400}, 422, "No window fits"),
            ({"first_day": "2026-1-1"}, 422, "date like"),
            ({"last_day": "2026-02-30"}, 422, "not a real date"),
            ({"train_days": 0}, 422, None),
            ({"objective": "profit"}, 422, None),
        ]
        for extra, code, words in cases:
            with self.subTest(extra=extra):
                r = self.client.post("/api/research/walkforwards", json={"sweep_job_id": job_id, **extra})
                self.assertEqual(r.status_code, code, r.text)
                if words:
                    self.assertIn(words, str(r.json()["detail"]))
        self.assertEqual([j["kind"] for j in store.list_research_jobs()], ["sweep"])         # nothing was created

    def test_the_holdout_stays_locked_for_a_span_that_reaches_it(self):
        job_id = self.sweep_job()
        with support.holdout_on("2026-08-01"):
            r = self.client.post("/api/research/walkforwards", json={"sweep_job_id": job_id, "last_day": "2026-12-31"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("held back", r.json()["detail"])

    def test_only_a_finished_sweep_can_be_walked_forward(self):
        self.assertEqual(self.client.post("/api/research/walkforwards", json={"sweep_job_id": "nope"}).status_code, 404)
        store.create_research_job({"job_id": "sweep-run-1", "kind": "sweep", "name": "r", "status": "running",
                                   "created_ns": 1, "spec_json": "{}"})
        r = self.client.post("/api/research/walkforwards", json={"sweep_job_id": "sweep-run-1"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("has not finished", r.json()["detail"])

    def test_a_second_walkforward_of_the_same_sweep_is_409(self):
        job_id = self.sweep_job()
        gate = _Gate(WfPool())
        with mock.patch.object(pool, "run_all", gate):
            first = self.client.post("/api/research/walkforwards", json={"sweep_job_id": job_id})
            self.assertEqual(first.status_code, 202)
            self.assertTrue(gate.entered.wait(10))
            again = self.client.post("/api/research/walkforwards", json={"sweep_job_id": job_id})
            self.assertEqual(again.status_code, 409)
            self.assertIn("already running", again.json()["detail"])
            gate.release.set()
            _wait_done(self.client, first.json()["job_id"])

    def test_the_job_calls_the_command_line_functions_in_order(self):
        job_id = self.sweep_job()
        calls = []
        real_run, real_finish, real_render = walkforward.run_windows, walkforward.finish, wf_report.render
        with mock.patch.object(pool, "run_all", WfPool()), \
                mock.patch.object(walkforward, "run_windows", lambda *a, **k: calls.append("run_windows") or real_run(*a, **k)), \
                mock.patch.object(walkforward, "finish", lambda j: calls.append("finish") or real_finish(j)), \
                mock.patch.object(wf_report, "render", lambda j, **k: calls.append("render") or real_render(j, **k)):
            r = self.client.post("/api/research/walkforwards", json={"sweep_job_id": job_id})
            _wait_done(self.client, r.json()["job_id"])
        self.assertEqual(calls, ["run_windows", "finish", "render"])


class SensitivityTests(JobsBase):
    def setUp(self):
        super().setUp()
        from stockify import bundle
        for name, value in (("resolve_packs", [{"sha256": "a" * 64}]), ("resolve_bundle", {})):
            p = mock.patch.object(bundle, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def test_the_cost_check_runs_the_suite_and_writes_sensitivity_json(self):
        run = self.finished_run()
        fake = SensPool(lambda c: 5_000)
        with mock.patch.object(pool, "run_all", fake):
            r = self.client.post(f"/api/tests/{run}/sensitivity")
            self.assertEqual(r.status_code, 202, r.text)
            job = r.json()
            final = _wait_done(self.client, job["job_id"])
        self.assertTrue(job["job_id"].startswith("sens-"))
        self.assertEqual((final["kind"], final["status"], final["target"]), ("sensitivity", "done", run), final)
        self.assertTrue((paths.run_path(run) / "sensitivity.json").is_file())
        got = self.client.get(f"/api/tests/{run}/sensitivity")                    # the page reads the result in place
        self.assertEqual(got.status_code, 200)
        self.assertEqual({j.role for j in fake.jobs}, {"sensitivity"})

    def test_the_404_for_a_check_not_run_yet_names_no_command(self):
        run = self.finished_run()
        r = self.client.get(f"/api/tests/{run}/sensitivity")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("python", r.text.lower())
        self.assertNotIn(".venv", r.text)

    def test_unknown_unfinished_and_locked_tests_are_refused(self):
        self.assertEqual(self.client.post("/api/tests/nope/sensitivity").status_code, 404)
        late = self.finished_run("late", base_cfg("2026-09-01", "2026-09-30"))
        with support.holdout_on("2026-08-01"):
            r = self.client.post(f"/api/tests/{late}/sensitivity")
        self.assertEqual(r.status_code, 422)
        self.assertIn("held back", r.json()["detail"])
        run = self.finished_run("busy")
        store.update_run(run, status="running")
        self.assertEqual(self.client.post(f"/api/tests/{run}/sensitivity").status_code, 422)

    def test_a_second_cost_check_of_the_same_test_is_409(self):
        run = self.finished_run()
        gate = _Gate(SensPool(lambda c: 5_000))
        with mock.patch.object(pool, "run_all", gate):
            first = self.client.post(f"/api/tests/{run}/sensitivity")
            self.assertEqual(first.status_code, 202)
            self.assertTrue(gate.entered.wait(10))
            again = self.client.post(f"/api/tests/{run}/sensitivity")
            self.assertEqual(again.status_code, 409)
            self.assertIn("already running", again.json()["detail"])
            gate.release.set()
            _wait_done(self.client, first.json()["job_id"])

    def test_it_is_the_same_function_the_command_calls(self):
        run = self.finished_run()
        with mock.patch.object(sensitivity, "run_suite", wraps=sensitivity.run_suite) as spy, \
                mock.patch.object(pool, "run_all", SensPool(lambda c: 5_000)):
            r = self.client.post(f"/api/tests/{run}/sensitivity")
            _wait_done(self.client, r.json()["job_id"])
        self.assertEqual(spy.call_args.args, (run,))
        self.assertNotIn("unlock_reason", spy.call_args.kwargs)           # never unlocked from the web


class DataJobsTests(JobsBase):
    def fake_fetch(self, record):  # noqa: ANN001
        def fetch_one(dataset, day, http, **kw):  # noqa: ANN001, ANN003
            record.append((dataset, day, kw))
            return nse_files.FetchResult(dataset, day.isoformat(), "ok", detail="fake")
        return fetch_one

    def test_backfill_calls_the_commands_function_for_each_trading_day_and_dataset(self):
        record = []
        with mock.patch.object(nse_files, "fetch_one", self.fake_fetch(record)), \
                mock.patch.object(nse_files, "settle", return_value=[]), \
                mock.patch.object(nse_files, "Http", lambda cfg=None: object()):
            r = self.client.post("/api/data/backfill", json={"first_day": "2026-11-02", "last_day": "2026-11-06"})
            self.assertEqual(r.status_code, 202, r.text)
            job = r.json()
            self.assertEqual((job["kind"], job["target"]), ("backfill", "2026-11-02..2026-11-06"))
            final = _wait_done(self.client, job["job_id"])
        self.assertEqual(final["status"], "done", final)
        days = sorted({d for _, d, _ in record})
        self.assertEqual(len(days), len(record) // 3)
        self.assertEqual({ds for ds, _, _ in record}, {"fo", "cm", "idx"})          # the command's own default datasets
        self.assertTrue(all(kw.get("allow_unverified") is False for _, _, kw in record))
        self.assertEqual((final["done"], final["total"]), (len(record), len(record)))
        self.assertIn("downloaded", final["plain"])

    def test_backfill_progress_is_visible_while_it_runs_and_failures_say_so(self):
        gate = threading.Event()
        record = []

        def slow(dataset, day, http, **kw):  # noqa: ANN001, ANN003
            record.append(dataset)
            if len(record) == 2:
                gate.wait(10)
            return nse_files.FetchResult(dataset, day.isoformat(), "error" if len(record) == 4 else "ok", detail="x")

        with mock.patch.object(nse_files, "fetch_one", slow), mock.patch.object(nse_files, "settle", return_value=[]), \
                mock.patch.object(nse_files, "Http", lambda cfg=None: object()):
            r = self.client.post("/api/data/backfill", json={"first_day": "2026-11-02", "last_day": "2026-11-03"})
            end = time.monotonic() + 10
            while self.client.get(f"/api/jobs/{r.json()['job_id']}").json()["done"] < 1 and time.monotonic() < end:
                time.sleep(0.01)
            mid = self.client.get(f"/api/jobs/{r.json()['job_id']}").json()
            gate.set()
            final = _wait_done(self.client, r.json()["job_id"])
        self.assertEqual(mid["status"], "running")
        self.assertIn("of 6 files finished", mid["plain"])
        self.assertEqual(final["status"], "failed")
        self.assertIn("1 failed", final["error"])
        self.assertIn("already fetched are skipped", final["error"])

    def test_backfill_validation_and_limits(self):
        cap = research.settings()["coverage"]["max_days"]
        today = date.today() + timedelta(days=40)
        cases = [
            ({"first_day": "2026-11-05", "last_day": "2026-11-02"}, 422, "after the last day"),
            ({"first_day": "2026-11-0x", "last_day": "2026-11-02"}, 422, "date like"),
            ({"first_day": "2026-11-02", "last_day": today.isoformat()}, 422, "have not happened"),
            ({"first_day": "2024-01-01", "last_day": "2026-11-02"}, 422, f"At most {cap} days"),
        ]
        for body, code, words in cases:
            with self.subTest(body=body):
                r = self.client.post("/api/data/backfill", json=body)
                self.assertEqual(r.status_code, code, r.text)
                self.assertIn(words, str(r.json()["detail"]))

    def test_a_second_download_of_the_same_days_is_409(self):
        gate = threading.Event()

        def slow(dataset, day, http, **kw):  # noqa: ANN001, ANN003
            gate.wait(10)
            return nse_files.FetchResult(dataset, day.isoformat(), "ok", detail="x")

        body = {"first_day": "2026-11-02", "last_day": "2026-11-03"}
        with mock.patch.object(nse_files, "fetch_one", slow), mock.patch.object(nse_files, "settle", return_value=[]), \
                mock.patch.object(nse_files, "Http", lambda cfg=None: object()):
            first = self.client.post("/api/data/backfill", json=body)
            self.assertEqual(first.status_code, 202)
            same = self.client.post("/api/data/backfill", json=body)
            self.assertEqual(same.status_code, 409)
            self.assertIn("already running", same.json()["detail"])
            other = self.client.post("/api/data/backfill", json={"first_day": "2026-11-04", "last_day": "2026-11-05"})
            self.assertEqual(other.status_code, 409)
            gate.set()
            _wait_done(self.client, first.json()["job_id"])

    def test_report_calls_the_commands_function_and_says_so(self):
        with mock.patch.object(quality, "report", return_value={"summary_path": "x"}) as spy:
            r = self.client.post("/api/data/report", json={"day": "2026-11-05"})
            self.assertEqual(r.status_code, 202, r.text)
            final = _wait_done(self.client, r.json()["job_id"])
        spy.assert_called_once_with(date(2026, 11, 5))
        self.assertEqual((final["status"], final["target"]), ("done", "2026-11-05"))
        self.assertIn("2026-11-05", final["plain"])

    def test_a_report_for_a_day_with_no_recording_fails_in_plain_words(self):
        r = self.client.post("/api/data/report", json={"day": "2026-11-05"})
        final = _wait_done(self.client, r.json()["job_id"])
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["error"], "no recording for 2026-11-05")
        for bad in ("2026-13-01", "yesterday", "20261105"):
            self.assertEqual(self.client.post("/api/data/report", json={"day": bad}).status_code, 422)

    def test_the_quality_404_names_no_command(self):
        with mock.patch.object(quality, "stored_readonly", return_value=({}, {})):
            r = self.client.get("/api/data/quality?date=2026-11-05")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("python", r.text.lower())
        self.assertNotIn(".venv", r.text)


class ProtectionTests(JobsBase):
    """The same protection as every other POST: the web origin only, loopback hosts only."""

    ROUTES = [("/api/research/sweeps", {"base_run_id": "x", "axes": {RANGE: [1]}}),
              ("/api/research/walkforwards", {"sweep_job_id": "x"}),
              ("/api/tests/x/sensitivity", None),
              ("/api/data/backfill", {"first_day": "2026-11-02", "last_day": "2026-11-03"}),
              ("/api/data/report", {"day": "2026-11-05"})]

    def test_a_foreign_origin_never_starts_a_job(self):
        for path, body in self.ROUTES:
            with self.subTest(path=path):
                r = self.client.post(path, json=body, headers={"Origin": "http://evil.example"})
                self.assertEqual(r.status_code, 403)
        self.assertEqual(self.web_jobs.get_jobs().all(), [])

    def test_the_web_origin_is_let_through_to_the_route(self):
        r = self.client.post("/api/research/sweeps", json={"base_run_id": "x", "axes": {RANGE: [1]}},
                             headers={"Origin": WEB})
        self.assertEqual(r.status_code, 404)           # past the middleware, then "no such test"

    def test_no_route_takes_a_path_a_command_or_a_config(self):
        from server.schemas import (BackfillRequestDTO, QualityReportRequestDTO, SweepRequestDTO,
                                    WalkforwardRequestDTO)
        fields = {n for m in (SweepRequestDTO, WalkforwardRequestDTO, BackfillRequestDTO, QualityReportRequestDTO)
                  for n in m.model_fields}
        self.assertEqual(fields, {"base_run_id", "name", "axes", "objective", "confirm", "sweep_job_id", "train_days",
                                  "test_days", "step_days", "first_day", "last_day", "day"})


class ManagerTests(unittest.TestCase):
    def test_a_job_that_raises_is_failed_not_a_dead_thread(self):
        from server.services import web_jobs
        m = web_jobs.WebJobs()

        def boom(job):  # noqa: ANN001
            raise RuntimeError("secret path C:\\x")

        j = m.start("report", "2026-11-05", boom)
        m.join(5)
        self.assertEqual(j.status, "failed")
        self.assertNotIn("secret", j.error)
        self.assertNotIn("C:", j.error)

    def test_lifecycle_running_then_done(self):
        from server.services import web_jobs
        m = web_jobs.WebJobs()
        gate = threading.Event()
        j = m.start("report", "d", lambda job: gate.wait(5))
        self.assertEqual(j.status, "running")
        self.assertEqual(m.active("report"), [j])
        gate.set()
        m.join(5)
        self.assertEqual(j.status, "done")
        self.assertIsNotNone(j.finished_ns)
        self.assertEqual(m.active(), [])

    def test_words(self):
        from server.services import web_jobs as w
        self.assertEqual(w.progress_words("sweep", "running", 7, 15, None, None), "Running: 7 of 15 tries finished…")
        self.assertEqual(w.progress_words("sweep", "failed", 1, 15, None, "the run failed"), "Failed: the run failed")
        self.assertIn("STOCKIFY was closed", w.progress_words("sweep", "interrupted", 1, 15, None, None))
        self.assertEqual(w.backfill_sentence({"ok": 3, "skipped": 1}), "Finished: 3 downloaded, 1 already there.")
        self.assertEqual(w.backfill_sentence({}), "Finished: there were no trading days to fetch.")


if __name__ == "__main__":
    unittest.main()
