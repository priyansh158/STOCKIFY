"""The /tests form's parameter checks (web/src/lib/params.ts) agree with the server's
(stockify.strategies._catalogue.validate_params) on a shared fixture (M3 review fix 7)."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify.strategies._catalogue import validate_params

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "param_cases.json"
CHECK = Path(__file__).with_name("params_check.ts")


def _cases() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def _schema(kind: str) -> list[dict]:
    return [{"name": "x", "label": "", "type": kind, "default": None, "required": False, "help": ""}]


class ParamCheckTests(unittest.TestCase):
    def test_server_matches_the_fixture(self):
        for c in _cases():
            with self.subTest(case=c):
                problems = validate_params(_schema(c["type"]), {"x": c["value"]})
                self.assertEqual(problems, [] if c["problem"] is None else [f"x: {c['problem']}"])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_form_agrees_with_the_server(self):
        out = subprocess.run(["node", str(CHECK), str(FIXTURE)], capture_output=True, text=True, timeout=60,
                             cwd=REPO)
        self.assertEqual(out.returncode, 0, out.stderr)
        answers = json.loads(out.stdout)
        cases = _cases()
        self.assertEqual(len(answers), len(cases))
        disagree = [(c["type"], c["value"], c["problem"], a) for c, a in zip(cases, answers) if a != c["problem"]]
        self.assertEqual(disagree, [], "(type, value, server, form)")

    def test_the_fixture_covers_the_tricky_cases(self):
        values = {(c["type"], json.dumps(c["value"])) for c in _cases()}
        for kind, value in (("int", "1_000"), ("int", "1__0"), ("decimal", "1_000.5"), ("decimal", "NaN"),
                            ("hhmm", " 9:15 "), ("bool", "true")):
            self.assertIn((kind, json.dumps(value)), values)


if __name__ == "__main__":
    unittest.main()
