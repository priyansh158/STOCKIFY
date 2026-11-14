"""DM 7.1 (D-145): the web UI tells the owner to run no terminal command. Every job is a button that runs on the server.
stockify/commands.py stays for command-line and log hints only; no string the API sends to a page names a command."""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import commands

REPO = Path(__file__).resolve().parents[2]
WEB_SRC = REPO / "web" / "src"

# What a terminal command in a page looks like: the venv interpreter, `python -m`, the launcher, `-m stockify`, a
# stockify sub-command with its flags, and the phrases that introduce one.
TERMINAL = [re.compile(p, re.IGNORECASE) for p in (
    r"\.venv", r"python(?:\.exe)?\s+-m\b", r"run\.ps1", r"-m\s+stockify\b", r"\bgeo_monitor\b",
    r"\bstockify\s+(?:nse|sweep|walkforward|sensitivity|data|research|run|daemon)\b",
    r"\s--(?:from|to|date|config|run|sweep)\s", r"in a terminal", r"terminal in the STOCKIFY folder", r"\bnpm run\b")]


def _web_files():  # noqa: ANN202
    for f in WEB_SRC.rglob("*"):
        if f.suffix in {".ts", ".tsx", ".css"} and f.is_file():
            yield f


class NoTerminalCommandsInTheUi(unittest.TestCase):
    def test_no_file_under_web_src_holds_a_terminal_command(self):
        offenders = []
        for f in _web_files():
            text = f.read_text(encoding="utf-8")
            for rx in TERMINAL:
                m = rx.search(text)
                if m:
                    offenders.append(f"{f.relative_to(REPO)}: {m.group(0).strip()!r}")
        self.assertEqual(offenders, [])

    def test_the_old_command_helper_is_gone(self):
        self.assertFalse((WEB_SRC / "lib" / "commands.ts").exists())

    def test_the_patterns_do_catch_what_they_are_for(self):
        for sample in (r".\.venv\Scripts\python.exe -m stockify sweep --config x.yaml", r".\run.ps1 -DaemonOnly",
                       "python -m geo_monitor", "run this in a terminal in the STOCKIFY folder",
                       "stockify nse backfill --from a"):
            with self.subTest(sample=sample):
                self.assertTrue(any(rx.search(sample) for rx in TERMINAL))
        for fine in ("Run sensitivity check", "Download missing days", "Start a new sweep",
                     "terminal: the run finished (stop)"):
            with self.subTest(fine=fine):
                self.assertFalse(any(rx.search(fine) for rx in TERMINAL))

    def test_every_job_has_a_button_in_the_app(self):
        """What replaced each command: the component that starts the job and the call it makes."""
        wanted = {
            "web/src/app/research/NewSweep.tsx": "startSweep",
            "web/src/app/research/sweeps/[id]/WalkforwardPanel.tsx": "startWalkforward",
            "web/src/app/tests/[id]/ResearchPanel.tsx": "startSensitivity",
            "web/src/app/data/DataActions.tsx": "startBackfill",
        }
        for rel, call in wanted.items():
            with self.subTest(rel=rel):
                self.assertIn(call, (REPO / rel).read_text(encoding="utf-8"))
        self.assertIn("startQualityReport", (REPO / "web/src/app/data/DataActions.tsx").read_text(encoding="utf-8"))


class NoCommandInWhatTheApiSends(unittest.TestCase):
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

    def test_the_api_routers_import_no_command_helper(self):
        """The routers answer pages; a command is for the command line and the log, so they never build one."""
        for f in (REPO / "server").rglob("*.py"):
            self.assertNotIn("stockify.commands", f.read_text(encoding="utf-8"), str(f))

    def test_the_shared_helper_still_spells_the_venv_interpreter_for_cli_and_log_hints(self):
        self.assertEqual(commands.stockify_command("sweep --config F"),
                         f"{commands.VENV_PYTHON} -m stockify sweep --config F")
        self.assertEqual(commands.VENV_PYTHON, r".\.venv\Scripts\python.exe")


if __name__ == "__main__":
    unittest.main()
