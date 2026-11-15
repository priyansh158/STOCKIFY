"""LM 7.1 (D-150): the Geo alerts page's helpers (web/src/lib/monitor.ts) give the hand-checked words, the web side
names the API's job kind and routes, and no terminal command reaches web/src. Skipped, with a message, when node is
missing."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

REPO = Path(__file__).resolve().parents[2]
CHECK = Path(__file__).with_name("geo_check.ts")
HOOK = Path(__file__).with_name("ts_resolve_hook.mjs").as_uri()


@unittest.skipUnless(shutil.which("node"), "node is not installed: the monitor word checks need it")
class MonitorWordChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", "--import", str(HOOK), str(CHECK)], capture_output=True, text=True, encoding="utf-8",
                             timeout=60, cwd=REPO)
        assert out.returncode == 0, out.stderr
        cls.a = json.loads(out.stdout)

    def test_the_last_check_line(self):
        self.assertEqual(self.a["page"], "Last check: 13 Nov 2026, 10:05 IST from this page. 8 stories checked, no new alerts.")
        self.assertEqual(self.a["background"], "Last check: 13 Nov 2026, 10:05 IST by the background monitor.")
        self.assertEqual(self.a["none"], "No check has been recorded yet.")
        self.assertIn("could not be read", self.a["unread"])

    def test_the_button_is_blocked_only_by_the_scheduled_monitor(self):
        self.assertEqual(self.a["blocked"][0], None)
        self.assertIn("background monitor is checking", self.a["blocked"][1])
        self.assertEqual(self.a["blocked"][2], None)          # a check from this page shows its own progress


class WebSideMatchesApi(unittest.TestCase):
    def test_routes_and_kind_are_the_apis(self):
        jobs = (REPO / "web" / "src" / "lib" / "jobs.ts").read_text(encoding="utf-8")
        self.assertIn('api<WebJobDTO>("/api/alerts/check", POST())', jobs)
        types = (REPO / "web" / "src" / "lib" / "types.ts").read_text(encoding="utf-8")
        from server.services import web_jobs
        for kind in web_jobs.KINDS:
            self.assertIn(f'"{kind}"', types.split("export type WebJobKind")[1].split(";")[0])
        from server.routers import alerts
        paths = {r.path for r in alerts.router.routes}
        self.assertTrue({"/api/alerts/check", "/api/alerts/monitor"} <= paths)

    def test_the_monitor_status_fields_are_mirrored(self):
        from server.schemas import GeoMonitorStatusDTO
        types = (REPO / "web" / "src" / "lib" / "types.ts").read_text(encoding="utf-8")
        body = types.split("export interface GeoMonitorStatus")[1].split("}")[0]
        for f in GeoMonitorStatusDTO.model_fields:
            self.assertIn(f, body)


if __name__ == "__main__":
    unittest.main()
