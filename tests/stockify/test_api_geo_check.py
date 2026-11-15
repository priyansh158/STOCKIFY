"""LM 7.1 (D-150): the "Check the news now" button. POST /api/alerts/check runs one monitor cycle in the existing
web-job machinery; GET /api/alerts/monitor says when the last check ran. Nothing here touches the network, Gemini or
email: the news client, the classifier and the mail sender are stand-ins (tests.support also blocks real email)."""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest, isolated_env

import geo_alerts
from geo_monitor import cycle_guard, worker
from tests.test_geo_monitor import FakeArticle, FakeNewsClient, FakeSearchResult

WEB = support.API_BASE_URL.replace("8001", "3000")


def _wait_done(client, job_id: str, timeout: float = 20.0) -> dict:  # noqa: ANN001
    end = time.monotonic() + timeout
    while True:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running" or time.monotonic() > end:
            return j
        time.sleep(0.02)


@support.requires_fastapi
class CheckBase(SandboxedDBTest):
    db_modules = (geo_alerts,)

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        from server.services import web_jobs
        self.web_jobs = web_jobs
        web_jobs.set_jobs(web_jobs.WebJobs())
        self.addCleanup(web_jobs.set_jobs, None)
        for p in (patch.object(worker, "load_env", lambda: None),
                  patch.object(worker, "_holdings_for_queries", return_value=([], []))):
            p.start()
            self.addCleanup(p.stop)
        env = isolated_env({"GEMINI_API_KEY": "test-key"})
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app, base_url=support.API_BASE_URL)

    def tearDown(self):
        self.web_jobs.get_jobs().join(10)
        super().tearDown()

    def news(self, articles=(), errors=None):
        """Install a news client stand-in that returns `articles`."""
        client = FakeNewsClient(FakeSearchResult(list(articles), errors))
        p = patch.object(worker, "GoogleNewsClient", lambda **_k: client)
        p.start()
        self.addCleanup(p.stop)
        return client


