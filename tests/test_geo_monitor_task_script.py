"""The news monitor's Scheduled Task (scripts/manage_geo_monitor.ps1).

On 2026-11-15 at 18:40 PST Windows ended the monitor (last result 0xC000013A), as it ended the recorder's launcher
that day, and nothing started it again: no news was checked for about 42 hours. The task now carries the recorder's
watchdog (GK 7.7): a trigger that starts it every few minutes, a no-op under IgnoreNew while it runs. Stop disables
the task so the watchdog cannot undo a deliberate stop, and ends the monitor's Python process too.
"""
import tests.support  # noqa: F401  (sandboxes state first)

import unittest
from pathlib import Path

from tests.stockify.daemon.test_task_script import _section

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "manage_geo_monitor.ps1"


class GeoMonitorTaskScriptTests(unittest.TestCase):
    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_install_registers_a_logon_trigger_and_a_watchdog(self):
        install = _section(self.text, "Install")
        self.assertIn("-AtLogOn", install)
        self.assertRegex(install, r"-RepetitionInterval \(New-TimeSpan -Minutes \d+\)")
        self.assertIn("-Trigger @($trigger, $watchdog)", install)

    def test_a_second_start_is_ignored_while_the_monitor_runs(self):
        self.assertIn("-MultipleInstances IgnoreNew", _section(self.text, "Install"))

    def test_stop_disables_the_task_and_start_enables_it(self):
        self.assertIn("Disable-ScheduledTask", _section(self.text, "Stop"))
        self.assertIn("Enable-ScheduledTask", _section(self.text, "Start"))

    def test_stop_also_ends_the_monitor_and_its_child_interpreter(self):
        stop = _section(self.text, "Stop")
        self.assertIn('-match "-m geo_monitor"', stop)
        self.assertIn(".venv", stop)
        self.assertIn("ParentProcessId -eq $d.ProcessId", stop)
        self.assertIn("Stop-Process", stop)

    def test_status_reports_the_watchdog(self):
        self.assertIn("Watchdog:", _section(self.text, "Status"))

    def test_the_task_still_starts_hidden(self):
        self.assertIn("-NoProfile -WindowStyle Hidden -File", _section(self.text, "Install"))


if __name__ == "__main__":
    unittest.main()
