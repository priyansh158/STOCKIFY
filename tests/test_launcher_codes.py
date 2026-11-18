"""LCB 5.2 (M5.7): the launcher acts on the daemon's exit codes (stockify/daemon/exitcodes.py)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

from stockify.daemon import exitcodes

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "run_stockify_daemon.ps1"


class LauncherCodesTest(unittest.TestCase):
    def text(self) -> str:
        return LAUNCHER.read_text(encoding="utf-8")

    def test_launcher_restarts_after_the_minimum_delay_on_76(self):
        text = self.text()
        self.assertNotIn("Wait-FreshToken", text)
        self.assertRegex(text, r"(?s)\$code -eq \$ExitToken\).*?Start-Sleep -Seconds \$MinDelay.*?continue")

    def test_the_wait_for_a_login_lives_in_the_daemon_not_the_launcher(self):
        text = self.text()
        self.assertNotIn("kite_token.json", text)           # the launcher never touches the token store

    def test_launcher_constants_hold_the_daemon_codes(self):
        text = self.text()
        want = {"ExitSleep": exitcodes.SLEEP, "ExitToken": exitcodes.TOKEN_EXPIRED,
                "ExitAlreadyRunning": exitcodes.ALREADY_RUNNING, "ExitEngineStuck": exitcodes.ENGINE_STUCK,
                "ExitLockFailed": exitcodes.LOCK_FAILED}
        for name, value in want.items():
            with self.subTest(name=name):
                self.assertRegex(text, rf"(?m)^\${name} = {value}\b")
        self.assertEqual(set(re.findall(r"(?m)^\$(Exit[A-Za-z]+) = \d+", text)), set(want))


RUN_PS1 = ROOT / "run.ps1"


@unittest.skipUnless(shutil.which("pwsh"), "pwsh is not installed")
class RemoteHostLauncherTest(unittest.TestCase):
    """PL 1.4 (review I-1): run.ps1 copies STOCKIFY_REMOTE_HOST from the root settings file for the web server,
    and clears it first, so removing the line turns the phone login off even when the shell still holds a value."""

    def snippet(self) -> str:
        text = RUN_PS1.read_text(encoding="utf-8")
        m = re.search(r"(?s)# BEGIN remote-host\r?\n(.*?)# END remote-host", text)
        self.assertIsNotNone(m, "run.ps1 marks the remote-host lines with BEGIN/END remote-host")
        return m.group(1)

    def run_with(self, lines, inherited):
        with tempfile.TemporaryDirectory() as root:
            if lines is not None:
                Path(root, "." + "env").write_text("\n".join(lines) + "\n", encoding="utf-8")
            script = Path(root, "probe.ps1")
            script.write_text(f"$Root = '{root}'\n" + self.snippet() +
                              "\nif ($null -eq $env:STOCKIFY_REMOTE_HOST) { 'UNSET' } else { '[' + $env:STOCKIFY_REMOTE_HOST + ']' }\n",
                              encoding="utf-8")
            env = dict(os.environ)
            env.pop("STOCKIFY_REMOTE_HOST", None)
            if inherited is not None:
                env["STOCKIFY_REMOTE_HOST"] = inherited
            out = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(script)], capture_output=True,
                                 text=True, timeout=60, env=env)
            self.assertEqual(out.returncode, 0, out.stderr)
            return out.stdout.strip()

    def test_a_removed_line_clears_a_value_left_in_the_shell(self):
        old = "laptop.tail1234.ts.net"
        self.assertEqual(self.run_with(["KITE_API_KEY=x"], old), "UNSET")
        self.assertEqual(self.run_with(None, old), "UNSET")
        self.assertEqual(self.run_with([], None), "UNSET")

    def test_the_line_is_read_as_python_dotenv_reads_it(self):
        for lines, want in ((["STOCKIFY_REMOTE_HOST=laptop.tail1234.ts.net"], "[laptop.tail1234.ts.net]"),
                            (["STOCKIFY_REMOTE_HOST=a.b.ts.net", " STOCKIFY_REMOTE_HOST = \"c.d.ts.net\" "], "[c.d.ts.net]"),
                            (["STOCKIFY_REMOTE_HOST=c.d.ts.net   # the laptop"], "[c.d.ts.net]")):
            with self.subTest(lines=lines):
                self.assertEqual(self.run_with(lines, "stale.tail1234.ts.net"), want)


if __name__ == "__main__":
    unittest.main()