class CheckEndpointTests(CheckBase):
    def test_a_check_answers_202_and_ends_with_a_plain_result(self):
        self.news([FakeArticle(f"g{i}", "New showroom opens in Pune") for i in range(8)])
        r = self.client.post("/api/alerts/check")
        self.assertEqual(r.status_code, 202, r.text)
        body = r.json()
        self.assertEqual((body["kind"], body["status"], body["target"]), ("geo", "running", "news"))
        done = _wait_done(self.client, body["job_id"])
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["plain"], "8 stories checked, no new alerts.")

    def test_a_check_stores_and_counts_a_new_alert(self):
        self.news([FakeArticle("g1", "RBI hikes repo rate")])
        c = [{"guid": "g1", "title": "RBI hikes repo rate", "severity": "HIGH", "disruption_score": 85}]
        with patch.object(worker, "classify_batch", return_value=c), \
                patch.object(worker, "try_send_alert_email", return_value=None) as mail:
            done = _wait_done(self.client, self.client.post("/api/alerts/check").json()["job_id"])
        self.assertEqual(done["plain"], "1 new alert (1 story checked). 1 emailed.")
        self.assertEqual(len(geo_alerts.list_alerts()), 1)
        mail.assert_called_once()                      # the cycle is the scheduled monitor's, not a dry run

    def test_the_last_check_is_recorded_as_from_the_page(self):
        self.news([FakeArticle("g1", "New showroom opens in Pune")])
        _wait_done(self.client, self.client.post("/api/alerts/check").json()["job_id"])
        s = self.client.get("/api/alerts/monitor").json()
        self.assertEqual((s["last_by"], s["checking"]), ("page", False))
        self.assertIsInstance(s["last_check_at"], int)
        self.assertEqual(s["last_plain"], "1 story checked, no new alerts.")

    def test_a_second_check_while_one_runs_is_refused_with_409(self):
        release, entered = threading.Event(), threading.Event()

        def slow(*_a, **_k):
            entered.set()
            release.wait(20)
            return {"fetched": 1, "new": 0, "suspects": 0, "classified": 0, "alerts": 0, "emailed": 0, "errors": []}
        with patch.object(worker, "run_once", slow):
            first = self.client.post("/api/alerts/check")
            self.assertTrue(entered.wait(10))
            second = self.client.post("/api/alerts/check")
            self.assertEqual(second.status_code, 409)
            self.assertTrue(second.json()["detail"])
            self.assertTrue(self.client.get("/api/alerts/monitor").json()["checking"])
            release.set()
            self.assertEqual(_wait_done(self.client, first.json()["job_id"])["status"], "done")
        self.assertFalse(self.client.get("/api/alerts/monitor").json()["checking"])

    def test_a_cycle_of_the_scheduled_monitor_blocks_a_check_with_409(self):
        with cycle_guard.hold():
            r = self.client.post("/api/alerts/check")
        self.assertEqual(r.status_code, 409)
        self.assertIn("background news monitor", r.json()["detail"])
        self.assertEqual(self.web_jobs.get_jobs().all(), [])          # no job was left behind

    def test_a_cycle_that_starts_in_the_gap_is_a_plain_failure_not_a_second_cycle(self):
        with patch.object(cycle_guard, "is_busy", return_value=False), cycle_guard.hold():
            r = self.client.post("/api/alerts/check")
            done = _wait_done(self.client, r.json()["job_id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("background news monitor", done["error"])

    def test_network_down_is_said_in_plain_words(self):
        self.news([], errors=["ConnectionError: boom"])
        done = _wait_done(self.client, self.client.post("/api/alerts/check").json()["job_id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("could not read Google News", done["error"])
        self.assertNotIn("ConnectionError", done["plain"])
        self.assertIsNone(self.client.get("/api/alerts/monitor").json()["last_check_at"])   # not a check

    def test_a_missing_key_is_said_in_plain_words(self):
        self.news([FakeArticle("g1", "RBI hikes repo rate")])
        with isolated_env({"GEMINI_API_KEY": "", "GOOGLE_AI_API_KEY": ""}):
            done = _wait_done(self.client, self.client.post("/api/alerts/check").json()["job_id"])
        self.assertEqual(done["status"], "done")
        self.assertIn("no Gemini key is set in the settings file", done["plain"])

    def test_a_gemini_failure_is_said_without_its_text(self):
        self.news([FakeArticle("g1", "RBI hikes repo rate")])
        with patch.object(worker, "classify_batch", side_effect=RuntimeError("429 quota key=test-key")):
            done = _wait_done(self.client, self.client.post("/api/alerts/check").json()["job_id"])
        self.assertIn("quota may be used up", done["plain"])
        self.assertNotIn("test-key", done["plain"])

    def test_an_unexpected_crash_is_a_plain_failure(self):
        with patch.object(worker, "run_once", side_effect=RuntimeError("secret detail C:\\x\\y")):
            done = _wait_done(self.client, self.client.post("/api/alerts/check").json()["job_id"])
        self.assertEqual(done["status"], "failed")
        self.assertNotIn("secret detail", done["plain"])
        self.assertIn("stopped unexpectedly", done["error"])

    def test_a_check_cannot_be_stopped(self):
        release = threading.Event()
        with patch.object(worker, "run_once", lambda: release.wait(20) or {"fetched": 1, "errors": []}):
            jid = self.client.post("/api/alerts/check").json()["job_id"]
            r = self.client.post(f"/api/jobs/{jid}/cancel")
            self.assertEqual(r.status_code, 409)
            release.set()
            _wait_done(self.client, jid)

    def test_nothing_is_taken_from_the_client(self):
        self.news([])
        r = self.client.post("/api/alerts/check?dry_run=true&x=1", json={"dry_run": True})
        self.assertEqual(r.status_code, 202)
        _wait_done(self.client, r.json()["job_id"])


class CheckProtectionTests(CheckBase):
    def test_a_foreign_origin_never_starts_a_check(self):
        with patch.object(worker, "run_once") as run:
            r = self.client.post("/api/alerts/check", headers={"Origin": "http://evil.example"})
            self.assertEqual(r.status_code, 403)
            self.assertEqual(self.web_jobs.get_jobs().all(), [])
        run.assert_not_called()

    def test_the_web_origin_is_let_through(self):
        self.news([])
        r = self.client.post("/api/alerts/check", headers={"Origin": WEB})
        self.assertEqual(r.status_code, 202)
        _wait_done(self.client, r.json()["job_id"])


class MonitorStatusTests(CheckBase):
    def test_nothing_recorded_yet(self):
        self.assertEqual(self.client.get("/api/alerts/monitor").json(),
                         {"last_check_at": None, "last_by": None, "last_plain": None, "checking": False})

    def test_the_route_does_not_shadow_an_alert_id(self):
        self.assertEqual(self.client.get("/api/alerts/999").status_code, 404)


class CycleGuardTests(SandboxedDBTest):
    db_modules = (geo_alerts,)

    def test_a_second_holder_is_refused_and_the_lock_is_freed(self):
        self.assertFalse(cycle_guard.is_busy())
        with cycle_guard.hold():
            self.assertTrue(cycle_guard.is_busy())
            with self.assertRaises(cycle_guard.CycleBusy), cycle_guard.hold():
                pass
        self.assertFalse(cycle_guard.is_busy())
        with cycle_guard.hold():
            pass

    def test_the_lock_is_freed_when_the_cycle_raises(self):
        with self.assertRaises(ValueError), cycle_guard.hold():
            raise ValueError("x")
        self.assertFalse(cycle_guard.is_busy())

    def test_another_process_is_excluded_and_its_death_frees_the_lock(self):
        import subprocess
        import sys
        import os
        code = ("import sys, pathlib, tests.support, stockify_paths\n"
                "stockify_paths.state_dir = lambda: pathlib.Path(sys.argv[1])\n"
                "from geo_monitor import cycle_guard\n"
                "with cycle_guard.hold():\n    print('held', flush=True)\n    sys.stdin.readline()\n")
        env = dict(os.environ)
        p = subprocess.Popen([sys.executable, "-c", code, str(__import__("stockify_paths").state_dir())], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                             env=env, cwd=str(__import__("pathlib").Path(__file__).resolve().parents[2]))
        try:
            self.assertEqual(p.stdout.readline().strip(), "held")
            self.assertTrue(cycle_guard.is_busy())
            p.kill()                                   # a crash, not a clean release
            p.wait(10)
            end = time.monotonic() + 5                # Windows drops a dead process's locks a moment after it exits
            while cycle_guard.is_busy() and time.monotonic() < end:
                time.sleep(0.05)
            self.assertFalse(cycle_guard.is_busy())
        finally:
            p.kill()

    def test_the_scheduled_monitor_skips_a_cycle_while_the_app_checks(self):
        calls = []
        with patch.object(worker, "load_env", lambda: None), patch.object(worker, "run_once", lambda: calls.append(1)), \
                cycle_guard.hold():
            worker.run_forever(once=True)
        self.assertEqual(calls, [])

    def test_the_scheduled_monitor_records_its_cycle_as_background(self):
        stats = {"fetched": 3, "new": 3, "suspects": 0, "classified": 0, "alerts": 0, "emailed": 0, "errors": []}
        with patch.object(worker, "load_env", lambda: None), patch.object(worker, "run_once", lambda: stats):
            worker.run_forever(once=True)
        self.assertEqual(geo_alerts.last_cycle()["source"], "background")


class TaskWindowTests(unittest.TestCase):
    def test_both_scheduled_tasks_start_hidden(self):
        """The monitor's task had no -WindowStyle Hidden, so a console window opened at every logon."""
        from pathlib import Path
        scripts = Path(__file__).resolve().parents[2] / "scripts"
        for name in ("manage_geo_monitor.ps1", "manage_stockify_daemon.ps1"):
            with self.subTest(name=name):
                self.assertIn("-NoProfile -WindowStyle Hidden -File", (scripts / name).read_text(encoding="utf-8"))


class SentenceTests(unittest.TestCase):
    def test_words(self):
        from server.services import web_jobs as w
        self.assertEqual(w.geo_sentence({"fetched": 1}), "1 story checked, no new alerts.")
        self.assertEqual(w.geo_sentence({"fetched": 250, "alerts": 2, "emailed": 0, "errors": ["email not configured (skipped)"]}),
                         "2 new alerts (250 stories checked). None was emailed. Email is not set up in the settings file, "
                         "so no email was sent.")
        self.assertIsNone(w.geo_failure({"fetched": 5, "errors": ["x"]}))
        self.assertIsNone(w.geo_failure({"fetched": 0, "errors": []}))
        self.assertTrue(w.geo_failure({"fetched": 0, "errors": ["x"]}))


if __name__ == "__main__":
    unittest.main()
