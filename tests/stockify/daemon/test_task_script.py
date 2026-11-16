"""The recorder's Scheduled Task (scripts/manage_stockify_daemon.ps1, D-32).

On 2026-11-15 Windows ended the launcher twice (exit 0xC000013A) and nothing started it again until the next logon,
so the first live session was nearly missed. The task therefore carries a watchdog trigger that starts it every few
minutes; IgnoreNew makes that a no-op while the launcher runs, and Stop disables the task so the watchdog cannot undo
a deliberate stop.
"""
import tests.support  # noqa: F401  (sandboxes state first)

import re
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "manage_stockify_daemon.ps1"


def _section(text: str, action: str) -> str:
    """The body of one `switch` arm, e.g. the lines under `"Stop" {`."""
    m = re.search(r'^\s*"%s"\s*\{(.*?)(?=^\s*"\w+"\s*\{|\Z)' % action, text, re.S | re.M)   # up to the next arm
    assert m, f"no {action} arm in {SCRIPT.name}"
    return m.group(1)


class RecorderTaskScriptTests(unittest.TestCase):
    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_install_registers_a_logon_trigger_and_a_watchdog(self):
        install = _section(self.text, "Install")
        self.assertIn("-AtLogOn", install)
        self.assertRegex(install, r"-RepetitionInterval \(New-TimeSpan -Minutes \d+\)")
        self.assertIn("-Trigger @($trigger, $watchdog)", install)

    def test_a_second_start_is_ignored_while_the_launcher_runs(self):
        self.assertIn("-MultipleInstances IgnoreNew", _section(self.text, "Install"))

    def test_stop_disables_the_task_and_start_enables_it(self):
        self.assertIn("Disable-ScheduledTask", _section(self.text, "Stop"))
        self.assertIn("Enable-ScheduledTask", _section(self.text, "Start"))

    def test_the_task_still_starts_hidden(self):
        self.assertIn("-NoProfile -WindowStyle Hidden -File", _section(self.text, "Install"))


if __name__ == "__main__":
    unittest.main()
