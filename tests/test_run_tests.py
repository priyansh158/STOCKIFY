"""The suite runner refuses to go green without the C++ engine unless told to (M3 review fix 1)."""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports

from tests import run_tests

ONE_SMALL_TEST = "test_run_tests.Probe.test_nothing"


class Probe(unittest.TestCase):
    def test_nothing(self):
        pass


class RunnerEngineCheckTests(unittest.TestCase):
    def _main(self, *flags: str, problem: str | None, env: dict | None = None) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = ["run_tests.py", ONE_SMALL_TEST, "--keep-sandbox", "-q", *flags]
        with mock.patch.object(run_tests, "engine_problem", return_value=problem), \
                mock.patch.object(sys, "argv", argv), support.isolated_env(env or {}, clear=False), \
                redirect_stdout(out), redirect_stderr(err):
            code = run_tests.main()
        return code, out.getvalue()

    def test_a_missing_engine_fails_the_run(self):
        code, text = self._main(problem="stockify._engine is not built (No module named stockify._engine)")
        self.assertEqual(code, run_tests.EXIT_NO_ENGINE)
        self.assertEqual(code, 3)
        self.assertIn("ENGINE NOT LOADABLE", text)
        self.assertIn("stockify._engine is not built", text)
        self.assertIn("--python-only", text)

    def test_python_only_accepts_it(self):
        for flags, env in ((("--python-only",), None), ((), {run_tests.PYTHON_ONLY_ENV: "1"})):
            with self.subTest(flags=flags, env=env):
                code, text = self._main(*flags, problem="built for Python 3.11, this is Python 3.12", env=env)
                self.assertEqual(code, 0)
                self.assertIn("PYTHON-ONLY RUN", text)
                self.assertNotIn("ENGINE NOT LOADABLE", text)

    def test_a_loadable_engine_changes_nothing(self):
        code, text = self._main(problem=None)
        self.assertEqual(code, 0)
        self.assertNotIn("ENGINE NOT LOADABLE", text)
        self.assertNotIn("PYTHON-ONLY", text)

    def test_engine_problem_reports_the_real_state(self):
        from stockify import engine
        self.assertEqual(run_tests.engine_problem() is None, engine.available())


if __name__ == "__main__":
    unittest.main()
