"""LCM 7.1: every terminal command the web UI shows comes from web/src/lib/commands.ts, uses the venv interpreter, and names
a command and flags the CLI really has."""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

REPO = Path(__file__).resolve().parents[2]
WEB_SRC = REPO / "web" / "src"
COMMANDS = WEB_SRC / "lib" / "commands.ts"


class UiCommandsTest(unittest.TestCase):
    def test_no_file_spells_a_bare_python_command(self):
        bare = re.compile(r"(?<![\w.\/])(?:python|py)\s+-m\s+stockify")
        offenders = []
        for f in WEB_SRC.rglob("*"):
            if f.suffix in {".ts", ".tsx"} and f != COMMANDS and bare.search(f.read_text(encoding="utf-8")):
                offenders.append(str(f.relative_to(REPO)))
        # the venv form is only written in commands.ts
        for f in WEB_SRC.rglob("*"):
            if f.suffix in {".ts", ".tsx"} and f != COMMANDS and "python.exe" in f.read_text(encoding="utf-8"):
                offenders.append(str(f.relative_to(REPO)))
        self.assertEqual(offenders, [])

    def test_the_shared_helper_uses_the_venv_interpreter(self):
        text = COMMANDS.read_text(encoding="utf-8")
        self.assertIn(r'".\\.venv\\Scripts\\python.exe"', text)

    def test_the_example_sweep_file_exists(self):
        self.assertTrue((REPO / "examples" / "sweeps" / "orb_grid.yaml").is_file())
        self.assertIn('EXAMPLE_SWEEP = "examples/sweeps/orb_grid.yaml"', COMMANDS.read_text(encoding="utf-8"))

    def test_the_cli_knows_each_command_and_flag_the_ui_names(self):
        checks = [(["sweep"], "--config"), (["walkforward"], "--sweep"), (["sensitivity"], "--run"), (["nse", "fetch"], "--date")]
        for cmd, flag in checks:
            with self.subTest(cmd=cmd):
                out = subprocess.run([sys.executable, "-m", "stockify", *cmd, "--help"], capture_output=True, text=True,
                                     encoding="utf-8", cwd=REPO, timeout=60)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertIn(flag, out.stdout)


if __name__ == "__main__":
    unittest.main()
