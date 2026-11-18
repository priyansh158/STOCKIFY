"""The web server answers loopback Hosts only (audit 2026-11-09 M2, review I-1).

Next's /api rewrite forwards any Host to the API, and its server-rendered pages fetch the API
themselves, so a page at a DNS name rebound to 127.0.0.1 would read the owner's holdings through
port 3000. `web/src/proxy.ts` (Next 16's request-interception file) refuses such a request before
any page renders or any /api request is proxied; the rule lives in `web/src/lib/loopbackHost.ts`.
One setting, STOCKIFY_REMOTE_HOST, adds exactly one Tailscale host name (the phone login,
docs/superpowers/specs/2026-11-18-phone-login-tailscale-design.md); both settings are tested here.
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


TS_HOST = "laptop.tail1234.ts.net"
TS_SERVED = [TS_HOST, TS_HOST + ":443", TS_HOST + ":3000", " " + TS_HOST + " "]
TS_REFUSED = ["other.tail1234.ts.net", "laptop.tail9999.ts.net", TS_HOST + ".evil.com", "evil" + TS_HOST,
              "x." + TS_HOST, TS_HOST.upper(), "Laptop.tail1234.ts.net", TS_HOST + ".", "tail1234.ts.net", "ts.net",
              "https://" + TS_HOST, TS_HOST + "/", TS_HOST + ",evil.example", TS_HOST + ":", TS_HOST + ":443:1"]
SETTING_OK = {TS_HOST: TS_HOST, "  " + TS_HOST + "\t": TS_HOST, "a.b.c.ts.net": "a.b.c.ts.net",
              "my-pc.tail-net.ts.net": "my-pc.tail-net.ts.net"}
SETTING_IGNORED = [None, "", "  ", "https://" + TS_HOST, TS_HOST + ":443", TS_HOST + "/", "*.tail1234.ts.net",
                   "**.ts.net", TS_HOST.upper(), TS_HOST + ".", "tail1234.ts.net", "ts.net", "laptop..ts.net",
                   "-laptop.tail1234.ts.net", "laptop-.tail1234.ts.net", "laptop.tail1234.ts.net.evil.com",
                   "laptop.tail1234.ts.network", "localhost", "127.0.0.1", TS_HOST + ",evil.ts.net",
                   "laptop.tail1234.ts.net\nx", "a" * 64 + ".tail1234.ts.net", ("a" * 60 + ".") * 5 + "ts.net"]


def _node(values, *mode):
    out = subprocess.run(["node", str(CHECK), json.dumps(values), *mode], capture_output=True, text=True,
                         timeout=60, cwd=REPO)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    answers = json.loads(out.stdout)
    assert len(answers) == len(values)
    return answers


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class WebLoopbackHostTests(unittest.TestCase):
    def test_the_rule_serves_loopback_hosts_and_refuses_every_other(self):
        hosts = SERVED + REFUSED + TS_SERVED + TS_REFUSED
        answers = _node(hosts)
        wrong = [(h, a) for h, a in zip(hosts, answers) if a != (h in SERVED)]
        self.assertEqual(wrong, [], "(host, served)")

    def test_off_the_setting_serves_loopback_only(self):
        hosts = SERVED + REFUSED + TS_SERVED + TS_REFUSED
        for setting in (None, "", *SETTING_IGNORED[3:]):
            with self.subTest(setting=setting):
                answers = _node(hosts, json.dumps(setting))
                wrong = [(h, a) for h, a in zip(hosts, answers) if a != (h in SERVED)]
                self.assertEqual(wrong, [], "(host, served)")

    def test_on_the_one_tailscale_host_is_served_too_and_nothing_else(self):
        hosts = SERVED + REFUSED + TS_SERVED + TS_REFUSED
        answers = _node(hosts, json.dumps(TS_HOST))
        wrong = [(h, a) for h, a in zip(hosts, answers) if a != (h in SERVED or h in TS_SERVED)]
        self.assertEqual(wrong, [], "(host, served)")

    def test_the_setting_is_checked_as_the_api_checks_it(self):
        import os
        import stockify_paths
        values = list(SETTING_OK) + SETTING_IGNORED
        answers = _node(values, "setting")
        expected = [SETTING_OK.get(v) for v in values]
        self.assertEqual(answers, expected)
        for value, answer in zip(values, answers):                    # the API's rule, stockify_paths
            with self.subTest(value=value), tests.support.isolated_env({}, clear=False):
                os.environ.pop("STOCKIFY_REMOTE_HOST", None)
                if value is not None:
                    os.environ["STOCKIFY_REMOTE_HOST"] = value
                self.assertEqual(stockify_paths.remote_host(), answer)


class WebLoopbackHostSourceTests(unittest.TestCase):

    def test_the_proxy_file_refuses_before_anything_else_runs(self):
        text = PROXY.read_text(encoding="utf-8")
        self.assertRegex(text, r"export function proxy\(")
        self.assertIn('from "@/lib/loopbackHost"', text)
        self.assertRegex(text, r"isServedHost\(request\.headers\.get\(\"host\"\), process\.env\.STOCKIFY_REMOTE_HOST\)")
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
        self.assertNotIn("process.env", text, "the rule reads no setting itself; proxy.ts passes the one it has")

    def test_the_dev_server_allows_only_the_checked_setting(self):
        text = (WEB / "next.config.ts").read_text(encoding="utf-8")
        self.assertIn("remoteHostSetting(process.env.STOCKIFY_REMOTE_HOST)", text)
        self.assertIn("allowedDevOrigins: REMOTE_HOST ? [REMOTE_HOST] : []", text)


if __name__ == "__main__":
    unittest.main()
