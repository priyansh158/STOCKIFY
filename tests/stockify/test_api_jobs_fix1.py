"""DM 7.4: the review's fixes to the jobs the web starts (D-145): a start that fails is a 422, Stop, the command line's jobs
counting toward the limit, pruned threads, the unverified-source sentence, no holdout unlock from the app, the one-try
rule, no command in the page sentence, the runs.job_id index."""
from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports

import stockify.store as store
from stockify.data import nse_files, quality
from stockify.research import pool
from tests.stockify.research.test_walkforward import FakePool as WfPool
from tests.stockify.test_api_jobs import RANGE, JobsBase, _wait_done

REPO_ROOT = Path(__file__).resolve().parents[2]


class _Cancellable:
    """A pool stand-in that behaves like the real one on Stop: it finishes two tries, then waits for the cancel event
    the job thread installed (pool._cancel) and raises pool.Cancelled."""

    def __init__(self):
        self.inner, self.entered = WfPool(), threading.Event()

    def __call__(self, jobs, *, workers=None):  # noqa: ANN001
        self.inner(jobs[:2], workers=workers)                 # two finished runs are kept
        self.entered.set()
        ev = pool._cancel.get()
        end = time.monotonic() + 20
        while ev is not None and not ev.is_set() and time.monotonic() < end:
            time.sleep(0.01)
        if ev is not None and ev.is_set():
            raise pool.Cancelled("stopped by the owner")
        return []


