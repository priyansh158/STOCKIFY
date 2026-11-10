"""The web server answers loopback Hosts only (audit 2026-11-09 M2, review I-1).

Next's /api rewrite forwards any Host to the API, and its server-rendered pages fetch the API
themselves, so a page at a DNS name rebound to 127.0.0.1 would read the owner's holdings through
port 3000. `web/src/proxy.ts` (Next 16's request-interception file) refuses such a request before
any page renders or any /api request is proxied; the rule lives in `web/src/lib/loopbackHost.ts`.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

REPO = Path(__file__).resolve().parents[2]
WEB = REPO / "web"
PROXY = WEB / "src" / "proxy.ts"
RULE = WEB / "src" / "lib" / "loopbackHost.ts"
CHECK = Path(__file__).with_name("loopback_host_check.ts")

SERVED = ["127.0.0.1", "127.0.0.1:3000", "localhost", "localhost:3000", "LOCALHOST:3000", "[::1]", "[::1]:3000",
          " localhost:3000 "]
REFUSED = [None, "", "evil.example", "evil.example:3000", "127.0.0.1.evil.example", "localhost.evil.example:3000",
           "www.localhost", "localhost.", "localhost.:3000", "0.0.0.0:3000", "192.168.1.10:3000", "[::2]:3000",
           "[0:0:0:0:0:0:0:1]", "::1", "127.0.0.1:3000:1", "127.0.0.1:abc", "127.0.0.1:", "local host",
           "127.0.0.1　", "127.0.0.1:3000/", "http://127.0.0.1:3000", "127.1", "2130706433", "127.0.0.1,evil.example"]


class WebLoopbackHostTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_rule_serves_loopback_hosts_and_refuses_every_other(self):
        hosts = SERVED + REFUSED
        out = subprocess.run(["node", str(CHECK), json.dumps(hosts)], capture_output=True, text=True, timeout=60,
                             cwd=REPO)
        self.assertEqual(out.returncode, 0, out.stderr)
        answers = json.loads(out.stdout)
        self.assertEqual(len(answers), len(hosts))
        wrong = [(h, a) for h, a in zip(hosts, answers) if a != (h in SERVED)]
        self.assertEqual(wrong, [], "(host, served)")

    def test_the_proxy_file_refuses_before_anything_else_runs(self):
        text = PROXY.read_text(encoding="utf-8")
        self.assertRegex(text, r"export function proxy\(")
        self.assertIn('from "@/lib/loopbackHost"', text)
        self.assertRegex(text, r"isLoopbackHost\(request\.headers\.get\(\"host\"\)\)")
        self.assertIn("status: 400", text)
        self.assertNotIn("export const config", text,
                         "no matcher: the check must cover every route, pages and the /api rewrite alike")
        self.assertNotIn("runtime", text, "Next 16: the runtime option is not allowed in a proxy file")
        self.assertFalse((WEB / "src" / "middleware.ts").exists(), "Next 16 deprecated the middleware file")
        self.assertFalse((WEB / "middleware.ts").exists())
        self.assertFalse((WEB / "proxy.ts").exists(), "the app lives in src/, so the proxy file does too")

    def test_the_rule_names_exactly_the_loopback_hosts(self):
        text = RULE.read_text(encoding="utf-8")
        names = set(re.findall(r'"([^"]+)"', re.search(r"LOOPBACK_HOSTS = \[(.*)\];", text).group(1)))
        self.assertEqual(names, {"127.0.0.1", "localhost", "[::1]"})
        self.assertNotIn("process.env", text, "no setting relaxes the check")


if __name__ == "__main__":
    unittest.main()
