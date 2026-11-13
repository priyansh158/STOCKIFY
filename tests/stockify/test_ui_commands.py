"""LCM 7.2: every terminal command the web UI shows comes from web/src/lib/commands.ts, uses the venv interpreter, and names
a command and flags the CLI really has."""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import commands

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

    def test_python_and_web_spell_the_interpreter_the_same_way(self):
        """stockify/commands.py (server and stockify strings) and web/src/lib/commands.ts (web strings) agree."""
        m = re.search(r'export const PYTHON = "(.*?)";', COMMANDS.read_text(encoding="utf-8"))
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).replace("\\\\", "\\"), commands.VENV_PYTHON)
        self.assertEqual(commands.stockify_command("sweep --config F"), f"{commands.VENV_PYTHON} -m stockify sweep --config F")

    def test_no_user_facing_string_in_server_or_stockify_spells_a_bare_python_command(self):
        """Review I2: error details, plain-English lines and log hints build commands with stockify.commands. Module,
        class and function docstrings (developer notes) are not user-facing and are skipped."""
        bare = re.compile(r"(?<![\w.\/])(?:python|py)\s+-m\s+stockify")
        offenders = []
        for root in ("server", "stockify"):
            for f in (REPO / root).rglob("*.py"):
                tree = ast.parse(f.read_text(encoding="utf-8"))
                docs = {id(n.body[0].value) for n in ast.walk(tree)
                        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                        and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
                for n in ast.walk(tree):
                    if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs \
                            and bare.search(n.value):
                        offenders.append(f"{f.relative_to(REPO)}:{n.lineno}")
        self.assertEqual(offenders, [])

    def test_the_shared_helper_uses_the_venv_interpreter(self):
        text = COMMANDS.read_text(encoding="utf-8")
        self.assertIn(r'".\\.venv\\Scripts\\python.exe"', text)

    def test_the_example_sweep_file_exists(self):
        self.assertTrue((REPO / "examples" / "sweeps" / "orb_grid.yaml").is_file())
        self.assertIn('EXAMPLE_SWEEP = "examples/sweeps/orb_grid.yaml"', COMMANDS.read_text(encoding="utf-8"))

    def test_the_cli_knows_each_command_and_flag_the_ui_names(self):
        checks = [(["sweep"], "--config"), (["walkforward"], "--sweep"), (["sensitivity"], "--run"), (["nse", "fetch"], "--date"), (["nse", "backfill"], "--from"), (["nse", "backfill"], "--to")]
        for cmd, flag in checks:
            with self.subTest(cmd=cmd):
                out = subprocess.run([sys.executable, "-m", "stockify", *cmd, "--help"], capture_output=True, text=True,
                                     encoding="utf-8", cwd=REPO, timeout=60)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertIn(flag, out.stdout)


if __name__ == "__main__":
    unittest.main()