class FixRound1Tests(JobsBase):
    def test_i1_a_start_that_fails_at_once_is_always_a_422_never_a_202_running(self):
        m = self.web_jobs.WebJobs()

        def boom(job):  # noqa: ANN001
            raise RuntimeError("nope")

        for i in range(300):
            j = m.start("sweep", f"t{i}", boom, wait_for_id=True)
            self.assertEqual(j.status, "failed", i)               # status is written before the id is published
        self.finished_run()
        with mock.patch("stockify.research.sweep.run_sweep", side_effect=RuntimeError("at once")):
            r = self.post_sweep(objective="net_pnl")
        self.assertEqual(r.status_code, 422, r.text)

    def test_i2_stop_ends_a_running_sweep_keeps_finished_tries_and_frees_the_slot(self):
        self.finished_run()
        fake = _Cancellable()
        with mock.patch.object(pool, "run_all", fake):
            job = self.post_sweep(objective="net_pnl").json()
            self.assertTrue(fake.entered.wait(10))
            r = self.client.post(f"/api/research/jobs/{job['job_id']}/cancel")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["status"], "running")
            self.assertEqual(r.json()["plain"], "Stopping…")
            final = _wait_done(self.client, job["job_id"])
        self.assertEqual(final["status"], "cancelled", final)
        self.assertIn("Stopped by you", final["plain"])
        row = store.get_research_job(job["job_id"])
        self.assertEqual((row["status"], row["error"]), ("cancelled", "cancelled by the user"))
        from stockify.research import registry
        # D-85: the two tries that finished count; the one that never ran does not (a cancelled sweep is not "done")
        self.assertEqual(registry.n_tries(row["family_id"]), 2)
        self.assertIn("you stopped it", self.client.get(f"/api/research/sweeps/{job['job_id']}").json()["detail"])
        with mock.patch.object(pool, "run_all", WfPool()):          # the research slot is free again
            again = self.post_sweep(objective="net_pnl")
            self.assertEqual(again.status_code, 202, again.text)
            _wait_done(self.client, again.json()["job_id"])

    def test_i2_stop_is_refused_for_what_cannot_be_stopped(self):
        self.assertEqual(self.client.post("/api/jobs/nope/cancel").status_code, 404)
        store.create_research_job({"job_id": "sweep-cli-9", "kind": "sweep", "name": "c", "status": "running",
                                   "created_ns": 1, "spec_json": "{}"})
        r = self.client.post("/api/research/jobs/sweep-cli-9/cancel")
        self.assertEqual(r.status_code, 409)
        self.assertIn("outside the app", r.json()["detail"])
        with mock.patch.object(quality, "report", return_value={}):
            rep = self.client.post("/api/data/report", json={"day": "2026-11-05"}).json()
            _wait_done(self.client, rep["job_id"])
        self.assertEqual(self.client.post(f"/api/jobs/{rep['job_id']}/cancel").status_code, 409)       # ended

    def test_i2_stop_ends_a_download_between_files(self):
        gate = threading.Event()
        seen = []

        def slow(dataset, day, http, **kw):  # noqa: ANN001, ANN003
            seen.append(dataset)
            gate.wait(10)
            return nse_files.FetchResult(dataset, day.isoformat(), "ok", detail="x")

        with mock.patch.object(nse_files, "fetch_one", slow), mock.patch.object(nse_files, "settle", return_value=[]), \
                mock.patch.object(nse_files, "Http", lambda cfg=None: object()):
            r = self.client.post("/api/data/backfill", json={"first_day": "2026-11-02", "last_day": "2026-11-06"})
            self.assertEqual(self.client.post(f"/api/jobs/{r.json()['job_id']}/cancel").status_code, 200)
            gate.set()
            final = _wait_done(self.client, r.json()["job_id"])
        self.assertEqual(final["status"], "cancelled")
        self.assertEqual(len(seen), 1)                           # no file was fetched after the stop

    def test_m3_a_job_running_from_the_command_line_counts_toward_the_limit(self):
        from stockify import timeutil
        self.finished_run()
        store.create_research_job({"job_id": "sweep-cli-1", "kind": "sweep", "name": "c", "status": "running",
                                   "created_ns": timeutil.now_ns(), "spec_json": "{}"})
        r = self.post_sweep()
        self.assertEqual(r.status_code, 409)
        self.assertIn("outside the app", r.json()["detail"])
        store.update_research_job("sweep-cli-1", created_ns=1)        # a row from a crashed terminal, long ago
        with mock.patch.object(pool, "run_all", WfPool()):
            ok = self.post_sweep(objective="net_pnl")
            self.assertEqual(ok.status_code, 202, ok.text)
            _wait_done(self.client, ok.json()["job_id"])

    def test_m4_finished_threads_are_not_kept(self):
        m = self.web_jobs.WebJobs()
        for i in range(5):
            m.start("report", f"d{i}", lambda job: None)
            m.join(5)
        m.start("report", "last", lambda job: None)
        m.join(5)
        self.assertLessEqual(len(m._threads), 2)

    def test_m5_an_unverified_source_is_not_blamed_on_the_site(self):
        from server.services import web_jobs
        self.assertIn("not verified", web_jobs.reason_of(nse_files.UnverifiedSource("fo is not verified")))
        self.assertNotIn("unreachable", web_jobs.reason_of(nse_files.UnverifiedSource("x")))
        self.assertIn("unreachable", web_jobs.reason_of(nse_files.NseError("x")))
        with self.assertRaises(nse_files.NseError):                      # it is still an NseError for the CLI
            raise nse_files.UnverifiedSource("x")

    def test_m6_a_test_cannot_be_started_with_the_holdout_unlocked_from_the_app(self):
        r = self.client.post("/api/tests", json={"unlock_holdout": "because I want to", "name": "x"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("cannot be unlocked from the app", str(r.json()["detail"]))

    def test_m7_one_try_is_refused_by_the_server_like_the_form(self):
        self.finished_run()
        r = self.post_sweep(axes={RANGE: [5]})
        self.assertEqual(r.status_code, 422)
        self.assertIn("single try", r.json()["detail"])

    def test_m9_the_router_does_not_patch_sentences_and_the_page_line_has_no_command(self):
        router = (REPO_ROOT / "server" / "routers" / "research.py").read_text(encoding="utf-8")
        self.assertNotIn("backtick", router)
        self.assertNotIn('in d["plain"]', router)
        src = (REPO_ROOT / "stockify" / "research" / "wf_report.py").read_text(encoding="utf-8")
        self.assertIn("notes(doc, command=False)", src)

    def test_m11_runs_job_id_has_an_index_from_a_migration(self):
        conn = store._connect()
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertIn("runs_job_id", names)
        self.assertTrue((REPO_ROOT / "stockify" / "data" / "migrations" / "lab" / "0006_runs_job_id_index.sql").is_file())


if __name__ == "__main__":
    unittest.main()
