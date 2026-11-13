"""LCB 5.2 (M5.7): the launcher acts on the daemon's exit codes (stockify/daemon/exitcodes.py)."""
from __future__ import annotations

import re
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


if __name__ == "__main__":
    unittest.main()
